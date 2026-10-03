from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from app import Settings, create_app
from providers.base import ExtractionResult, ProviderError


class FakeProvider:
    def __init__(self, name: str, *, succeeds: bool = True) -> None:
        self.name = name
        self.succeeds = succeeds
        self.calls = 0

    async def extract(self, video_id: str) -> ExtractionResult:
        self.calls += 1
        if not self.succeeds:
            raise ProviderError("expected test failure")
        temp_dir = Path(tempfile.mkdtemp(prefix="test-audio-"))
        path = temp_dir / "audio.m4a"
        path.write_bytes(b"x" * 12_000)
        return ExtractionResult(path, "audio/mp4", path.stat().st_size, lambda: shutil.rmtree(temp_dir, ignore_errors=True))


def make_client(*providers: FakeProvider, rate_limit_requests: int = 5) -> TestClient:
    settings = Settings(api_key="test-secret", rate_limit_requests=rate_limit_requests)
    return TestClient(create_app(settings, list(providers)))


def test_health_is_public() -> None:
    client = make_client(FakeProvider("primary"))
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_extraction_requires_api_key() -> None:
    client = make_client(FakeProvider("primary"))
    response = client.get("/extract", params={"video_id": "dQw4w9WgXcQ"})
    assert response.status_code == 401


def test_fallback_and_binary_response() -> None:
    primary = FakeProvider("primary", succeeds=False)
    fallback = FakeProvider("fallback")
    client = make_client(primary, fallback)
    response = client.get(
        "/extract",
        params={"video_id": "dQw4w9WgXcQ"},
        headers={"X-API-Key": "test-secret"},
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/mp4"
    assert response.headers["x-extractor-cache"] == "miss"
    assert len(response.content) == 12_000
    assert primary.calls == 1
    assert fallback.calls == 1


def test_successful_result_is_cached() -> None:
    provider = FakeProvider("primary")
    client = make_client(provider)
    headers = {"X-API-Key": "test-secret"}
    first = client.get("/extract?video_id=dQw4w9WgXcQ", headers=headers)
    second = client.get("/extract?video_id=dQw4w9WgXcQ", headers=headers)
    assert first.status_code == second.status_code == 200
    assert first.headers["x-extractor-cache"] == "miss"
    assert second.headers["x-extractor-cache"] == "hit"
    assert provider.calls == 1


def test_invalid_video_id_is_rejected_before_provider() -> None:
    provider = FakeProvider("primary")
    client = make_client(provider)
    response = client.get(
        "/extract?video_id=not-valid",
        headers={"X-API-Key": "test-secret"},
    )
    assert response.status_code == 400
    assert provider.calls == 0


def test_rate_limit_is_enforced() -> None:
    client = make_client(FakeProvider("primary"), rate_limit_requests=1)
    headers = {"X-API-Key": "test-secret"}
    assert client.get("/extract?video_id=dQw4w9WgXcQ", headers=headers).status_code == 200
    assert client.get("/extract?video_id=9bZkp7q19f0", headers=headers).status_code == 429
