from __future__ import annotations

from typing import Dict, List

from ..config import AppConfig
from ..models import Node


def select_group_members(
    nodes: List[Node],
    scores: Dict[str, float],
    status: Dict[str, Dict],
    cfg: AppConfig,
) -> Dict[str, List[str]]:
    """
    根据路由配置和排序结果，为每个 group 挑选成员节点名称。
    返回 {group_name: [node_name, ...]}
    """
    alive = [n for n in nodes if (status.get(n.key) or {}).get("alive")]
    if not alive:
        return {g: [] for g in cfg.routing.groups}

    result: Dict[str, List[str]] = {}
    for gname, gcfg in cfg.routing.groups.items():
        if gcfg.strategy == "latency":
            sortd = sorted(alive, key=lambda n: (status.get(n.key) or {}).get("latency_ms") or 1e9)
        elif gcfg.strategy == "speed":
            sortd = sorted(alive, key=lambda n: -((status.get(n.key) or {}).get("download_mbps") or 0))
        elif gcfg.strategy == "round_robin":
            sortd = list(alive)
        else:  # weighted_score
            sortd = sorted(alive, key=lambda n: -(scores.get(n.key, 0.0)))
        result[gname] = [n.name for n in sortd[: max(1, gcfg.top_n)]]
    return result
