from __future__ import annotations

import asyncio
import logging
import sys
from collections import deque
from datetime import datetime
from typing import Deque, List


class LogBus(logging.Handler):
    """In-memory log ring buffer + async broadcast to WebSocket clients."""

    def __init__(self, capacity: int = 800):
        super().__init__()
        self.buffer: Deque[dict] = deque(maxlen=capacity)
        self._subscribers: List[asyncio.Queue] = []
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = {
                "ts": datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S"),
                "level": record.levelname,
                "name": record.name,
                "msg": self.format(record),
            }
        except Exception:
            return
        self.buffer.append(entry)
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._broadcast, entry)

    def _broadcast(self, entry: dict) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(entry)
            except asyncio.QueueFull:
                pass

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)

    def history(self) -> List[dict]:
        return list(self.buffer)


bus = LogBus()


def setup_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(level.upper())
    root.handlers.clear()

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    root.addHandler(stream)

    bus.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(bus)
