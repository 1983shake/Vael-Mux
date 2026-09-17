"""节点数据模型与 ID 生成。"""

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional


def now_str() -> str:
    return datetime.now().isoformat(timespec="seconds")


def make_node_id(node_type: str, server: str, port: int, credential: str) -> str:
    key = f"{node_type}://{server}:{port}#{credential}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


@dataclass
class NodeRecord:
    # ---- 标识 ----
    id: str = ""
    type: str = ""
    name: str = ""
    server: str = ""
    port: int = 0

    # ---- 协议字段 ----
    uuid: str = ""
    password: str = ""
    cipher: str = ""
    alterId: int = 0
    network: str = "tcp"
    tls: bool = False
    sni: str = ""
    host: str = ""
    path: str = ""
    flow: str = ""
    insecure: bool = False
    skip_cert_verify: bool = False
    raw: str = ""

    # ---- 用户状态 ----
    enabled: bool = True

    # ---- 检测指标（节点本身） ----
    latency_ms: Optional[int] = None
    latency_avg_ms: Optional[int] = None
    latency_max_ms: Optional[int] = None
    jitter_ms: Optional[int] = None
    success_rate: float = 0.0
    speed_cps: Optional[float] = None

    # ---- 多目标测试结果 ----
    # {
    #   "latency": {"CF": {"alive": true, "latency_ms": 45}, ...},
    #   "speed":   {"CF": {"ok": true, "speed_mbps": 12.5}, ...},
    # }
    targets: Dict[str, Any] = field(default_factory=dict)

    # ---- 元数据 ----
    source: str = ""
    created_at: str = ""
    updated_at: str = ""
    last_checked: str = ""

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "NodeRecord":
        valid = set(cls.__dataclass_fields__)
        data = {k: v for k, v in d.items() if k in valid}
        rec = cls(**data)
        rec.ensure_id()
        if not rec.created_at:
            rec.created_at = now_str()
        return rec

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def ensure_id(self) -> None:
        if not self.id:
            self.id = make_node_id(
                self.type,
                self.server,
                int(self.port or 0),
                self.uuid or self.password or self.name or "",
            )

    def touch(self) -> None:
        self.updated_at = now_str()

    def mark_checked(self) -> None:
        self.last_checked = now_str()
