from __future__ import annotations

from typing import Any

import aiohttp

from ..models import AlertEvent


class WebhookNotifier:
    def __init__(self, url: str, headers: dict[str, str] | None = None, timeout_sec: float = 10.0) -> None:
        self.url = url
        self.headers = headers or {}
        self.timeout_sec = timeout_sec

    async def send(self, event: AlertEvent) -> None:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                self.url,
                json=event.to_dict(),
                headers=self.headers,
                timeout=self.timeout_sec,
            ) as resp:
                body = await resp.text()
                if resp.status >= 300:
                    raise RuntimeError(f"Webhook error {resp.status}: {body}")
