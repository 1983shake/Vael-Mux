from __future__ import annotations

import base64
import json
from typing import Any, Dict, List

import yaml

from ..models import Node


def node_to_mihomo(node: Node) -> Dict[str, Any]:
    p = node.params
    proto = node.protocol
    base: Dict[str, Any] = {
        "name": node.name,
        "type": proto,
        "server": node.server,
        "port": node.port,
    }

    if proto == "vmess":
        base.update(
            {
                "uuid": p.get("uuid", ""),
                "alterId": int(p.get("alterId", 0) or 0),
                "cipher": p.get("cipher", "auto"),
                "network": p.get("network", "tcp"),
                "tls": bool(p.get("tls", False)),
                "skip-cert-verify": True,
            }
        )
        if p.get("sni"):
            base["servername"] = p["sni"]
        if p.get("network") == "ws":
            base["ws-opts"] = {
                "path": p.get("ws_path", "/"),
                "headers": {"Host": p.get("ws_host", "")},
            }
    elif proto == "vless":
        base.update(
            {
                "uuid": p.get("uuid", ""),
                "network": p.get("network", "tcp"),
                "tls": bool(p.get("tls", False)),
                "skip-cert-verify": True,
                "udp": True,
            }
        )
        if p.get("flow"):
            base["flow"] = p["flow"]
        if p.get("sni"):
            base["servername"] = p["sni"]
        if p.get("network") == "ws":
            base["ws-opts"] = {
                "path": p.get("ws_path", "/"),
                "headers": {"Host": p.get("ws_host", "")},
            }
        if p.get("network") == "grpc":
            base["grpc-opts"] = {"grpc-service-name": p.get("grpc_service_name", "")}
    elif proto == "trojan":
        base.update(
            {
                "password": p.get("password", ""),
                "sni": p.get("sni", ""),
                "skip-cert-verify": True,
                "udp": True,
            }
        )
    elif proto == "ss":
        base.update(
            {
                "cipher": p.get("cipher", "aes-128-gcm"),
                "password": p.get("password", ""),
                "udp": True,
            }
        )
    elif proto == "hysteria2":
        base.update(
            {
                "password": p.get("password", ""),
                "sni": p.get("sni", ""),
                "skip-cert-verify": True,
            }
        )
    return base


def build_mihomo_config(nodes: List[Node], cfg, groups: Dict[str, List[str]] | None = None) -> Dict[str, Any]:
    proxies = [node_to_mihomo(n) for n in nodes]

    # PROXY 选择器组 + 自动 url-test 组
    proxy_names = [p["name"] for p in proxies]
    proxy_groups: List[Dict[str, Any]] = [
        {
            "name": "PROXY",
            "type": "select",
            "proxies": ["AUTO", *proxy_names] if proxy_names else ["DIRECT"],
        },
        {
            "name": "AUTO",
            "type": "url-test",
            "url": cfg.alive_test.urls[0] if cfg.alive_test.urls else "http://www.gstatic.com/generate_204",
            "interval": max(60, cfg.alive_test.interval_seconds),
            "tolerance": 50,
            "proxies": proxy_names or ["DIRECT"],
        },
    ]

    if groups:
        for gname, members in groups.items():
            proxy_groups.append(
                {
                    "name": gname,
                    "type": "url-test",
                    "url": cfg.alive_test.urls[0] if cfg.alive_test.urls else "http://www.gstatic.com/generate_204",
                    "interval": max(60, cfg.alive_test.interval_seconds),
                    "tolerance": 30,
                    "proxies": members or ["DIRECT"],
                }
            )

    rules = ["MATCH,PROXY"]
    for r in cfg.routing.rules:
        targets = []
        if r.domains:
            targets.append("DOMAIN-SUFFIX," + r.domains[0] + "," + r.group) if len(r.domains) == 1 else None
            for d in r.domains:
                rules.insert(0, f"DOMAIN-SUFFIX,{d.lstrip('*.')},{r.group}")
        for ip in r.ips:
            rules.insert(0, f"IP-CIDR,{ip},{r.group},no-resolve")

    config: Dict[str, Any] = {
        "mixed-port": cfg.proxy.mixed_port,
        "allow-lan": cfg.proxy.allow_lan,
        "mode": cfg.proxy.mode,
        "log-level": "info",
        "external-controller": cfg.kernel.external_controller,
        "secret": cfg.kernel.secret,
        "proxies": proxies,
        "proxy-groups": proxy_groups,
        "rules": rules,
    }
    return config


def dump_mihomo_yaml(nodes: List[Node], cfg) -> str:
    return yaml.safe_dump(
        build_mihomo_config(nodes, cfg),
        allow_unicode=True,
        sort_keys=False,
    )


def dump_v2ray_base64(nodes: List[Node]) -> str:
    links = [_node_to_uri(n) for n in nodes]
    payload = "\n".join([l for l in links if l])
    return base64.b64encode(payload.encode("utf-8")).decode("ascii")


def _node_to_uri(n: Node) -> str:
    p = n.params
    q = []
    name = n.name

    if n.protocol == "vmess":
        obj = {
            "v": "2",
            "ps": name,
            "add": n.server,
            "port": str(n.port),
            "id": p.get("uuid", ""),
            "aid": str(p.get("alterId", 0)),
            "scy": p.get("cipher", "auto"),
            "net": p.get("network", "tcp"),
            "type": "none",
            "host": p.get("ws_host", ""),
            "path": p.get("ws_path", "/"),
            "tls": "tls" if p.get("tls") else "",
            "sni": p.get("sni", ""),
        }
        return "vmess://" + base64.b64encode(json.dumps(obj, ensure_ascii=False).encode("utf-8")).decode("ascii")

    if n.protocol == "vless":
        q.append(("encryption", "none"))
        if p.get("tls"):
            q.append(("security", "tls"))
        if p.get("sni"):
            q.append(("sni", p["sni"]))
        if p.get("network"):
            q.append(("type", p["network"]))
        if p.get("ws_path"):
            q.append(("path", p["ws_path"]))
        if p.get("ws_host"):
            q.append(("host", p["ws_host"]))
        if p.get("flow"):
            q.append(("flow", p["flow"]))
        query = up_encode(q)
        return f"vless://{p.get('uuid','')}@{n.server}:{n.port}?{query}#{name}"

    if n.protocol == "trojan":
        if p.get("sni"):
            q.append(("sni", p["sni"]))
        query = up_encode(q)
        return f"trojan://{p.get('password','')}@{n.server}:{n.port}?{query}#{name}"

    if n.protocol == "ss":
        userinfo = base64.urlsafe_b64encode(f"{p.get('cipher','')}:{p.get('password','')}".encode("utf-8")).decode("ascii").rstrip("=")
        return f"ss://{userinfo}@{n.server}:{n.port}#{name}"

    if n.protocol == "hysteria2":
        if p.get("sni"):
            q.append(("sni", p["sni"]))
        if p.get("skip_cert_verify"):
            q.append(("insecure", "1"))
        query = up_encode(q)
        return f"hysteria2://{p.get('password','')}@{n.server}:{n.port}?{query}#{name}"

    return ""


def up_encode(pairs) -> str:
    import urllib.parse as up

    return up.urlencode(pairs)
