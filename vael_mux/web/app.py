from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from ..config import AppConfig
from ..database import Database
from ..kernel.mihomo import MihomoKernel
from ..logger import bus
from ..models import Node
from ..routing.engine import select_group_members
from ..routing.scorer import compute_scores, sort_nodes
from ..scheduler import PeriodicJob
from ..subscription.fetcher import fetch_subscription
from ..subscription.generator import (
    build_mihomo_config,
    dump_mihomo_yaml,
    dump_v2ray_base64,
)
from ..subscription.parser import parse_subscription, parse_uri
from ..testing.alive import test_alive_via_kernel
from ..testing.speed import test_speed_via_kernel

log = logging.getLogger("vael-mux.web")

STATIC_DIR = Path(__file__).parent / "static"


def create_app(cfg: AppConfig, cfg_path: Path) -> FastAPI:
    app = FastAPI(title="Vael-Mux", version="0.1.0")

    state: Dict[str, Any] = {
        "cfg": cfg,
        "cfg_path": cfg_path,
        "db": Database("./data/vael_mux.db"),
        "kernel": MihomoKernel(
            binary=cfg.kernel.binary,
            config_dir=cfg.kernel.config_dir,
            controller=cfg.kernel.external_controller,
            secret=cfg.kernel.secret,
        ),
        "scores": {},
        "last_refresh": None,
        "lock": asyncio.Lock(),
    }

    # ---------- lifecycle ----------

    @app.on_event("startup")
    async def _startup():
        loop = asyncio.get_running_loop()
        bus.bind_loop(loop)
        log.info("Vael-Mux starting")

        try:
            await refresh_all()
        except Exception as e:
            log.exception("initial refresh failed: %s", e)

        # 周期性测活
        async def job_alive():
            await refresh_alive_only()

        async def job_speed():
            await refresh_speed_only()

        state["job_alive"] = PeriodicJob("alive", cfg.alive_test.interval_seconds, job_alive)
        state["job_speed"] = PeriodicJob("speed", max(300, cfg.alive_test.interval_seconds * 2), job_speed)
        state["job_alive"].start()
        state["job_speed"].start()

    @app.on_event("shutdown")
    async def _shutdown():
        for k in ("job_alive", "job_speed"):
            job = state.get(k)
            if job:
                await job.stop()
        state["kernel"].stop()

    # ---------- core operations ----------

    async def refresh_subscriptions() -> int:
        cfg: AppConfig = state["cfg"]
        db: Database = state["db"]
        total = 0
        for src in cfg.subscriptions:
            if not src.enabled:
                continue
            nodes: List[Node] = []
            if src.url:
                text = await fetch_subscription(src.url, src.user_agent)
                if text:
                    nodes.extend(parse_subscription(text, source=src.name))
            for link in src.inline_nodes:
                n = parse_uri(link, source=src.name)
                if n:
                    nodes.append(n)
            # 覆盖式更新该来源
            db.clear_nodes(src.name)
            db.upsert_nodes(nodes)
            total += len(nodes)
            log.info("source '%s' -> %d nodes", src.name, len(nodes))
        return total

    async def apply_routing() -> None:
        cfg: AppConfig = state["cfg"]
        db: Database = state["db"]
        kernel: MihomoKernel = state["kernel"]

        nodes = db.all_nodes()
        status = db.all_status()
        scores = compute_scores(nodes, status, cfg.scoring.weights)
        state["scores"] = scores

        groups = select_group_members(nodes, scores, status, cfg)
        mihomo_cfg = build_mihomo_config(nodes, cfg, groups)
        path = kernel.write_config(mihomo_cfg)

        if cfg.proxy.enabled:
            if not kernel.is_running():
                kernel.start()
                # 给内核启动留出时间
                await asyncio.sleep(1.2)
            else:
                try:
                    await kernel.reload(path)
                except Exception as e:
                    log.warning("reload failed, restarting kernel: %s", e)
                    kernel.stop()
                    await asyncio.sleep(0.5)
                    kernel.start()
                    await asyncio.sleep(1.2)

    async def refresh_alive_only() -> None:
        cfg: AppConfig = state["cfg"]
        db: Database = state["db"]
        kernel: MihomoKernel = state["kernel"]
        if not cfg.alive_test.enabled:
            return
        nodes = db.all_nodes()
        if not nodes:
            return
        if not kernel.is_running():
            await apply_routing()
        log.info("alive test: %d nodes", len(nodes))
        results = await test_alive_via_kernel(
            kernel,
            nodes,
            urls=cfg.alive_test.urls,
            timeout=cfg.alive_test.timeout,
            concurrency=cfg.alive_test.concurrency,
        )
        for k, r in results.items():
            db.set_status(k, **r)
        log.info("alive test done")

    async def refresh_speed_only() -> None:
        cfg: AppConfig = state["cfg"]
        db: Database = state["db"]
        kernel: MihomoKernel = state["kernel"]
        if not cfg.speed_test.enabled:
            return
        # 只对 alive 的节点测速，最多前 30 个，避免过长
        nodes = [n for n in db.all_nodes() if (db.get_status(n.key) or {}).get("alive")]
        if not nodes:
            return
        nodes = nodes[:30]
        log.info("speed test: %d nodes", len(nodes))
        speeds = await test_speed_via_kernel(
            kernel,
            nodes,
            urls=cfg.speed_test.urls,
            mixed_port=cfg.proxy.mixed_port,
            timeout=cfg.speed_test.timeout,
        )
        for k, mbps in speeds.items():
            db.set_status(k, download_mbps=mbps)
        log.info("speed test done")

    async def refresh_all() -> None:
        async with state["lock"]:
            await refresh_subscriptions()
            await apply_routing()
            await refresh_alive_only()
            await refresh_speed_only()
            await apply_routing()
            state["last_refresh"] = time.time()

    state["refresh_all"] = refresh_all

    # ---------- REST ----------

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    @app.get("/api/config")
    async def get_config():
        return JSONResponse(state["cfg"].model_dump())

    @app.put("/api/config")
    async def put_config(payload: dict):
        try:
            new_cfg = AppConfig.model_validate(payload)
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))
        new_cfg.save(state["cfg_path"])
        state["cfg"] = new_cfg
        log.info("config updated & saved")
        return {"ok": True}

    @app.get("/api/nodes")
    async def get_nodes():
        db: Database = state["db"]
        nodes = db.all_nodes()
        status = db.all_status()
        scores = compute_scores(nodes, status, state["cfg"].scoring.weights)
        ordered = sort_nodes(nodes, status, scores, state["cfg"].scoring.sort_mode)
        return {
            "total": len(nodes),
            "sort_mode": state["cfg"].scoring.sort_mode,
            "nodes": [{**n.to_dict(), "status": status.get(n.key, {}), "score": scores.get(n.key, 0.0)} for n in ordered],
        }

    @app.post("/api/refresh")
    async def do_refresh():
        try:
            await state["refresh_all"]()
        except Exception as e:
            log.exception("refresh failed: %s", e)
            raise HTTPException(status_code=500, detail=str(e))
        return {"ok": True}

    @app.post("/api/test/alive")
    async def do_alive():
        await refresh_alive_only()
        await apply_routing()
        return {"ok": True}

    @app.post("/api/test/speed")
    async def do_speed():
        await refresh_speed_only()
        await apply_routing()
        return {"ok": True}

    @app.get("/api/kernel")
    async def kernel_info():
        kernel: MihomoKernel = state["kernel"]
        return {
            "running": kernel.is_running(),
            "version": await kernel.version(),
            "controller": kernel.controller,
            "binary": kernel.binary,
        }

    @app.get("/api/export/mihomo", response_class=PlainTextResponse)
    async def export_mihomo():
        db: Database = state["db"]
        nodes = db.all_nodes()
        status = db.all_status()
        scores = compute_scores(nodes, status, state["cfg"].scoring.weights)
        nodes = sort_nodes(nodes, status, scores, state["cfg"].scoring.sort_mode)
        return dump_mihomo_yaml(nodes, state["cfg"])

    @app.get("/api/export/v2ray", response_class=PlainTextResponse)
    async def export_v2ray():
        db: Database = state["db"]
        nodes = db.all_nodes()
        status = db.all_status()
        scores = compute_scores(nodes, status, state["cfg"].scoring.weights)
        nodes = sort_nodes(nodes, status, scores, state["cfg"].scoring.sort_mode)
        return dump_v2ray_base64(nodes)

    # ---------- WebSocket ----------

    @app.websocket("/ws/logs")
    async def ws_logs(ws: WebSocket):
        await ws.accept()
        q = bus.subscribe()
        try:
            # 先推历史
            for entry in bus.history():
                await ws.send_text(json.dumps({"type": "log", "data": entry}))
            while True:
                try:
                    entry = await asyncio.wait_for(q.get(), timeout=30)
                    await ws.send_text(json.dumps({"type": "log", "data": entry}))
                except asyncio.TimeoutError:
                    await ws.send_text(json.dumps({"type": "ping"}))
        except WebSocketDisconnect:
            pass
        except Exception as e:
            log.debug("ws logs error: %s", e)
        finally:
            bus.unsubscribe(q)

    # 挂静态文件
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    return app
