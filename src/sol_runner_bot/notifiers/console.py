from __future__ import annotations

import json

from ..models import AlertEvent


class ConsoleNotifier:
    async def send(self, event: AlertEvent) -> None:
        print(json.dumps(event.to_dict(), ensure_ascii=False))
