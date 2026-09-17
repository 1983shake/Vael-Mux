"""节点检测。

说明：本模块采用 TCP 连接可达性 + 建连延迟 作为节点可用性判断。
这是自包含、无外部依赖的近似方案；对精细的协议级探测（如真实 HTTP
请求穿透），可在 check_node 中替换为代理客户端探测。
"""

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger("vael-mux.checker")

ProgressCallback = Callable[[int, int, int], Awaitable[None]]


async def _tcp_check(host: str, port: int, timeout_s: float) -> Optional[int]:
    try:
        start = time.monotonic()
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout_s)
        latency = int((time.monotonic() - start) * 1000)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return latency
    except Exception:
        return None


async def check_node(node: Dict[str, Any], timeout_s: float) -> Optional[Dict[str, Any]]:
    host = node.get("server")
    port = node.get("port")
    if not host or not port:
        return None
    try:
        port = int(port)
    except (TypeError, ValueError):
        return None

    latency = await _tcp_check(host, port, timeout_s)
    if latency is None:
        return None
    enriched = dict(node)
    enriched["latency"] = latency
    return enriched


async def check_all(
    nodes: List[Dict[str, Any]],
    concurrent: int = 50,
    timeout_ms: int = 5000,
    progress_callback: Optional[ProgressCallback] = None,
) -> List[Dict[str, Any]]:
    total = len(nodes)
    if total == 0:
        return []

    timeout_s = max(timeout_ms, 100) / 1000.0
    sem = asyncio.Semaphore(max(1, concurrent))
    lock = asyncio.Lock()

    done = 0
    alive = 0
    alive_nodes: List[Dict[str, Any]] = []

    async def worker(node: Dict[str, Any]) -> None:
        nonlocal done, alive
        async with sem:
            result = await check_node(node, timeout_s)
        async with lock:
            done += 1
            if result is not None:
                alive += 1
                alive_nodes.append(result)
            if progress_callback and (done % 5 == 0 or done == total):
                await progress_callback(done, total, alive)

    await asyncio.gather(*(worker(n) for n in nodes))
    # 保持结果按延迟升序，方便下游直接使用
    alive_nodes.sort(key=lambda x: x.get("latency", 10**9))
    return alive_nodes
