"""运行时模块：全局状态 + 日志（标准输出 / 环形缓冲 / WebSocket 广播）。

本模块合并了原来的 logger.py 与 state.py：
  - 日志广播与状态广播共享同一批 WebSocket 订阅者，合并后逻辑内聚；
  - AppState 使用 __slots__，避免每个实例携带 __dict__；
  - 日志环形缓冲仅保留 (level, line) 两个字段，且上限下调至 300 条。
"""

import asyncio
import logging
import os
import sys
from collections import deque
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

_VALID_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


# ============================================================ 全局状态
class TaskStage(str, Enum):
    WEB_READY = "web_ready"
    CONFIG_LOADING = "config_loading"
    FETCHING = "fetching"
    PARSING = "parsing"
    CHECKING = "checking"
    EXPORTING = "exporting"
    IDLE = "idle"
    ERROR = "error"
    STOPPED = "stopped"


class AppState:
    """全局状态。使用 __slots__ 减少实例常驻内存。"""

    __slots__ = (
        "stage",
        "message",
        "progress",
        "phase",
        "total_subscriptions",
        "fetched_subscriptions",
        "total_nodes",
        "checked_nodes",
        "alive_nodes",
        "enabled_nodes",
        "speed_total",
        "speed_checked",
        "speed_passed",
        "max_valid_nodes",
        "limit_reached",
        "exported_nodes",
        "running",
        "stop_requested",
        "started_at",
        "finished_at",
        "last_updated",
        "subscribers",
    )

    def __init__(self) -> None:
        self.stage = TaskStage.WEB_READY.value
        self.message = "Web 服务已启动"
        self.progress = 0.0
        self.phase = ""
        self.total_subscriptions = 0
        self.fetched_subscriptions = 0
        self.total_nodes = 0
        self.checked_nodes = 0
        self.alive_nodes = 0
        self.enabled_nodes = 0
        self.speed_total = 0
        self.speed_checked = 0
        self.speed_passed = 0
        self.max_valid_nodes = 0
        self.limit_reached = False
        self.exported_nodes = 0
        self.running = False
        self.stop_requested = False
        self.started_at: Optional[str] = None
        self.finished_at: Optional[str] = None
        self.last_updated: Optional[str] = None
        self.subscribers: List[Any] = []

    def reset_run(self, total_subscriptions: int, max_valid: int) -> None:
        """重置一次流水线运行所需字段。"""
        self.running = True
        self.stop_requested = False
        self.phase = ""
        self.started_at = datetime.now().isoformat(timespec="seconds")
        self.finished_at = None
        self.total_subscriptions = total_subscriptions
        self.fetched_subscriptions = 0
        self.total_nodes = 0
        self.checked_nodes = 0
        self.alive_nodes = 0
        self.enabled_nodes = 0
        self.exported_nodes = 0
        self.speed_total = 0
        self.speed_checked = 0
        self.speed_passed = 0
        self.max_valid_nodes = max_valid
        self.limit_reached = False

    def snapshot(self) -> Dict[str, Any]:
        return {
            "stage": self.stage,
            "message": self.message,
            "progress": round(self.progress, 4),
            "phase": self.phase,
            "total_subscriptions": self.total_subscriptions,
            "fetched_subscriptions": self.fetched_subscriptions,
            "total_nodes": self.total_nodes,
            "checked_nodes": self.checked_nodes,
            "alive_nodes": self.alive_nodes,
            "enabled_nodes": self.enabled_nodes,
            "speed_total": self.speed_total,
            "speed_checked": self.speed_checked,
            "speed_passed": self.speed_passed,
            "max_valid_nodes": self.max_valid_nodes,
            "limit_reached": self.limit_reached,
            "exported_nodes": self.exported_nodes,
            "running": self.running,
            "stop_requested": self.stop_requested,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "last_updated": self.last_updated,
        }

    async def _send_all(self, payload: Dict[str, Any]) -> None:
        dead = []
        for ws in self.subscribers:
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            try:
                self.subscribers.remove(ws)
            except ValueError:
                pass

    async def notify(self) -> None:
        self.last_updated = datetime.now().isoformat(timespec="seconds")
        await self._send_all({"event": "state", **self.snapshot()})

    async def broadcast_event(self, event: str, data: Optional[Dict[str, Any]] = None) -> None:
        await self._send_all({"event": event, **(data or {})})

    async def broadcast_log(self, entry: Dict[str, Any]) -> None:
        await self._send_all({"event": "log", **entry})


# 模块级单例
state = AppState()


# ============================================================ 日志
LOG_BUFFER: deque = deque(maxlen=300)
LOG_BACKFILL = 200  # WebSocket 连接建立时回填的历史日志条数

_loop: Optional[asyncio.AbstractEventLoop] = None
_broadcast_cb: Optional[Callable[[Dict[str, Any]], Any]] = None


def _resolve_level() -> str:
    """日志级别解析：环境变量 > 配置文件 > INFO。"""
    env = os.environ.get("VAEL_LOG_LEVEL", "").strip().upper()
    if env in _VALID_LEVELS:
        return env
    try:
        from app.config import load_base_config

        cfg = load_base_config()
        raw = (cfg.get("logging") or {}).get("level", "INFO")
        level = str(raw or "INFO").strip().upper()
        if level in _VALID_LEVELS:
            return level
    except Exception:
        pass
    return "INFO"


def apply_log_level(level: Optional[str] = None) -> str:
    """运行时应用日志级别。传入 None 时重新从配置/环境变量解析。"""
    new_level = (level or _resolve_level()).strip().upper()
    if new_level not in _VALID_LEVELS:
        new_level = "INFO"
    logging.getLogger("vael-mux").setLevel(new_level)
    return new_level


def set_log_broadcast(
    loop: asyncio.AbstractEventLoop,
    callback: Callable[[Dict[str, Any]], Any],
) -> None:
    """注入广播回调（由 Web 层在启动时调用）。"""
    global _loop, _broadcast_cb
    _loop = loop
    _broadcast_cb = callback


def get_recent_logs(limit: int = LOG_BACKFILL) -> List[Dict[str, Any]]:
    """获取最近日志（按时间升序）。limit <= 0 表示返回全部。"""
    if limit <= 0:
        return list(LOG_BUFFER)
    return list(LOG_BUFFER)[-limit:]


class _RingBufferHandler(logging.Handler):
    """把日志记录写入内存缓冲，并通过注入的回调广播。

    为避免常驻内存膨胀，只保留 level 与渲染后的单行文本两个字段。
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = record.levelname
            try:
                msg = record.getMessage()
            except Exception:
                msg = str(record.msg)
            short_name = record.name.replace("vael-mux.", "")
            entry = {
                "level": level,
                "line": f"{datetime.now().strftime('%H:%M:%S')} [{level}] {short_name}: {msg}",
            }
            LOG_BUFFER.append(entry)

            if _broadcast_cb is not None and _loop is not None and _loop.is_running():
                try:
                    asyncio.run_coroutine_threadsafe(_broadcast_cb(entry), _loop)
                except Exception:
                    pass
        except Exception:
            # 日志系统自身出错不影响主流程
            pass


def setup_logger(name: str = "vael-mux") -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(_resolve_level())
    logger.propagate = False

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    logger.addHandler(stream)
    logger.addHandler(_RingBufferHandler())
    return logger


logger = setup_logger()
