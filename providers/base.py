from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol


class ProviderError(Exception):
    """Expected extraction failure that can be passed to the next provider."""


@dataclass(slots=True)
class ExtractionResult:
    path: Path
    media_type: str
    size: int
    cleanup: Callable[[], None]


class AudioProvider(Protocol):
    name: str

    async def extract(self, video_id: str) -> ExtractionResult:
        ...
