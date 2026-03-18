from __future__ import annotations

import asyncio
from typing import Any

import aiohttp

from ..models import Snapshot


class HttpMetricsPollProvider:
    def __init__(
        self,
        url: str,
        interval_sec: float = 5.0,
        headers: dict[str, str] | None = None,
        timeout_sec: float = 10.0,
    ) -> None:
        self.url = url
        self.interval_sec = interval_sec
        self.headers = headers or {}
        self.timeout_sec = timeout_sec

    async def __aiter__(self):
        async with aiohttp.ClientSession(headers=self.headers) as session:
            while True:
                async with session.get(self.url, timeout=self.timeout_sec) as resp:
                    body = await resp.json()
                    if resp.status >= 300:
                        raise RuntimeError(f"HTTP provider error {resp.status}: {body}")
                    snapshots = self._parse_body(body)
                    for snap in snapshots:
                        yield snap
                await asyncio.sleep(self.interval_sec)

    def _parse_body(self, body: Any) -> list[Snapshot]:
        if isinstance(body, dict) and "snapshots" in body:
            payload = body["snapshots"]
        else:
            payload = body
        if not isinstance(payload, list):
            raise ValueError("HTTP provider must return a list or an object with a 'snapshots' list")
        return [Snapshot.from_dict(item) for item in payload]
