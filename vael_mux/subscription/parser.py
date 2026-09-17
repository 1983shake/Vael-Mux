from __future__ import annotations

import base64
import json
import urllib.parse as up
from typing import Any, Dict, List, Optional

from ..models import Node


def _b64decode(s: str) -> str:
    s = s.strip().replace("-", "+").replace("_", "/")
    s += "=" * (-len(s) % 4)
    return base64.b64decode(s).decode("utf-8", errors="replace")


def parse_subscription(text: str, source: str = "") -> List[Node]:
    text = (text or "").strip()
    if not text:
        return []

    # 整段 base64 编码（v2ray 订阅常见格式）
    if "://" not in text:
        try:
            text = _b64decode(text)
        except Exception:
            return []

    nodes: List[Node] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or "://" not in line:
            continue
        n = parse_uri(line, source)
        if n:
            nodes.append(n)
    return nodes


def parse_uri(uri: str, source: str = "") -> Optional[Node]:
    scheme = uri.split("://", 1)[0].lower()
    try:
        if scheme == "vmess":
            return _parse_vmess(uri, source)
        if scheme == "vless":
            return _parse_vless(uri, source)
        if scheme == "trojan":
            return _parse_trojan(uri, source)
        if scheme in ("ss", "shadowsocks"):
            return _parse_ss(uri, source)
        if scheme in ("hysteria2", "hy2"):
            return _parse_hy2(uri, source)
    except Exception:
        return None
    return None


def _split(uri: str):
    rest = uri.split("://", 1)[1]
    frag = ""
    if "#" in rest:
        rest, frag = rest.split("#", 1)
    frag = up.unquote(frag)
    query = ""
    if "?" in rest:
        rest, query = rest.split("?", 1)
    return rest, query, frag


def _parse_vmess(uri: str, source: str) -> Optional[Node]:
    body = uri.split("://", 1)[1]
    try:
        data = json.loads(_b64decode(body))
    except Exception:
        return None
    host = str(data.get("add", "")).strip()
    if not host:
        return None
    port = int(data.get("port", 0) or 0)
    params: Dict[str, Any] = {
        "uuid": data.get("id"),
        "alterId": int(data.get("aid", 0) or 0),
        "cipher": data.get("scy") or "auto",
        "network": data.get("net") or "tcp",
        "tls": bool(data.get("tls")),
        "sni": data.get("sni") or data.get("host") or "",
        "ws_path": data.get("path") or "/",
        "ws_host": data.get("host") or "",
        "grpc_service_name": data.get("path") or "",
    }
    name = data.get("ps") or f"vmess-{host}"
    return Node(name=name, protocol="vmess", server=host, port=port, params=params, source=source)


def _parse_vless(uri: str, source: str) -> Optional[Node]:
    rest, query, frag = _split(uri)
    if "@" not in rest:
        return None
    userinfo, hostport = rest.rsplit("@", 1)
    uuid = up.unquote(userinfo)
    host, _, port = hostport.rpartition(":")
    q = dict(up.parse_qsl(query))
    params: Dict[str, Any] = {
        "uuid": uuid,
        "flow": q.get("flow", ""),
        "network": q.get("type", "tcp"),
        "tls": q.get("security", "none") in ("tls", "reality"),
        "sni": q.get("sni", "") or q.get("host", ""),
        "ws_path": q.get("path", "/"),
        "ws_host": q.get("host", ""),
        "grpc_service_name": q.get("serviceName", ""),
    }
    name = frag or f"vless-{host}"
    return Node(name=name, protocol="vless", server=host, port=int(port or 0), params=params, source=source)


def _parse_trojan(uri: str, source: str) -> Optional[Node]:
    rest, query, frag = _split(uri)
    if "@" not in rest:
        return None
    userinfo, hostport = rest.rsplit("@", 1)
    password = up.unquote(userinfo)
    host, _, port = hostport.rpartition(":")
    q = dict(up.parse_qsl(query))
    params: Dict[str, Any] = {
        "password": password,
        "sni": q.get("sni", "") or q.get("peer", ""),
        "skip_cert_verify": q.get("allowInsecure", "0") in ("1", "true"),
    }
    name = frag or f"trojan-{host}"
    return Node(name=name, protocol="trojan", server=host, port=int(port or 0), params=params, source=source)


def _parse_ss(uri: str, source: str) -> Optional[Node]:
    rest, query, frag = _split(uri)
    if "@" in rest:
        userinfo, hostport = rest.rsplit("@", 1)
        try:
            method_pw = _b64decode(userinfo)
        except Exception:
            method_pw = up.unquote(userinfo)
        host, _, port = hostport.rpartition(":")
    else:
        # 旧式 ss://base64(method:password@host:port)
        try:
            decoded = _b64decode(rest)
        except Exception:
            return None
        method_pw, _, hostport = decoded.rpartition("@")
        host, _, port = hostport.rpartition(":")
    if ":" not in method_pw:
        return None
    cipher, password = method_pw.split(":", 1)
    params: Dict[str, Any] = {"cipher": cipher, "password": password}
    name = frag or f"ss-{host}"
    return Node(name=name, protocol="ss", server=host, port=int(port or 0), params=params, source=source)


def _parse_hy2(uri: str, source: str) -> Optional[Node]:
    rest, query, frag = _split(uri)
    if "@" not in rest:
        return None
    userinfo, hostport = rest.rsplit("@", 1)
    password = up.unquote(userinfo)
    host, _, port = hostport.rpartition(":")
    q = dict(up.parse_qsl(query))
    params: Dict[str, Any] = {
        "password": password,
        "sni": q.get("sni", "") or q.get("peer", ""),
        "skip_cert_verify": q.get("insecure", "0") in ("1", "true"),
    }
    name = frag or f"hy2-{host}"
    return Node(name=name, protocol="hysteria2", server=host, port=int(port or 0), params=params, source=source)
