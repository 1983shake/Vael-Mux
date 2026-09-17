"""Web 管理界面 (默认 8100)。"""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.core.startup import run_pipeline, start_scheduler, trigger_now
from app.utils.logger import logger
from app.utils.state import TaskStage, state
from app.web.ws import ws_router

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"


async def _background_bootstrap() -> None:
    """Web 就绪后执行：完整流水线 -> 启动调度器。"""
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
    # 阶段一：Web 先行 —— 立即标记就绪
    state.stage = TaskStage.WEB_READY.value
    state.message = "Web 服务已启动，后台初始化进行中..."
    await state.notify()
    logger.info("Web 服务就绪，开始后台初始化")

    # 阶段二：后台异步执行，不阻塞 Web
    task = asyncio.create_task(_background_bootstrap())

    yield

    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


def create_web_app() -> FastAPI:
    app = FastAPI(title="Vael-Mux", version="1.0.0", lifespan=lifespan)
    app.include_router(ws_router)

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/")
    async def index():
        return FileResponse(str(STATIC_DIR / "index.html"))

    @app.get("/health")
    async def health():
        return {"status": "ok", "stage": state.stage}

    @app.get("/api/state")
    async def get_state():
        return state.snapshot()

    @app.post("/api/trigger")
    async def trigger():
        if state.running:
            return JSONResponse(
                {"ok": False, "message": "已有任务正在运行"},
                status_code=409,
            )
        trigger_now()
        return {"ok": True, "message": "已触发检测任务"}

    return app
