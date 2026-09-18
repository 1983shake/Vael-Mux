"""将存活节点导出为 mihomo / sing-box / base64 / v2ray 订阅。

输出格式：
  - mihomo      : Clash.Meta / Mihomo YAML
  - singbox     : sing-box JSON
  - base64      : 通用 Base64 URI 列表（vmess/vless/trojan/ss/... 原始分享链接）
  - v2ray       : v2rayN / v2rayNG / v2rayA 订阅（内容与 base64 相同，
                  但单独文件便于客户端识别与路由分发）
  - v2ray-json  : v2ray 完整 config.json（含 inbounds / outbounds / routing）

URI 生成策略：
  - 优先使用节点自带的 raw（从 URI 订阅源解析而来）
  - raw 为空时（Clash YAML 源 / 手动新增 / 前端编辑过的节点），
    根据节点字段反向生成标准分享链接
  - 字段缺失无法生成的节点会被跳过，并打印 warning，方便排查

数量一致性：
  - 每种格式写入前先统计实际能导出的节点数
  - export_all 打印各格式的导出数量，与传入的节点总数对比
  - 若某格式数量少于总数，会提示原因
"""

import base64
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlencode

import yaml

logger = logging.getLogger("vael-mux.exporter")


async def export_all(nodes: List[Dict[str, Any]], output_cfg: Dict[str, Any]) -> List[str]:
    out_dir = Path(output_cfg.get("directory", "./output"))
    out_dir.mkdir(parents=True, exist_ok=True)

    formats = {f.lower() for f in output_cfg.get("formats", [])}
    written: List[str] = []
    total = len(nodes)
    counts: Dict[str, int] = {}

    if "mihomo" in formats or "clash" in formats:
        ok = [n for n in nodes if _to_mihomo(n) is not None]
        counts["mihomo.yaml"] = len(ok)
        path = out_dir / "mihomo.yaml"
        path.write_text(export_mihomo(ok), encoding="utf-8")
        written.append(str(path))
        _log_count("mihomo.yaml", len(ok), total, nodes)

    if "singbox" in formats or "sing-box" in formats:
        ok = [n for n in nodes if _to_singbox(n) is not None]
        counts["singbox.json"] = len(ok)
        path = out_dir / "singbox.json"
        path.write_text(export_singbox(ok), encoding="utf-8")
        written.append(str(path))
        _log_count("singbox.json", len(ok), total, nodes)

    if "base64" in formats:
        uris = _collect_uris(nodes)
        counts["base64.txt"] = len(uris)
        path = out_dir / "base64.txt"
        path.write_text(
            base64.b64encode("\n".join(uris).encode("utf-8")).decode("ascii"),
            encoding="utf-8",
        )
        written.append(str(path))
        _log_count("base64.txt", len(uris), total, nodes)

    if "v2ray" in formats:
        uris = _collect_uris(nodes)
        counts["v2ray.txt"] = len(uris)
        path = out_dir / "v2ray.txt"
        path.write_text(
            base64.b64encode("\n".join(uris).encode("utf-8")).decode("ascii"),
            encoding="utf-8",
        )
        written.append(str(path))
        _log_count("v2ray.txt", len(uris), total, nodes)

    if "v2ray-json" in formats or "v2ray_json" in formats:
        ok = [n for n in nodes if _to_v2ray_outbound(n) is not None]
        counts["v2ray.json"] = len(ok)
        path = out_dir / "v2ray.json"
        path.write_text(export_v2ray_json(ok), encoding="utf-8")
        written.append(str(path))
        _log_count("v2ray.json", len(ok), total, nodes)

    # 汇总
    if total == 0:
        logger.info("无有效节点，所有订阅文件已清空")
    else:
        summary = ", ".join(f"{k}={v}" for k, v in counts.items())
        logger.info(f"导出完成：输入 {total} 个有效节点，各格式导出数量：{summary}")

        # 若有格式少于总数，提示原因
        min_count = min(counts.values()) if counts else total
        if min_count < total:
            logger.warning(
                f"各格式导出数量不一致（最少 {min_count} / 总数 {total}）。"
                f"常见原因：hysteria2 不被 v2ray.json 支持；"
                f"或节点缺少 uuid / password / cipher 等字段。"
            )

    return written


def _log_count(fmt: str, exported: int, total: int, nodes: List[Dict[str, Any]]) -> None:
    """记录单个格式的导出数量，不一致时打印被跳过的节点名。"""
    if exported == total:
        return
    skipped = total - exported
    # 找出未能导出的节点名（最多列 5 个）
    skipped_names: List[str] = []
    for n in nodes:
        ok = False
        if fmt == "mihomo.yaml":
            ok = _to_mihomo(n) is not None
        elif fmt == "singbox.json":
            ok = _to_singbox(n) is not None
        elif fmt == "v2ray.json":
            ok = _to_v2ray_outbound(n) is not None
        elif fmt in ("base64.txt", "v2ray.txt"):
            ok = bool(n.get("raw")) or (_node_to_uri(n) is not None)
        if not ok:
            skipped_names.append(str(n.get("name") or n.get("server") or "?"))
            if len(skipped_names) >= 5:
                break
    sample = ", ".join(skipped_names) if skipped_names else "(未知)"
    logger.warning(f"{fmt}: 输入 {total} -> 导出 {exported}（跳过 {skipped}），示例: {sample}")


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
    """导出 sing-box JSON。

    注意：selector 的 outbounds 只包含**实际生成 outbound 成功**的 tag，
    避免引用不存在的节点（之前会列出所有 name 导致客户端报错）。
    """
    proxy_outbounds: List[Dict[str, Any]] = []
    proxy_tags: List[str] = []

    for n in nodes:
        ob = _to_singbox(n)
        if ob is None:
            continue
        tag = ob.get("tag")
        if not tag:
            continue
        proxy_outbounds.append(ob)
        proxy_tags.append(tag)

    outbounds: List[Dict[str, Any]] = [
        {
            "type": "selector",
            "tag": "Vael-Mux",
            "outbounds": proxy_tags + ["direct"],
        },
        {"type": "direct", "tag": "direct"},
    ]
    outbounds.extend(proxy_outbounds)

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

    内容与通用 base64 一致（base64 编码的原始 URI 列表）。
    """
    uris = _collect_uris(nodes)
    joined = "\n".join(uris)
    return base64.b64encode(joined.encode("utf-8")).decode("ascii")


def _collect_uris(nodes: List[Dict[str, Any]]) -> List[str]:
    """收集节点的分享链接。

    优先使用 raw（URI 订阅源解析来的原始文本）；
    raw 为空时从节点字段反向生成（适用于 Clash YAML 源 / 手动新增的节点）。
    字段缺失无法生成的节点会被跳过，并打印 warning。
    """
    result: List[str] = []
    skipped: List[Dict[str, Any]] = []

    for n in nodes:
        raw = n.get("raw")
        if raw:
            result.append(str(raw))
            continue
        uri = _node_to_uri(n)
        if uri:
            result.append(uri)
        else:
            skipped.append(n)

    if skipped:
        names = [str(x.get("name") or f"{x.get('type')}-{x.get('server')}:{x.get('port')}") for x in skipped[:5]]
        logger.warning(
            f"无法生成分享链接 {len(skipped)} 个节点（示例: {', '.join(names)}）；"
            f"这些节点不会出现在 base64.txt / v2ray.txt。"
            f"常见原因：uuid / password / cipher 等字段缺失。"
        )

    return result


# ---------------------------------------------------------------- URI 反向生成
def _node_to_uri(n: Dict[str, Any]) -> Optional[str]:
    t = n.get("type")
    if t == "vmess":
        return _vmess_uri(n)
    if t == "vless":
        return _vless_uri(n)
    if t == "trojan":
        return _trojan_uri(n)
    if t == "ss":
        return _ss_uri(n)
    if t == "hysteria2":
        return _hysteria2_uri(n)
    return None


def _vmess_uri(n: Dict[str, Any]) -> Optional[str]:
    host = n.get("server")
    port = n.get("port")
    uuid = n.get("uuid")
    if not host or not port or not uuid:
        return None

    net = n.get("network") or "tcp"
    tls_val = "tls" if n.get("tls") else ""
    data: Dict[str, Any] = {
        "v": "2",
        "ps": n.get("name") or f"vmess-{host}",
        "add": host,
        "port": str(int(port)),
        "id": uuid,
        "aid": str(int(n.get("alterId", 0) or 0)),
        "scy": n.get("cipher") or "auto",
        "net": net,
        "type": "none",
        "host": n.get("host") or "",
        "path": n.get("path") or "",
        "tls": tls_val,
        "sni": n.get("sni") or "",
    }
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    encoded = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")
    return f"vmess://{encoded}"


def _vless_uri(n: Dict[str, Any]) -> Optional[str]:
    host = n.get("server")
    port = n.get("port")
    uuid = n.get("uuid")
    if not host or not port or not uuid:
        return None

    qs: Dict[str, str] = {}
    net = n.get("network") or "tcp"
    qs["type"] = net
    if n.get("tls"):
        qs["security"] = "tls"
    if n.get("sni"):
        qs["sni"] = n["sni"]
    if n.get("host"):
        qs["host"] = n["host"]
    if n.get("path"):
        qs["path"] = n["path"]
    if n.get("flow"):
        qs["flow"] = n["flow"]
    if n.get("skip_cert_verify") or n.get("insecure"):
        qs["allowInsecure"] = "1"

    name = quote(str(n.get("name") or f"vless-{host}"), safe="")
    query = urlencode(qs)
    return f"vless://{uuid}@{host}:{int(port)}?{query}#{name}"


def _trojan_uri(n: Dict[str, Any]) -> Optional[str]:
    host = n.get("server")
    port = n.get("port")
    pw = n.get("password")
    if not host or not port or not pw:
        return None

    qs: Dict[str, str] = {}
    if n.get("sni"):
        qs["sni"] = n["sni"]
    if n.get("skip_cert_verify") or n.get("insecure"):
        qs["allowInsecure"] = "1"
    net = n.get("network") or "tcp"
    if net != "tcp":
        qs["type"] = net
        if n.get("host"):
            qs["host"] = n["host"]
        if n.get("path"):
            qs["path"] = n["path"]

    name = quote(str(n.get("name") or f"trojan-{host}"), safe="")
    query = urlencode(qs)
    sep = "?" if query else ""
    return f"trojan://{quote(str(pw), safe='')}@{host}:{int(port)}{sep}{query}#{name}"


def _ss_uri(n: Dict[str, Any]) -> Optional[str]:
    host = n.get("server")
    port = n.get("port")
    method = n.get("cipher")
    pw = n.get("password")
    if not host or not port or not method or pw is None:
        return None

    # SIP002: ss://base64(method:password)@host:port#name
    userinfo = base64.urlsafe_b64encode(f"{method}:{pw}".encode("utf-8")).decode("ascii").rstrip("=")
    name = quote(str(n.get("name") or f"ss-{host}"), safe="")
    return f"ss://{userinfo}@{host}:{int(port)}#{name}"


def _hysteria2_uri(n: Dict[str, Any]) -> Optional[str]:
    host = n.get("server")
    port = n.get("port")
    pw = n.get("password")
    if not host or not port or not pw:
        return None

    qs: Dict[str, str] = {}
    if n.get("sni"):
        qs["sni"] = n["sni"]
    if n.get("skip_cert_verify") or n.get("insecure"):
        qs["insecure"] = "1"

    name = quote(str(n.get("name") or f"hy2-{host}"), safe="")
    query = urlencode(qs)
    sep = "?" if query else ""
    return f"hysteria2://{quote(str(pw), safe='')}@{host}:{int(port)}{sep}{query}#{name}"


# ============================================================ v2ray config.json
def export_v2ray_json(nodes: List[Dict[str, Any]]) -> str:
    """v2ray 完整配置 (config.json)。

    不支持 hysteria2（v2ray 核心不包含该协议），传入的节点应事先过滤。
    """
    proxy_outbounds: List[Dict[str, Any]] = []
    for n in nodes:
        ob = _to_v2ray_outbound(n)
        if ob is not None:
            proxy_outbounds.append(ob)

    outbounds: List[Dict[str, Any]] = []
    if proxy_outbounds:
        outbounds.extend(proxy_outbounds)
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
                "sniffing": {"enabled": True, "destOverride": ["http", "tls"]},
            },
            {
                "port": 1081,
                "listen": "127.0.0.1",
                "protocol": "http",
                "sniffing": {"enabled": True, "destOverride": ["http", "tls"]},
            },
        ],
        "outbounds": outbounds,
        "routing": {
            "domainStrategy": "IPIfNonMatch",
            "rules": [
                {"type": "field", "outboundTag": "direct", "domain": ["geosite:cn"]},
                {"type": "field", "outboundTag": "direct", "ip": ["geoip:cn", "geoip:private"]},
                {"type": "field", "outboundTag": "block", "domain": ["geosite:category-ads-all"]},
            ],
        },
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
            "settings": {"vnext": [{"address": host, "port": port, "users": [user]}]},
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
            "settings": {"vnext": [{"address": host, "port": port, "users": [user]}]},
            "streamSettings": _v2ray_stream_settings(n),
        }

    if t == "trojan":
        return {
            "protocol": "trojan",
            "tag": tag,
            "settings": {"servers": [{"address": host, "port": port, "password": n.get("password", "")}]},
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
                ]
            },
        }

    # hysteria2 不属于 v2ray 核心协议，跳过
    return None


def _v2ray_stream_settings(n: Dict[str, Any], force_tls: bool = False) -> Dict[str, Any]:
    net = n.get("network") or "tcp"
    security = "tls" if (n.get("tls") or force_tls) else "none"

    s: Dict[str, Any] = {"network": net, "security": security}

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
