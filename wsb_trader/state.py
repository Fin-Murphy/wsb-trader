"""Shared in-memory state published by the bot and consumed by the dashboard."""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from wsb_trader.extractor import TickerMention
    from wsb_trader.trader import Account, Position


@dataclass
class Event:
    ts: str
    kind: str
    data: dict


@dataclass
class DashboardState:
    events: deque[Event] = field(default_factory=lambda: deque(maxlen=200))
    account: Account | None = None
    positions: list[Position] = field(default_factory=list)
    mentions: list[TickerMention] = field(default_factory=list)
    last_tick_at: str | None = None
    _subscribers: list[asyncio.Queue] = field(default_factory=list)

    def emit(self, kind: str, **data) -> None:
        event = Event(
            ts=datetime.now(timezone.utc).isoformat(),
            kind=kind,
            data=data,
        )
        self.events.append(event)
        for q in self._subscribers:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass
