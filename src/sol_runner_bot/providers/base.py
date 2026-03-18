from __future__ import annotations

from typing import Protocol

from ..models import Snapshot


class SnapshotProvider(Protocol):
    async def __aiter__(self):  # pragma: no cover - protocol only
        yield Snapshot  # type: ignore[misc]
