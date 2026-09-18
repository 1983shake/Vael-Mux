"""配置加载与解析。"""

import os
from pathlib import Path
from typing import Any, Dict, List

import yaml

DEFAULT_CONFIG_PATH = os.environ.get("VAEL_CONFIG_PATH", "config/config.yaml")

_VALID_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


# ============================================================ 内部工具
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


def _to_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


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


# ============================================================ YAML 序列化
def _str_representer(dumper, data):
    """多行字符串（如 subscriptions）用块标量 '|' 输出，更易读。"""
    if "\n" in data and data.strip():
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


yaml.SafeDumper.add_representer(str, _str_representer)


# ============================================================ targets 解析
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


# ============================================================ 加载
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


# ============================================================ 保存
def _normalize_targets(raw: Any, with_size: bool) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    if not isinstance(raw, list):
        return result
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        name = str(item.get("name") or "").strip() or f"T{i + 1}"
        entry: Dict[str, Any] = {
            "name": name,
            "url": url,
            "enabled": bool(item.get("enabled", True)),
        }
        if with_size and item.get("size_hint") is not None:
            entry["size_hint"] = _to_int(item.get("size_hint"), 0)
        result.append(entry)
    return result


def _normalize_for_dump(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """整理写回时的配置结构：去内部字段、补默认值、稳定顺序。"""
    out: Dict[str, Any] = {}

    # ---- server ----
    server = cfg.get("server") or {}
    out["server"] = {
        "host": str(server.get("host") or "0.0.0.0"),
        "web_port": _to_int(server.get("web_port"), 8100),
        "api_port": _to_int(server.get("api_port"), 8110),
    }

    # ---- logging ----
    logging_cfg = cfg.get("logging") or {}
    level = str(logging_cfg.get("level") or "INFO").upper()
    if level not in _VALID_LEVELS:
        level = "INFO"
    out["logging"] = {"level": level}

    # ---- subscriptions（块标量）----
    subs = cfg.get("subscriptions")
    if isinstance(subs, list):
        text = "\n".join(str(x) for x in subs)
    else:
        text = str(subs or "")
    out["subscriptions"] = text.rstrip("\n") + "\n"

    # ---- check ----
    check = cfg.get("check") or {}
    out["check"] = {
        "concurrent": _to_int(check.get("concurrent"), 50),
        "timeout_ms": _to_int(check.get("timeout_ms"), 5000),
        "samples": _to_int(check.get("samples"), 3),
        "include_history": bool(check.get("include_history", False)),
        "max_valid_nodes": _to_int(check.get("max_valid_nodes"), 0),
        "latency_targets": _normalize_targets(check.get("latency_targets"), with_size=False),
        "speed_targets": _normalize_targets(check.get("speed_targets"), with_size=True),
        "schedule": str(check.get("schedule") or "").strip(),
    }

    # ---- output ----
    output = cfg.get("output") or {}
    formats = output.get("formats") or []
    if not isinstance(formats, list) or not formats:
        formats = ["mihomo", "singbox", "base64"]
    out["output"] = {
        "max_nodes": _to_int(output.get("max_nodes"), 0),
        "formats": [str(f) for f in formats],
        "directory": str(output.get("directory") or "./output"),
    }

    # ---- notify ----
    notify = cfg.get("notify") or {}
    out["notify"] = {"webhook": str(notify.get("webhook") or "")}

    return out


_HEADER = (
    "# ============================================================\n"
    "# Vael-Mux 配置文件（由 Web 控制台保存）\n"
    "# ============================================================\n\n"
)


def save_config(data: Dict[str, Any], path: str | None = None) -> str:
    """将结构化配置写回 YAML 文件（原子写入），返回写入路径。"""
    cfg_path = _find_config(path)
    clean = _normalize_for_dump(data)
    text = yaml.safe_dump(
        clean,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=4096,
    )
    tmp = cfg_path.with_suffix(cfg_path.suffix + ".tmp")
    tmp.write_text(_HEADER + text, encoding="utf-8")
    tmp.replace(cfg_path)
    return str(cfg_path)
