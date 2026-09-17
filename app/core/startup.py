"""后台流水线编排：拉取 -> 解析 -> 合并存储 -> 检测 -> 导出。"""

import asyncio
import logging
from datetime import datetime
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import load_base_config, parse_subscriptions
from app.core.checker import check_all, check_node_metrics
from app.core.exporter import export_all
from app.core.fetcher import fetch_all
from app.core.parser import parse_all
from app.core.store import NodeStore
from app.models import NodeRecord
from app.utils.state import TaskStage, state

logger = logging.getLogger("vael-mux.startup")

_pipeline_lock = asyncio.Lock()
_scheduler: AsyncIOScheduler | None = None
_store: NodeStore | None = None


# ------------------------------------------------------- 单例
def get_store(config: dict | None = None) -> NodeStore:
    global _store
    if _store is None:
        cfg = config or load_base_config()
        out_dir = Path(cfg["output"].get("directory", "./output"))
        _store = NodeStore(out_dir / "nodes.json")
    return _store


# ------------------------------------------------------- 选择要导出的节点
def select_exportable(records: list[NodeRecord], config: dict) -> list[NodeRecord]:
    alive = [r for r in records if r.enabled and r.latency_ms is not None]
    alive.sort(key=lambda r: (r.latency_ms or 10**9, -(r.speed_cps or 0)))
    max_n = int(config.get("output", {}).get("max_nodes", 0) or 0)
    if max_n > 0:
        alive = alive[:max_n]
    return alive


# ------------------------------------------------------- 导出（独立，供 UI 调用）
async def regenerate_subscriptions(config: dict | None = None) -> list[str]:
    cfg = config or load_base_config()
    store = get_store(cfg)
    records = await store.all()
    selected = select_exportable(records, cfg)
    await export_all([r.to_dict() for r in selected], cfg.get("output", {}))
    state.exported_nodes = len(selected)
    return [r.id for r in selected]


# ------------------------------------------------------- 单节点重测
async def retest_single(node_id: str) -> NodeRecord | None:
    cfg = load_base_config()
    store = get_store(cfg)
    rec = await store.get(node_id)
    if rec is None:
        return None

    check_cfg = cfg.get("check", {})
    metrics = await check_node_metrics(
        rec,
        timeout_s=int(check_cfg.get("timeout_ms", 5000)) / 1000.0,
        samples=int(check_cfg.get("samples", 3)),
        speed_test=bool(check_cfg.get("speed_test", True)),
        speed_duration_s=int(check_cfg.get("speed_duration_ms", 500)) / 1000.0,
    )
    await store.update_metrics(node_id, metrics)
    await store.save()
    return await store.get(node_id)


# ------------------------------------------------------- 主流水线
async def run_pipeline() -> None:
    if _pipeline_lock.locked():
        logger.info("流水线正在运行，跳过本次触发")
        return

    async with _pipeline_lock:
        config = load_base_config()
        urls = parse_subscriptions(config.get("subscriptions"))
        store = get_store(config)
        await store.ensure_loaded()

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
            if not urls:
                raise RuntimeError("未配置任何订阅源")

            # ---------- 拉取 ----------
            state.stage = TaskStage.FETCHING.value
            state.message = "正在拉取订阅源..."
            state.progress = 0.05
            await state.notify()
            raw_texts = await fetch_all(urls, progress_callback=_on_fetch_progress)

            # ---------- 解析 & 合并 ----------
            state.stage = TaskStage.PARSING.value
            state.message = "正在解析节点..."
            state.progress = 0.25
            await state.notify()
            parsed = parse_all(raw_texts)
            new_records = [NodeRecord.from_dict(item) for item in parsed]

            await store.bulk_upsert(new_records, preserve_user_state=True)
            all_records = await store.all()

            state.total_nodes = len(all_records)
            state.message = f"解析到 {len(new_records)} 个新节点，" f"存储中共 {len(all_records)} 个节点"
            await state.notify()
            logger.info(state.message)

            # ---------- 检测（仅启用节点） ----------
            state.stage = TaskStage.CHECKING.value
            to_check = [r for r in all_records if r.enabled]
            state.enabled_nodes = len(to_check)
            state.message = f"正在检测 {len(to_check)} 个启用节点..."
            await state.notify()

            check_cfg = config.get("check", {})
            alive = await check_all(
                to_check,
                concurrent=int(check_cfg.get("concurrent", 50)),
                timeout_ms=int(check_cfg.get("timeout_ms", 5000)),
                samples=int(check_cfg.get("samples", 3)),
                speed_test=bool(check_cfg.get("speed_test", True)),
                speed_duration_ms=int(check_cfg.get("speed_duration_ms", 500)),
                speed_concurrency=int(check_cfg.get("speed_concurrency", 10)),
                progress_callback=_on_check_progress,
            )
            await store.save()
            logger.info(f"检测完成: 可用 {len(alive)} / 检测 {len(to_check)}")

            # ---------- 导出 ----------
            state.stage = TaskStage.EXPORTING.value
            state.message = "正在生成订阅文件..."
            state.progress = 0.92
            await state.notify()

            all_records = await store.all()
            selected = select_exportable(all_records, config)
            await export_all([r.to_dict() for r in selected], config.get("output", {}))
            state.exported_nodes = len(selected)
            logger.info(f"已导出 {len(selected)} 个节点")

            state.stage = TaskStage.IDLE.value
            state.progress = 1.0
            state.running = False
            state.finished_at = datetime.now().isoformat(timespec="seconds")
            state.message = f"完成：可用 {len(alive)} / 检测 {len(to_check)}，" f"导出 {len(selected)} 个节点"
            await state.notify()
            await state.broadcast_event("nodes_updated")

            await _notify_webhook(config, len(alive), len(to_check))

        except Exception as e:
            logger.exception("流水线失败")
            state.stage = TaskStage.ERROR.value
            state.message = f"执行失败：{e}"
            state.running = False
            state.finished_at = datetime.now().isoformat(timespec="seconds")
            await state.notify()


async def _on_fetch_progress(done: int, total: int) -> None:
    state.fetched_subscriptions = done
    if total:
        state.progress = 0.05 + 0.20 * (done / total)
    await state.notify()


async def _on_check_progress(done: int, total: int, alive: int) -> None:
    state.checked_nodes = done
    state.alive_nodes = alive
    if total:
        state.progress = 0.30 + 0.60 * (done / total)
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
    asyncio.create_task(run_pipeline())
