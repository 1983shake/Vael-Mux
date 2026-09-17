"""节点检测：多目标 TCP 延迟 + 多目标 HTTP 下载测速。

有效性定义（新）：
  - has_valid_latency:  节点自身 server:port 握手成功
                        且 latency_targets 中**所有**目标握手成功
  - has_valid_speed:    speed_targets 中**所有**目标测速成功
                        （未配置 speed_targets 时视为满足）
  - is_fully_valid:     has_valid_latency && has_valid_speed

流程：
  1) 延迟阶段
     - 对每个节点，先测自身 server:port TCP 握手
     - 再对 latency_targets 中每个目标做 TCP 握手
     - 延迟有效节点数达到 max_latency 时立即停止后续 worker
  2) 速度阶段
     - 只对延迟有效节点测速（按节点自身延迟升序）
     - 对每个 speed_targets 目标做 HTTP GET 下载
     - 速度有效节点数达到 max_speed 时立即停止后续 worker

注意：速度测试是"从本机直连目标 URL"，不是经过节点代理的端到端测速。
"""

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx

from app.models import NodeRecord

logger = logging.getLogger("vael-mux.checker")

ProgressCallback = Callable[[int, int, int], Awaitable[None]]
SpeedProgressCallback = Callable[[int, int, int], Awaitable[None]]


# ---------------------------------------------------------------- 基础工具
async def _tcp_ping(host: str, port: int, timeout_s: float) -> Optional[int]:
    if not host or not port:
        return None
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


def _url_host_port(url: str) -> Tuple[Optional[str], int]:
    try:
        p = urlparse(url)
    except Exception:
        return None, 0
    host = p.hostname
    if not host:
        return None, 0
    if p.port:
        try:
            port = int(p.port)
        except (TypeError, ValueError):
            return None, 0
    elif p.scheme == "http":
        port = 80
    else:
        port = 443
    return host, port


async def _http_speed(
    url: str,
    timeout_s: float = 10.0,
    max_bytes: int = 2_000_000,
) -> Optional[float]:
    """HTTP GET 下载测速，返回 Mbps。"""
    if not url:
        return None
    try:
        start = time.monotonic()
        total = 0
        async with httpx.AsyncClient(
            timeout=timeout_s,
            follow_redirects=True,
            headers={"User-Agent": "Vael-Mux/1.5"},
        ) as client:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                async for chunk in resp.aiter_bytes(65536):
                    total += len(chunk)
                    if total >= max_bytes:
                        break
                    if time.monotonic() - start > timeout_s:
                        break
        elapsed = time.monotonic() - start
        if elapsed <= 0 or total == 0:
            return None
        return round(total * 8 / elapsed / 1_000_000, 2)
    except Exception:
        return None


def _empty_metrics() -> Dict[str, Any]:
    return {
        "latency_ms": None,
        "latency_avg_ms": None,
        "latency_max_ms": None,
        "jitter_ms": None,
        "success_rate": 0.0,
        "speed_cps": None,
    }


# ---------------------------------------------------------------- 有效性判定
def has_valid_latency(
    rec: NodeRecord,
    latency_targets: Optional[List[Dict[str, Any]]] = None,
) -> bool:
    """延迟有效：自身延迟非空 + 所有延迟目标均非空。"""
    if rec.latency_ms is None:
        return False
    targets = latency_targets or []
    if not targets:
        return True
    data = (rec.targets or {}).get("latency") or {}
    for tgt in targets:
        name = tgt.get("name") or "?"
        entry = data.get(name)
        if not entry or entry.get("latency_ms") is None:
            return False
    return True


def has_valid_speed(
    rec: NodeRecord,
    speed_targets: Optional[List[Dict[str, Any]]] = None,
) -> bool:
    """速度有效：所有速度目标均有有效测速（未配置时视为满足）。"""
    targets = speed_targets or []
    if not targets:
        return True
    data = (rec.targets or {}).get("speed") or {}
    for tgt in targets:
        name = tgt.get("name") or "?"
        entry = data.get(name)
        if not entry or entry.get("speed_mbps") is None:
            return False
    return True


def is_fully_valid(
    rec: NodeRecord,
    latency_targets: Optional[List[Dict[str, Any]]] = None,
    speed_targets: Optional[List[Dict[str, Any]]] = None,
) -> bool:
    """同时具备有效延迟与有效速度。"""
    return has_valid_latency(rec, latency_targets) and has_valid_speed(rec, speed_targets)


# ---------------------------------------------------------------- 单节点基础延迟
async def check_node_metrics(
    rec: NodeRecord,
    timeout_s: float = 5.0,
    samples: int = 3,
    latency_targets: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """单节点重测：节点自身延迟 + 所有延迟目标。"""
    latency_targets = latency_targets or []
    host = rec.server
    port = int(rec.port or 0)

    rec.targets.setdefault("latency", {})
    rec.targets.setdefault("speed", {})

    if not host or not port:
        return _empty_metrics()

    pings = await asyncio.gather(
        *[_tcp_ping(host, port, timeout_s) for _ in range(max(1, samples))],
        return_exceptions=True,
    )
    valid = [p for p in pings if isinstance(p, int)]

    if not valid:
        for tgt in latency_targets:
            name = tgt.get("name") or "?"
            rec.targets["latency"][name] = {"alive": False, "latency_ms": None}
        return _empty_metrics()

    # 重新测试所有延迟目标
    for tgt in latency_targets:
        name = tgt.get("name") or "?"
        url = tgt.get("url") or ""
        t_host, t_port = _url_host_port(url)
        if not t_host:
            rec.targets["latency"][name] = {"alive": False, "latency_ms": None}
            continue
        ms = await _tcp_ping(t_host, t_port, timeout_s)
        rec.targets["latency"][name] = {
            "alive": ms is not None,
            "latency_ms": ms,
        }

    return {
        "latency_ms": min(valid),
        "latency_avg_ms": int(sum(valid) / len(valid)),
        "latency_max_ms": max(valid),
        "jitter_ms": max(valid) - min(valid),
        "success_rate": round(len(valid) / len(pings), 3),
        "speed_cps": None,
    }


# ---------------------------------------------------------------- 批量检测
async def check_all(
    records: List[NodeRecord],
    concurrent: int = 50,
    timeout_ms: int = 5000,
    samples: int = 3,
    latency_targets: Optional[List[Dict[str, Any]]] = None,
    speed_targets: Optional[List[Dict[str, Any]]] = None,
    speed_concurrency: int = 10,
    max_latency: int = 0,
    max_speed: int = 0,
    progress_callback: Optional[ProgressCallback] = None,
    speed_progress_callback: Optional[SpeedProgressCallback] = None,
) -> List[NodeRecord]:
    """完整检测流程。

    - 延迟阶段：延迟有效节点数达到 max_latency 时立即停止
    - 速度阶段：速度有效节点数达到 max_speed 时立即停止
    - 0 表示对应阶段不限制
    - 返回 is_fully_valid 为真的节点（节点自身延迟升序）
    """
    total = len(records)
    if total == 0:
        return []

    timeout_s = max(timeout_ms, 100) / 1000.0
    latency_targets = latency_targets or []
    speed_targets = speed_targets or []
    stop_latency = max_latency if max_latency and max_latency > 0 else 0
    stop_speed = max_speed if max_speed and max_speed > 0 else 0
    require_speed = bool(speed_targets)

    # ==================== 阶段 1：延迟 ====================
    sem = asyncio.Semaphore(max(1, concurrent))
    lock = asyncio.Lock()
    shared = {"done": 0, "alive": 0, "stop": False}

    async def latency_worker(rec: NodeRecord) -> None:
        if shared["stop"]:
            return
        async with sem:
            if shared["stop"]:
                return

            host = rec.server
            port = int(rec.port or 0)

            pings = await asyncio.gather(
                *[_tcp_ping(host, port, timeout_s) for _ in range(max(1, samples))],
                return_exceptions=True,
            )
            valid = [p for p in pings if isinstance(p, int)]

            rec.targets.setdefault("latency", {})
            rec.targets.setdefault("speed", {})

            if not valid:
                rec.latency_ms = None
                rec.latency_avg_ms = None
                rec.latency_max_ms = None
                rec.jitter_ms = None
                rec.success_rate = 0.0
                # 记录所有延迟目标为失败
                for tgt in latency_targets:
                    name = tgt.get("name") or "?"
                    rec.targets["latency"][name] = {
                        "alive": False,
                        "latency_ms": None,
                    }
                rec.mark_checked()
                async with lock:
                    shared["done"] += 1
                    if progress_callback and (shared["done"] % 5 == 0 or shared["done"] == total):
                        await progress_callback(shared["done"], total, shared["alive"])
                return

            rec.latency_ms = min(valid)
            rec.latency_avg_ms = int(sum(valid) / len(valid))
            rec.latency_max_ms = max(valid)
            rec.jitter_ms = max(valid) - min(valid)
            rec.success_rate = round(len(valid) / len(pings), 3)

            # 对每个 latency_target 做 TCP 握手
            for tgt in latency_targets:
                name = tgt.get("name") or "?"
                url = tgt.get("url") or ""
                t_host, t_port = _url_host_port(url)
                if not t_host:
                    rec.targets["latency"][name] = {
                        "alive": False,
                        "latency_ms": None,
                    }
                    continue
                ms = await _tcp_ping(t_host, t_port, timeout_s)
                rec.targets["latency"][name] = {
                    "alive": ms is not None,
                    "latency_ms": ms,
                }

            rec.mark_checked()

            # 只有当所有延迟目标都成功，才计入"延迟有效"
            lat_ok = has_valid_latency(rec, latency_targets)

            async with lock:
                shared["done"] += 1
                if lat_ok:
                    shared["alive"] += 1
                    if stop_latency and shared["alive"] >= stop_latency:
                        shared["stop"] = True
                if progress_callback and (shared["done"] % 5 == 0 or shared["done"] == total):
                    await progress_callback(shared["done"], total, shared["alive"])

    await asyncio.gather(*(latency_worker(r) for r in records))

    alive_by_latency = [r for r in records if has_valid_latency(r, latency_targets)]
    alive_by_latency.sort(key=lambda r: r.latency_ms or 10**9)
    if stop_latency:
        alive_by_latency = alive_by_latency[:stop_latency]

    logger.info(
        f"延迟测试完成: 有效 {len(alive_by_latency)} / 检测 {shared['done']} / 总 {total}"
        f"（上限 {stop_latency or '∞'}，{'已达上限' if shared['stop'] else '未达上限'}）"
    )

    # ==================== 阶段 2：速度 ====================
    if require_speed and alive_by_latency:
        # 清空本次将要测速的节点的速度数据，避免旧数据干扰
        for r in alive_by_latency:
            r.targets["speed"] = {}

        s_sem = asyncio.Semaphore(max(1, speed_concurrency))
        s_lock = asyncio.Lock()
        shared_speed = {"done": 0, "passed": 0, "stop": False}
        speed_total = len(alive_by_latency)

        async def speed_worker(rec: NodeRecord) -> None:
            if shared_speed["stop"]:
                return
            async with s_sem:
                if shared_speed["stop"]:
                    return

                # 逐个测试速度目标（需要全部通过）
                for tgt in speed_targets:
                    name = tgt.get("name") or "?"
                    url = tgt.get("url") or ""
                    max_bytes = int(tgt.get("size_hint") or 2_000_000)
                    mbps = await _http_speed(url, timeout_s=10.0, max_bytes=max_bytes)
                    rec.targets["speed"][name] = {
                        "ok": mbps is not None,
                        "speed_mbps": mbps,
                    }

                # 所有速度目标都成功才算"速度有效"
                spd_ok = has_valid_speed(rec, speed_targets)

                async with s_lock:
                    shared_speed["done"] += 1
                    if spd_ok:
                        shared_speed["passed"] += 1
                        if stop_speed and shared_speed["passed"] >= stop_speed:
                            shared_speed["stop"] = True

                    if speed_progress_callback and (shared_speed["done"] % 3 == 0 or shared_speed["done"] == speed_total or shared_speed["stop"]):
                        await speed_progress_callback(
                            shared_speed["done"],
                            speed_total,
                            shared_speed["passed"],
                        )

        await asyncio.gather(*(speed_worker(r) for r in alive_by_latency))

        logger.info(
            f"速度测试完成: 有效 {shared_speed['passed']} / 检测 {shared_speed['done']}"
            f" / 总 {speed_total}（上限 {stop_speed or '∞'}，"
            f"{'已达上限' if shared_speed['stop'] else '未达上限'}）"
        )

    # ==================== 过滤 & 截断 ====================
    passed = [r for r in alive_by_latency if is_fully_valid(r, latency_targets, speed_targets)]
    passed.sort(key=lambda r: r.latency_ms or 10**9)
    if stop_speed:
        passed = passed[:stop_speed]

    return passed
