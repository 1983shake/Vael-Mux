"""将存活节点导出为 mihomo / sing-box / base64 / v2ray 订阅。

输出格式：
  - mihomo      : Clash.Meta / Mihomo YAML
  - singbox     : sing-box JSON
  - base64      : 通用 Base64 URI 列表（vmess/vless/trojan/ss/... 原始分享链接）
  - v2ray       : v2rayN / v2rayNG / v2rayA 订阅（内容与 base64 相同，
                  但单独文件便于客户端识别与路由分发）
  - v2ray-json  : v2ray 完整 config.json（含 inbounds / outbounds / routing）
"""

import base64
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger("vael-mux.exporter")


async def export_all(nodes: List[Dict[str, Any]], output_cfg: Dict[str, Any]) -> List[str]:
    out_dir = Path(output_cfg.get("directory", "./output"))
    out_dir.mkdir(parents=True, exist_ok=True)

    formats = {f.lower() for f in output_cfg.get("formats", [])}
    written: List[str] = []

    if "mihomo" in formats or "clash" in formats:
        path = out_dir / "mihomo.yaml"
        path.write_text(export_mihomo(nodes), encoding="utf-8")
        written.append(str(path))

    if "singbox" in formats or "sing-box" in formats:
        path = out_dir / "singbox.json"
        path.write_text(export_singbox(nodes), encoding="utf-8")
        written.append(str(path))

    if "base64" in formats:
        path = out_dir / "base64.txt"
        path.write_text(export_base64(nodes), encoding="utf-8")
        written.append(str(path))

    # v2ray 订阅（base64 URI 列表），单独文件便于 v2rayN/v2rayNG 订阅使用
    if "v2ray" in formats:
        path = out_dir / "v2ray.txt"
        path.write_text(export_v2ray_sub(nodes), encoding="utf-8")
        written.append(str(path))

    # v2ray 完整 config.json
    if "v2ray-json" in formats or "v2ray_json" in formats:
        path = out_dir / "v2ray.json"
        path.write_text(export_v2ray_json(nodes), encoding="utf-8")
        written.append(str(path))

    return written


# ============================================================ Mihomo
def export_mihomo(nodes: List[Dict[str, Any]]) -> str:
    proxies: List[Dict[str, Any]] = []
    names: List[str] = []
    for n in nodes:
        p = _to_mihomo(n)
        if p is None:
            continue
        proxies.append(p)
        names.append(p["name"])

    doc = {
        "mixed-port": 7890,
        "allow-lan": False,
        "mode": "rule",
        "log-level": "info",
        "proxies": proxies,
        "proxy-groups": [
            {
                "name": "Vael-Mux",
                "type": "select",
                "proxies": (names + ["DIRECT"]) if names else ["DIRECT"],
            },
        ],
        "rules": ["MATCH,Vael-Mux"],
    }
    return yaml.safe_dump(doc, allow_unicode=True, sort_keys=False)


def _to_mihomo(n: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    t = n.get("type")
    name = str(n.get("name") or f"{t}-{n.get('server')}")
    base: Dict[str, Any] = {
        "name": name,
        "server": n.get("server"),
        "port": int(n.get("port", 0) or 0),
    }
    if not base["server"] or not base["port"]:
        return None

    if t == "vmess":
        base.update(
            {
                "type": "vmess",
                "uuid": n.get("uuid", ""),
                "alterId": int(n.get("alterId", 0) or 0),
                "cipher": n.get("cipher") or "auto",
                "network": n.get("network") or "tcp",
                "tls": bool(n.get("tls")),
            }
        )
        _apply_ws_opts(base, n)
        return base

    if t == "vless":
        base.update(
            {
                "type": "vless",
                "uuid": n.get("uuid", ""),
                "network": n.get("network") or "tcp",
                "tls": bool(n.get("tls")),
            }
        )
        if n.get("flow"):
            base["flow"] = n["flow"]
        if n.get("sni"):
            base["servername"] = n["sni"]
        _apply_ws_opts(base, n)
        return base

    if t == "trojan":
        base.update(
            {
                "type": "trojan",
                "password": n.get("password", ""),
                "skip-cert-verify": bool(n.get("skip_cert_verify") or n.get("insecure", False)),
            }
        )
        if n.get("sni"):
            base["sni"] = n["sni"]
        return base

    if t == "ss":
        base.update(
            {
                "type": "ss",
                "cipher": n.get("cipher", ""),
                "password": n.get("password", ""),
            }
        )
        return base

    if t == "hysteria2":
        base.update(
            {
                "type": "hysteria2",
                "password": n.get("password", ""),
                "skip-cert-verify": bool(n.get("skip_cert_verify") or n.get("insecure", False)),
            }
        )
        if n.get("sni"):
            base["sni"] = n["sni"]
        return base

    return None


def _apply_ws_opts(base: Dict[str, Any], n: Dict[str, Any]) -> None:
    if base.get("network") == "ws":
        opts: Dict[str, Any] = {}
        if n.get("path"):
            opts["path"] = n["path"]
        if n.get("host"):
            opts["headers"] = {"Host": n["host"]}
        if opts:
            base["ws-opts"] = opts


# ============================================================ sing-box
def export_singbox(nodes: List[Dict[str, Any]]) -> str:
    outbounds: List[Dict[str, Any]] = [
        {"type": "selector", "tag": "Vael-Mux", "outbounds": [n["name"] for n in nodes if n.get("name")] + ["direct"]},
        {"type": "direct", "tag": "direct"},
    ]

    for n in nodes:
        ob = _to_singbox(n)
        if ob is not None:
            outbounds.append(ob)

    doc = {
        "log": {"level": "info"},
        "outbounds": outbounds,
        "route": {
            "rules": [],
            "final": "Vael-Mux",
        },
    }
    return json.dumps(doc, ensure_ascii=False, indent=2)


def _to_singbox(n: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    t = n.get("type")
    tag = str(n.get("name") or f"{t}-{n.get('server')}")
    host = n.get("server")
    port = n.get("port")
    if not host or not port:
        return None

    base = {"tag": tag, "server": host, "server_port": int(port)}

    if t == "vmess":
        base.update(
            {
                "type": "vmess",
                "uuid": n.get("uuid", ""),
                "security": n.get("cipher") or "auto",
                "alter_id": int(n.get("alterId", 0) or 0),
            }
        )
        if n.get("tls"):
            base["tls"] = {"enabled": True, "server_name": n.get("sni") or n.get("host") or ""}
        _apply_singbox_transport(base, n)
        return base

    if t == "vless":
        base.update(
            {
                "type": "vless",
                "uuid": n.get("uuid", ""),
            }
        )
        if n.get("flow"):
            base["flow"] = n["flow"]
        if n.get("tls"):
            base["tls"] = {"enabled": True, "server_name": n.get("sni", "")}
        _apply_singbox_transport(base, n)
        return base

    if t == "trojan":
        base.update(
            {
                "type": "trojan",
                "password": n.get("password", ""),
            }
        )
        base["tls"] = {"enabled": True, "server_name": n.get("sni", "")}
        return base

    if t == "ss":
        base.update(
            {
                "type": "shadowsocks",
                "method": n.get("cipher", ""),
                "password": n.get("password", ""),
            }
        )
        return base

    if t == "hysteria2":
        base = {
            "type": "hysteria2",
            "tag": tag,
            "server": host,
            "server_port": int(port),
            "password": n.get("password", ""),
        }
        base["tls"] = {"enabled": True, "server_name": n.get("sni", "")}
        return base

    return None


def _apply_singbox_transport(base: Dict[str, Any], n: Dict[str, Any]) -> None:
    net = n.get("network")
    if net == "ws":
        base["transport"] = {
            "type": "ws",
            "path": n.get("path") or "/",
            "headers": {"Host": n.get("host")} if n.get("host") else {},
        }


# ============================================================ Base64
def export_base64(nodes: List[Dict[str, Any]]) -> str:
    uris = _collect_uris(nodes)
    joined = "\n".join(uris)
    return base64.b64encode(joined.encode("utf-8")).decode("ascii")


# ============================================================ v2ray 订阅
def export_v2ray_sub(nodes: List[Dict[str, Any]]) -> str:
    """v2rayN / v2rayNG / v2rayA 订阅格式。

    内容与通用 base64 一致（base64 编码的原始 URI 列表），但使用独立文件，
    便于用户按客户端拆分不同的订阅端点，避免与其他格式互相影响。
    """
    uris = _collect_uris(nodes)
    joined = "\n".join(uris)
    return base64.b64encode(joined.encode("utf-8")).decode("ascii")


def _collect_uris(nodes: List[Dict[str, Any]]) -> List[str]:
    return [n["raw"] for n in nodes if n.get("raw")]


# ============================================================ v2ray config.json
def export_v2ray_json(nodes: List[Dict[str, Any]]) -> str:
    """v2ray 完整配置 (config.json)。

    生成的 outbounds 中第一个节点作为默认出口；其余节点通过 routing 规则
    保持可路由状态，也可由客户端在 UI 上切换。
    """
    proxy_outbounds: List[Dict[str, Any]] = []
    for n in nodes:
        ob = _to_v2ray_outbound(n)
        if ob is not None:
            proxy_outbounds.append(ob)

    outbounds: List[Dict[str, Any]] = []
    if proxy_outbounds:
        outbounds.extend(proxy_outbounds)
    # 内置 direct / block
    outbounds.append({"protocol": "freedom", "tag": "direct"})
    outbounds.append({"protocol": "blackhole", "tag": "block"})

    default_tag = proxy_outbounds[0]["tag"] if proxy_outbounds else "direct"

    doc = {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {
                "port": 1080,
                "listen": "127.0.0.1",
                "protocol": "socks",
                "settings": {"udp": True, "auth": "noauth"},
                "sniffing": {
                    "enabled": True,
                    "destOverride": ["http", "tls"],
                },
            },
            {
                "port": 1081,
                "listen": "127.0.0.1",
                "protocol": "http",
                "sniffing": {
                    "enabled": True,
                    "destOverride": ["http", "tls"],
                },
            },
        ],
        "outbounds": outbounds,
        "routing": {
            "domainStrategy": "IPIfNonMatch",
            "rules": [
                {
                    "type": "field",
                    "outboundTag": "direct",
                    "domain": ["geosite:cn"],
                },
                {
                    "type": "field",
                    "outboundTag": "direct",
                    "ip": ["geoip:cn", "geoip:private"],
                },
                {
                    "type": "field",
                    "outboundTag": "block",
                    "domain": ["geosite:category-ads-all"],
                },
            ],
        },
        # 元信息，v2ray 会忽略未知顶层字段；客户端可读取默认出口标签
        "_vael_mux": {
            "default_outbound": default_tag,
            "generated_by": "Vael-Mux",
            "node_count": len(proxy_outbounds),
        },
    }
    return json.dumps(doc, ensure_ascii=False, indent=2)


def _to_v2ray_outbound(n: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    t = n.get("type")
    tag = str(n.get("name") or f"{t}-{n.get('server')}")
    host = n.get("server")
    port = n.get("port")
    if not host or not port:
        return None
    try:
        port = int(port)
    except (TypeError, ValueError):
        return None

    if t == "vmess":
        user = {
            "id": n.get("uuid", ""),
            "alterId": int(n.get("alterId", 0) or 0),
            "security": n.get("cipher") or "auto",
        }
        return {
            "protocol": "vmess",
            "tag": tag,
            "settings": {
                "vnext": [
                    {
                        "address": host,
                        "port": port,
                        "users": [user],
                    }
                ],
            },
            "streamSettings": _v2ray_stream_settings(n),
        }

    if t == "vless":
        user: Dict[str, Any] = {
            "id": n.get("uuid", ""),
            "encryption": "none",
        }
        if n.get("flow"):
            user["flow"] = n["flow"]
        return {
            "protocol": "vless",
            "tag": tag,
            "settings": {
                "vnext": [
                    {
                        "address": host,
                        "port": port,
                        "users": [user],
                    }
                ],
            },
            "streamSettings": _v2ray_stream_settings(n),
        }

    if t == "trojan":
        return {
            "protocol": "trojan",
            "tag": tag,
            "settings": {
                "servers": [
                    {
                        "address": host,
                        "port": port,
                        "password": n.get("password", ""),
                    }
                ],
            },
            "streamSettings": _v2ray_stream_settings(n, force_tls=True),
        }

    if t == "ss":
        return {
            "protocol": "shadowsocks",
            "tag": tag,
            "settings": {
                "servers": [
                    {
                        "address": host,
                        "port": port,
                        "method": n.get("cipher", ""),
                        "password": n.get("password", ""),
                    }
                ],
            },
        }

    # hysteria2 不属于 v2ray 核心协议，跳过
    return None


def _v2ray_stream_settings(n: Dict[str, Any], force_tls: bool = False) -> Dict[str, Any]:
    net = n.get("network") or "tcp"
    security = "tls" if (n.get("tls") or force_tls) else "none"

    s: Dict[str, Any] = {
        "network": net,
        "security": security,
    }

    if security == "tls":
        tls_settings: Dict[str, Any] = {}
        if n.get("sni"):
            tls_settings["serverName"] = n["sni"]
        if n.get("skip_cert_verify"):
            tls_settings["allowInsecure"] = True
        s["tlsSettings"] = tls_settings

    if net == "ws":
        ws: Dict[str, Any] = {}
        if n.get("path"):
            ws["path"] = n["path"]
        if n.get("host"):
            ws["headers"] = {"Host": n["host"]}
        s["wsSettings"] = ws
    elif net == "grpc":
        grpc: Dict[str, Any] = {}
        if n.get("path"):
            grpc["serviceName"] = n["path"].lstrip("/")
        s["grpcSettings"] = grpc
    elif net == "http" or net == "h2":
        http_settings: Dict[str, Any] = {}
        if n.get("host"):
            http_settings["host"] = [n["host"]]
        if n.get("path"):
            http_settings["path"] = n["path"]
        s["httpSettings"] = http_settings

    return s
