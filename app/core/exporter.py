"""将存活节点导出为 mihomo / sing-box / base64 订阅。"""

import base64
import json
import logging
from pathlib import Path
from typing import Any, Dict, List

import yaml

logger = logging.getLogger("vael-mux.exporter")


async def export_all(nodes: List[Dict[str, Any]], output_cfg: Dict[str, Any]) -> List[str]:
    out_dir = Path(output_cfg.get("directory", "./output"))
    out_dir.mkdir(parents=True, exist_ok=True)

    formats = [f.lower() for f in output_cfg.get("formats", [])]
    written: List[str] = []

    if "mihomo" in formats or "clash" in formats:
        path = out_dir / "mihomo.yaml"
        path.write_text(export_mihomo(nodes), encoding="utf-8")
        written.append(str(path))

    if "singbox" in formats or "sing-box" in formats:
        path = out_dir / "singbox.json"
        path.write_text(export_singbox(nodes), encoding="utf-8")
        written.append(str(path))

    if "base64" in formats or "v2ray" in formats:
        path = out_dir / "base64.txt"
        path.write_text(export_base64(nodes), encoding="utf-8")
        written.append(str(path))

    return written


# ---------------------------------------------------------------- Mihomo
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


def _to_mihomo(n: Dict[str, Any]) -> Dict[str, Any] | None:
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
                "skip-cert-verify": bool(n.get("skip_cert_verify", False)),
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
                "skip-cert-verify": bool(n.get("insecure", False)),
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


# ---------------------------------------------------------------- sing-box
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


def _to_singbox(n: Dict[str, Any]) -> Dict[str, Any] | None:
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


# ---------------------------------------------------------------- Base64
def export_base64(nodes: List[Dict[str, Any]]) -> str:
    uris = [n["raw"] for n in nodes if n.get("raw")]
    joined = "\n".join(uris)
    return base64.b64encode(joined.encode("utf-8")).decode("ascii")
