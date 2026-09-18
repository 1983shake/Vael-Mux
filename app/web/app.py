"""Web 管理界面 (默认 8100)。"""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List

from fastapi import Body, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.config import enabled_targets, load_base_config
from app.core.checker import is_fully_valid
from app.core.startup import (
    get_store,
    regenerate_subscriptions,
    retest_single,
    run_pipeline,
    start_scheduler,
    stop_pipeline,
    trigger_now,
)
from app.models import NodeRecord
from app.utils.runtime import (
    LOG_BACKFILL,
    TaskStage,
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

# 修改这些字段会触发自动重测
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


class NoCacheStaticFiles(StaticFiles):
    """静态文件禁用浏览器缓存，确保前端更新后立即生效。"""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers.update(_NO_CACHE_HEADERS)
        return response


async def _background_bootstrap() -> None:
    try:
        await run_pipeline()
    except Exception:
        logger.exception("首次流水线执行失败")
    try:
        start_scheduler()
    except Exception:
        logger.exception("调度器启动失败")


@asynccontextmanager
async def lifespan(app: FastAPI):
    loop = asyncio.get_running_loop()
    set_log_broadcast(loop, state.broadcast_log)

    state.stage = TaskStage.WEB_READY.value
    state.message = "Web 服务已启动，后台初始化进行中..."
    await state.notify()
    logger.info("Web 服务就绪，开始后台初始化")

    task = asyncio.create_task(_background_bootstrap())
    yield
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


def _sort_records(records: List[NodeRecord]) -> None:
    records.sort(
        key=lambda r: (
            r.latency_ms is None,
            r.latency_ms if r.latency_ms is not None else 10**9,
            -(r.speed_cps or 0),
        )
    )


def _asset_version() -> str:
    """用文件 mtime 作为资源版本号，确保 app.js / style.css 更新后自动失效缓存。"""
    parts = []
    for name in ("app.js", "style.css"):
        try:
            parts.append(str(int((STATIC_DIR / name).stat().st_mtime)))
        except Exception:
            parts.append("0")
    return "-".join(parts)


def create_web_app() -> FastAPI:
    app = FastAPI(title="Vael-Mux", version=__version__, lifespan=lifespan)

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
            # 1) 当前状态
            await websocket.send_json({"event": "state", **state.snapshot()})
            # 2) 回填最近日志
            for entry in get_recent_logs(LOG_BACKFILL):
                try:
                    await websocket.send_json({"event": "log", **entry})
                except Exception:
                    break
            # 3) 保持连接（仅服务端推送）
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
        """动态注入资源版本号，避免浏览器缓存旧版 app.js / style.css。"""
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
            "include_history": bool(check_cfg.get("include_history", False)),
            "max_valid_nodes": state.max_valid_nodes,
        }

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

        store = get_store()
        records = await store.all()

        q = search.strip().lower()
        f = filter.strip().lower()

        items: List[NodeRecord] = []
        for r in records:
            if not is_fully_valid(r, latency_targets, speed_targets):
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
