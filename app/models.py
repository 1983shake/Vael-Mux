"""配置加载 / 保存 + 自修复，以及节点数据模型。

本模块由原 app/config.py 与 app/models.py 合并而来。

配置自修复
    每次加载时，对 YAML 内容做规范化与修复：
      - 段落类型错误（如 check 不是 dict）→ 重置
      - 字段缺失 → 补默认值
      - 类型错误（如 concurrent 是字符串 "abc"）→ 回退默认值
      - 越界值（如 concurrent=0）→ 裁剪到合法范围
      - 枚举值非法（如 latency_mode="foo"）→ 回退 all
      - cron 表达式非法 → 清空
      - 旧字段 server.web_port / server.api_port → 移除（端口由 docker-compose 控制）
      - targets 缺 url / 格式错误 → 跳过并记录
    只要有实际修复，就会把修复后的配置原子写回磁盘，并打 warning 日志。

首次启动
    若 config.yaml 不存在，会用内置默认配置创建（含默认延迟/速度目标、
    默认 cron），之后由自修复流程持续保持规范。
"""

import hashlib
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

_VALID_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
_VALID_MODES = frozenset({"all", "any"})

_repair_logger = logging.getLogger("vael-mux.config")


# ============================================================ 通用工具
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


def normalize_mode(v: Any, default: str = "all") -> str:
    """将任意输入归一化为 'all' 或 'any'。"""
    s = str(v or default).strip().lower()
    return s if s in _VALID_MODES else default


# ============================================================ YAML 序列化
def _str_representer(dumper, data):
    """多行字符串（如 subscriptions）用块标量 '|' 输出，更易读。"""
    if "\n" in data and data.strip():
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


yaml.SafeDumper.add_representer(str, _str_representer)


# ============================================================ 路径解析
def _resolve_config_path(
    path: Optional[str] = None,
    *,
    must_exist: bool = False,
) -> Path:
    """确定配置文件路径。

    - 传入 path：直接使用（must_exist=True 时校验存在性）
    - 否则按优先级：VAEL_CONFIG_PATH > config/config.yaml > /app/config/config.yaml
    - must_exist=True 时全部不存在则抛 FileNotFoundError
    - 不存在且 must_exist=False 时，返回默认写入位置（VAEL_CONFIG_PATH 或 config/config.yaml）
    """
    if path:
        p = Path(path)
        if must_exist and not p.exists():
            raise FileNotFoundError(f"配置文件不存在: {p}")
        return p

    env_path = os.environ.get("VAEL_CONFIG_PATH")
    candidates: List[Path] = []
    if env_path:
        candidates.append(Path(env_path))
    candidates.append(Path("config/config.yaml"))
    candidates.append(Path("/app/config/config.yaml"))

    for c in candidates:
        if c.exists():
            return c

    if must_exist:
        raise FileNotFoundError(f"未找到配置文件，尝试过: {[str(c) for c in candidates]}")

    if env_path:
        return Path(env_path)
    return Path("config/config.yaml")


# ============================================================ 默认配置
def _default_config_dict() -> Dict[str, Any]:
    """内置默认配置（首次启动时用于创建 config.yaml）。"""
    return {
        "server": {"host": "0.0.0.0"},
        "logging": {"level": "INFO"},
        "subscriptions": "",
        "check": {
            "concurrent": 50,
            "timeout_ms": 5000,
            "samples": 3,
            "include_history": False,
            "max_valid_nodes": 50,
            "latency_mode": "all",
            "speed_mode": "all",
            "latency_targets": [
                {
                    "name": "CF",
                    "url": "https://www.cloudflare.com/cdn-cgi/trace",
                    "enabled": True,
                },
                {
                    "name": "GG",
                    "url": "https://www.google.com/generate_204",
                    "enabled": True,
                },
                {
                    "name": "GH",
                    "url": "https://github.com/robots.txt",
                    "enabled": True,
                },
            ],
            "speed_targets": [
                {
                    "name": "CF",
                    "url": "https://speed.cloudflare.com/__down?bytes=2000000",
                    "size_hint": 2000000,
                    "enabled": True,
                },
                {
                    "name": "GH",
                    "url": "https://raw.githubusercontent.com/git/git/master/README.md",
                    "size_hint": 500000,
                    "enabled": True,
                },
            ],
            "schedule": "0 9,21 * * *",
        },
        "output": {
            "max_nodes": 0,
            "formats": ["mihomo", "singbox", "base64", "v2ray", "v2ray-json"],
            "directory": "./output",
        },
        "notify": {"webhook": ""},
    }


_HEADER = (
    "# ============================================================\n"
    "# Vael-Mux 配置文件\n"
    "# 本文件由程序自动生成 / 维护；如被删除，重启后将自动重建\n"
    "# 端口说明：内部固定 8100(Web) / 8110(API)，对外端口由 docker-compose.yml 控制\n"
    "# ============================================================\n\n"
)


# ============================================================ YAML 写盘
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

    server = cfg.get("server") or {}
    out["server"] = {
        "host": str(server.get("host") or "0.0.0.0"),
    }

    logging_cfg = cfg.get("logging") or {}
    level = str(logging_cfg.get("level") or "INFO").upper()
    if level not in _VALID_LEVELS:
        level = "INFO"
    out["logging"] = {"level": level}

    subs = cfg.get("subscriptions")
    if isinstance(subs, list):
        text = "\n".join(str(x) for x in subs)
    else:
        text = str(subs or "")
    out["subscriptions"] = text.rstrip("\n") + "\n"

    check = cfg.get("check") or {}
    out["check"] = {
        "concurrent": _to_int(check.get("concurrent"), 50),
        "timeout_ms": _to_int(check.get("timeout_ms"), 5000),
        "samples": _to_int(check.get("samples"), 3),
        "include_history": bool(check.get("include_history", False)),
        "max_valid_nodes": _to_int(check.get("max_valid_nodes"), 0),
        "latency_mode": normalize_mode(check.get("latency_mode"), "all"),
        "speed_mode": normalize_mode(check.get("speed_mode"), "all"),
        "latency_targets": _normalize_targets(check.get("latency_targets"), with_size=False),
        "speed_targets": _normalize_targets(check.get("speed_targets"), with_size=True),
        "schedule": str(check.get("schedule") or "").strip(),
    }

    output = cfg.get("output") or {}
    formats = output.get("formats") or []
    if not isinstance(formats, list) or not formats:
        formats = ["mihomo", "singbox", "base64"]
    out["output"] = {
        "max_nodes": _to_int(output.get("max_nodes"), 0),
        "formats": [str(f) for f in formats],
        "directory": str(output.get("directory") or "./output"),
    }

    notify = cfg.get("notify") or {}
    out["notify"] = {"webhook": str(notify.get("webhook") or "")}

    return out


def _write_yaml_file(cfg_path: Path, data: Dict[str, Any]) -> None:
    """原子写入配置文件。"""
    clean = _normalize_for_dump(data)
    text = yaml.safe_dump(
        clean,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=4096,
    )
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cfg_path.with_suffix(cfg_path.suffix + ".tmp")
    tmp.write_text(_HEADER + text, encoding="utf-8")
    tmp.replace(cfg_path)


# ============================================================ 自修复
def _repair_int(
    section: Dict[str, Any],
    key: str,
    default: int,
    label: str,
    fixes: List[str],
    min_v: Optional[int] = None,
    max_v: Optional[int] = None,
) -> None:
    raw = section.get(key)
    if raw is None:
        section[key] = default
        return
    try:
        v = int(raw)
    except (TypeError, ValueError):
        section[key] = default
        fixes.append(f"{label}={raw!r} 非整数，已设为 {default}")
        return
    if min_v is not None and v < min_v:
        section[key] = min_v
        fixes.append(f"{label}={v} 小于下限 {min_v}，已修正为 {min_v}")
        return
    if max_v is not None and v > max_v:
        section[key] = max_v
        fixes.append(f"{label}={v} 超过上限 {max_v}，已修正为 {max_v}")
        return
    section[key] = v


def _repair_bool(
    section: Dict[str, Any],
    key: str,
    default: bool,
    label: str,
    fixes: List[str],
) -> None:
    raw = section.get(key)
    if raw is None:
        section[key] = default
        return
    if isinstance(raw, bool):
        return
    if isinstance(raw, (int, float)):
        section[key] = bool(raw)
        return
    s = str(raw).strip().lower()
    if s in ("true", "1", "yes", "y", "on"):
        section[key] = True
    elif s in ("false", "0", "no", "n", "off", ""):
        section[key] = False
    else:
        section[key] = default
        fixes.append(f"{label}={raw!r} 无法解析为布尔值，已设为 {default}")


def _repair_targets(raw: Any, label: str, fixes: List[str]) -> List[Dict[str, Any]]:
    """修复 targets 列表。"""
    if raw is None:
        return []
    if not isinstance(raw, list):
        fixes.append(f"{label} 格式错误，已重置为空列表")
        return []

    out: List[Dict[str, Any]] = []
    for i, item in enumerate(raw):
        if isinstance(item, str):
            s = item.strip()
            if not s or s.startswith("#"):
                continue
            out.append({"name": f"T{i + 1}", "url": s, "enabled": True})
            continue
        if not isinstance(item, dict):
            fixes.append(f"{label} 第 {i + 1} 项格式错误，已跳过")
            continue
        url = str(item.get("url") or item.get("address") or "").strip()
        if not url:
            fixes.append(f"{label} 第 {i + 1} 项缺少 url，已跳过")
            continue
        name = str(item.get("name") or "").strip() or f"T{i + 1}"
        entry: Dict[str, Any] = {
            "name": name,
            "url": url,
            "enabled": _to_bool(item.get("enabled", True), True),
        }
        if "size_hint" in item:
            sh = _to_int(item.get("size_hint"), 0)
            if sh > 0:
                entry["size_hint"] = sh
        out.append(entry)
    return out


def _repair_config(data: Dict[str, Any]) -> List[str]:
    """就地修复配置中的缺失 / 无效字段，返回修复说明列表。"""
    fixes: List[str] = []

    # ---------- server ----------
    server = data.get("server")
    if not isinstance(server, dict):
        data["server"] = {}
        if server is not None:
            fixes.append("server 段格式错误，已重置")
        server = data["server"]

    for dead in ("web_port", "api_port"):
        if dead in server:
            server.pop(dead, None)
            fixes.append(f"server.{dead} 已废弃（端口由 docker-compose 控制），已移除")

    host_raw = server.get("host")
    if host_raw is None or not str(host_raw).strip():
        server["host"] = "0.0.0.0"
        if host_raw is not None:
            fixes.append("server.host 为空，已设为 '0.0.0.0'")
    else:
        server["host"] = str(host_raw).strip()

    # ---------- logging ----------
    log_cfg = data.get("logging")
    if not isinstance(log_cfg, dict):
        data["logging"] = {}
        if log_cfg is not None:
            fixes.append("logging 段格式错误，已重置")
        log_cfg = data["logging"]

    level_raw = log_cfg.get("level")
    level = str(level_raw or "").strip().upper()
    if level not in _VALID_LEVELS:
        log_cfg["level"] = "INFO"
        if level_raw not in (None, "", "INFO"):
            fixes.append(f"logging.level={level_raw!r} 无效，已设为 'INFO'")
    else:
        log_cfg["level"] = level

    # ---------- subscriptions ----------
    subs = data.get("subscriptions")
    if subs is None:
        data["subscriptions"] = ""
    elif isinstance(subs, list):
        data["subscriptions"] = "\n".join(str(x) for x in subs) + "\n"
    else:
        data["subscriptions"] = str(subs)

    # ---------- check ----------
    check = data.get("check")
    if not isinstance(check, dict):
        data["check"] = {}
        if check is not None:
            fixes.append("check 段格式错误，已重置")
        check = data["check"]

    _repair_int(check, "concurrent", 50, "check.concurrent", fixes, min_v=1)
    _repair_int(check, "timeout_ms", 5000, "check.timeout_ms", fixes, min_v=100)
    _repair_int(check, "samples", 3, "check.samples", fixes, min_v=1)
    _repair_int(check, "max_valid_nodes", 50, "check.max_valid_nodes", fixes, min_v=0)
    _repair_bool(check, "include_history", False, "check.include_history", fixes)

    for key, label in (
        ("latency_mode", "check.latency_mode"),
        ("speed_mode", "check.speed_mode"),
    ):
        raw = check.get(key)
        s = str(raw or "").strip().lower()
        if s not in _VALID_MODES:
            check[key] = "all"
            if raw not in (None, "", "all"):
                fixes.append(f"{label}={raw!r} 无效，已设为 'all'")
        else:
            check[key] = s

    check["latency_targets"] = _repair_targets(check.get("latency_targets"), "check.latency_targets", fixes)
    check["speed_targets"] = _repair_targets(check.get("speed_targets"), "check.speed_targets", fixes)

    sched_raw = check.get("schedule")
    if sched_raw is None:
        check["schedule"] = ""
    else:
        s = str(sched_raw).strip()
        if not s:
            check["schedule"] = ""
        else:
            try:
                from apscheduler.triggers.cron import CronTrigger

                CronTrigger.from_crontab(s)
                check["schedule"] = s
            except Exception:
                check["schedule"] = ""
                fixes.append(f"check.schedule={s!r} 非法 cron 表达式，已清空")

    # ---------- output ----------
    output = data.get("output")
    if not isinstance(output, dict):
        data["output"] = {}
        if output is not None:
            fixes.append("output 段格式错误，已重置")
        output = data["output"]

    fmts_raw = output.get("formats")
    if not isinstance(fmts_raw, list) or not fmts_raw:
        output["formats"] = ["mihomo", "singbox", "base64", "v2ray", "v2ray-json"]
    else:
        cleaned = [str(f).strip() for f in fmts_raw if str(f).strip()]
        output["formats"] = cleaned or ["mihomo", "singbox", "base64"]

    _repair_int(output, "max_nodes", 0, "output.max_nodes", fixes, min_v=0)

    dir_raw = output.get("directory")
    if dir_raw is None or not str(dir_raw).strip():
        output["directory"] = "./output"
        if dir_raw is not None:
            fixes.append("output.directory 为空，已设为 './output'")
    else:
        output["directory"] = str(dir_raw).strip()

    # ---------- notify ----------
    notify = data.get("notify")
    if not isinstance(notify, dict):
        data["notify"] = {}
        if notify is not None:
            fixes.append("notify 段格式错误，已重置")
        notify = data["notify"]

    wh = notify.get("webhook")
    notify["webhook"] = "" if wh is None else str(wh).strip()

    return fixes


# ============================================================ 对外接口
def ensure_config_file(path: Optional[str] = None) -> Path:
    """确保配置文件存在；不存在则用内置默认配置创建。返回最终路径。"""
    cfg_path = _resolve_config_path(path, must_exist=False)
    if cfg_path.exists():
        return cfg_path

    _write_yaml_file(cfg_path, _default_config_dict())
    _repair_logger.info(f"配置文件不存在，已创建默认配置：{cfg_path}")
    return cfg_path


def load_base_config(path: Optional[str] = None) -> Dict[str, Any]:
    """加载完整配置，每次调用都会重新读取，并执行自修复。"""
    cfg_path = ensure_config_file(path)

    with open(cfg_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raw = {}

    fixes = _repair_config(raw)

    if fixes:
        try:
            _write_yaml_file(cfg_path, raw)
            for msg in fixes:
                _repair_logger.warning(f"配置自修复：{msg}")
            _repair_logger.warning(f"已写回修复后的配置：{cfg_path}")
        except Exception as e:
            _repair_logger.error(f"配置自修复写回失败：{e}")

    raw["_config_path"] = str(cfg_path)
    return raw


def save_config(data: Dict[str, Any], path: Optional[str] = None) -> str:
    """将结构化配置写回 YAML 文件（原子写入），返回写入路径。"""
    cfg_path = _resolve_config_path(path, must_exist=False)
    _write_yaml_file(cfg_path, data)
    return str(cfg_path)


def get_include_history(config: Optional[Dict[str, Any]] = None) -> bool:
    cfg = config or load_base_config()
    raw = (cfg.get("check") or {}).get("include_history", False)
    return _to_bool(raw, default=False)


def parse_subscriptions(raw: Any) -> List[str]:
    """解析订阅源列表，支持块标量和列表两种格式。"""
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


def enabled_targets(targets: Any) -> List[Dict[str, Any]]:
    """从 targets 中过滤出 enabled 的目标（缺省视为启用）。"""
    if not targets:
        return []
    return [t for t in targets if isinstance(t, dict) and t.get("enabled", True)]


# ============================================================ 节点数据模型
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
