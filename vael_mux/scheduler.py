from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

log = logging.getLogger("vael-mux.scheduler")


class PeriodicJob:
    def __init__(self, name: str, interval: int, fn: Callable[[], Awaitable[None]]):
        self.name = name
        self.interval = max(5, int(interval))
        self.fn = fn
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if self._task:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name=f"job-{self.name}")

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.fn()
            except Exception as e:
                log.exception("job %s failed: %s", self.name, e)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                continue

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def trigger(self) -> None:
        try:
            await self.fn()
        except Exception as e:
            log.exception("manual trigger %s failed: %s", self.name, e)
