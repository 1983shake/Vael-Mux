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
        """批量 upsert。preserve_user_state=True 时保留用户对已有节点的修改。"""
        async with self._lock:
            for rec in records:
                rec.ensure_id()
                existing = self._nodes.get(rec.id)
                if existing and preserve_user_state:
                    rec.name = existing.name or rec.name
                    rec.enabled = existing.enabled
                    rec.created_at = existing.created_at or rec.created_at
                elif not rec.created_at:
                    rec.created_at = datetime.now().isoformat(timespec="seconds")
                rec.touch()
                self._nodes[rec.id] = rec

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
