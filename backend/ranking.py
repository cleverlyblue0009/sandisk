from __future__ import annotations

import math
import time
from typing import Any


def semantic_scores_from_distances(best_distances: dict[int, float]) -> dict[int, float]:
    if not best_distances:
        return {}
    inverse_scores = {file_id: 1.0 / (1.0 + max(distance, 0.0)) for file_id, distance in best_distances.items()}
    max_inverse = max(inverse_scores.values()) or 1.0
    return {file_id: value / max_inverse for file_id, value in inverse_scores.items()}


def recency_score(modified_time: float, now: float | None = None, half_life_days: float = 30.0) -> float:
    now = now or time.time()
    seconds_old = max(0.0, now - modified_time)
    days_old = seconds_old / 86400.0
    if half_life_days <= 0:
        return 0.0
    decay = math.log(2) / half_life_days
    return math.exp(-decay * days_old)


def keyword_match_score(keywords: list[str], text_fields: list[str]) -> float:
    cleaned_keywords = [keyword.strip().lower() for keyword in keywords if keyword.strip()]
    if not cleaned_keywords:
        return 0.0

    searchable = " ".join(text_fields).lower()
    matched = sum(1 for keyword in set(cleaned_keywords) if keyword in searchable)
    return matched / max(1, len(set(cleaned_keywords)))


def rank_file_candidates(candidates: dict[int, dict[str, Any]], keywords: list[str]) -> list[dict[str, Any]]:
    if not candidates:
        return []

    best_distances = {
        file_id: min(float(distance) for distance in candidate["distances"])
        for file_id, candidate in candidates.items()
    }
    semantic_scores = semantic_scores_from_distances(best_distances)

    ranked: list[dict[str, Any]] = []
    now = time.time()
    for file_id, candidate in candidates.items():
        metadata = candidate["metadata"]
        semantic = semantic_scores.get(file_id, 0.0)
        recency = recency_score(float(metadata["modified_time"]), now=now)
        keyword = keyword_match_score(
            keywords,
            [
                str(metadata.get("filename", "")),
                str(metadata.get("path", "")),
                str(metadata.get("category", "")),
                str(metadata.get("extension", "")),
            ],
        )

        semantic_component = 0.65 * semantic
        recency_component = 0.25 * recency
        keyword_component = 0.10 * keyword
        final_score = semantic_component + recency_component + keyword_component

        ranked.append(
            {
                "file_id": file_id,
                "metadata": metadata,
                "top_chunks": candidate["chunks"],
                "distance_stats": {
                    "best_distance": min(candidate["distances"]),
                    "mean_distance": sum(candidate["distances"]) / len(candidate["distances"]),
                    "hit_count": len(candidate["distances"]),
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
                "final_score": final_score,
            }
        )

    ranked.sort(key=lambda item: item["final_score"], reverse=True)
    return ranked
