from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

from providers.base import AudioProvider, ExtractionResult, ProviderError
from providers.http_fallback import HttpFallbackProvider
from providers.yt_dlp import YtDlpProvider

logger = logging.getLogger("youtube_extract")
VIDEO_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")


@dataclass(frozen=True, slots=True)
class Settings:
    api_key: str
    max_audio_bytes: int = 30 * 1024 * 1024
    max_duration_seconds: int = 480
    request_timeout_seconds: int = 120
    rate_limit_window_seconds: int = 60
    rate_limit_requests: int = 5
    cache_ttl_seconds: int = 1800
    cache_max_items: int = 2

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            api_key=os.getenv("EXTRACT_API_KEY", ""),
            max_audio_bytes=int(os.getenv("MAX_AUDIO_BYTES", str(30 * 1024 * 1024))),
            max_duration_seconds=int(os.getenv("MAX_DURATION_SECONDS", "480")),
            request_timeout_seconds=int(os.getenv("EXTRACTION_TIMEOUT_SECONDS", "120")),
            rate_limit_window_seconds=int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60")),
            rate_limit_requests=int(os.getenv("RATE_LIMIT_REQUESTS", "5")),
            cache_ttl_seconds=int(os.getenv("CACHE_TTL_SECONDS", "1800")),
            cache_max_items=int(os.getenv("CACHE_MAX_ITEMS", "2")),
        )


@dataclass(slots=True)
class CachedAudio:
    result: ExtractionResult
    expires_at: float
    active_streams: int = 0
    evicted: bool = False


def default_providers(settings: Settings) -> list[AudioProvider]:
    providers: list[AudioProvider] = [
        YtDlpProvider(
            timeout_seconds=settings.request_timeout_seconds,
            max_duration_seconds=settings.max_duration_seconds,
        )
    ]
    cookie_file = os.getenv("YT_DLP_COOKIES_FILE", "")
    if cookie_file and Path(cookie_file).is_file():
        providers.append(
            YtDlpProvider(
                timeout_seconds=settings.request_timeout_seconds,
                max_duration_seconds=settings.max_duration_seconds,
                cookies_file=cookie_file,
            )
        )
    fallback_url = os.getenv("FALLBACK_PROVIDER_URL", "")
    if fallback_url:
        providers.append(
            HttpFallbackProvider(
                fallback_url,
                os.getenv("FALLBACK_PROVIDER_API_KEY", ""),
                settings.request_timeout_seconds,
            )
        )
    return providers


def create_app(settings: Settings | None = None, providers: list[AudioProvider] | None = None) -> FastAPI:
    config = settings or Settings.from_env()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = config
    app.state.providers = providers if providers is not None else default_providers(config)
    app.state.cache: dict[str, CachedAudio] = {}
    app.state.rate_limits: dict[str, tuple[float, int]] = {}
    app.state.extraction_lock = asyncio.Lock()

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/extract")
    async def extract(request: Request, video_id: str) -> StreamingResponse:
        if not config.api_key:
            raise HTTPException(status_code=503, detail="extractor_not_configured")
        supplied_key = request.headers.get("X-API-Key", "")
        if not secrets.compare_digest(supplied_key, config.api_key):
            raise HTTPException(status_code=401, detail="invalid_api_key")
        if not VIDEO_ID_PATTERN.fullmatch(video_id):
            raise HTTPException(status_code=400, detail="invalid_video_id")
        _enforce_rate_limit(app, request)

        result, from_cache = await _get_audio(app, video_id)
        headers = {
            "Content-Length": str(result.size),
            "X-Extractor-Cache": "hit" if from_cache else "miss",
            "Content-Disposition": f'inline; filename="{video_id}.m4a"',
        }
        return StreamingResponse(
            _stream_result(result, lambda: _release_cache(app, video_id, result)),
            media_type=result.media_type,
            headers=headers,
        )

    return app


def _enforce_rate_limit(app: FastAPI, request: Request) -> None:
    config: Settings = app.state.settings
    forwarded_for = request.headers.get("X-Forwarded-For", "").split(",", 1)[0].strip()
    key = forwarded_for or (request.client.host if request.client else "unknown")
    now = time.monotonic()
    started, count = app.state.rate_limits.get(key, (now, 0))
    if now - started >= config.rate_limit_window_seconds:
        started, count = now, 0
    if count >= config.rate_limit_requests:
        raise HTTPException(status_code=429, detail="rate_limit_exceeded", headers={"Retry-After": str(config.rate_limit_window_seconds)})
    app.state.rate_limits[key] = (started, count + 1)


async def _get_audio(app: FastAPI, video_id: str) -> tuple[ExtractionResult, bool]:
    config: Settings = app.state.settings
    now = time.monotonic()
    cached: CachedAudio | None = app.state.cache.get(video_id)
    if cached and cached.expires_at > now and cached.result.path.exists():
        cached.active_streams += 1
        return cached.result, True
    if cached:
        _evict_cache_entry(app, video_id, cached)
        app.state.cache.pop(video_id, None)

    async with app.state.extraction_lock:
        cached = app.state.cache.get(video_id)
        if cached and cached.expires_at > time.monotonic() and cached.result.path.exists():
            cached.active_streams += 1
            return cached.result, True
        if cached:
            _evict_cache_entry(app, video_id, cached)
            app.state.cache.pop(video_id, None)
        for provider in app.state.providers:
            try:
                result = await provider.extract(video_id)
                app.state.cache[video_id] = CachedAudio(
                    result,
                    time.monotonic() + config.cache_ttl_seconds,
                    active_streams=1,
                )
                while len(app.state.cache) > config.cache_max_items:
                    oldest_id = min(app.state.cache, key=lambda item: app.state.cache[item].expires_at)
                    oldest = app.state.cache[oldest_id]
                    _evict_cache_entry(app, oldest_id, oldest)
                    app.state.cache.pop(oldest_id, None)
                return result, False
            except ProviderError:
                logger.warning("audio provider failed", extra={"provider": provider.name})
        raise HTTPException(status_code=502, detail="audio_extraction_failed")


def _evict_cache_entry(app: FastAPI, video_id: str, cached: CachedAudio) -> None:
    cached.evicted = True
    if cached.active_streams == 0:
        cached.result.cleanup()


def _release_cache(app: FastAPI, video_id: str, result: ExtractionResult) -> None:
    cached: CachedAudio | None = app.state.cache.get(video_id)
    if cached is None or cached.result is not result:
        result.cleanup()
        return
    cached.active_streams = max(0, cached.active_streams - 1)
    if cached.evicted and cached.active_streams == 0:
        result.cleanup()


async def _stream_result(result: ExtractionResult, release: Callable[[], None]) -> AsyncIterator[bytes]:
    try:
        with result.path.open("rb") as audio_file:
            while chunk := audio_file.read(64 * 1024):
                yield chunk
    finally:
        release()


app = create_app()
