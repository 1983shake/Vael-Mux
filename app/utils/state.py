"""全局状态管理器：维护流水线进度，向所有 WebSocket 订阅者广播。"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional


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


@dataclass
class AppState:
    stage: str = TaskStage.WEB_READY.value
    message: str = "Web 服务已启动"
    progress: float = 0.0

    # 检测子阶段："latency" | "speed" | ""
    phase: str = ""

    total_subscriptions: int = 0
    fetched_subscriptions: int = 0
    total_nodes: int = 0
    checked_nodes: int = 0
    alive_nodes: int = 0
    enabled_nodes: int = 0
    exported_nodes: int = 0

    # 速度阶段
    speed_total: int = 0
    speed_checked: int = 0
    speed_passed: int = 0

    # 上限
    max_latency_nodes: int = 0
    max_speed_nodes: int = 0
    max_alive: int = 0  # 显示用（= max_speed_nodes 或 max_latency_nodes）
    limit_reached: bool = False

    running: bool = False
    stop_requested: bool = False
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    last_updated: Optional[str] = None

    subscribers: List[Any] = field(default_factory=list)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

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
            "exported_nodes": self.exported_nodes,
            "speed_total": self.speed_total,
            "speed_checked": self.speed_checked,
            "speed_passed": self.speed_passed,
            "max_latency_nodes": self.max_latency_nodes,
            "max_speed_nodes": self.max_speed_nodes,
            "max_alive": self.max_alive,
            "limit_reached": self.limit_reached,
            "running": self.running,
            "stop_requested": self.stop_requested,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "last_updated": self.last_updated,
        }

    async def _send_all(self, payload: Dict[str, Any]) -> None:
        dead = []
        for ws in list(self.subscribers):
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


state = AppState()
