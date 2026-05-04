"""Async event bus that collects events from plugin triggers and feeds them
into the persistent Claude session at turn boundaries.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class Event:
    source: str
    body: str
    metadata: dict = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_user_message(self) -> str:
        """Render as a user turn that Claude will see via stream-json input."""
        parts = [f"FROM {self.source}"]
        for k, v in self.metadata.items():
            parts.append(f"{k}={v}")
        prefix = "[" + " | ".join(parts) + "]"
        return f"{prefix}\n{self.body}".strip()


class EventBus:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[Event] = asyncio.Queue()

    async def put(self, event: Event) -> None:
        await self._queue.put(event)

    async def get(self) -> Event:
        return await self._queue.get()

    def drain_nowait(self, max_n: int = 32) -> list[Event]:
        events: list[Event] = []
        try:
            while len(events) < max_n:
                events.append(self._queue.get_nowait())
        except asyncio.QueueEmpty:
            pass
        return events

    def empty(self) -> bool:
        return self._queue.empty()
