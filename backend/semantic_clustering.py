from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

import numpy as np
from sklearn.cluster import KMeans

from database import Database
from utils import top_terms

TOPIC_PATTERNS: dict[str, tuple[str, ...]] = {
    "Operating Systems": ("operating system", "os ", "deadlock", "scheduling", "kernel", "process"),
    "Software Engineering": ("software engineering", "uml", "srs", "design pattern", "agile", "testing"),
    "Machine Learning": ("machine learning", "neural", "regression", "classification", "dataset", "model"),
    "Personal Documents": ("resume", "bank", "aadhaar", "invoice", "personal", "certificate"),
}

EXAM_TERMS = {"exam", "midsem", "endsem", "quiz", "revision", "viva", "test", "prep"}
COURSEWORK_TERMS = {"assignment", "homework", "lab", "coursework", "tutorial", "worksheet"}
PROJECT_TERMS = {"project", "hackathon", "prototype", "implementation", "repo", "milestone"}


@dataclass
class _ClusterItem:
    file_id: int
    file_name: str
    file_path: str
    file_type: str
    modified_time: float
    text: str
    vector: np.ndarray


class SemanticClusteringEngine:
    def __init__(self, max_clusters: int = 8) -> None:
        self.max_clusters = max(2, max_clusters)

    def refresh_clusters(self, database: Database) -> dict[str, Any]:
        rows = database.fetch_text_file_embeddings()
        grouped: dict[int, dict[str, Any]] = defaultdict(
            lambda: {
                "file_name": "",
                "file_path": "",
                "file_type": "",
                "modified_time": 0.0,
                "texts": [],
                "vectors": [],
            }
        )

        for row in rows:
            file_id = int(row["file_id"])
            grouped[file_id]["file_name"] = str(row["file_name"])
            grouped[file_id]["file_path"] = str(row["file_path"])
            grouped[file_id]["file_type"] = str(row["file_type"])
            grouped[file_id]["modified_time"] = float(row["modified_time"])
            grouped[file_id]["texts"].append(str(row.get("content", "")))
            vector = np.frombuffer(row["embedding"], dtype=np.float32)
            grouped[file_id]["vectors"].append(vector)

        items: list[_ClusterItem] = []
        for file_id, payload in grouped.items():
            if not payload["vectors"]:
                continue
            mean_vector = np.mean(np.stack(payload["vectors"], axis=0), axis=0)
            merged_text = " ".join(payload["texts"])
            items.append(
                _ClusterItem(
                    file_id=file_id,
                    file_name=payload["file_name"],
                    file_path=payload["file_path"],
                    file_type=payload["file_type"],
                    modified_time=float(payload["modified_time"]),
                    text=merged_text,
                    vector=mean_vector.astype(np.float32),
                )
            )

        database.clear_all_clusters()
        if not items:
            return {"cluster_count": 0, "assigned_files": 0}
        if len(items) == 1:
            topic = self._infer_topic([items[0]])
            context = self._infer_context([items[0]])
            database.update_file_cluster(
                file_id=items[0].file_id,
                cluster_id=0,
                cluster_label=topic,
                context_label=context,
            )
            return {"cluster_count": 1, "assigned_files": 1}

        matrix = np.stack([item.vector for item in items], axis=0)
        n_clusters = min(self.max_clusters, max(2, int(math.sqrt(len(items)))))
        n_clusters = min(n_clusters, len(items))
        # KMeans is used here per requirements (alternative to HDBSCAN).
        model = KMeans(n_clusters=n_clusters, random_state=42, n_init=10)
        labels = model.fit_predict(matrix)

        cluster_to_items: dict[int, list[_ClusterItem]] = defaultdict(list)
        for item, cluster_id in zip(items, labels):
            cluster_to_items[int(cluster_id)].append(item)

        cluster_names: dict[int, tuple[str, str]] = {}
        for cluster_id, cluster_items in cluster_to_items.items():
            topic = self._infer_topic(cluster_items)
            context = self._infer_context(cluster_items)
            cluster_names[int(cluster_id)] = (topic, context)

        for item, cluster_id in zip(items, labels):
            topic, context = cluster_names[int(cluster_id)]
            database.update_file_cluster(
                file_id=item.file_id,
                cluster_id=int(cluster_id),
                cluster_label=topic,
                context_label=context,
            )

        return {"cluster_count": len(cluster_to_items), "assigned_files": len(items)}

    def _infer_topic(self, items: list[_ClusterItem]) -> str:
        text = " ".join(f"{item.file_name} {item.text}" for item in items).lower()
        scores: dict[str, int] = {}
        for topic, patterns in TOPIC_PATTERNS.items():
            scores[topic] = sum(text.count(pattern) for pattern in patterns)
        best_topic, best_score = max(scores.items(), key=lambda item: item[1])
        if best_score > 0:
            return best_topic

        terms = top_terms(text, limit=3)
        if terms:
            label = " ".join(term.title() for term in terms[:2])
            return label[:48]
        return "General Study Material"

    def _infer_context(self, items: list[_ClusterItem]) -> str:
        text = " ".join(f"{item.file_name} {item.file_path} {item.text}" for item in items).lower()
        words = set(re.findall(r"[a-zA-Z]{3,}", text))

        exam_hits = len(words & EXAM_TERMS)
        coursework_hits = len(words & COURSEWORK_TERMS)
        project_hits = len(words & PROJECT_TERMS)

        code_files = sum(1 for item in items if item.file_type == "code")
        code_ratio = code_files / max(1, len(items))

        recent_times = [item.modified_time for item in items]
        spread_days = 0.0
        if recent_times:
            spread_days = max(recent_times) - min(recent_times)
            spread_days /= 86400.0

        if exam_hits >= max(coursework_hits, project_hits) and exam_hits > 0:
            return "Exam Preparation"
        if project_hits > 0 and (code_ratio >= 0.3 or spread_days <= 21):
            return "Projects"
        if coursework_hits > 0:
            return "Coursework"
        if code_ratio >= 0.5:
            return "Projects"
        return "General Study Material"
