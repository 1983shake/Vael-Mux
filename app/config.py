"""配置加载与解析。"""

import os
from pathlib import Path
from typing import Any, Dict, List

import yaml

DEFAULT_CONFIG_PATH = os.environ.get("VAEL_CONFIG_PATH", "config/config.yaml")


def _find_config(path: str | None = None) -> Path:
    candidates = []
    if path:
        candidates.append(Path(path))
    env_path = os.environ.get("VAEL_CONFIG_PATH")
    if env_path:
        candidates.append(Path(env_path))
    candidates.append(Path("config/config.yaml"))
    candidates.append(Path("/app/config/config.yaml"))

    for c in candidates:
        if c.exists():
            return c
    raise FileNotFoundError(f"未找到配置文件，尝试过: {[str(c) for c in candidates]}")


def load_base_config(path: str | None = None) -> Dict[str, Any]:
    """加载完整配置，每次调用都会重新读取（支持热重载）。"""
    cfg_path = _find_config(path)
    with open(cfg_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    data.setdefault("server", {})
    data["server"].setdefault("host", "0.0.0.0")
    data["server"].setdefault("web_port", 8100)
    data["server"].setdefault("api_port", 8110)

    data.setdefault("check", {})
    data["check"].setdefault("concurrent", 50)
    data["check"].setdefault("timeout_ms", 5000)
    data["check"].setdefault("schedule", "")

    data.setdefault("output", {})
    data["output"].setdefault("formats", ["mihomo", "singbox", "base64"])
    data["output"].setdefault("directory", "./output")

    data.setdefault("notify", {})
    data["notify"].setdefault("webhook", "")

    data["_config_path"] = str(cfg_path)
    return data


def parse_subscriptions(raw: Any) -> List[str]:
    """解析订阅源列表。

    支持两种格式：
      1. YAML 块标量字符串（推荐）：每行一个 URL，支持 # 注释、空行
      2. YAML 列表
    """
    if raw is None:
        return []

    if isinstance(raw, list):
        lines = [str(x) for x in raw]
    else:
        lines = str(raw).splitlines()

    result: List[str] = []
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        result.append(s)
    return result
