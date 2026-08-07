import os
import re
import tempfile
import shutil
from pathlib import Path

from fastapi import FastAPI, Query, HTTPException, Request, BackgroundTasks, Depends
from fastapi.responses import StreamingResponse, JSONResponse
import yt_dlp

app = FastAPI(title="YouTube Extract Service")

VIDEO_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")
API_KEY = os.getenv("YT_EXTRACT_API_KEY")
COOKIES_FILE = os.getenv("YT_COOKIES_FILE")

MIME_MAP = {
    "mp3": "audio/mpeg",
    "m4a": "audio/mp4",
    "webm": "audio/webm",
    "ogg": "audio/ogg",
    "opus": "audio/ogg",
}


async def verify_api_key(request: Request):
    if API_KEY is not None:
        provided = request.headers.get("x-api-key")
        if provided != API_KEY:
            raise HTTPException(status_code=401, detail="Invalid or missing API key")
    return True


@app.get("/health")
async def health():
    return JSONResponse({"status": "ok"})


@app.get("/extract")
async def extract(
    background_tasks: BackgroundTasks,
    video_id: str = Query(..., min_length=11, max_length=11),
    _: bool = Depends(verify_api_key),
):
    if not VIDEO_ID_PATTERN.match(video_id):
        raise HTTPException(status_code=400, detail="Invalid video_id format")

    url = f"https://www.youtube.com/watch?v={video_id}"
    tmpdir = tempfile.mkdtemp(prefix="ytx_")

    tmp_cookie_file = None
    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        tmp_cookie_file = os.path.join(tmpdir, "cookies.txt")
        shutil.copy2(COOKIES_FILE, tmp_cookie_file)

    def cleanup():
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass

    background_tasks.add_task(cleanup)

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(tmpdir, "audio.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "overwrites": True,
        "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
        "js_runtimes": {"nodejs": {}},
    }

    if tmp_cookie_file:
        ydl_opts["cookiefile"] = tmp_cookie_file

    def attempt_extract(opts):
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=True)

    try:
        info = attempt_extract(ydl_opts)
    except yt_dlp.utils.DownloadError:
        if tmp_cookie_file:
            ydl_opts.pop("cookiefile", None)
            try:
                info = attempt_extract(ydl_opts)
            except yt_dlp.utils.DownloadError as e:
                raise HTTPException(status_code=500, detail=f"Download failed: {e}")
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Extraction failed: {e}")
        else:
            raise HTTPException(status_code=500, detail="Download failed")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Extraction failed: {e}")

    if not info:
        raise HTTPException(status_code=500, detail="Extraction returned no info")

    ext = info.get("ext", "mp3")
    mime = MIME_MAP.get(ext, "application/octet-stream")

    files = [f for f in Path(tmpdir).iterdir() if f.is_file()]
    if not files:
        raise HTTPException(status_code=500, detail="Downloaded file not found")

    file_path = files[0]

    def iterfile(path):
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(65536)
                if not chunk:
                    break
                yield chunk

    filename = f"audio.{ext}"
    headers = {
        "Content-Disposition": f"attachment; filename=\"{filename}\"",
    }

    return StreamingResponse(
        iterfile(file_path),
        media_type=mime,
        headers=headers,
    )
