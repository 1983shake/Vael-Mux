"""Web 管理界面 (8100) + API / 订阅输出服务 (8110)。

本模块由原 app/web/app.py 与 app/web/api.py 合并而来：
  - create_web_app()：Web 管理界面（含 WebSocket 实时状态、配置面板、代理控制）
  - create_api_app()：订阅输出（/sub/{fmt}）

端口说明：
  - 内部端口固定 8100 / 8110，由 app.main 传入 uvicorn
  - 对外端口由 docker-compose.yml 的 ports 映射控制
  - 配置文件中的 server.web_port / server.api_port 会被忽略
"""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List

from apscheduler.triggers.cron import CronTrigger
from fastapi import Body, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.checker import is_fully_valid
from app.models import (
    NodeRecord,
    enabled_targets,
    load_base_config,
    normalize_mode,
    save_config,
)
from app.pipeline import (
    apply_proxy_config,
    get_store,
    regenerate_subscriptions,
    reload_scheduler,
    retest_single,
    spawn_pipeline,
    start_scheduler,
    stop_pipeline,
    trigger_now,
)
from app.proxy import proxy_manager
from app.runtime import (
    LOG_BACKFILL,
    TaskStage,
    apply_log_level,
    get_recent_logs,
    logger,
    set_log_broadcast,
    state,
)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

PATCHABLE_FIELDS = {
    "type",
    "name",
    "server",
    "port",
    "uuid",
    "password",
    "cipher",
    "alterId",
    "network",
    "tls",
    "sni",
    "host",
    "path",
    "flow",
    "insecure",
    "skip_cert_verify",
    "enabled",
}

RETEST_FIELDS = {
    "type",
    "server",
    "port",
    "uuid",
    "password",
    "cipher",
    "alterId",
    "network",
    "tls",
    "sni",
    "host",
    "path",
    "flow",
    "insecure",
    "skip_cert_verify",
}

CREATABLE_TYPES = frozenset({"vmess", "vless", "trojan", "ss", "hysteria2"})

_NO_CACHE_HEADERS = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}

_VALID_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
_VALID_PROXY_MODES = frozenset({"local", "lan", "remote"})

# API 订阅格式映射
FORMAT_MAP = {
    "mihomo": ("mihomo.yaml", "text/yaml; charset=utf-8"),
    "clash": ("mihomo.yaml", "text/yaml; charset=utf-8"),
    "singbox": ("singbox.json", "application/json; charset=utf-8"),
    "sing-box": ("singbox.json", "application/json; charset=utf-8"),
    "base64": ("base64.txt", "text/plain; charset=utf-8"),
    "v2ray": ("v2ray.txt", "text/plain; charset=utf-8"),
    "v2rayn": ("v2ray.txt", "text/plain; charset=utf-8"),
    "v2rayng": ("v2ray.txt", "text/plain; charset=utf-8"),
    "v2raya": ("v2ray.txt", "text/plain; charset=utf-8"),
    "v2ray-json": ("v2ray.json", "application/json; charset=utf-8"),
    "v2ray_json": ("v2ray.json", "application/json; charset=utf-8"),
    "v2ray-config": ("v2ray.json", "application/json; charset=utf-8"),
}

_PROFILE_INTERVAL_FILES = frozenset({"mihomo.yaml", "v2ray.txt", "base64.txt"})


# ============================================================ 共享工具
class NoCacheStaticFiles(StaticFiles):
    """静态文件禁用浏览器缓存。"""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers.update(_NO_CACHE_HEADERS)
        return response


def _asset_version() -> str:
    """用文件 mtime 作为资源版本号。"""
    parts = []
    for name in ("app.js", "style.css"):
        try:
            parts.append(str(int((STATIC_DIR / name).stat().st_mtime)))
        except Exception:
            parts.append("0")
    return "-".join(parts)


# ============================================================ Web 服务 (8100)
async def _background_bootstrap() -> None:
    try:
        await spawn_pipeline()
    except Exception:
        logger.exception("首次流水线执行失败")
    try:
        start_scheduler()
    except Exception:
        logger.exception("调度器启动失败")
    try:
        await apply_proxy_config()
    except Exception:
        logger.exception("代理启动失败")


@asynccontextmanager
async def _web_lifespan(app: FastAPI):
    loop = asyncio.get_running_loop()
    set_log_broadcast(loop, state.broadcast_log)

    state.stage = TaskStage.WEB_READY.value
    state.message = "Web 服务已启动，后台初始化进行中..."
    await state.notify()
    logger.info("Web 服务就绪，开始后台初始化")

    task = asyncio.create_task(_background_bootstrap())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        try:
            await proxy_manager.stop()
        except Exception:
            logger.exception("停止内置代理失败")


def _sort_records(records: List[NodeRecord]) -> None:
    records.sort(
        key=lambda r: (
            r.latency_ms is None,
            r.latency_ms if r.latency_ms is not None else 10**9,
            -(r.speed_cps or 0),
        )
    )


def _validate_config(payload: Dict[str, Any]) -> None:
    """校验即将写回的配置。"""
    check = payload.get("check") or {}
    output = payload.get("output") or {}
    proxy = payload.get("proxy") or {}

    # ---- logging ----
    level = str((payload.get("logging") or {}).get("level") or "INFO").upper()
    if level not in _VALID_LOG_LEVELS:
        raise HTTPException(400, f"日志级别无效: {level}")

    # ---- check ----
    for key, label in (("concurrent", "节点并发数"), ("samples", "延迟采样次数")):
        try:
            v = int(check.get(key) or 0)
        except (TypeError, ValueError):
            raise HTTPException(400, f"{label}必须为整数")
        if v < 1:
            raise HTTPException(400, f"{label}必须 >= 1")

    try:
        timeout_ms = int(check.get("timeout_ms") or 0)
    except (TypeError, ValueError):
        raise HTTPException(400, "超时时间必须为整数")
    if timeout_ms < 100:
        raise HTTPException(400, "超时时间必须 >= 100 毫秒")

    try:
        max_valid = int(check.get("max_valid_nodes") or 0)
    except (TypeError, ValueError):
        raise HTTPException(400, "有效节点上限必须为整数")
    if max_valid < 0:
        raise HTTPException(400, "有效节点上限不能为负数")

    for key, label in (("latency_mode", "有效延迟判定"), ("speed_mode", "有效速度判定")):
        raw = check.get(key)
        if raw is None:
            continue
        if str(raw).strip().lower() not in ("all", "any"):
            raise HTTPException(400, f"{label}必须为 all 或 any")

    for key, label in (("latency_targets", "延迟目标"), ("speed_targets", "速度目标")):
        targets = check.get(key)
        if targets is None:
            continue
        if not isinstance(targets, list):
            raise HTTPException(400, f"{label}必须为列表")
        for i, t in enumerate(targets):
            if not isinstance(t, dict):
                raise HTTPException(400, f"{label}第 {i + 1} 项格式错误")
            if not str(t.get("url") or "").strip():
                raise HTTPException(400, f"{label}第 {i + 1} 项缺少 URL")

    schedule = str(check.get("schedule") or "").strip()
    if schedule:
        try:
            CronTrigger.from_crontab(schedule)
        except Exception as e:
            raise HTTPException(400, f"cron 表达式无效: {e}")

    # ---- output ----
    formats = output.get("formats")
    if formats is not None:
        if not isinstance(formats, list) or not formats:
            raise HTTPException(400, "输出格式不能为空")
        for f in formats:
            if not str(f).strip():
                raise HTTPException(400, "输出格式不能包含空值")

    # ---- proxy ----
    if not isinstance(proxy, dict):
        raise HTTPException(400, "proxy 段必须为对象")

    mode = str(proxy.get("mode") or "local").strip().lower()
    if mode not in _VALID_PROXY_MODES:
        raise HTTPException(400, "代理模式无效（应为 local / lan / remote）")

    for key, label in (("http_port", "代理 HTTP 端口"), ("socks_port", "代理 SOCKS 端口")):
        v = proxy.get(key)
        if v is None:
            continue
        try:
            port = int(v)
        except (TypeError, ValueError):
            raise HTTPException(400, f"{label}必须为整数")
        if not (1 <= port <= 65535):
            raise HTTPException(400, f"{label}必须在 1-65535 之间")

    if proxy.get("enabled") and mode == "remote":
        u = str(proxy.get("username") or "").strip()
        p = str(proxy.get("password") or "").strip()
        if not u or not p:
            raise HTTPException(400, "远程代理模式必须配置用户名与密码")


def create_web_app() -> FastAPI:
    app = FastAPI(title="Vael-Mux", version=__version__, lifespan=_web_lifespan)

    if STATIC_DIR.exists():
        app.mount(
            "/static",
            NoCacheStaticFiles(directory=str(STATIC_DIR)),
            name="static",
        )

    # ----------------------------------------------------- WebSocket 实时状态
    @app.websocket("/ws/status")
    async def ws_status(websocket: WebSocket) -> None:
        await websocket.accept()
        state.subscribers.append(websocket)
        try:
            await websocket.send_json({"event": "state", **state.snapshot()})
            for entry in get_recent_logs(LOG_BACKFILL):
                try:
                    await websocket.send_json({"event": "log", **entry})
                except Exception:
                    break
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            try:
                state.subscribers.remove(websocket)
            except ValueError:
                pass

    # ----------------------------------------------------- 页面
    @app.get("/")
    async def index():
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        v = _asset_version()
        html = html.replace('href="/static/style.css"', f'href="/static/style.css?v={v}"')
        html = html.replace('src="/static/app.js"', f'src="/static/app.js?v={v}"')
        return HTMLResponse(html, headers=_NO_CACHE_HEADERS)

    @app.get("/health")
    async def health():
        return {"status": "ok", "stage": state.stage}

    # ----------------------------------------------------- 版本信息
    @app.get("/api/version")
    async def get_version():
        return {"version": __version__}

    # ----------------------------------------------------- 全局状态
    @app.get("/api/state")
    async def get_state():
        return state.snapshot()

    # ----------------------------------------------------- 配置读取 / 保存
    @app.get("/api/config")
    async def get_config():
        cfg = load_base_config()
        clean = {k: v for k, v in cfg.items() if not str(k).startswith("_")}
        return JSONResponse(clean, headers=_NO_CACHE_HEADERS)

    @app.put("/api/config")
    async def put_config(payload: Dict[str, Any] = Body(...)):
        if not isinstance(payload, dict):
            raise HTTPException(400, "配置必须为对象")

        # 端口不再由配置管理：剥离 server 下的旧端口字段，避免写回
        server = payload.get("server")
        if isinstance(server, dict):
            server.pop("web_port", None)
            server.pop("api_port", None)

        _validate_config(payload)

        try:
            path = save_config(payload)
        except Exception as e:
            logger.exception("保存配置失败")
            raise HTTPException(500, f"保存配置失败: {e}")

        applied_level = apply_log_level()
        try:
            reload_scheduler()
        except Exception:
            logger.exception("调度器重载失败")

        try:
            await apply_proxy_config()
        except Exception:
            logger.exception("应用代理配置失败")

        logger.info(f"配置已保存到 {path}，日志级别：{applied_level}")

        await state.broadcast_event("nodes_updated")
        await state.broadcast_event("proxy_updated")

        return {
            "ok": True,
            "path": path,
            "message": f"配置已保存（日志级别 {applied_level}）。",
        }

    # ----------------------------------------------------- 检测控制
    @app.post("/api/trigger")
    async def trigger():
        if state.running:
            return JSONResponse(
                {"ok": False, "message": "已有任务正在运行"},
                status_code=409,
            )
        trigger_now()

        cfg = load_base_config()
        include_history = bool((cfg.get("check") or {}).get("include_history", False))
        msg = "已触发检测任务" + ("（含历史节点）" if include_history else "")
        return {"ok": True, "message": msg}

    @app.post("/api/stop")
    async def stop():
        if not state.running:
            return JSONResponse(
                {"ok": False, "message": "当前没有正在运行的任务"},
                status_code=409,
            )
        ok = await stop_pipeline()
        if ok:
            return {"ok": True, "message": "已请求停止，等待当前任务结束"}
        return JSONResponse(
            {"ok": False, "message": "停止请求未生效"},
            status_code=409,
        )

    # ----------------------------------------------------- 目标配置
    @app.get("/api/targets")
    async def get_targets():
        config = load_base_config()
        check_cfg = config.get("check", {})
        latency_targets = check_cfg.get("latency_targets", []) or []
        speed_targets = check_cfg.get("speed_targets", []) or []
        return {
            "latency_targets": latency_targets,
            "speed_targets": speed_targets,
            "latency_targets_enabled": enabled_targets(latency_targets),
            "speed_targets_enabled": enabled_targets(speed_targets),
            "latency_mode": normalize_mode(check_cfg.get("latency_mode"), "all"),
            "speed_mode": normalize_mode(check_cfg.get("speed_mode"), "all"),
            "include_history": bool(check_cfg.get("include_history", False)),
            "max_valid_nodes": state.max_valid_nodes,
        }

    # ----------------------------------------------------- 内置代理
    @app.get("/api/proxy")
    async def get_proxy():
        cfg = load_base_config()
        proxy_cfg = cfg.get("proxy") or {}
        return JSONResponse(
            {
                "config": proxy_cfg,
                "status": proxy_manager.status(),
                "binary_available": proxy_manager.binary_available(),
            },
            headers=_NO_CACHE_HEADERS,
        )

    @app.post("/api/proxy/restart")
    async def restart_proxy():
        cfg = load_base_config()
        proxy_cfg = cfg.get("proxy") or {}
        if not proxy_cfg.get("enabled"):
            return JSONResponse(
                {"ok": False, "message": "代理未启用（请先在配置中开启）"},
                status_code=409,
            )
        status = await apply_proxy_config(cfg)
        await state.broadcast_event("proxy_updated")
        if status.get("error"):
            return JSONResponse({"ok": False, "status": status}, status_code=500)
        return {"ok": True, "status": status}

    @app.post("/api/proxy/stop")
    async def stop_proxy():
        status = await proxy_manager.stop()
        await state.broadcast_event("proxy_updated")
        return {"ok": True, "status": status}

    # ----------------------------------------------------- 节点列表（后端分页）
    @app.get("/api/nodes")
    async def list_nodes(
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=500),
        search: str = Query("", max_length=200),
        filter: str = Query("", max_length=20),
    ):
        config = load_base_config()
        check_cfg = config.get("check", {})
        latency_targets = check_cfg.get("latency_targets", []) or []
        speed_targets = check_cfg.get("speed_targets", []) or []
        latency_mode = normalize_mode(check_cfg.get("latency_mode"), "all")
        speed_mode = normalize_mode(check_cfg.get("speed_mode"), "all")

        store = get_store()
        records = await store.all()

        q = search.strip().lower()
        f = filter.strip().lower()

        items: List[NodeRecord] = []
        for r in records:
            if not is_fully_valid(r, latency_targets, speed_targets, latency_mode, speed_mode):
                continue
            if f == "enabled" and not r.enabled:
                continue
            if f == "disabled" and r.enabled:
                continue
            if q and q not in f"{r.name} {r.server} {r.type}".lower():
                continue
            items.append(r)

        _sort_records(items)

        total = len(items)
        page_size = max(1, min(500, page_size))
        pages = max(1, (total + page_size - 1) // page_size)
        page = min(max(1, page), pages)
        start = (page - 1) * page_size
        end = start + page_size

        return JSONResponse(
            {
                "nodes": [r.to_dict() for r in items[start:end]],
                "total": total,
                "page": page,
                "page_size": page_size,
                "pages": pages,
                "latency_targets": latency_targets,
                "speed_targets": speed_targets,
                "latency_mode": latency_mode,
                "speed_mode": speed_mode,
            },
            headers=_NO_CACHE_HEADERS,
        )

    @app.get("/api/nodes/{node_id}")
    async def get_node(node_id: str):
        store = get_store()
        rec = await store.get(node_id)
        if rec is None:
            raise HTTPException(404, "节点不存在")
        return rec.to_dict()

    # ----------------------------------------------------- 新增节点
    @app.post("/api/nodes")
    async def create_node(payload: Dict[str, Any] = Body(...)):
        store = get_store()

        ptype = str(payload.get("type") or "").strip().lower()
        server = str(payload.get("server") or "").strip()
        try:
            port = int(payload.get("port") or 0)
        except (TypeError, ValueError):
            raise HTTPException(400, "port 必须为整数")

        if ptype not in CREATABLE_TYPES:
            raise HTTPException(400, f"不支持的协议类型: {ptype or '(空)'}")
        if not server:
            raise HTTPException(400, "服务器地址不能为空")
        if not (1 <= port <= 65535):
            raise HTTPException(400, "端口必须在 1-65535 之间")

        data: Dict[str, Any] = {k: v for k, v in payload.items() if k in PATCHABLE_FIELDS}
        data["type"] = ptype
        data["server"] = server
        data["port"] = port
        data.setdefault("enabled", True)
        data.setdefault("network", "tcp")

        if "alterId" in data:
            try:
                data["alterId"] = int(data["alterId"])
            except (TypeError, ValueError):
                data["alterId"] = 0
        for bkey in ("tls", "enabled", "insecure", "skip_cert_verify"):
            if bkey in data:
                data[bkey] = bool(data[bkey])

        rec = NodeRecord.from_dict(data)
        if not rec.name:
            rec.name = f"{ptype}-{server}:{port}"
        rec.ensure_id()
        rec.touch()

        if await store.get(rec.id) is not None:
            raise HTTPException(409, "节点已存在（类型 / 服务器 / 端口 / 凭证 完全一致）")

        await store.bulk_upsert([rec], preserve_user_state=False)
        await store.save()

        if rec.enabled:
            await retest_single(rec.id)

        await regenerate_subscriptions()
        await state.broadcast_event("nodes_updated")

        updated = await store.get(rec.id)
        return {"ok": True, "node": updated.to_dict() if updated else None}

    @app.patch("/api/nodes/{node_id}")
    async def patch_node(node_id: str, patch: Dict[str, Any] = Body(...)):
        store = get_store()
        if await store.get(node_id) is None:
            raise HTTPException(404, "节点不存在")

        filtered = {k: v for k, v in patch.items() if k in PATCHABLE_FIELDS}
        if not filtered:
            raise HTTPException(400, "无可更新字段")

        if "port" in filtered:
            try:
                filtered["port"] = int(filtered["port"])
            except (TypeError, ValueError):
                raise HTTPException(400, "port 必须为整数")
        if "alterId" in filtered:
            try:
                filtered["alterId"] = int(filtered["alterId"])
            except (TypeError, ValueError):
                raise HTTPException(400, "alterId 必须为整数")
        for bkey in ("tls", "enabled", "insecure", "skip_cert_verify"):
            if bkey in filtered:
                filtered[bkey] = bool(filtered[bkey])

        need_retest = any(k in RETEST_FIELDS for k in filtered)

        await store.update(node_id, filtered)

        if need_retest:
            await retest_single(node_id)
        else:
            await store.save()

        await regenerate_subscriptions()
        await state.broadcast_event("nodes_updated")

        updated = await store.get(node_id)
        return {
            "ok": True,
            "node": updated.to_dict() if updated else None,
            "retested": need_retest,
        }

    @app.delete("/api/nodes/{node_id}")
    async def delete_node(node_id: str):
        store = get_store()
        if not await store.delete(node_id):
            raise HTTPException(404, "节点不存在")
        await store.save()
        await regenerate_subscriptions()
        await state.broadcast_event("nodes_updated")
        return {"ok": True}

    @app.post("/api/nodes/{node_id}/enable")
    async def enable_node(node_id: str):
        store = get_store()
        if await store.get(node_id) is None:
            raise HTTPException(404, "节点不存在")
        await store.set_enabled([node_id], True)
        await store.save()
        await regenerate_subscriptions()
        await state.broadcast_event("nodes_updated")
        return {"ok": True}

    @app.post("/api/nodes/{node_id}/disable")
    async def disable_node(node_id: str):
        store = get_store()
        if await store.get(node_id) is None:
            raise HTTPException(404, "节点不存在")
        await store.set_enabled([node_id], False)
        await store.save()
        await regenerate_subscriptions()
        await state.broadcast_event("nodes_updated")
        return {"ok": True}

    @app.post("/api/nodes/{node_id}/retest")
    async def retest_node(node_id: str):
        rec = await retest_single(node_id)
        if rec is None:
            raise HTTPException(404, "节点不存在")
        await regenerate_subscriptions()
        await state.broadcast_event("nodes_updated")
        return {"ok": True, "node": rec.to_dict()}

    @app.post("/api/nodes/batch")
    async def batch_action(payload: Dict[str, Any] = Body(...)):
        ids: List[str] = payload.get("ids") or []
        action: str = payload.get("action") or ""
        if not ids:
            raise HTTPException(400, "ids 不能为空")
        if action not in ("enable", "disable", "delete", "retest"):
            raise HTTPException(400, f"未知操作: {action}")

        store = get_store()
        if action == "enable":
            await store.set_enabled(ids, True)
        elif action == "disable":
            await store.set_enabled(ids, False)
        elif action == "delete":
            await store.delete_many(ids)
        elif action == "retest":
            for nid in ids:
                await retest_single(nid)

        await store.save()
        await regenerate_subscriptions()
        await state.broadcast_event("nodes_updated")
        return {"ok": True, "count": len(ids), "action": action}

    return app


# ============================================================ API / 订阅服务 (8110)
def create_api_app() -> FastAPI:
    app = FastAPI(title="Vael-Mux API", version=__version__)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/api/status")
    async def status():
        return state.snapshot()

    @app.get("/api/formats")
    async def formats():
        return {
            "formats": sorted(FORMAT_MAP.keys()),
            "canonical": ["mihomo", "singbox", "base64", "v2ray", "v2ray-json"],
        }

    @app.get("/sub/{fmt}")
    async def get_sub(fmt: str):
        key = fmt.lower()
        if key not in FORMAT_MAP:
            raise HTTPException(status_code=404, detail=f"未知订阅格式: {fmt}")

        filename, media_type = FORMAT_MAP[key]
        config = load_base_config()
        out_dir = Path(config["output"].get("directory", "./output"))
        path = out_dir / filename

        if not path.exists():
            raise HTTPException(
                status_code=503,
                detail="订阅文件尚未生成，请等待首次检测完成",
            )

        headers = {"Cache-Control": "no-store"}
        if filename in _PROFILE_INTERVAL_FILES:
            headers["Profile-Update-Interval"] = "12"

        return FileResponse(
            str(path),
            media_type=media_type,
            filename=filename,
            headers=headers,
        )

    return app
