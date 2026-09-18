"""节点检测：单节点流水线（延迟 -> 速度），节点级并发。

有效性定义：
  - 有效延迟：节点自身 server:port 握手成功
             且 latency_targets 中所有【启用】目标握手成功
  - 有效速度：speed_targets 中所有【启用】目标测速成功
              （speed_mbps 必须 > 0；未配置或全部禁用时视为满足）
  - 有效节点：有效延迟 && 有效速度

目标开关：
  - 每个 target 支持 enabled 字段；enabled=False 时完全跳过
    （不检测、不计入有效性判定、不打印日志）
  - 缺省视为启用

流程（单节点流水线 + 节点级并发）：
  1) 所有待测节点同时入队，由并发信号量控制实际并行数
  2) 每个节点独立完成「延迟 -> 速度」，互不等待
  3) 延迟无效 -> 跳过速度测试，不计入有效延迟，也不计入有效节点
  4) 有效延迟 -> 立刻进入速度测试
  5) 有效速度 -> 计入「有效节点」，达到 max_valid_nodes 立即停止

并发控制：
  - 唯一并发限制是 check.concurrent，作用于「节点」维度
  - 每个节点内部：延迟 -> 速度 串行执行

日志：
  - 每次检测地址（节点自身 / 每个启用目标）的结果都会打印
  - 日志级别由 logging.level 或 VAEL_LOG_LEVEL 控制

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
def _is_enabled(tgt: Dict[str, Any]) -> bool:
    """目标是否启用（缺省启用）。"""
    return bool(tgt.get("enabled", True))


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
    """HTTP GET 下载测速，返回 Mbps。

    返回 None 表示失败或速度 <= 0（视为无效）。
    """
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
        # 排除 0.0：速度必须为正
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
    """速度值是否有效：非 None 且 > 0。"""
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
) -> bool:
    """有效延迟：自身延迟非空 + 所有【启用】延迟目标均非空。"""
    if rec.latency_ms is None:
        return False
    targets = latency_targets or []
    active = [t for t in targets if _is_enabled(t)]
    if not active:
        return True
    data = (rec.targets or {}).get("latency") or {}
    for tgt in active:
        name = tgt.get("name") or "?"
        entry = data.get(name)
        if not entry or entry.get("latency_ms") is None:
            return False
    return True


def has_valid_speed(
    rec: NodeRecord,
    speed_targets: Optional[List[Dict[str, Any]]] = None,
) -> bool:
    """有效速度：所有【启用】速度目标均有有效测速（speed_mbps > 0）。

    无启用目标时视为满足。
    """
    targets = speed_targets or []
    active = [t for t in targets if _is_enabled(t)]
    if not active:
        return True
    data = (rec.targets or {}).get("speed") or {}
    for tgt in active:
        name = tgt.get("name") or "?"
        entry = data.get(name)
        if not entry or not _speed_is_valid(entry.get("speed_mbps")):
            return False
    return True


def is_fully_valid(
    rec: NodeRecord,
    latency_targets: Optional[List[Dict[str, Any]]] = None,
    speed_targets: Optional[List[Dict[str, Any]]] = None,
) -> bool:
    """同时具备有效延迟与有效速度。"""
    return has_valid_latency(rec, latency_targets) and has_valid_speed(rec, speed_targets)


# ---------------------------------------------------------------- 单节点检测
async def _check_latency_one(
    rec: NodeRecord,
    timeout_s: float,
    samples: int,
    latency_targets: List[Dict[str, Any]],
) -> bool:
    """延迟检测一个节点。返回 True 表示「有效延迟」（所有启用目标都可达）。"""
    host = rec.server
    port = int(rec.port or 0)
    tag = rec.name or f"{rec.type}-{host}:{port}"

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

    all_targets_ok = True
    for tgt in latency_targets:
        if not _is_enabled(tgt):
            continue
        name = tgt.get("name") or "?"
        url = tgt.get("url") or ""
        t_host, t_port = _url_host_port(url)
        if not t_host:
            rec.targets["latency"][name] = {"alive": False, "latency_ms": None}
            logger.info(f"[延迟] {tag} [{name}] -> 无效地址")
            all_targets_ok = False
            continue
        ms = await _tcp_ping(t_host, t_port, timeout_s)
        rec.targets["latency"][name] = {
            "alive": ms is not None,
            "latency_ms": ms,
        }
        if ms is None:
            logger.info(f"[延迟] {tag} [{name}] {t_host}:{t_port} -> 失败")
            all_targets_ok = False
        else:
            logger.info(f"[延迟] {tag} [{name}] {t_host}:{t_port} -> {ms}ms")

    rec.mark_checked()

    if all_targets_ok:
        logger.info(f"[延迟] {tag} => 有效延迟")
    else:
        logger.info(f"[延迟] {tag} => 无效延迟（存在失败目标）")

    return all_targets_ok


async def _check_speed_one(
    rec: NodeRecord,
    speed_targets: List[Dict[str, Any]],
) -> bool:
    """速度检测一个节点。返回 True 表示「有效速度」（所有启用目标都成功）。"""
    tag = rec.name or f"{rec.type}-{rec.server}:{rec.port}"

    rec.targets.setdefault("speed", {})
    rec.targets["speed"] = {}

    active = [t for t in (speed_targets or []) if _is_enabled(t)]
    if not active:
        logger.info(f"[速度] {tag} => 无启用的速度目标，视为有效")
        return True

    all_ok = True
    for tgt in active:
        name = tgt.get("name") or "?"
        url = tgt.get("url") or ""
        max_bytes = int(tgt.get("size_hint") or 2_000_000)
        mbps = await _http_speed(url, timeout_s=10.0, max_bytes=max_bytes)
        # mbps 为 None 或 <= 0 都视为失败
        ok = _speed_is_valid(mbps)
        rec.targets["speed"][name] = {
            "ok": ok,
            "speed_mbps": mbps if ok else None,
        }
        if not ok:
            logger.info(f"[速度] {tag} [{name}] {url} -> 失败（速度无效）")
            all_ok = False
        else:
            logger.info(f"[速度] {tag} [{name}] {url} -> {mbps} Mbps")

    if all_ok:
        logger.info(f"[速度] {tag} => 有效速度")
    else:
        logger.info(f"[速度] {tag} => 无效速度（存在失败目标）")

    return all_ok


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
) -> List[NodeRecord]:
    """完整检测流程（单节点流水线 + 节点级并发）。

    - concurrent：节点并发数（同时进行「延迟 -> 速度」完整流程的节点数）
    - max_valid：有效节点上限，达到后立即停止
    - 返回 is_fully_valid 为真的节点（按节点自身延迟升序）
    """
    total = len(records)
    if total == 0:
        return []

    timeout_s = max(timeout_ms, 100) / 1000.0
    latency_targets = latency_targets or []
    speed_targets = speed_targets or []

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

    # 延迟阶段进度回调节流：最多更新约 200 次
    latency_step = max(1, total // 200)
    # 速度阶段进度回调节流：每 5 次检查推送一次
    speed_step = 5

    async def worker(rec: NodeRecord) -> None:
        if shared["stop"]:
            return

        # ---------- 延迟 ----------
        async with sem:
            if shared["stop"]:
                return
            lat_ok = await _check_latency_one(rec, timeout_s, samples, latency_targets)

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

        # ---------- 速度（无独立并发，持有节点信号量直接跑）----------
        spd_ok = await _check_speed_one(rec, speed_targets)

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
        f"（上限 {stop_limit or '∞'}，{'已达上限' if shared['stop'] else '未达上限'}）"
    )

    passed.sort(key=lambda r: r.latency_ms or 10**9)
    if stop_limit:
        passed = passed[:stop_limit]

    return passed
