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


@dataclass
class AppState:
    stage: str = TaskStage.WEB_READY.value
    message: str = "Web 服务已启动"
    progress: float = 0.0

    total_subscriptions: int = 0
    fetched_subscriptions: int = 0
    total_nodes: int = 0
    checked_nodes: int = 0
    alive_nodes: int = 0

    running: bool = False
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
            "total_subscriptions": self.total_subscriptions,
            "fetched_subscriptions": self.fetched_subscriptions,
            "total_nodes": self.total_nodes,
            "checked_nodes": self.checked_nodes,
            "alive_nodes": self.alive_nodes,
            "running": self.running,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "last_updated": self.last_updated,
        }

    async def notify(self) -> None:
        """向所有订阅者推送当前快照，清理失效连接。"""
        self.last_updated = datetime.now().isoformat(timespec="seconds")
        payload = self.snapshot()

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


state = AppState()
