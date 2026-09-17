from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, List, Optional

import aiohttp

from ..models import Node

log = logging.getLogger("vael-mux.testing.speed")


async def test_speed_via_kernel(
    kernel,
    nodes: List[Node],
    urls: List[str],
    mixed_port: int,
    timeout: float,
) -> Dict[str, float]:
    """
    通过内核 mixed-port 串行做下载测速。
    切换 PROXY 选择器到目标节点 → 通过 mixed-port 下载 → 记录 Mbps。
    返回 {node_key: mbps}
    """
    results: Dict[str, float] = {}
    proxy_url = f"http://127.0.0.1:{mixed_port}"

    for node in nodes:
        try:
            await kernel.switch("PROXY", node.name)
        except Exception as e:
            log.debug("switch to %s failed: %s", node.name, e)
            continue

        # 让内核状态稳定
        await asyncio.sleep(0.15)

        best = 0.0
        for url in urls:
            try:
                mbps = await _download_test(proxy_url, url, timeout)
                if mbps > best:
                    best = mbps
            except Exception as e:
                log.debug("speed test %s via %s failed: %s", url, node.name, e)
        if best > 0:
            results[node.key] = best

    # 恢复自动
    try:
        await kernel.switch("PROXY", "AUTO")
    except Exception:
        pass

    return results


async def _download_test(proxy_url: str, url: str, timeout: float) -> float:
    timeout_cfg = aiohttp.ClientTimeout(total=timeout)
    start = time.perf_counter()
    total = 0
    async with aiohttp.ClientSession(timeout=timeout_cfg) as s:
        async with s.get(url, proxy=proxy_url, ssl=False) as r:
            r.raise_for_status()
            async for chunk in r.content.iter_chunked(64 * 1024):
                total += len(chunk)
                # 限时截断，避免长连接拖慢
                if time.perf_counter() - start > timeout:
                    break
    elapsed = max(0.001, time.perf_counter() - start)
    mbps = (total * 8) / elapsed / 1_000_000
    return mbps
