from __future__ import annotations

import asyncio
import os
from pathlib import Path

from .base import ProviderError


async def normalize_audio(path: Path, *, timeout_seconds: int) -> Path:
    normalized_path = path.with_name("normalized.m4a")
    bitrate = os.getenv("OUTPUT_AUDIO_BITRATE_KBPS", "96")
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            os.getenv("FFMPEG_BIN", "ffmpeg"),
            "-y",
            "-i",
            str(path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "22050",
            "-c:a",
            "aac",
            "-b:a",
            f"{bitrate}k",
            str(normalized_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(process.communicate(), timeout_seconds)
    except (asyncio.TimeoutError, OSError) as exc:
        raise ProviderError("audio normalization unavailable or timed out") from exc
    if process.returncode != 0 or not normalized_path.is_file():
        detail = stderr.decode(errors="replace")[-300:]
        raise ProviderError(f"audio normalization failed: {detail}")
    path.unlink(missing_ok=True)
    return normalized_path
