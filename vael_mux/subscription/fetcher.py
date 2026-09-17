from __future__ import annotations

import logging
from typing import Optional

import aiohttp

log = logging.getLogger("vael-mux.subscription.fetcher")


async def fetch_subscription(url: str, user_agent: str = "clash.meta", timeout: float = 30.0) -> Optional[str]:
    headers = {"User-Agent": user_agent, "Accept": "*/*"}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
                ssl=False,
            ) as r:
                r.raise_for_status()
                text = await r.text(errors="replace")
                log.info("fetched %s (%d bytes)", url, len(text))
                return text
    except Exception as e:
        log.warning("fetch failed %s: %s", url, e)
        return None
