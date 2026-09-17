"""并发拉取订阅源。"""

import asyncio
import logging
from typing import Awaitable, Callable, List, Optional

import httpx

logger = logging.getLogger("vael-mux.fetcher")

ProgressCallback = Callable[[int, int], Awaitable[None]]


async def fetch_one(client: httpx.AsyncClient, url: str) -> Optional[str]:
    try:
        resp = await client.get(url, timeout=30.0, follow_redirects=True)
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        logger.warning(f"订阅源拉取失败 [{url}]: {e}")
        return None


async def fetch_all(
    urls: List[str],
    progress_callback: Optional[ProgressCallback] = None,
    max_concurrency: int = 10,
) -> List[str]:
    results: List[str] = []
    total = len(urls)
    if total == 0:
        return results

    sem = asyncio.Semaphore(max_concurrency)
    lock = asyncio.Lock()
    completed = 0

    async with httpx.AsyncClient(
        headers={"User-Agent": "Vael-Mux/1.1"},
        timeout=30.0,
        follow_redirects=True,
    ) as client:

        async def worker(url: str) -> None:
            nonlocal completed
            async with sem:
                text = await fetch_one(client, url)
            async with lock:
                completed += 1
                if text:
                    results.append(text)
                if progress_callback:
                    await progress_callback(completed, total)

        await asyncio.gather(*(worker(u) for u in urls))

    return results
