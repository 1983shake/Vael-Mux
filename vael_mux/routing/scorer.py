from __future__ import annotations

from typing import Dict, List, Optional

from ..models import Node, NodeStatus


def compute_scores(
    nodes: List[Node],
    status: Dict[str, Dict],
    weights: Dict[str, float],
) -> Dict[str, float]:
    """加权评分：延迟(越低越好)+下载速度(越高越好)+稳定性(越高越好)，归一化到 0-100。"""
    if not nodes:
        return {}

    latencies = []
    speeds = []
    for n in nodes:
        s = status.get(n.key) or {}
        if s.get("latency_ms") and s.get("alive"):
            latencies.append(s["latency_ms"])
        if s.get("download_mbps"):
            speeds.append(s["download_mbps"])

    min_lat = min(latencies) if latencies else 0
    max_lat = max(latencies) if latencies else 1
    min_spd = 0
    max_spd = max(speeds) if speeds else 1
    if max_spd <= 0:
        max_spd = 1

    scores: Dict[str, float] = {}
    for n in nodes:
        s = status.get(n.key) or {}
        if not s.get("alive"):
            scores[n.key] = 0.0
            continue

        lat = s.get("latency_ms") or max_lat
        # 延迟归一化：越低越好
        if max_lat > min_lat:
            lat_score = 1.0 - (lat - min_lat) / (max_lat - min_lat)
        else:
            lat_score = 1.0

        spd = s.get("download_mbps") or 0.0
        spd_score = min(1.0, spd / max_spd) if max_spd > 0 else 0.0

        stab = s.get("stability", 0.0)

        score = (
            weights.get("latency", 0.5) * lat_score + weights.get("download_speed", 0.3) * spd_score + weights.get("stability", 0.2) * stab
        ) * 100.0

        scores[n.key] = round(score, 2)

    return scores


def sort_nodes(
    nodes: List[Node],
    status: Dict[str, Dict],
    scores: Dict[str, float],
    mode: str = "weighted_score",
) -> List[Node]:
    def key_latency(n: Node):
        s = status.get(n.key) or {}
        return s.get("latency_ms") or 999999

    def key_speed(n: Node):
        s = status.get(n.key) or {}
        return -(s.get("download_mbps") or 0.0)

    if mode == "latency":
        return sorted(nodes, key=key_latency)
    if mode == "speed":
        return sorted(nodes, key=key_speed)
    if mode == "round_robin":
        return list(nodes)
    # weighted_score
    return sorted(nodes, key=lambda n: -(scores.get(n.key, 0.0)))
