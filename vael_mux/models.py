from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class Node:
    name: str
    protocol: str
    server: str
    port: int
    params: Dict[str, Any] = field(default_factory=dict)
    source: str = ""

    @property
    def key(self) -> str:
        ident = self.params.get("uuid") or self.params.get("password") or self.params.get("id") or ""
        return f"{self.protocol}|{self.server}|{self.port}|{ident}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "protocol": self.protocol,
            "server": self.server,
            "port": self.port,
            "params": self.params,
            "source": self.source,
            "key": self.key,
        }


@dataclass
class NodeStatus:
    key: str
    alive: bool = False
    latency_ms: Optional[float] = None
    download_mbps: Optional[float] = None
    stability: float = 0.0
    score: float = 0.0
    last_checked: Optional[float] = None
    last_error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "alive": self.alive,
            "latency_ms": self.latency_ms,
            "download_mbps": self.download_mbps,
            "stability": self.stability,
            "score": self.score,
            "last_checked": self.last_checked,
            "last_error": self.last_error,
        }
