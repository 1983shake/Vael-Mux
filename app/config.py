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


def _to_bool(v: Any, default: bool = False) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "1", "yes", "y", "on"):
            return True
        if s in ("false", "0", "no", "n", "off", ""):
            return False
    return default


def _parse_targets(raw: Any, prefix: str = "T") -> List[Dict[str, Any]]:
    """解析 target 列表为 [{name, url, enabled, size_hint?}, ...]。"""
    if not raw:
        return []

    result: List[Dict[str, Any]] = []

    def _add(item: Any, idx: int) -> None:
        if isinstance(item, str):
            s = item.strip()
            if not s or s.startswith("#"):
                return
            result.append({"name": f"{prefix}{idx + 1}", "url": s, "enabled": True})
            return
        if isinstance(item, dict):
            url = str(item.get("url") or item.get("address") or "").strip()
            if not url:
                return
            name = str(item.get("name") or item.get("short") or "").strip()
            if not name:
                name = f"{prefix}{idx + 1}"
            entry: Dict[str, Any] = {
                "name": name,
                "url": url,
                "enabled": _to_bool(item.get("enabled", True), default=True),
            }
            if "size_hint" in item:
                try:
                    entry["size_hint"] = int(item["size_hint"])
                except (TypeError, ValueError):
                    pass
            result.append(entry)

    if isinstance(raw, str):
        n = 0
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            _add(line, n)
            n += 1
    elif isinstance(raw, list):
        for i, item in enumerate(raw):
            _add(item, i)

    return result


def enabled_targets(targets: Any) -> List[Dict[str, Any]]:
    """从 targets 中过滤出 enabled 的目标（缺省视为启用）。"""
    if not targets:
        return []
    return [t for t in targets if isinstance(t, dict) and t.get("enabled", True)]


def load_base_config(path: str | None = None) -> Dict[str, Any]:
    """加载完整配置，每次调用都会重新读取（支持热重载）。"""
    cfg_path = _find_config(path)
    with open(cfg_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    data.setdefault("server", {})
    data["server"].setdefault("host", "0.0.0.0")
    data["server"].setdefault("web_port", 8100)
    data["server"].setdefault("api_port", 8110)

    data.setdefault("logging", {})
    data["logging"].setdefault("level", "INFO")

    data.setdefault("check", {})
    data["check"].setdefault("concurrent", 50)
    data["check"].setdefault("timeout_ms", 5000)
    data["check"].setdefault("samples", 3)
    data["check"].setdefault("include_history", False)
    data["check"].setdefault("max_valid_nodes", 0)
    data["check"].setdefault("schedule", "")
    data["check"]["latency_targets"] = _parse_targets(data["check"].get("latency_targets"), "L")
    data["check"]["speed_targets"] = _parse_targets(data["check"].get("speed_targets"), "S")

    data.setdefault("output", {})
    data["output"].setdefault("max_nodes", 0)
    data["output"].setdefault("formats", ["mihomo", "singbox", "base64"])
    data["output"].setdefault("directory", "./output")

    data.setdefault("notify", {})
    data["notify"].setdefault("webhook", "")

    data["_config_path"] = str(cfg_path)
    return data


def get_include_history(config: Dict[str, Any] | None = None) -> bool:
    """读取 check.include_history，做类型兼容。"""
    cfg = config or load_base_config()
    raw = (cfg.get("check") or {}).get("include_history", False)
    return _to_bool(raw, default=False)


def parse_subscriptions(raw: Any) -> List[str]:
    """解析订阅源列表，支持块标量（推荐）和列表两种格式。"""
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
