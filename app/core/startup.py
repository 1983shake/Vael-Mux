"""后台流水线编排：拉取 -> 解析 -> 检测 -> 导出。"""

import asyncio
import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import load_base_config, parse_subscriptions
from app.core.checker import check_all
from app.core.exporter import export_all
from app.core.fetcher import fetch_all
from app.core.parser import parse_all
from app.utils.state import TaskStage, state

logger = logging.getLogger("vael-mux.startup")

_pipeline_lock = asyncio.Lock()
_scheduler: AsyncIOScheduler | None = None


async def run_pipeline() -> None:
    """执行一次完整流水线。若已有实例在运行则直接返回。"""
    if _pipeline_lock.locked():
        logger.info("流水线正在运行，跳过本次触发")
        return

    async with _pipeline_lock:
        config = load_base_config()
        urls = parse_subscriptions(config.get("subscriptions"))

        # 重置状态
        state.running = True
        state.started_at = datetime.now().isoformat(timespec="seconds")
        state.finished_at = None
        state.total_subscriptions = len(urls)
        state.fetched_subscriptions = 0
        state.total_nodes = 0
        state.checked_nodes = 0
        state.alive_nodes = 0
        state.progress = 0.0

        state.stage = TaskStage.CONFIG_LOADING.value
        state.message = f"配置加载完成，共 {len(urls)} 个订阅源"
        await state.notify()

        try:
            # ---------- 1. 拉取 ----------
            if not urls:
                raise RuntimeError("未配置任何订阅源")

            state.stage = TaskStage.FETCHING.value
            state.message = "正在拉取订阅源..."
            state.progress = 0.05
            await state.notify()

            raw_texts = await fetch_all(urls, progress_callback=_on_fetch_progress)
            logger.info(f"拉取完成: {len(raw_texts)}/{len(urls)} 个订阅源成功")

            # ---------- 2. 解析 ----------
            state.stage = TaskStage.PARSING.value
            state.message = "正在解析节点..."
            state.progress = 0.32
            await state.notify()

            nodes = parse_all(raw_texts)
            state.total_nodes = len(nodes)
            state.progress = 0.35
            state.message = f"解析到 {len(nodes)} 个节点（已去重）"
            await state.notify()
            logger.info(f"解析完成: {len(nodes)} 个节点")

            # ---------- 3. 检测 ----------
            state.stage = TaskStage.CHECKING.value
            state.message = f"正在检测 {len(nodes)} 个节点..."
            await state.notify()

            alive = await check_all(
                nodes,
                concurrent=int(config["check"].get("concurrent", 50)),
                timeout_ms=int(config["check"].get("timeout_ms", 5000)),
                progress_callback=_on_check_progress,
            )
            logger.info(f"检测完成: {len(alive)}/{len(nodes)} 个节点可用")

            # ---------- 4. 导出 ----------
            state.stage = TaskStage.EXPORTING.value
            state.message = "正在生成订阅文件..."
            state.progress = 0.92
            await state.notify()

            written = await export_all(alive, config.get("output", {}))
            logger.info(f"导出完成: {written}")

            # ---------- 5. 完成 ----------
            state.stage = TaskStage.IDLE.value
            state.progress = 1.0
            state.running = False
            state.finished_at = datetime.now().isoformat(timespec="seconds")
            state.message = f"完成：{len(alive)} / {len(nodes)} 个节点可用"
            await state.notify()

            await _notify_webhook(config, len(alive), len(nodes))

        except Exception as e:
            logger.exception("流水线执行失败")
            state.stage = TaskStage.ERROR.value
            state.message = f"执行失败：{e}"
            state.running = False
            state.finished_at = datetime.now().isoformat(timespec="seconds")
            await state.notify()


async def _on_fetch_progress(done: int, total: int) -> None:
    state.fetched_subscriptions = done
    if total:
        state.progress = 0.05 + 0.25 * (done / total)
    await state.notify()


async def _on_check_progress(done: int, total: int, alive: int) -> None:
    state.checked_nodes = done
    state.alive_nodes = alive
    if total:
        state.progress = 0.35 + 0.55 * (done / total)
    await state.notify()


async def _notify_webhook(config: dict, alive: int, total: int) -> None:
    url = (config.get("notify") or {}).get("webhook") or ""
    if not url:
        return
    try:
        import httpx

        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(url, json={"alive": alive, "total": total})
    except Exception as e:
        logger.warning(f"Webhook 通知失败: {e}")


def start_scheduler() -> None:
    """启动 cron 定时任务。重复调用安全。"""
    global _scheduler
    if _scheduler is not None:
        return

    config = load_base_config()
    schedule = (config.get("check") or {}).get("schedule") or ""
    if not schedule.strip():
        logger.info("未配置 schedule，跳过定时任务")
        return

    try:
        _scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
        _scheduler.add_job(
            run_pipeline,
            CronTrigger.from_crontab(schedule),
            id="vael-mux-pipeline",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        _scheduler.start()
        logger.info(f"调度器已启动，cron: {schedule}")
    except Exception as e:
        logger.error(f"调度器启动失败: {e}")


def trigger_now() -> None:
    """手动触发一次流水线（不阻塞）。"""
    asyncio.create_task(run_pipeline())
