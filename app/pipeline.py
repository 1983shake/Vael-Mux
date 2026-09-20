"""后台流水线编排：拉取 -> 解析 -> 检测 -> 导出 -> 清空重导入。

核心语义：
  - 只有流水线正常走到导出成功后，才清空 store 并写入本次有效节点
  - 若中途失败或被手动停止：store 保持不变（不导入任何新节点）
  - include_history=True 时，历史节点仅参与检测，不写入最终 store、不导出
  - 导出数量 = 本次订阅源中的有效节点数，由 check.max_valid_nodes 唯一决定
  - 目标开关（latency_targets / speed_targets 的 enabled 字段）
  - 判定模式（check.latency_mode / check.speed_mode：all | any）
  - 运行时应用日志级别（logging.level / VAEL_LOG_LEVEL）

去重提示：
  - 解析阶段：不同订阅源之间的重复节点
  - 构建阶段：同一批 uuid/password/name 生成相同 ID 的节点

取消语义：
  - 所有流水线入口都通过 spawn_pipeline() 启动，任务引用记录到 _current_task
  - stop_pipeline() 对该任务调用 .cancel()
"""

import asyncio
import logging
from datetime import datetime
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.checker import check_all, check_node_metrics, is_fully_valid
from app.exporter import export_all
from app.ingest import fetch_all, parse_all_with_stats
from app.models import NodeRecord, load_base_config, normalize_mode, parse_subscriptions
from app.runtime import TaskStage, apply_log_level, state
from app.store import NodeStore

logger = logging.getLogger("vael-mux.pipeline")

_pipeline_lock = asyncio.Lock()
_scheduler: AsyncIOScheduler | None = None
_store: NodeStore | None = None
_current_task: asyncio.Task | None = None

_MODE_LABEL = {"all": "全部通过", "any": "任一通过"}


# ============================================================ 任务启动 / 取消
def spawn_pipeline() -> asyncio.Task:
    """启动一次流水线任务，并把引用记录到 _current_task。

    所有流水线入口都必须走这里，否则 stop_pipeline() 找不到要取消的任务。
    """
    global _current_task
    _current_task = asyncio.create_task(run_pipeline())
    return _current_task


# ============================================================ store
def get_store(config: dict | None = None) -> NodeStore:
    global _store
    if _store is None:
        cfg = config or load_base_config()
        out_dir = Path(cfg["output"].get("directory", "./output"))
        _store = NodeStore(out_dir / "nodes.json")
    return _store


def _resolve_max_valid(config: dict) -> int:
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


def _resolve_modes(config: dict) -> tuple[str, str]:
    check_cfg = config.get("check", {}) or {}
    return (
        normalize_mode(check_cfg.get("latency_mode"), "all"),
        normalize_mode(check_cfg.get("speed_mode"), "all"),
    )


# ============================================================ 导出（供 Web 复用）
def select_exportable(records: list[NodeRecord], config: dict) -> list[NodeRecord]:
    check_cfg = config.get("check", {}) or {}
    latency_targets = check_cfg.get("latency_targets", []) or []
    speed_targets = check_cfg.get("speed_targets", []) or []
    latency_mode, speed_mode = _resolve_modes(config)

    valid = [r for r in records if r.enabled and is_fully_valid(r, latency_targets, speed_targets, latency_mode, speed_mode)]
    valid.sort(key=lambda r: (r.latency_ms or 10**9, -(r.speed_cps or 0)))
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


# ============================================================ 流水线
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
    lat = sum(1 for t in (check_cfg.get("latency_targets") or []) if t.get("enabled", True))
    spd = sum(1 for t in (check_cfg.get("speed_targets") or []) if t.get("enabled", True))
    return lat, spd


def _dedupe_records_by_id(records: list[NodeRecord]) -> tuple[list[NodeRecord], int]:
    """按 NodeRecord.id 去重，返回 (去重后的列表, 移除数)。"""
    seen = set()
    out: list[NodeRecord] = []
    for r in records:
        if r.id in seen:
            continue
        seen.add(r.id)
        out.append(r)
    return out, len(records) - len(out)


async def run_pipeline() -> None:
    if _pipeline_lock.locked():
        logger.info("流水线正在运行，跳过本次触发")
        return

    async with _pipeline_lock:
        config = load_base_config()

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
        latency_mode, speed_mode = _resolve_modes(config)

        state.reset_run(total_subscriptions=len(urls), max_valid=max_valid)
        state.stage = TaskStage.CONFIG_LOADING.value
        state.message = (
            f"配置加载完成，共 {len(urls)} 个订阅源"
            + ("（含历史节点测试）" if include_history else "")
            + f"，节点并发 {concurrent}，启用目标：延迟 {lat_n} 个 / 速度 {spd_n} 个，"
            + f"判定：延迟 {_MODE_LABEL[latency_mode]} / 速度 {_MODE_LABEL[speed_mode]}"
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

            # ---------- 解析 ----------
            _raise_if_stopped()
            state.stage = TaskStage.PARSING.value
            state.message = "正在解析节点..."
            state.progress = 0.22
            await state.notify()

            parsed, parse_stats = parse_all_with_stats(raw_texts)

            raw_records = [NodeRecord.from_dict(item) for item in parsed]
            new_records, id_removed = _dedupe_records_by_id(raw_records)
            if id_removed > 0:
                logger.info(f"节点去重（ID 冲突）：移除 {id_removed} 个" f"（{len(raw_records)} -> {len(new_records)}）")

            new_ids = {r.id for r in new_records}

            all_stored = await store.all()
            history_records = [r for r in all_stored if r.id not in new_ids] if include_history else []

            combined = [r for r in history_records if r.enabled] + [r for r in new_records if r.enabled]
            test_list = _dedupe_preserve_order(combined)
            test_removed = len(combined) - len(test_list)
            if test_removed > 0:
                logger.info(f"待测列表去重：移除 {test_removed} 个重复节点")

            state.total_nodes = len(test_list)

            dedup_hint = ""
            if parse_stats["removed"] > 0 or id_removed > 0:
                dedup_hint = f"；去重：移除 {parse_stats['removed']} 个源内重复" + (f" + {id_removed} 个 ID 冲突" if id_removed > 0 else "")

            if include_history and history_records:
                state.message = f"新节点 {len(new_records)} 个，" f"历史节点 {len(history_records)} 个，" f"合并待测 {len(test_list)} 个" + dedup_hint
            else:
                state.message = f"解析到 {len(new_records)} 个新节点，" f"启用待测 {len(test_list)} 个" + dedup_hint
            await state.notify()
            logger.info(state.message)

            if logger.isEnabledFor(logging.DEBUG):
                per_src = parse_stats.get("per_source") or []
                for i, n in enumerate(per_src):
                    logger.debug(f"  订阅源 #{i + 1}: 解析出 {n} 个节点")

            # ---------- 检测 ----------
            _raise_if_stopped()
            state.stage = TaskStage.CHECKING.value
            state.enabled_nodes = len(test_list)

            latency_targets = check_cfg.get("latency_targets", []) or []
            speed_targets = check_cfg.get("speed_targets", []) or []

            state.phase = "latency"
            state.message = (
                f"检测中：延迟 -> 速度（上限 {max_valid or '∞'}，"
                f"节点并发 {concurrent}，延迟 {lat_n} 个目标 [{_MODE_LABEL[latency_mode]}] / "
                f"速度 {spd_n} 个目标 [{_MODE_LABEL[speed_mode]}]）"
            )
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
                latency_mode=latency_mode,
                speed_mode=speed_mode,
            )

            logger.info(f"检测完成: 通过节点 {len(alive)} / 待测 {len(test_list)}")

            # ---------- 导出 ----------
            _raise_if_stopped()
            state.stage = TaskStage.EXPORTING.value
            state.message = "正在生成订阅文件..."
            state.progress = 0.92
            await state.notify()

            valid_list = [r for r in alive if r.id in new_ids]

            await export_all([r.to_dict() for r in valid_list], config.get("output", {}))

            state.exported_nodes = len(valid_list)
            state.speed_passed = len(valid_list)
            state.alive_nodes = len(valid_list)

            logger.info(f"已导出 {len(valid_list)} 个有效节点" + (f"（上限 max_valid_nodes={max_valid}）" if max_valid else ""))

            # ---------- 清空重导入 ----------
            await store.replace_all(valid_list, preserve_user_state=True)
            await store.save()
            logger.info(
                f"已更新节点列表: 导入 {len(valid_list)} 个有效节点"
                + (f"（上限 max_valid_nodes={max_valid}）" if max_valid else "")
                + (f"，历史节点 {len(history_records)} 个已移除" if include_history and history_records else "")
            )

            # ---------- 完成 ----------
            state.phase = ""
            state.limit_reached = bool(max_valid and len(valid_list) >= max_valid)
            state.stage = TaskStage.IDLE.value
            state.progress = 1.0
            state.running = False
            state.stop_requested = False
            state.finished_at = datetime.now().isoformat(timespec="seconds")

            parts = [
                f"有效延迟 {len(valid_list)} / {len(test_list)}",
                f"有效节点 {len(valid_list)}",
            ]
            if max_valid and len(valid_list) >= max_valid:
                parts.append(f"已达上限 {max_valid}")
            parts.append(f"导出 {len(valid_list)} 个")
            state.message = "完成：" + "，".join(parts)
            if parse_stats["removed"] > 0 or id_removed > 0:
                state.message += f"（去重 {parse_stats['removed'] + id_removed} 个）"

            await state.notify()
            await state.broadcast_event("nodes_updated")
            await _notify_webhook(config, len(valid_list), len(test_list))

        except asyncio.CancelledError:
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


# ============================================================ 调度器
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


def reload_scheduler() -> None:
    """配置热更新后重启调度器（schedule 变化时生效）。"""
    global _scheduler
    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
        except Exception:
            pass
        _scheduler = None
    start_scheduler()


async def _scheduled_run() -> None:
    try:
        await spawn_pipeline()
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("定时任务执行失败")


# ============================================================ 对外控制
def trigger_now() -> None:
    if state.running:
        logger.info("已有任务运行中，忽略本次触发")
        return
    spawn_pipeline()


async def stop_pipeline() -> bool:
    global _current_task

    if not state.running:
        return False

    state.stop_requested = True
    state.message = "正在停止..."
    await state.notify()

    task = _current_task
    if task is not None and not task.done():
        task.cancel()
        logger.info("已发送取消信号")
    else:
        logger.warning("未找到可取消的流水线任务引用（将在下个检查点退出）")

    return True
