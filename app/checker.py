"""节点检测：单节点流水线（延迟 -> 速度），节点级并发。

有效性定义：
  - 有效延迟：节点自身 server:port 握手成功（硬前提）
             且按 latency_mode 判定 latency_targets 中启用目标：
               - "all"：所有启用目标都必须握手成功
               - "any"：至少一个启用目标握手成功
             无启用目标时视为满足。
  - 有效速度：按 speed_mode 判定 speed_targets 中启用目标：
               - "all"：所有启用目标都必须测速成功（speed_mbps > 0）
               - "any"：至少一个启用目标测速成功
             无启用目标时视为满足。
  - 有效节点：有效延迟 && 有效速度
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
SpeedProgressCallback = Callable[[int, int], Awaitable[None]]

_MODE_LABEL = {"all": "全部通过", "any": "任一通过"}


# ---------------------------------------------------------------- 基础工具
def _is_enabled(tgt: Dict[str, Any]) -> bool:
    return bool(tgt.get("enabled", True))


def _norm_mode(mode: Any) -> str:
    return "any" if str(mode or "all").strip().lower() == "any" else "all"


def _enabled_list(targets: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    return [t for t in (targets or []) if _is_enabled(t)]


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
    if not url:
        return None
    try:
        start = time.monotonic()
        total = 0
        async with httpx.AsyncClient(
            timeout=timeout_s,
            follow_redirects=True,
            headers={"User-Agent": "Vael-Mux/1.7"},
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
        mbps = round(total * 8 / elapsed / 1_000_000, 2)
        if mbps <= 0:
            return None
        return mbps
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


def _speed_is_valid(mbps: Any) -> bool:
    if mbps is None:
        return False
    try:
        return float(mbps) > 0
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------- 有效性判定
def has_valid_latency(
    rec: NodeRecord,
    latency_targets: Optional[List[Dict[str, Any]]] = None,
    mode: str = "all",
) -> bool:
    if rec.latency_ms is None:
        return False

    active = _enabled_list(latency_targets)
    if not active:
        return True

    data = (rec.targets or {}).get("latency") or {}
    results = []
    for tgt in active:
        name = tgt.get("name") or "?"
        entry = data.get(name)
        results.append(bool(entry and entry.get("latency_ms") is not None))

    return any(results) if _norm_mode(mode) == "any" else all(results)


def has_valid_speed(
    rec: NodeRecord,
    speed_targets: Optional[List[Dict[str, Any]]] = None,
    mode: str = "all",
) -> bool:
    active = _enabled_list(speed_targets)
    if not active:
        return True

    data = (rec.targets or {}).get("speed") or {}
    results = []
    for tgt in active:
        name = tgt.get("name") or "?"
        entry = data.get(name)
        results.append(bool(entry and _speed_is_valid(entry.get("speed_mbps"))))

    return any(results) if _norm_mode(mode) == "any" else all(results)


def is_fully_valid(
    rec: NodeRecord,
    latency_targets: Optional[List[Dict[str, Any]]] = None,
    speed_targets: Optional[List[Dict[str, Any]]] = None,
    latency_mode: str = "all",
    speed_mode: str = "all",
) -> bool:
    return has_valid_latency(rec, latency_targets, latency_mode) and has_valid_speed(rec, speed_targets, speed_mode)


def _latency_pass_count(
    rec: NodeRecord,
    targets: List[Dict[str, Any]],
) -> Tuple[int, int]:
    active = _enabled_list(targets)
    data = (rec.targets or {}).get("latency") or {}
    passed = 0
    for tgt in active:
        entry = data.get(tgt.get("name") or "?")
        if entry and entry.get("latency_ms") is not None:
            passed += 1
    return passed, len(active)


def _speed_pass_count(
    rec: NodeRecord,
    targets: List[Dict[str, Any]],
) -> Tuple[int, int]:
    active = _enabled_list(targets)
    data = (rec.targets or {}).get("speed") or {}
    passed = 0
    for tgt in active:
        entry = data.get(tgt.get("name") or "?")
        if entry and _speed_is_valid(entry.get("speed_mbps")):
            passed += 1
    return passed, len(active)


# ---------------------------------------------------------------- 单节点检测
async def _check_latency_one(
    rec: NodeRecord,
    timeout_s: float,
    samples: int,
    latency_targets: List[Dict[str, Any]],
    mode: str = "all",
) -> bool:
    host = rec.server
    port = int(rec.port or 0)
    tag = rec.name or f"{rec.type}-{host}:{port}"
    mode = _norm_mode(mode)

    rec.targets.setdefault("latency", {})
    rec.targets.setdefault("speed", {})

    if not host or not port:
        rec.latency_ms = None
        rec.latency_avg_ms = None
        rec.latency_max_ms = None
        rec.jitter_ms = None
        rec.success_rate = 0.0
        logger.info(f"[延迟] {tag} | 无效：缺少服务器或端口")
        for tgt in latency_targets:
            if not _is_enabled(tgt):
                continue
            name = tgt.get("name") or "?"
            rec.targets["latency"][name] = {"alive": False, "latency_ms": None}
            logger.info(f"[延迟] {tag} [{name}] -> 跳过（节点地址无效）")
        rec.mark_checked()
        return False

    pings = await asyncio.gather(
        *[_tcp_ping(host, port, timeout_s) for _ in range(max(1, samples))],
        return_exceptions=True,
    )
    valid = [p for p in pings if isinstance(p, int)]

    if not valid:
        rec.latency_ms = None
        rec.latency_avg_ms = None
        rec.latency_max_ms = None
        rec.jitter_ms = None
        rec.success_rate = 0.0
        logger.info(f"[延迟] {tag} | 自身 {host}:{port} -> 失败")
        for tgt in latency_targets:
            if not _is_enabled(tgt):
                continue
            name = tgt.get("name") or "?"
            rec.targets["latency"][name] = {"alive": False, "latency_ms": None}
            t_host, t_port = _url_host_port(tgt.get("url") or "")
            target_str = f"{t_host}:{t_port}" if t_host else "(无效地址)"
            logger.info(f"[延迟] {tag} [{name}] {target_str} -> 跳过（自身不可达）")
        rec.mark_checked()
        return False

    rec.latency_ms = min(valid)
    rec.latency_avg_ms = int(sum(valid) / len(valid))
    rec.latency_max_ms = max(valid)
    rec.jitter_ms = max(valid) - min(valid)
    rec.success_rate = round(len(valid) / len(pings), 3)
    logger.info(
        f"[延迟] {tag} | 自身 {host}:{port} -> {rec.latency_ms}ms"
        f"（avg {rec.latency_avg_ms}ms, max {rec.latency_max_ms}ms,"
        f" jitter {rec.jitter_ms}ms, 成功率 {rec.success_rate}）"
    )

    for tgt in latency_targets:
        if not _is_enabled(tgt):
            continue
        name = tgt.get("name") or "?"
        url = tgt.get("url") or ""
        t_host, t_port = _url_host_port(url)
        if not t_host:
            rec.targets["latency"][name] = {"alive": False, "latency_ms": None}
            logger.info(f"[延迟] {tag} [{name}] -> 无效地址")
            continue
        ms = await _tcp_ping(t_host, t_port, timeout_s)
        rec.targets["latency"][name] = {
            "alive": ms is not None,
            "latency_ms": ms,
        }
        if ms is None:
            logger.info(f"[延迟] {tag} [{name}] {t_host}:{t_port} -> 失败")
        else:
            logger.info(f"[延迟] {tag} [{name}] {t_host}:{t_port} -> {ms}ms")

    rec.mark_checked()

    ok = has_valid_latency(rec, latency_targets, mode)
    passed, total_t = _latency_pass_count(rec, latency_targets)
    if ok:
        logger.info(f"[延迟] {tag} => 有效延迟" f"（{_MODE_LABEL[mode]}，{passed}/{total_t} 目标通过）")
    else:
        logger.info(f"[延迟] {tag} => 无效延迟" f"（{_MODE_LABEL[mode]}，{passed}/{total_t} 目标通过）")

    return ok


async def _check_speed_one(
    rec: NodeRecord,
    speed_targets: List[Dict[str, Any]],
    mode: str = "all",
) -> bool:
    tag = rec.name or f"{rec.type}-{rec.server}:{rec.port}"
    mode = _norm_mode(mode)

    rec.targets.setdefault("speed", {})
    rec.targets["speed"] = {}

    active = _enabled_list(speed_targets)
    if not active:
        logger.info(f"[速度] {tag} => 无启用的速度目标，视为有效")
        return True

    for tgt in active:
        name = tgt.get("name") or "?"
        url = tgt.get("url") or ""
        max_bytes = int(tgt.get("size_hint") or 2_000_000)
        mbps = await _http_speed(url, timeout_s=10.0, max_bytes=max_bytes)
        ok = _speed_is_valid(mbps)
        rec.targets["speed"][name] = {
            "ok": ok,
            "speed_mbps": mbps if ok else None,
        }
        if not ok:
            logger.info(f"[速度] {tag} [{name}] {url} -> 失败（速度无效）")
        else:
            logger.info(f"[速度] {tag} [{name}] {url} -> {mbps} Mbps")

    ok = has_valid_speed(rec, speed_targets, mode)
    passed, total_t = _speed_pass_count(rec, speed_targets)
    if ok:
        logger.info(f"[速度] {tag} => 有效速度" f"（{_MODE_LABEL[mode]}，{passed}/{total_t} 目标通过）")
    else:
        logger.info(f"[速度] {tag} => 无效速度" f"（{_MODE_LABEL[mode]}，{passed}/{total_t} 目标通过）")

    return ok


async def check_node_metrics(
    rec: NodeRecord,
    timeout_s: float = 5.0,
    samples: int = 3,
    latency_targets: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """单节点重测：节点自身延迟 + 所有延迟目标（供 Web 端「立即重测」使用）。"""
    latency_targets = latency_targets or []
    host = rec.server
    port = int(rec.port or 0)
    tag = rec.name or f"{rec.type}-{host}:{port}"

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
        logger.info(f"[重测] {tag} | 自身 {host}:{port} -> 失败")
        for tgt in latency_targets:
            if not _is_enabled(tgt):
                continue
            name = tgt.get("name") or "?"
            rec.targets["latency"][name] = {"alive": False, "latency_ms": None}
            t_host, t_port = _url_host_port(tgt.get("url") or "")
            target_str = f"{t_host}:{t_port}" if t_host else "(无效地址)"
            logger.info(f"[重测] {tag} [{name}] {target_str} -> 跳过（自身不可达）")
        return _empty_metrics()

    for tgt in latency_targets:
        if not _is_enabled(tgt):
            continue
        name = tgt.get("name") or "?"
        url = tgt.get("url") or ""
        t_host, t_port = _url_host_port(url)
        if not t_host:
            rec.targets["latency"][name] = {"alive": False, "latency_ms": None}
            logger.info(f"[重测] {tag} [{name}] -> 无效地址")
            continue
        ms = await _tcp_ping(t_host, t_port, timeout_s)
        rec.targets["latency"][name] = {
            "alive": ms is not None,
            "latency_ms": ms,
        }
        if ms is None:
            logger.info(f"[重测] {tag} [{name}] {t_host}:{t_port} -> 失败")
        else:
            logger.info(f"[重测] {tag} [{name}] {t_host}:{t_port} -> {ms}ms")

    metrics = {
        "latency_ms": min(valid),
        "latency_avg_ms": int(sum(valid) / len(valid)),
        "latency_max_ms": max(valid),
        "jitter_ms": max(valid) - min(valid),
        "success_rate": round(len(valid) / len(pings), 3),
        "speed_cps": None,
    }
    logger.info(f"[重测] {tag} | 自身 {host}:{port} -> {metrics['latency_ms']}ms")
    return metrics


# ---------------------------------------------------------------- 批量检测
async def check_all(
    records: List[NodeRecord],
    concurrent: int = 50,
    timeout_ms: int = 5000,
    samples: int = 3,
    latency_targets: Optional[List[Dict[str, Any]]] = None,
    speed_targets: Optional[List[Dict[str, Any]]] = None,
    max_valid: int = 0,
    progress_callback: Optional[ProgressCallback] = None,
    speed_progress_callback: Optional[SpeedProgressCallback] = None,
    latency_mode: str = "all",
    speed_mode: str = "all",
) -> List[NodeRecord]:
    total = len(records)
    if total == 0:
        return []

    timeout_s = max(timeout_ms, 100) / 1000.0
    latency_targets = latency_targets or []
    speed_targets = speed_targets or []
    latency_mode = _norm_mode(latency_mode)
    speed_mode = _norm_mode(speed_mode)

    stop_limit = max_valid if max_valid and max_valid > 0 else 0
    concurrent = max(1, int(concurrent))

    sem = asyncio.Semaphore(concurrent)
    lock = asyncio.Lock()

    shared = {
        "latency_done": 0,
        "latency_valid": 0,
        "speed_done": 0,
        "speed_valid": 0,
        "stop": False,
    }
    passed: List[NodeRecord] = []

    latency_step = max(1, total // 200)
    speed_step = 5

    async def worker(rec: NodeRecord) -> None:
        if shared["stop"]:
            return

        async with sem:
            if shared["stop"]:
                return
            lat_ok = await _check_latency_one(rec, timeout_s, samples, latency_targets, latency_mode)

        async with lock:
            shared["latency_done"] += 1
            if lat_ok:
                shared["latency_valid"] += 1
            lat_done = shared["latency_done"]
            lat_valid = shared["latency_valid"]

        if progress_callback and (lat_done == total or lat_done % latency_step == 0):
            await progress_callback(lat_done, total, lat_valid)

        if not lat_ok:
            return

        if shared["stop"]:
            return

        spd_ok = await _check_speed_one(rec, speed_targets, speed_mode)

        async with lock:
            shared["speed_done"] += 1
            if spd_ok:
                shared["speed_valid"] += 1
                passed.append(rec)
                if stop_limit and shared["speed_valid"] >= stop_limit:
                    shared["stop"] = True
            spd_done = shared["speed_done"]
            spd_valid = shared["speed_valid"]
            stop_now = shared["stop"]

        if speed_progress_callback and (spd_ok or stop_now or spd_done % speed_step == 0):
            await speed_progress_callback(spd_done, spd_valid)

    await asyncio.gather(*(worker(r) for r in records))

    logger.info(
        f"检测完成: 有效延迟 {shared['latency_valid']} / 检测 {shared['latency_done']}"
        f" / 总 {total}；有效速度（有效节点） {shared['speed_valid']}"
        f"（上限 {stop_limit or '∞'}，{'已达上限' if shared['stop'] else '未达上限'}；"
        f"延迟判定={_MODE_LABEL[latency_mode]}，速度判定={_MODE_LABEL[speed_mode]}）"
    )

    passed.sort(key=lambda r: r.latency_ms or 10**9)
    if stop_limit:
        passed = passed[:stop_limit]

    return passed
