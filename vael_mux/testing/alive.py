from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, List, Optional

from ..models import Node

log = logging.getLogger("vael-mux.testing.alive")


async def test_alive_via_kernel(kernel, nodes: List[Node], urls: List[str], timeout: float, concurrency: int) -> Dict[str, Dict]:
    """
    通过 Mihomo 内核的 /proxies/{name}/delay 接口做原生测活。
    返回 {node_key: {alive, latency_ms, stability, last_error}}
    """
    sem = asyncio.Semaphore(max(1, concurrency))
    results: Dict[str, Dict] = {}

    async def worker(node: Node):
        async with sem:
            latencies: List[float] = []
            errors = []
            for url in urls:
                for attempt in range(2):
                    try:
                        data = await kernel.delay(node.name, url, timeout=int(timeout * 1000))
                        if isinstance(data, dict) and "delay" in data:
                            latencies.append(float(data["delay"]))
                            break
                        else:
                            errors.append(str(data.get("message", "unknown")))
                    except Exception as e:
                        errors.append(str(e))
                        await asyncio.sleep(0.15)
            if latencies:
                avg = sum(latencies) / len(latencies)
                stability = min(1.0, len(latencies) / max(1, len(urls) * 2))
                results[node.key] = {
                    "alive": True,
                    "latency_ms": avg,
                    "stability": stability,
                    "last_error": None,
                }
            else:
                results[node.key] = {
                    "alive": False,
                    "latency_ms": None,
                    "stability": 0.0,
                    "last_error": errors[-1] if errors else "no response",
                }

    await asyncio.gather(*(worker(n) for n in nodes))
    return results
