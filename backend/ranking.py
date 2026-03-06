from __future__ import annotations

import math
import time
from typing import Any

from utils import keyword_score

SEMANTIC_WEIGHT = 0.65
RECENCY_WEIGHT = 0.25
KEYWORD_WEIGHT = 0.10


def semantic_scores_from_distances(best_distances: dict[int, float]) -> dict[int, float]:
    if not best_distances:
        return {}
    inverse_scores = {
        file_id: 1.0 / (1.0 + max(0.0, float(distance)))
        for file_id, distance in best_distances.items()
    }
    max_inverse = max(inverse_scores.values()) or 1.0
    return {file_id: score / max_inverse for file_id, score in inverse_scores.items()}


def recency_score(modified_time: float, now: float | None = None, half_life_days: float = 21.0) -> float:
    now = now or time.time()
    age_seconds = max(0.0, now - float(modified_time))
    age_days = age_seconds / 86400.0
    if half_life_days <= 0:
        return 0.0
    decay = math.log(2) / half_life_days
    return math.exp(-decay * age_days)


def rank_file_candidates(candidates: dict[int, dict[str, Any]], keywords: list[str]) -> list[dict[str, Any]]:
    if not candidates:
        return []

    best_distances = {
        file_id: min(float(distance) for distance in payload["distances"])
        for file_id, payload in candidates.items()
    }
    semantic_lookup = semantic_scores_from_distances(best_distances)
    now = time.time()

    ranked: list[dict[str, Any]] = []
    for file_id, payload in candidates.items():
        meta = payload["metadata"]
        semantic = semantic_lookup.get(file_id, 0.0)
        recency = recency_score(float(meta["modified_time"]), now=now)
        keyword = keyword_score(
            keywords,
            " ".join(
                [
                    str(meta.get("file_name", "")),
                    str(meta.get("file_path", "")),
                    str(meta.get("file_type", "")),
                    str(meta.get("cluster_label", "")),
                    str(meta.get("context_label", "")),
                ]
            ),
        )

        semantic_component = SEMANTIC_WEIGHT * semantic
        recency_component = RECENCY_WEIGHT * recency
        keyword_component = KEYWORD_WEIGHT * keyword
        final_score = semantic_component + recency_component + keyword_component

        ranked.append(
            {
                "file_id": file_id,
                "metadata": meta,
                "top_chunks": payload["chunks"],
                "distance_stats": {
                    "best_distance": min(payload["distances"]),
                    "mean_distance": sum(payload["distances"]) / len(payload["distances"]),
                    "hit_count": len(payload["distances"]),
                },
                "score_breakdown": {
                    "semantic_score": round(semantic, 4),
                    "recency_score": round(recency, 4),
                    "keyword_match_score": round(keyword, 4),
                    "semantic_component": round(semantic_component, 4),
                    "recency_component": round(recency_component, 4),
                    "keyword_component": round(keyword_component, 4),
                    "final_score": round(final_score, 4),
                    "formula": "0.65*semantic + 0.25*recency + 0.10*keyword",
                },
                "final_score": float(final_score),
            }
        )

    ranked.sort(key=lambda item: item["final_score"], reverse=True)
    return ranked
