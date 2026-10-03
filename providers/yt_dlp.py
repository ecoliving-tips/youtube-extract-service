from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from pathlib import Path

from .base import AudioProvider, ExtractionResult, ProviderError
from .audio import normalize_audio


class YtDlpProvider:
    name = "yt-dlp"

    def __init__(
        self,
        *,
        timeout_seconds: int = 120,
        max_duration_seconds: int = 480,
        cookies_file: str | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_duration_seconds = max_duration_seconds
        self.cookies_file = cookies_file
        self.executable = os.getenv("YT_DLP_BIN", "yt-dlp")

    async def extract(self, video_id: str) -> ExtractionResult:
        temp_dir = Path(tempfile.mkdtemp(prefix="youtube-audio-"))
        output_template = str(temp_dir / "audio.%(ext)s")
        cookie_file = None
        if self.cookies_file:
            cookie_file = temp_dir / "cookies.txt"
            try:
                shutil.copyfile(self.cookies_file, cookie_file)
            except OSError as exc:
                shutil.rmtree(temp_dir, ignore_errors=True)
                raise ProviderError("configured cookie file could not be copied") from exc
        command = [
            self.executable,
            "--no-playlist",
            "--no-progress",
            "--no-warnings",
            "--ignore-config",
            "--js-runtimes",
            "deno",
            "--max-filesize",
            os.getenv("MAX_AUDIO_BYTES", "31457280"),
            "--match-filter",
            f"duration <= {self.max_duration_seconds} & !is_live",
            "-f",
            "bestaudio[ext=m4a]/bestaudio",
            "-o",
            output_template,
            f"https://www.youtube.com/watch?v={video_id}",
        ]
        if cookie_file:
            command[1:1] = ["--cookies", str(cookie_file)]

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), self.timeout_seconds)
        except (asyncio.TimeoutError, OSError) as exc:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise ProviderError("yt-dlp unavailable or timed out") from exc

        if process.returncode != 0:
            shutil.rmtree(temp_dir, ignore_errors=True)
            detail = stderr.decode(errors="replace")[-300:]
            raise ProviderError(f"yt-dlp failed: {detail}")

        candidates = [path for path in temp_dir.iterdir() if path.is_file()]
        if len(candidates) != 1:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise ProviderError("yt-dlp returned no single audio file")

        path = candidates[0]
        try:
            path = await normalize_audio(path, timeout_seconds=self.timeout_seconds)
        except ProviderError:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise
        size = path.stat().st_size
        if size < 10_000 or size > int(os.getenv("MAX_AUDIO_BYTES", "31457280")):
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise ProviderError("audio file is outside the allowed size range")

        return ExtractionResult(
            path=path,
            media_type="audio/mp4" if path.suffix.lower() == ".m4a" else "audio/mpeg",
            size=size,
            cleanup=lambda: shutil.rmtree(temp_dir, ignore_errors=True),
        )
