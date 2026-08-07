import os
import re
import tempfile
import asyncio
import time
import random
import base64
import logging
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from fastapi import FastAPI, Query, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse

logger = logging.getLogger("yt-extract")

app = FastAPI(title="YouTube Extract Service")

VIDEO_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")
API_KEY = os.getenv("YT_EXTRACT_API_KEY", "")
YT_COOKIES_FILE = None
BGUTIL_SERVER_URL = "http://127.0.0.1:4416"

MIME_MAP = {
    "mp3": "audio/mpeg",
    "m4a": "audio/mp4",
    "webm": "audio/webm",
    "ogg": "audio/ogg",
    "opus": "audio/ogg",
}

PLAYER_CLIENTS = [
    c.strip() for c in os.getenv("YTDLP_PLAYER_CLIENTS", "mweb,web,ios").split(",")
    if c.strip()
] or ["mweb"]

MAX_ATTEMPTS = max(1, int(os.getenv("YTDLP_MAX_ATTEMPTS", "4")))
BACKOFF_BASE = max(1, int(os.getenv("YTDLP_BACKOFF_BASE_SEC", "5")))
DOWNLOAD_TIMEOUT = max(30, int(os.getenv("DOWNLOAD_TIMEOUT_SEC", "120")))


def _load_cookies_from_b64():
    global YT_COOKIES_FILE
    cookies_b64 = os.getenv("YT_COOKIES_B64", "")
    if not cookies_b64:
        return
    try:
        cookies_bytes = base64.b64decode(cookies_b64)
        tmp = tempfile.NamedTemporaryFile(
            mode="wb", suffix=".txt", prefix="yt_cookies_", delete=False
        )
        tmp.write(cookies_bytes)
        tmp.close()
        YT_COOKIES_FILE = tmp.name
        logger.info(f"Cookies loaded ({len(cookies_bytes)} bytes)")
    except Exception as e:
        logger.error(f"Failed to decode YT_COOKIES_B64: {e}")


@app.on_event("startup")
def _init():
    _load_cookies_from_b64()


@app.get("/health")
async def health():
    pot_status = "unreachable"
    try:
        req = Request(BGUTIL_SERVER_URL, method="GET")
        urlopen(req, timeout=2)
        pot_status = "ok"
    except HTTPError:
        pot_status = "ok"
    except Exception:
        pass
    return JSONResponse({
        "status": "ok",
        "po_token_server": pot_status,
        "cookies_loaded": YT_COOKIES_FILE is not None,
    })


@app.get("/extract")
async def extract(
    background_tasks: BackgroundTasks,
    video_id: str = Query(..., min_length=11, max_length=11),
    x_api_key: str = Query(None),
):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")

    if not VIDEO_ID_PATTERN.match(video_id):
        raise HTTPException(status_code=400, detail="Invalid video_id")

    url = f"https://www.youtube.com/watch?v={video_id}"
    tmp_path = None
    last_error = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        player_client = PLAYER_CLIENTS[(attempt - 1) % len(PLAYER_CLIENTS)]
        tmp = tempfile.NamedTemporaryFile(suffix=".m4a", delete=False)
        tmp.close()
        tmp_path = tmp.name

        cmd = [
            "yt-dlp",
            "--no-playlist",
            "-f", "ba/b*",
            "-S", "+size,+br,proto:m3u8_native:m3u8:https",
            "--force-ipv4",
            "--concurrent-fragments", "2",
            "--cache-dir", "/app/.ytdlp-cache",
            "--js-runtimes", "node",
            "--socket-timeout", "15",
            "--retries", "1",
            "--extractor-args", f"youtube:player_client={player_client}",
            "-o", tmp_path,
            "--force-overwrites",
        ]

        if YT_COOKIES_FILE and os.path.exists(YT_COOKIES_FILE):
            cmd.extend(["--cookies", YT_COOKIES_FILE])

        cmd.append(url)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=DOWNLOAD_TIMEOUT
            )

            if proc.returncode != 0:
                err = stderr.decode(errors="replace")
                err_lines = [
                    l for l in err.split("\n")
                    if l.startswith("ERROR:") or l.startswith("WARNING:") or "Sign in" in l
                ]
                err_msg = "\n".join(err_lines)[:1000] if err_lines else err[-500:]
                logger.warning(f"[yt-dlp] Failed (exit {proc.returncode}): {err_msg}")
                last_error = ValueError(f"yt-dlp exit {proc.returncode}: {err_msg[:500]}")
            else:
                if os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 10_000:
                    ext = os.path.splitext(tmp_path)[1].lower()
                    media_type = MIME_MAP.get(ext, "audio/mp4")

                    def cleanup():
                        try:
                            os.unlink(tmp_path)
                        except OSError:
                            pass

                    background_tasks.add_task(cleanup)
                    return FileResponse(
                        path=tmp_path,
                        media_type=media_type,
                        filename=f"{video_id}{ext}",
                        background=cleanup,
                    )
                else:
                    last_error = ValueError("Downloaded file too small or missing")
        except asyncio.TimeoutError:
            logger.warning(f"[yt-dlp] Timed out after {DOWNLOAD_TIMEOUT}s")
            last_error = ValueError("Download timed out")
        except Exception as e:
            last_error = ValueError(f"Unexpected error: {e}")
        finally:
            if last_error and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

        if attempt < MAX_ATTEMPTS and last_error and _is_retryable(last_error):
            backoff = (BACKOFF_BASE * (2 ** (attempt - 1))) + random.uniform(0, 1.5)
            logger.warning(f"Retrying {video_id} in {backoff:.1f}s...")
            await asyncio.sleep(backoff)
            continue

        if last_error:
            raise HTTPException(status_code=500, detail=str(last_error)[:500])

    raise HTTPException(status_code=500, detail="yt-dlp failed with no captured error")


def _is_retryable(err: Exception) -> bool:
    msg = str(err).lower()
    signatures = [
        "too many requests",
        "http error 429",
        "sign in to confirm",
        "confirm you're not a bot",
        "requested format is not available",
        "only images are available",
        "timed out",
        "unable to download webpage",
        "network is unreachable",
        "failed to establish a new connection",
    ]
    return any(sig in msg for sig in signatures)
