"""日志：标准输出 + 内存环形缓冲 + WebSocket 广播。

日志级别解析顺序：
  1) 环境变量 VAEL_LOG_LEVEL（若设置）
  2) 配置文件 logging.level
  3) 默认 INFO
"""

import asyncio
import logging
import os
import sys
from collections import deque
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

# 最近日志环形缓冲（供 Web UI 在连接时回填历史）
LOG_BUFFER: deque = deque(maxlen=500)

# 广播回调由 Web 层在启动时注入
_loop: Optional[asyncio.AbstractEventLoop] = None
_broadcast_cb: Optional[Callable[[Dict[str, Any]], Any]] = None

_VALID_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


def _resolve_level() -> str:
    """解析日志级别：环境变量优先，其次配置文件，最后 INFO。"""
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


_LEVEL = _resolve_level()


def apply_log_level(level: Optional[str] = None) -> str:
    """运行时应用日志级别。传入 None 时重新从配置/环境变量解析。"""
    global _LEVEL
    new_level = (level or _resolve_level()).strip().upper()
    if new_level not in _VALID_LEVELS:
        new_level = "INFO"
    _LEVEL = new_level
    logging.getLogger("vael-mux").setLevel(new_level)
    return new_level


def set_log_broadcast(
    loop: asyncio.AbstractEventLoop,
    callback: Callable[[Dict[str, Any]], Any],
) -> None:
    """注入广播回调。

    callback 应为 async 函数，接收单条日志 dict（ts / level / name / line）。
    """
    global _loop, _broadcast_cb
    _loop = loop
    _broadcast_cb = callback


def get_recent_logs(limit: int = 200) -> List[Dict[str, Any]]:
    """获取最近日志（按时间升序）。limit <= 0 表示返回全部。"""
    if limit <= 0:
        return list(LOG_BUFFER)
    return list(LOG_BUFFER)[-limit:]


class _RingBufferHandler(logging.Handler):
    """把日志记录写入内存缓冲，并通过注入的回调广播。"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            ts = datetime.now().strftime("%H:%M:%S")
            short_name = record.name.replace("vael-mux.", "")
            try:
                msg = record.getMessage()
            except Exception:
                msg = str(record.msg)

            entry = {
                "ts": ts,
                "level": record.levelname,
                "name": short_name,
                "line": f"{ts} [{record.levelname}] {short_name}: {msg}",
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
        logger.setLevel(_LEVEL)
        return logger
    logger.setLevel(_LEVEL)

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    logger.addHandler(stream)
    logger.addHandler(_RingBufferHandler())
    logger.propagate = False
    return logger


logger = setup_logger()
