"""订阅内容解析：支持 Clash YAML、Base64 编码、明文 URI 列表。"""

import base64
import json
import logging
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlparse

import yaml

logger = logging.getLogger("vael-mux.parser")


# ---------------------------------------------------------------- 主入口
def parse_all(raw_texts: List[str]) -> List[Dict[str, Any]]:
    nodes: List[Dict[str, Any]] = []
    for text in raw_texts:
        try:
            nodes.extend(parse_subscription_text(text))
        except Exception as e:
            logger.warning(f"解析订阅内容失败: {e}")
    return dedupe(nodes)


def parse_subscription_text(text: str) -> List[Dict[str, Any]]:
    text = text.strip()
    if not text:
        return []

    # 1) 尝试 Clash YAML
    if _looks_like_yaml(text):
        try:
            data = yaml.safe_load(text)
            if isinstance(data, dict) and isinstance(data.get("proxies"), list):
                return [_normalize_clash_proxy(p) for p in data["proxies"] if isinstance(p, dict)]
        except yaml.YAMLError:
            pass

    # 2) 尝试 Base64
    decoded = _try_b64_decode(text)
    if decoded is not None and _looks_like_uri_list(decoded):
        return _parse_uri_lines(decoded)

    # 3) 直接作为 URI 列表解析
    if _looks_like_uri_list(text):
        return _parse_uri_lines(text)

    # 4) 兜底：Base64 解出来再当 YAML 试一次
    if decoded is not None and _looks_like_yaml(decoded):
        try:
            data = yaml.safe_load(decoded)
            if isinstance(data, dict) and isinstance(data.get("proxies"), list):
                return [_normalize_clash_proxy(p) for p in data["proxies"] if isinstance(p, dict)]
        except yaml.YAMLError:
            pass

    return []


# ---------------------------------------------------------------- 格式判定
def _looks_like_yaml(text: str) -> bool:
    for line in text.splitlines()[:20]:
        s = line.strip()
        if s.startswith("proxies:") or s.startswith("proxy-groups:"):
            return True
    return False


def _looks_like_uri_list(text: str) -> bool:
    schemes = ("vmess://", "vless://", "trojan://", "ss://", "ssr://", "hysteria2://", "hy2://", "tuic://")
    for line in text.splitlines()[:50]:
        s = line.strip()
        if s.startswith(schemes):
            return True
    return False


def _try_b64_decode(text: str) -> Optional[str]:
    compact = "".join(text.split())
    if not compact:
        return None
    padding = "=" * (-len(compact) % 4)
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            return decoder(compact + padding).decode("utf-8", errors="ignore")
        except Exception:
            continue
    return None


# ---------------------------------------------------------------- URI 解析
def _parse_uri_lines(text: str) -> List[Dict[str, Any]]:
    nodes: List[Dict[str, Any]] = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        try:
            node = parse_uri(s)
            if node:
                nodes.append(node)
        except Exception as e:
            logger.debug(f"解析 URI 失败 [{s[:40]}...]: {e}")
    return nodes


def parse_uri(uri: str) -> Optional[Dict[str, Any]]:
    if uri.startswith("vmess://"):
        return _parse_vmess(uri)
    if uri.startswith("vless://"):
        return _parse_vless(uri)
    if uri.startswith("trojan://"):
        return _parse_trojan(uri)
    if uri.startswith("ss://"):
        return _parse_ss(uri)
    if uri.startswith("hysteria2://") or uri.startswith("hy2://"):
        return _parse_hysteria2(uri)
    return None


def _parse_vmess(uri: str) -> Optional[Dict[str, Any]]:
    payload = uri[len("vmess://") :]
    padding = "=" * (-len(payload) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload + padding).decode("utf-8"))
    except Exception:
        return None

    server = data.get("add")
    port = data.get("port")
    if not server or not port:
        return None

    try:
        port = int(port)
    except (TypeError, ValueError):
        return None

    return {
        "type": "vmess",
        "name": data.get("ps") or data.get("name") or f"vmess-{server}",
        "server": server,
        "port": port,
        "uuid": data.get("id", ""),
        "alterId": int(data.get("aid", 0) or 0),
        "cipher": data.get("scy") or "auto",
        "network": data.get("net") or "tcp",
        "tls": (data.get("tls") or "") == "tls",
        "sni": data.get("sni") or data.get("host") or "",
        "host": data.get("host") or "",
        "path": data.get("path") or "",
        "raw": uri,
    }


def _parse_vless(uri: str) -> Optional[Dict[str, Any]]:
    parsed = urlparse(uri)
    if not parsed.hostname or not parsed.port:
        return None
    qs = parse_qs(parsed.query)
    return {
        "type": "vless",
        "name": unquote(parsed.fragment) or f"vless-{parsed.hostname}",
        "server": parsed.hostname,
        "port": int(parsed.port),
        "uuid": parsed.username or "",
        "network": (qs.get("type") or ["tcp"])[0],
        "tls": (qs.get("security") or [""])[0] in ("tls", "reality"),
        "sni": (qs.get("sni") or qs.get("peer") or [""])[0],
        "host": (qs.get("host") or [""])[0],
        "path": (qs.get("path") or [""])[0],
        "flow": (qs.get("flow") or [""])[0],
        "raw": uri,
    }


def _parse_trojan(uri: str) -> Optional[Dict[str, Any]]:
    parsed = urlparse(uri)
    if not parsed.hostname or not parsed.port:
        return None
    qs = parse_qs(parsed.query)
    return {
        "type": "trojan",
        "name": unquote(parsed.fragment) or f"trojan-{parsed.hostname}",
        "server": parsed.hostname,
        "port": int(parsed.port),
        "password": unquote(parsed.username or ""),
        "sni": (qs.get("sni") or qs.get("peer") or [""])[0],
        "skip_cert_verify": (qs.get("allowInsecure") or ["0"])[0] == "1",
        "raw": uri,
    }


def _parse_ss(uri: str) -> Optional[Dict[str, Any]]:
    parsed = urlparse(uri)
    name = unquote(parsed.fragment) or ""
    if parsed.hostname and parsed.port:
        method, _, password = (unquote(parsed.username or "")).partition(":")
        return {
            "type": "ss",
            "name": name or f"ss-{parsed.hostname}",
            "server": parsed.hostname,
            "port": int(parsed.port),
            "cipher": method,
            "password": password,
            "raw": uri,
        }
    # ss://base64(method:pass@host:port)#name
    payload = uri[len("ss://") :]
    if "#" in payload:
        payload = payload.split("#", 1)[0]
    decoded = _try_b64_decode(payload)
    if not decoded:
        return None
    if "@" not in decoded:
        return None
    creds, _, hostpart = decoded.rpartition("@")
    method, _, password = creds.partition(":")
    host, _, port = hostpart.rpartition(":")
    try:
        port_i = int(port)
    except ValueError:
        return None
    return {
        "type": "ss",
        "name": name or f"ss-{host}",
        "server": host,
        "port": port_i,
        "cipher": method,
        "password": password,
        "raw": uri,
    }


def _parse_hysteria2(uri: str) -> Optional[Dict[str, Any]]:
    parsed = urlparse(uri)
    if not parsed.hostname or not parsed.port:
        return None
    qs = parse_qs(parsed.query)
    return {
        "type": "hysteria2",
        "name": unquote(parsed.fragment) or f"hy2-{parsed.hostname}",
        "server": parsed.hostname,
        "port": int(parsed.port),
        "password": unquote(parsed.username or ""),
        "sni": (qs.get("sni") or [""])[0],
        "insecure": (qs.get("insecure") or ["0"])[0] == "1",
        "raw": uri,
    }


# ---------------------------------------------------------------- Clash 归一化
_CLASH_TYPE_MAP = {
    "vmess": "vmess",
    "vless": "vless",
    "trojan": "trojan",
    "ss": "ss",
    "shadowsocks": "ss",
    "hysteria2": "hysteria2",
    "hy2": "hysteria2",
}


def _normalize_clash_proxy(proxy: Dict[str, Any]) -> Dict[str, Any]:
    p = dict(proxy)
    p["type"] = _CLASH_TYPE_MAP.get(str(p.get("type", "")).lower(), str(p.get("type", "")).lower())
    return p


# ---------------------------------------------------------------- 去重
def dedupe(nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    result = []
    for n in nodes:
        key = (n.get("type"), n.get("server"), n.get("port"), n.get("uuid") or n.get("password"))
        if key in seen:
            continue
        seen.add(key)
        result.append(n)
    return result
