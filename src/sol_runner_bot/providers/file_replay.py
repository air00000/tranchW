from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ..models import Snapshot


class FileReplayProvider:
    def __init__(self, file_path: str, sleep_sec: float = 0.0) -> None:
        self.file_path = Path(file_path)
        self.sleep_sec = sleep_sec

    async def __aiter__(self):
        for line in self.file_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            yield Snapshot.from_dict(json.loads(line))
            if self.sleep_sec > 0:
                await asyncio.sleep(self.sleep_sec)
