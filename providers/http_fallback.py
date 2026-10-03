from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import httpx

from .base import ExtractionResult, ProviderError
from .audio import normalize_audio


class HttpFallbackProvider:
    name = "configured-http"

    def __init__(self, url: str, api_key: str = "", timeout_seconds: int = 120) -> None:
        self.url = url
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    async def extract(self, video_id: str) -> ExtractionResult:
        temp_dir = Path(tempfile.mkdtemp(prefix="youtube-fallback-"))
        path = temp_dir / "audio.bin"
        headers = {"X-API-Key": self.api_key} if self.api_key else {}
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds, follow_redirects=False) as client:
                async with client.stream("GET", self.url, params={"video_id": video_id}, headers=headers) as response:
                    if response.status_code != 200:
                        raise ProviderError("configured provider returned an error")
                    media_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                    if not media_type.startswith("audio/"):
                        raise ProviderError("configured provider returned a non-audio response")
                    max_bytes = int(os.getenv("MAX_AUDIO_BYTES", "31457280"))
                    size = 0
                    with path.open("wb") as output:
                        async for chunk in response.aiter_bytes(64 * 1024):
                            size += len(chunk)
                            if size > max_bytes:
                                raise ProviderError("configured provider exceeded the size limit")
                            output.write(chunk)
        except (httpx.HTTPError, OSError, ProviderError) as exc:
            shutil.rmtree(temp_dir, ignore_errors=True)
            if isinstance(exc, ProviderError):
                raise
            raise ProviderError("configured provider request failed") from exc

        if size < 10_000:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise ProviderError("configured provider returned too little audio")
        try:
            path = await normalize_audio(path, timeout_seconds=self.timeout_seconds)
        except ProviderError:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise
        size = path.stat().st_size
        if size > int(os.getenv("MAX_AUDIO_BYTES", "31457280")):
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise ProviderError("normalized audio exceeded the size limit")
        return ExtractionResult(path, "audio/mp4", size, lambda: shutil.rmtree(temp_dir, ignore_errors=True))
