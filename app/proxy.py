"""内置代理服务：基于 sing-box 子进程。

设计要点
--------
- 依赖：容器内需存在 sing-box 可执行文件（由 Dockerfile 安装到 PATH）
- 节点来源：store 中当前有效（enabled 且完整通过检测）的节点
- 三种监听模式：

    ======== ============== ========== =============================
    模式      监听地址        认证       适用场景
    ======== ============== ========== =============================
    local    127.0.0.1      无         容器内部进程使用
    lan      0.0.0.0        可选       通过端口映射供局域网使用
    remote   0.0.0.0        强制       通过端口映射供公网使用
    ======== ============== ========== =============================

- 出站选择：

    auto_select = true   使用 sing-box urltest 自动选择延迟最低的节点
    auto_select = false  使用 selected_node_id 指定的节点（缺失时回退首个）

- 生命周期：由 pipeline / web 在节点更新、配置保存时调用
  ``apply_proxy_config()``，根据 ``proxy.enabled`` 自动启动 / 重启 / 停止。
"""

import asyncio
import json
import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.models import NodeRecord

logger = logging.getLogger("vael-mux.proxy")

SINGBOX_BINARY = "sing-box"
VALID_MODES = ("local", "lan", "remote")

_CONFIG_PATH = Path("./output/sing-box.json")


# ============================================================ 出站转换
def _apply_transport(base: Dict[str, Any], rec: NodeRecord) -> None:
    net = str(rec.network or "tcp").lower()
    if net == "ws":
        t: Dict[str, Any] = {"type": "ws", "path": rec.path or "/"}
        if rec.host:
            t["headers"] = {"Host": rec.host}
        base["transport"] = t
    elif net == "grpc":
        base["transport"] = {
            "type": "grpc",
            "service_name": str(rec.path or "").lstrip("/"),
        }
    elif net in ("http", "h2"):
        t = {"type": "http"}
        if rec.host:
            t["host"] = [rec.host]
        if rec.path:
            t["path"] = rec.path
        base["transport"] = t


def _tls_block(rec: NodeRecord) -> Dict[str, Any]:
    tls: Dict[str, Any] = {"enabled": True}
    if rec.sni or rec.host:
        tls["server_name"] = rec.sni or rec.host
    if rec.skip_cert_verify or rec.insecure:
        tls["insecure"] = True
    return tls


def _node_to_outbound(rec: NodeRecord, tag: str) -> Optional[Dict[str, Any]]:
    host = rec.server
    port = rec.port
    if not host or not port:
        return None

    t = rec.type
    base: Dict[str, Any] = {
        "tag": tag,
        "server": host,
        "server_port": int(port),
    }

    if t == "vmess":
        base.update(
            {
                "type": "vmess",
                "uuid": rec.uuid or "",
                "security": rec.cipher or "auto",
                "alter_id": int(rec.alterId or 0),
            }
        )
        if rec.tls:
            base["tls"] = _tls_block(rec)
        _apply_transport(base, rec)
        return base

    if t == "vless":
        base.update(
            {
                "type": "vless",
                "uuid": rec.uuid or "",
            }
        )
        if rec.flow:
            base["flow"] = rec.flow
        if rec.tls:
            base["tls"] = _tls_block(rec)
        _apply_transport(base, rec)
        return base

    if t == "trojan":
        base.update(
            {
                "type": "trojan",
                "password": rec.password or "",
                "tls": _tls_block(rec),
            }
        )
        return base

    if t == "ss":
        base.update(
            {
                "type": "shadowsocks",
                "method": rec.cipher or "",
                "password": rec.password or "",
            }
        )
        return base

    if t == "hysteria2":
        base.update(
            {
                "type": "hysteria2",
                "password": rec.password or "",
                "tls": _tls_block(rec),
            }
        )
        return base

    return None


# ============================================================ 代理管理器
class ProxyManager:
    def __init__(self) -> None:
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._pipe_task: Optional[asyncio.Task] = None
        self._monitor_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._status: Dict[str, Any] = {
            "running": False,
            "mode": "",
            "listen": "",
            "http_port": 0,
            "socks_port": 0,
            "node_count": 0,
            "selected": "",
            "error": "",
            "started_at": "",
        }

    @staticmethod
    def binary_available() -> bool:
        return shutil.which(SINGBOX_BINARY) is not None

    def status(self) -> Dict[str, Any]:
        return dict(self._status)

    # ------------------------------------------------- 启动
    async def start(
        self,
        proxy_cfg: Dict[str, Any],
        nodes: List[NodeRecord],
    ) -> Dict[str, Any]:
        async with self._lock:
            await self._stop_locked()

            if not proxy_cfg.get("enabled"):
                self._reset_status()
                return dict(self._status)

            binary = shutil.which(SINGBOX_BINARY)
            if not binary:
                self._set_error("未找到 sing-box 可执行文件，请确认容器镜像已安装 sing-box")
                return dict(self._status)

            mode = str(proxy_cfg.get("mode") or "local").lower()
            if mode not in VALID_MODES:
                mode = "local"
            listen = "127.0.0.1" if mode == "local" else "0.0.0.0"

            username = str(proxy_cfg.get("username") or "").strip()
            password = str(proxy_cfg.get("password") or "").strip()

            if mode == "remote" and (not username or not password):
                self._set_error("远程模式必须配置用户名与密码")
                return dict(self._status)

            if not nodes:
                self._set_error("没有可用的有效节点（请先完成一次检测）")
                return dict(self._status)

            try:
                http_port = int(proxy_cfg.get("http_port") or 7890)
                socks_port = int(proxy_cfg.get("socks_port") or 7891)
            except (TypeError, ValueError):
                http_port, socks_port = 7890, 7891

            auto_select = bool(proxy_cfg.get("auto_select", True))
            selected_id = str(proxy_cfg.get("selected_node_id") or "").strip()

            # ---- 构造出站 ----
            outbounds: List[Dict[str, Any]] = []
            tag_by_id: Dict[str, str] = {}
            for i, rec in enumerate(nodes):
                tag = f"node-{i}"
                ob = _node_to_outbound(rec, tag)
                if ob is None:
                    continue
                tag_by_id[rec.id] = tag
                outbounds.append(ob)

            if not outbounds:
                self._set_error("当前节点均无法转换为 sing-box 出站")
                return dict(self._status)

            node_tags = [ob["tag"] for ob in outbounds]

            if auto_select:
                selector: Dict[str, Any] = {
                    "type": "urltest",
                    "tag": "proxy",
                    "outbounds": node_tags,
                    "url": "https://www.gstatic.com/generate_204",
                    "interval": "5m",
                    "tolerance": 50,
                }
                selected_label = "自动（urltest）"
            else:
                chosen = tag_by_id.get(selected_id, node_tags[0])
                selector = {
                    "type": "selector",
                    "tag": "proxy",
                    "outbounds": node_tags,
                    "default": chosen,
                }
                selected_label = selected_id or node_tags[0]

            inbounds: List[Dict[str, Any]] = [
                {
                    "type": "http",
                    "tag": "http-in",
                    "listen": listen,
                    "listen_port": http_port,
                },
                {
                    "type": "socks",
                    "tag": "socks-in",
                    "listen": listen,
                    "listen_port": socks_port,
                },
            ]
            if username and password:
                for ib in inbounds:
                    ib["users"] = [{"username": username, "password": password}]

            config: Dict[str, Any] = {
                "log": {"level": "warn", "timestamp": True},
                "inbounds": inbounds,
                "outbounds": [
                    selector,
                    {"type": "direct", "tag": "direct"},
                    {"type": "block", "tag": "block"},
                ]
                + outbounds,
                "route": {"final": "proxy"},
            }

            # ---- 原子写配置 ----
            _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = _CONFIG_PATH.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(config, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(_CONFIG_PATH)

            # ---- 启动子进程 ----
            try:
                proc = await asyncio.create_subprocess_exec(
                    binary,
                    "run",
                    "-c",
                    str(_CONFIG_PATH),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except Exception as e:
                self._set_error(f"启动 sing-box 失败: {e}")
                logger.exception("启动 sing-box 失败")
                return dict(self._status)

            self._proc = proc

            # 等待 0.5s 检查是否秒退
            await asyncio.sleep(0.5)
            if proc.returncode is not None:
                self._proc = None
                self._set_error(f"sing-box 启动后立即退出（code={proc.returncode}）")
                return dict(self._status)

            self._pipe_task = asyncio.create_task(self._pipe_logs(proc))
            self._monitor_task = asyncio.create_task(self._monitor(proc))

            self._status = {
                "running": True,
                "mode": mode,
                "listen": listen,
                "http_port": http_port,
                "socks_port": socks_port,
                "node_count": len(outbounds),
                "selected": selected_label,
                "error": "",
                "started_at": datetime.now().isoformat(timespec="seconds"),
            }
            logger.info(
                f"代理已启动：模式={mode}，监听={listen}，"
                f"HTTP={http_port}，SOCKS5={socks_port}，"
                f"节点数={len(outbounds)}，选择={selected_label}"
            )
            return dict(self._status)

    # ------------------------------------------------- 停止
    async def stop(self) -> Dict[str, Any]:
        async with self._lock:
            await self._stop_locked()
            self._reset_status()
            return dict(self._status)

    async def _stop_locked(self) -> None:
        proc = self._proc
        self._proc = None

        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass
            except Exception:
                pass

        for attr in ("_pipe_task", "_monitor_task"):
            task = getattr(self, attr)
            setattr(self, attr, None)
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

    # ------------------------------------------------- 状态
    def _reset_status(self) -> None:
        self._status = {
            "running": False,
            "mode": "",
            "listen": "",
            "http_port": 0,
            "socks_port": 0,
            "node_count": 0,
            "selected": "",
            "error": "",
            "started_at": "",
        }

    def _set_error(self, msg: str) -> None:
        self._reset_status()
        self._status["error"] = msg
        logger.error(msg)

    # ------------------------------------------------- 子进程日志 / 监控
    async def _pipe_logs(self, proc: asyncio.subprocess.Process) -> None:
        async def _read(stream, level: int) -> None:
            if stream is None:
                return
            while True:
                try:
                    line = await stream.readline()
                except Exception:
                    break
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    logger.log(level, f"[sing-box] {text}")

        try:
            await asyncio.gather(
                _read(proc.stdout, logging.INFO),
                _read(proc.stderr, logging.WARNING),
            )
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def _monitor(self, proc: asyncio.subprocess.Process) -> None:
        try:
            code = await proc.wait()
        except asyncio.CancelledError:
            return
        except Exception:
            return
        if self._proc is proc:
            self._proc = None
            self._status["running"] = False
            self._status["error"] = f"sing-box 进程意外退出（code={code}）"
            logger.warning(f"sing-box 进程意外退出（code={code}）")


proxy_manager = ProxyManager()
