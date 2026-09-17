from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Literal, Optional

import yaml
from pydantic import BaseModel, Field


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000


class ProxyConfig(BaseModel):
    enabled: bool = True
    mixed_port: int = 7890
    allow_lan: bool = True
    mode: Literal["rule", "global", "direct"] = "rule"


class KernelConfig(BaseModel):
    type: Literal["mihomo"] = "mihomo"
    binary: str = "/app/bin/mihomo"
    external_controller: str = "127.0.0.1:9090"
    secret: str = ""
    config_dir: str = "./data/mihomo"


class SubscriptionSource(BaseModel):
    name: str
    url: Optional[str] = None
    enabled: bool = True
    user_agent: str = "clash.meta"
    inline_nodes: List[str] = Field(default_factory=list)


class AliveTestConfig(BaseModel):
    enabled: bool = True
    urls: List[str] = Field(
        default_factory=lambda: [
            "http://www.gstatic.com/generate_204",
            "http://cp.cloudflare.com/generate_204",
        ]
    )
    interval_seconds: int = 300
    timeout: float = 5.0
    concurrency: int = 64
    attempts: int = 2


class SpeedTestConfig(BaseModel):
    enabled: bool = True
    urls: List[str] = Field(default_factory=lambda: ["https://speed.cloudflare.com/__down?bytes=10000000"])
    timeout: float = 20.0
    concurrency: int = 4


class ScoringConfig(BaseModel):
    weights: Dict[str, float] = Field(
        default_factory=lambda: {
            "latency": 0.5,
            "download_speed": 0.3,
            "stability": 0.2,
        }
    )
    sort_mode: Literal["weighted_score", "latency", "speed", "round_robin"] = "weighted_score"


class RoutingRule(BaseModel):
    domains: List[str] = Field(default_factory=list)
    ips: List[str] = Field(default_factory=list)
    group: str


class RoutingGroup(BaseModel):
    strategy: Literal["latency", "speed", "weighted_score", "round_robin"] = "weighted_score"
    top_n: int = 3


class RoutingConfig(BaseModel):
    default_outbound: str = "auto"
    rules: List[RoutingRule] = Field(default_factory=list)
    groups: Dict[str, RoutingGroup] = Field(default_factory=dict)


class LoggingConfig(BaseModel):
    level: str = "INFO"
    buffer_size: int = 800


class AppConfig(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    proxy: ProxyConfig = Field(default_factory=ProxyConfig)
    kernel: KernelConfig = Field(default_factory=KernelConfig)
    subscriptions: List[SubscriptionSource] = Field(default_factory=list)
    alive_test: AliveTestConfig = Field(default_factory=AliveTestConfig)
    speed_test: SpeedTestConfig = Field(default_factory=SpeedTestConfig)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    @classmethod
    def load(cls, path: str | Path) -> "AppConfig":
        path = Path(path)
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return cls.model_validate(data)

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            yaml.safe_dump(self.model_dump(), f, allow_unicode=True, sort_keys=False)
