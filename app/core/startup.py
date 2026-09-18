"""后台流水线编排：拉取 -> 解析 -> 检测 -> 导出 -> 清空重导入。

核心语义：
  - 只有流水线**正常走到导出成功**后，才清空 store 并写入本次订阅源节点
  - 若中途失败或被手动停止：store 保持不变（不导入任何新节点）
  - include_history=True 时，历史节点仅参与检测，不写入最终 store
  - 目标开关（latency_targets / speed_targets 的 enabled 字段）
  - 运行时应用日志级别（logging.level / VAEL_LOG_LEVEL）
"""

import asyncio
import logging
from datetime import datetime
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import load_base_config, parse_subscriptions
from app.core.checker import check_all, check_node_metrics, is_fully_valid
from app.core.exporter import export_all
from app.core.fetcher import fetch_all
from app.core.parser import parse_all
from app.core.store import NodeStore
from app.models import NodeRecord
from app.utils.logger import apply_log_level
from app.utils.state import TaskStage, state

logger = logging.getLogger("vael-mux.startup")

_pipeline_lock = asyncio.Lock()
_scheduler: AsyncIOScheduler | None = None
_store: NodeStore | None = None
_current_task: asyncio.Task | None = None


def get_store(config: dict | None = None) -> NodeStore:
    global _store
    if _store is None:
        cfg = config or load_base_config()
        out_dir = Path(cfg["output"].get("directory", "./output"))
        _store = NodeStore(out_dir / "nodes.json")
    return _store


def _resolve_max_valid(config: dict) -> int:
    """解析「有效节点」上限。

    优先读 max_valid_nodes；若为 0，则回退到旧字段
    （max_speed_nodes / max_latency_nodes / max_alive_nodes）以保持兼容。
    """
    check_cfg = config.get("check", {}) or {}

    def _int(key: str) -> int:
        try:
            return int(check_cfg.get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0

    v = _int("max_valid_nodes")
    if v > 0:
        return v
    return _int("max_speed_nodes") or _int("max_latency_nodes") or _int("max_alive_nodes")


def _resolve_include_history(config: dict) -> bool:
    raw = (config.get("check") or {}).get("include_history", False)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    if isinstance(raw, str):
        return raw.strip().lower() in ("true", "1", "yes", "y", "on")
    return False


def select_exportable(records: list[NodeRecord], config: dict) -> list[NodeRecord]:
    """选择导出节点：启用 + 有效延迟 + 有效速度。"""
    check_cfg = config.get("check", {}) or {}
    latency_targets = check_cfg.get("latency_targets", []) or []
    speed_targets = check_cfg.get("speed_targets", []) or []

    valid = [r for r in records if r.enabled and is_fully_valid(r, latency_targets, speed_targets)]
    valid.sort(key=lambda r: (r.latency_ms or 10**9, -(r.speed_cps or 0)))
    max_n = int(config.get("output", {}).get("max_nodes", 0) or 0)
    if max_n > 0:
        valid = valid[:max_n]
    return valid


async def regenerate_subscriptions(config: dict | None = None) -> list[str]:
    cfg = config or load_base_config()
    store = get_store(cfg)
    records = await store.all()
    selected = select_exportable(records, cfg)
    await export_all([r.to_dict() for r in selected], cfg.get("output", {}))
    state.exported_nodes = len(selected)
    return [r.id for r in selected]


async def retest_single(node_id: str) -> NodeRecord | None:
    cfg = load_base_config()
    store = get_store(cfg)
    rec = await store.get(node_id)
    if rec is None:
        return None

    check_cfg = cfg.get("check", {})
    latency_targets = check_cfg.get("latency_targets", []) or []

    metrics = await check_node_metrics(
        rec,
        timeout_s=int(check_cfg.get("timeout_ms", 5000)) / 1000.0,
        samples=int(check_cfg.get("samples", 3)),
        latency_targets=latency_targets,
    )
    await store.update_metrics(node_id, metrics)
    await store.save()
    return await store.get(node_id)


def _raise_if_stopped() -> None:
    if state.stop_requested:
        raise asyncio.CancelledError()


def _dedupe_preserve_order(records: list[NodeRecord]) -> list[NodeRecord]:
    seen = set()
    result = []
    for r in records:
        if r.id in seen:
            continue
        seen.add(r.id)
        result.append(r)
    return result


def _count_enabled(check_cfg: dict) -> tuple[int, int]:
    """统计启用的延迟/速度目标数。"""
    lat = sum(1 for t in (check_cfg.get("latency_targets") or []) if t.get("enabled", True))
    spd = sum(1 for t in (check_cfg.get("speed_targets") or []) if t.get("enabled", True))
    return lat, spd


async def run_pipeline() -> None:
    """执行一次完整流水线。

    流程：
      1. 拉取订阅源
      2. 解析节点（**不写入 store**）
      3. 检测（延迟 -> 速度），历史节点仅参与检测
      4. 导出（基于本次检测结果）
      5. 导出成功 → 清空 store，写入本次订阅源节点

    若中途失败或被手动停止：
      store 保持不变（旧节点列表完整保留，不导入新节点）
    """
    if _pipeline_lock.locked():
        logger.info("流水线正在运行，跳过本次触发")
        return

    async with _pipeline_lock:
        config = load_base_config()

        # 应用最新日志级别（支持热更新）
        applied = apply_log_level()
        logger.info(f"日志级别：{applied}")

        urls = parse_subscriptions(config.get("subscriptions"))
        store = get_store(config)
        await store.ensure_loaded()

        check_cfg = config.get("check", {}) or {}
        max_valid = _resolve_max_valid(config)
        include_history = _resolve_include_history(config)
        concurrent = int(check_cfg.get("concurrent", 50) or 50)
        lat_n, spd_n = _count_enabled(check_cfg)

        state.running = True
        state.stop_requested = False
        state.phase = ""
        state.started_at = datetime.now().isoformat(timespec="seconds")
        state.finished_at = None
        state.total_subscriptions = len(urls)
        state.fetched_subscriptions = 0
        state.total_nodes = 0
        state.checked_nodes = 0
        state.alive_nodes = 0
        state.enabled_nodes = 0
        state.exported_nodes = 0
        state.speed_total = 0
        state.speed_checked = 0
        state.speed_passed = 0
        state.max_valid_nodes = max_valid
        state.limit_reached = False

        state.stage = TaskStage.CONFIG_LOADING.value
        state.message = (
            f"配置加载完成，共 {len(urls)} 个订阅源"
            + ("（含历史节点测试）" if include_history else "")
            + f"，节点并发 {concurrent}，启用目标：延迟 {lat_n} 个 / 速度 {spd_n} 个"
        )
        await state.notify()
        logger.info(state.message)

        try:
            if not urls:
                raise RuntimeError("未配置任何订阅源")

            # ---------- 拉取 ----------
            _raise_if_stopped()
            state.stage = TaskStage.FETCHING.value
            state.message = "正在拉取订阅源..."
            state.progress = 0.05
            await state.notify()
            raw_texts = await fetch_all(urls, progress_callback=_on_fetch_progress)

            # ---------- 解析（不写入 store）----------
            _raise_if_stopped()
            state.stage = TaskStage.PARSING.value
            state.message = "正在解析节点..."
            state.progress = 0.22
            await state.notify()

            parsed = parse_all(raw_texts)
            new_records = [NodeRecord.from_dict(item) for item in parsed]

            # 判断历史节点（仅用于检测，不写入最终 store）
            all_stored = await store.all()
            new_ids = {r.id for r in new_records}
            history_records = [r for r in all_stored if r.id not in new_ids] if include_history else []

            history_enabled = [r for r in history_records if r.enabled]
            new_enabled = [r for r in new_records if r.enabled]
            test_list = _dedupe_preserve_order(history_enabled + new_enabled)

            state.total_nodes = len(test_list)

            if include_history and history_records:
                state.message = f"新节点 {len(new_records)} 个，" f"历史节点 {len(history_records)} 个，" f"合并待测 {len(test_list)} 个"
            else:
                state.message = f"解析到 {len(new_records)} 个新节点，" f"启用待测 {len(test_list)} 个"
            await state.notify()
            logger.info(state.message)

            # ---------- 检测（单节点流水线 + 节点并发）----------
            _raise_if_stopped()
            state.stage = TaskStage.CHECKING.value
            state.enabled_nodes = len(test_list)

            latency_targets = check_cfg.get("latency_targets", []) or []
            speed_targets = check_cfg.get("speed_targets", []) or []

            state.phase = "latency"
            state.checked_nodes = 0
            state.alive_nodes = 0
            state.speed_total = 0
            state.speed_checked = 0
            state.speed_passed = 0
            state.message = f"检测中：延迟 -> 速度（上限 {max_valid or '∞'}，" f"节点并发 {concurrent}，延迟目标 {lat_n} / 速度目标 {spd_n}）"
            await state.notify()

            alive = await check_all(
                test_list,
                concurrent=concurrent,
                timeout_ms=int(check_cfg.get("timeout_ms", 5000)),
                samples=int(check_cfg.get("samples", 3)),
                latency_targets=latency_targets,
                speed_targets=speed_targets,
                max_valid=max_valid,
                progress_callback=_on_check_progress,
                speed_progress_callback=_on_speed_progress,
            )

            logger.info(f"检测完成: 有效节点 {len(alive)} / 待测 {len(test_list)}")

            state.phase = ""
            state.limit_reached = bool(max_valid and len(alive) >= max_valid)
            state.speed_passed = len(alive)

            # ---------- 导出（基于本次检测结果，不依赖 store）----------
            _raise_if_stopped()
            state.stage = TaskStage.EXPORTING.value
            state.message = "正在生成订阅文件..."
            state.progress = 0.92
            await state.notify()

            selected = select_exportable(new_records, config)
            await export_all([r.to_dict() for r in selected], config.get("output", {}))
            state.exported_nodes = len(selected)
            logger.info(f"已导出 {len(selected)} 个节点")

            # ---------- 导出成功 → 清空 store，写入本次订阅源节点 ----------
            await store.replace_all(new_records, preserve_user_state=True)
            await store.save()
            logger.info(
                f"已更新节点列表: 本次导入 {len(new_records)} 个节点"
                + (f"（含 {len(history_records)} 个历史节点已移除）" if include_history and history_records else "")
            )

            # ---------- 完成 ----------
            state.stage = TaskStage.IDLE.value
            state.progress = 1.0
            state.running = False
            state.stop_requested = False
            state.finished_at = datetime.now().isoformat(timespec="seconds")

            parts = [
                f"有效延迟 {state.alive_nodes} / {len(test_list)}",
                f"有效节点 {len(alive)}",
            ]
            if max_valid and len(alive) >= max_valid:
                parts.append(f"已达上限 {max_valid}")
            parts.append(f"导出 {len(selected)} 个")
            state.message = "完成：" + "，".join(parts)

            await state.notify()
            await state.broadcast_event("nodes_updated")

            await _notify_webhook(config, len(alive), len(test_list))

        except asyncio.CancelledError:
            # 手动停止：**不修改 store**，保留原有节点列表
            logger.info("流水线已被手动停止，节点列表保持不变")

            state.stage = TaskStage.STOPPED.value
            state.message = "已手动停止（节点列表未变更）"
            state.running = False
            state.stop_requested = False
            state.phase = ""
            state.progress = 0.0
            state.finished_at = datetime.now().isoformat(timespec="seconds")

            try:
                asyncio.create_task(state.notify())
            except Exception:
                pass

            raise

        except Exception as e:
            # 失败：**不修改 store**，保留原有节点列表
            logger.exception("流水线失败，节点列表保持不变")

            state.stage = TaskStage.ERROR.value
            state.message = f"执行失败：{e}（节点列表未变更）"
            state.running = False
            state.stop_requested = False
            state.phase = ""
            state.finished_at = datetime.now().isoformat(timespec="seconds")
            await state.notify()


async def _on_fetch_progress(done: int, total: int) -> None:
    state.fetched_subscriptions = done
    if total:
        state.progress = 0.05 + 0.15 * (done / total)
    await state.notify()


async def _on_check_progress(done: int, total: int, valid: int) -> None:
    state.checked_nodes = done
    state.alive_nodes = valid
    if state.phase != "latency":
        state.phase = "latency"
    if total:
        state.progress = 0.25 + 0.45 * (done / total)
    await state.notify()


async def _on_speed_progress(done: int, valid: int) -> None:
    state.speed_checked = done
    state.speed_passed = valid
    if done > 0:
        state.phase = "speed"
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
            _scheduled_run,
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


async def _scheduled_run() -> None:
    global _current_task
    _current_task = asyncio.create_task(run_pipeline())
    try:
        await _current_task
    except asyncio.CancelledError:
        pass


def trigger_now() -> None:
    """立即触发一次流水线。include_history 从配置读取。"""
    global _current_task
    if state.running:
        logger.info("已有任务运行中，忽略本次触发")
        return
    _current_task = asyncio.create_task(run_pipeline())


async def stop_pipeline() -> bool:
    global _current_task
    if not state.running:
        return False

    state.stop_requested = True
    state.message = "正在停止..."
    await state.notify()

    if _current_task and not _current_task.done():
        _current_task.cancel()
    return True
