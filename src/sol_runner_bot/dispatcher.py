from __future__ import annotations

from typing import Protocol

from .models import AlertEvent


class EventSink(Protocol):
    async def send(self, event: AlertEvent) -> None:  # pragma: no cover - protocol only
        ...


class EventDispatcher:
    def __init__(self, sinks: list[EventSink]) -> None:
        self.sinks = sinks

    async def dispatch(self, events: list[AlertEvent]) -> None:
        for event in events:
            for sink in self.sinks:
                await sink.send(event)
