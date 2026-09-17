"""节点检测：多采样 TCP 延迟 + 连接速率 (CPS) 近似测速。

说明：
  - latency_ms / latency_avg_ms / latency_max_ms / jitter_ms 均来自
    多次 TCP 握手时间采样。
  - speed_cps 是"连接速率"近似值：在时间窗口内可建立的 TCP 连接数
    除以秒数。用于衡量节点可承载的并发连接吞吐，不等价于带宽测速。
"""

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

from app.models import NodeRecord

logger = logging.getLogger("vael-mux.checker")

ProgressCallback = Callable[[int, int, int], Awaitable[None]]


async def _tcp_ping(host: str, port: int, timeout_s: float) -> Optional[int]:
    try:
        start = time.monotonic()
        r, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout_s)
        dt = int((time.monotonic() - start) * 1000)
        w.close()
        try:
            await w.wait_closed()
        except Exception:
            pass
        return dt
    except Exception:
        return None


async def _measure_speed(
    host: str,
    port: int,
    duration_s: float = 0.5,
    internal_concurrency: int = 5,
) -> Optional[float]:
    """在 duration_s 窗口内测量可建立的 TCP 连接速率（conn/s）。"""
    if not host or not port:
        return None

    sem = asyncio.Semaphore(internal_concurrency)
    stop = {"flag": False}
    count = 0
    lock = asyncio.Lock()

    async def one() -> None:
        nonlocal count
        async with sem:
            if stop["flag"]:
                return
            try:
                r, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=1.5)
                w.close()
                try:
                    await w.wait_closed()
                except Exception:
                    pass
                async with lock:
                    count += 1
            except Exception:
                pass

    start = time.monotonic()
    tasks: List[asyncio.Task] = []
    while time.monotonic() - start < duration_s:
        tasks.append(asyncio.create_task(one()))
        await asyncio.sleep(0.02)

    stop["flag"] = True
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    elapsed = time.monotonic() - start
    if elapsed <= 0:
        return None
    return round(count / elapsed, 2)


def _empty_metrics(success_rate: float = 0.0) -> Dict[str, Any]:
    return {
        "latency_ms": None,
        "latency_avg_ms": None,
        "latency_max_ms": None,
        "jitter_ms": None,
        "success_rate": success_rate,
        "speed_cps": None,
    }


async def check_node_metrics(
    rec: NodeRecord,
    timeout_s: float = 5.0,
    samples: int = 3,
    speed_test: bool = True,
    speed_duration_s: float = 0.5,
) -> Dict[str, Any]:
    """对单个节点执行检测，返回指标字典。"""
    host = rec.server
    port = int(rec.port or 0)
    if not host or not port:
        return _empty_metrics()

    pings = await asyncio.gather(
        *[_tcp_ping(host, port, timeout_s) for _ in range(max(1, samples))],
        return_exceptions=True,
    )
    valid = [p for p in pings if isinstance(p, int)]
    if not valid:
        return _empty_metrics(success_rate=0.0)

    metrics: Dict[str, Any] = {
        "latency_ms": min(valid),
        "latency_avg_ms": int(sum(valid) / len(valid)),
        "latency_max_ms": max(valid),
        "jitter_ms": max(valid) - min(valid),
        "success_rate": round(len(valid) / len(pings), 3),
        "speed_cps": None,
    }

    if speed_test:
        cps = await _measure_speed(host, port, duration_s=speed_duration_s)
        if cps is not None:
            metrics["speed_cps"] = cps

    return metrics


async def check_all(
    records: List[NodeRecord],
    concurrent: int = 50,
    timeout_ms: int = 5000,
    samples: int = 3,
    speed_test: bool = True,
    speed_duration_ms: int = 500,
    speed_concurrency: int = 10,
    progress_callback: Optional[ProgressCallback] = None,
) -> List[NodeRecord]:
    total = len(records)
    if total == 0:
        return []

    timeout_s = max(timeout_ms, 100) / 1000.0
    speed_duration_s = max(speed_duration_ms, 100) / 1000.0

    sem = asyncio.Semaphore(max(1, concurrent))
    lock = asyncio.Lock()
    done = 0
    alive = 0

    async def latency_worker(rec: NodeRecord) -> None:
        nonlocal done, alive
        async with sem:
            metrics = await check_node_metrics(
                rec,
                timeout_s=timeout_s,
                samples=samples,
                speed_test=False,
            )
        for k, v in metrics.items():
            setattr(rec, k, v)
        rec.mark_checked()
        async with lock:
            done += 1
            if rec.latency_ms is not None:
                alive += 1
            if progress_callback and (done % 5 == 0 or done == total):
                await progress_callback(done, total, alive)

    await asyncio.gather(*(latency_worker(r) for r in records))

    if speed_test:
        alive_recs = [r for r in records if r.latency_ms is not None]
        if alive_recs:
            s_sem = asyncio.Semaphore(max(1, speed_concurrency))

            async def speed_worker(r: NodeRecord) -> None:
                async with s_sem:
                    cps = await _measure_speed(
                        r.server,
                        int(r.port or 0),
                        duration_s=speed_duration_s,
                    )
                if cps is not None:
                    r.speed_cps = cps

            await asyncio.gather(*(speed_worker(r) for r in alive_recs))

    return [r for r in records if r.latency_ms is not None]
