"""节点存储：持久化到 output/nodes.json。"""

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from app.models import NodeRecord

logger = logging.getLogger("vael-mux.store")


class NodeStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = asyncio.Lock()
        self._nodes: Dict[str, NodeRecord] = {}
        self._loaded = False

    async def ensure_loaded(self) -> None:
        if self._loaded:
            return
        async with self._lock:
            if self._loaded:
                return
            self._load_sync()
            self._loaded = True

    def _load_sync(self) -> None:
        if not self.path.exists():
            self._nodes = {}
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            raw_list = data.get("nodes", []) if isinstance(data, dict) else []
            out: Dict[str, NodeRecord] = {}
            for item in raw_list:
                try:
                    rec = NodeRecord.from_dict(item)
                    out[rec.id] = rec
                except Exception as e:
                    logger.warning(f"跳过无效节点: {e}")
            self._nodes = out
            logger.info(f"已从 {self.path} 载入 {len(out)} 个节点")
        except Exception as e:
            logger.error(f"读取节点存储失败: {e}")
            self._nodes = {}

    def _save_sync(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "nodes": [r.to_dict() for r in self._nodes.values()],
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.path)

    async def save(self) -> None:
        async with self._lock:
            self._save_sync()

    def save_sync(self) -> None:
        """同步保存，用于任务取消等无法 await 的场景。

        注意：不获取异步锁，调用方需确保此时没有并发的 save()。
        在流水线取消的清理阶段调用是安全的。
        """
        try:
            self._save_sync()
        except Exception as e:
            logger.error(f"同步保存节点失败: {e}")

    # ---------- 读取 ----------
    async def all(self) -> List[NodeRecord]:
        await self.ensure_loaded()
        async with self._lock:
            return list(self._nodes.values())

    async def get(self, node_id: str) -> Optional[NodeRecord]:
        await self.ensure_loaded()
        async with self._lock:
            return self._nodes.get(node_id)

    # ---------- 写入 ----------
    async def bulk_upsert(
        self,
        records: List[NodeRecord],
        preserve_user_state: bool = True,
    ) -> None:
        """批量 upsert。preserve_user_state=True 时保留用户对已有节点的修改
        以及上次的 targets 测试结果。"""
        async with self._lock:
            for rec in records:
                rec.ensure_id()
                existing = self._nodes.get(rec.id)
                if existing and preserve_user_state:
                    rec.name = existing.name or rec.name
                    rec.enabled = existing.enabled
                    rec.created_at = existing.created_at or rec.created_at
                    # 保留上次的 targets 结果
                    if existing.targets:
                        rec.targets = existing.targets
                elif not rec.created_at:
                    rec.created_at = datetime.now().isoformat(timespec="seconds")
                rec.touch()
                self._nodes[rec.id] = rec

    async def replace_all(
        self,
        records: List[NodeRecord],
        preserve_user_state: bool = True,
    ) -> None:
        """清空存储，用新记录整体替换（先清空，再导入）。

        preserve_user_state=True 时，对同 ID 的已有节点保留：
          - name      用户对节点显示名的自定义
          - enabled   用户对节点启用/禁用的选择
          - created_at 首次入库时间

        targets / latency_ms / speed_cps 等测试数据使用新记录中的本次检测结果。
        """
        await self.ensure_loaded()
        async with self._lock:
            old_map = self._nodes if preserve_user_state else {}
            new_map: Dict[str, NodeRecord] = {}
            now = datetime.now().isoformat(timespec="seconds")
            for rec in records:
                rec.ensure_id()
                old = old_map.get(rec.id)
                if old is not None:
                    rec.name = old.name or rec.name
                    rec.enabled = old.enabled
                    rec.created_at = old.created_at or rec.created_at or now
                elif not rec.created_at:
                    rec.created_at = now
                rec.touch()
                new_map[rec.id] = rec
            self._nodes = new_map

    async def update(self, node_id: str, patch: Dict) -> Optional[NodeRecord]:
        await self.ensure_loaded()
        async with self._lock:
            rec = self._nodes.get(node_id)
            if rec is None:
                return None
            for k, v in patch.items():
                if hasattr(rec, k) and k != "id":
                    setattr(rec, k, v)
            rec.touch()
            return rec

    async def update_metrics(self, node_id: str, metrics: Dict) -> Optional[NodeRecord]:
        async with self._lock:
            rec = self._nodes.get(node_id)
            if rec is None:
                return None
            for k, v in metrics.items():
                if hasattr(rec, k):
                    setattr(rec, k, v)
            rec.mark_checked()
            return rec

    async def delete(self, node_id: str) -> bool:
        await self.ensure_loaded()
        async with self._lock:
            return self._nodes.pop(node_id, None) is not None

    async def delete_many(self, ids: Iterable[str]) -> int:
        n = 0
        async with self._lock:
            for i in ids:
                if i in self._nodes:
                    del self._nodes[i]
                    n += 1
        return n

    async def set_enabled(self, ids: Iterable[str], enabled: bool) -> int:
        n = 0
        async with self._lock:
            for i in ids:
                rec = self._nodes.get(i)
                if rec is not None:
                    rec.enabled = enabled
                    rec.touch()
                    n += 1
        return n
