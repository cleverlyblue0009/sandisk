from __future__ import annotations

import copy
from collections import OrderedDict, defaultdict
from datetime import datetime, timezone
import json
import re
import threading
from typing import Any

import numpy as np

from config import Settings
from database import Database
from embedding import EmbeddingEngine, FaissStore
from groq_client import GroqClient
from ranking import rank_file_candidates
from utils import top_terms

_COSINE_CLUSTER_THRESHOLD = 0.72
_QUERY_STOP_WORDS = {
    "what",
    "which",
    "when",
    "where",
    "did",
    "does",
    "do",
    "for",
    "from",
    "about",
    "with",
    "used",
    "use",
    "work",
    "worked",
    "files",
    "file",
    "documents",
    "document",
    "notes",
    "papers",
    "show",
    "my",
    "the",
    "this",
    "that",
    "last",
    "today",
    "yesterday",
    "week",
    "month",
}


class RetrievalService:
    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
        embedding_engine: EmbeddingEngine,
        faiss_store: FaissStore,
        groq_client: GroqClient,
    ) -> None:
        self.settings = settings
        self.database = database
        self.embedding_engine = embedding_engine
        self.faiss_store = faiss_store
        self.groq_client = groq_client
        self._cache_lock = threading.RLock()
        self._result_cache: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._analysis_cache: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._embedding_cache: OrderedDict[str, np.ndarray] = OrderedDict()
        self._result_cache_size = 128
        self._analysis_cache_size = 256
        self._embedding_cache_size = 256

    def search(self, query: str, top_k: int | None = None, result_limit: int | None = None) -> dict[str, Any]:
        top_k = min(15, int(top_k or self.settings.default_top_k or 15))
        result_limit = int(result_limit or self.settings.default_result_limit)
        query_key = query.strip().lower()
        cache_key = f"{query_key}|k={top_k}|limit={result_limit}|n={self.faiss_store.ntotal}"

        cached = self._cache_get(self._result_cache, cache_key)
        if cached is not None:
            return copy.deepcopy(cached)

        analysis = self._analyze_query_cached(query)
        expanded_query = analysis.get("expanded_query", query) or query
        keywords = [str(item) for item in analysis.get("keywords", [])]

        # Query embedding -> FAISS nearest neighbors -> metadata join.
        query_vector = self._embed_query_cached(expanded_query)
        distances, faiss_ids = self.faiss_store.search(query_vector, top_k=top_k)
        hit_pairs = [
            (int(faiss_id), float(distance))
            for faiss_id, distance in zip(faiss_ids, distances)
            if int(faiss_id) != -1
        ]
        if not hit_pairs:
            payload = {
                "query": query,
                "expanded_query": expanded_query,
                "analysis": analysis,
                "results": [],
                "grouped_results": [],
            }
            self._cache_put(self._result_cache, cache_key, payload, self._result_cache_size)
            return payload

        hit_lookup = self.database.fetch_hits_by_faiss_ids([item[0] for item in hit_pairs])
        candidates: dict[int, dict[str, Any]] = {}
        for faiss_id, distance in hit_pairs:
            hit = hit_lookup.get(faiss_id)
            if not hit:
                continue

            file_id = int(hit["file_id"])
            bucket = candidates.setdefault(
                file_id,
                {
                    "metadata": {
                        "file_path": str(hit["file_path"]),
                        "file_name": str(hit["file_name"]),
                        "file_type": str(hit["file_type"]),
                        "extension": str(hit["extension"]),
                        "modified_time": float(hit["modified_time"]),
                        "cluster_id": hit.get("cluster_id"),
                        "cluster_label": hit.get("cluster_label") or "Unclustered",
                        "context_label": hit.get("context_label") or "General Study Material",
                        "summary": str(hit.get("summary") or ""),
                        "topics_json": str(hit.get("topics_json") or ""),
                    },
                    "distances": [],
                    "chunks": [],
                },
            )
            bucket["distances"].append(distance)
            if len(bucket["chunks"]) < 3:
                bucket["chunks"].append(
                    {
                        "chunk_id": int(hit["chunk_id"]),
                        "chunk_index": int(hit["chunk_index"]),
                        "content": str(hit["content"])[:700],
                    }
                )

        ranked = rank_file_candidates(candidates, keywords=keywords)[:result_limit]
        normalized_results = [self._normalize_result(item) for item in ranked]
        # Results are grouped by semantic cluster/topic for context-aware answers.
        grouped = self._group_by_cluster(normalized_results)
        context_clusters = self._build_semantic_context_clusters(query, normalized_results)

        payload = {
            "query": query,
            "expanded_query": expanded_query,
            "analysis": analysis,
            "results": normalized_results,
            "grouped_results": grouped,
            "context_clusters": context_clusters,
        }
        self._cache_put(self._result_cache, cache_key, payload, self._result_cache_size)
        return payload

    def _normalize_result(self, item: dict[str, Any]) -> dict[str, Any]:
        meta = item["metadata"]
        modified_ts = float(meta["modified_time"])
        explanation = (
            f"Matched by semantic content and ranked with recency/keyword signals. "
            f"Cluster: {meta.get('cluster_label')} ({meta.get('context_label')})."
        )
        return {
            "file_id": int(item["file_id"]),
            "file_name": str(meta["file_name"]),
            "file_path": str(meta["file_path"]),
            "file_type": str(meta["file_type"]),
            "extension": str(meta["extension"]),
            "summary": str(meta.get("summary") or ""),
            "topics": self._parse_topics(meta.get("topics_json")),
            "cluster_id": meta.get("cluster_id"),
            "cluster_label": str(meta.get("cluster_label") or "Unclustered"),
            "context_label": str(meta.get("context_label") or "General Study Material"),
            "modified_time": modified_ts,
            "modified_time_iso": datetime.fromtimestamp(modified_ts, tz=timezone.utc).isoformat(),
            "top_chunks": item["top_chunks"],
            "distance_stats": item["distance_stats"],
            "score_breakdown": item["score_breakdown"],
            "final_score": round(float(item["final_score"]), 4),
            "explanation": explanation,
        }

    def _parse_topics(self, topics_json: Any) -> list[str]:
        raw = str(topics_json or "").strip()
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
        except Exception:
            return []
        if not isinstance(parsed, list):
            return []
        return [str(topic) for topic in parsed if str(topic).strip()]

    def _analyze_query_cached(self, query: str) -> dict[str, Any]:
        key = query.strip().lower()
        cached = self._cache_get(self._analysis_cache, key)
        if cached is not None:
            return copy.deepcopy(cached)
        analysis = self.groq_client.analyze_query(query)
        self._cache_put(self._analysis_cache, key, analysis, self._analysis_cache_size)
        return analysis

    def _embed_query_cached(self, expanded_query: str) -> np.ndarray:
        key = expanded_query.strip().lower()
        cached = self._cache_get(self._embedding_cache, key)
        if cached is not None:
            return np.asarray(cached, dtype=np.float32)
        vector = self.embedding_engine.encode_texts([expanded_query])
        self._cache_put(self._embedding_cache, key, vector, self._embedding_cache_size)
        return vector

    def _cache_get(
        self,
        cache: OrderedDict[str, Any],
        key: str,
    ) -> Any | None:
        with self._cache_lock:
            value = cache.get(key)
            if value is None:
                return None
            cache.move_to_end(key)
            return value

    def _cache_put(
        self,
        cache: OrderedDict[str, Any],
        key: str,
        value: Any,
        max_size: int,
    ) -> None:
        with self._cache_lock:
            cache[key] = value
            cache.move_to_end(key)
            while len(cache) > max_size:
                cache.popitem(last=False)

    def _group_by_cluster(self, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[str, dict[str, Any]] = defaultdict(
            lambda: {
                "cluster_label": "",
                "context_label": "",
                "results": [],
                "best_score": 0.0,
            }
        )
        for result in results:
            key = f"{result['cluster_label']}::{result['context_label']}"
            group = grouped[key]
            group["cluster_label"] = result["cluster_label"]
            group["context_label"] = result["context_label"]
            group["results"].append(result)
            group["best_score"] = max(group["best_score"], float(result["final_score"]))

        ordered = sorted(grouped.values(), key=lambda item: item["best_score"], reverse=True)
        for group in ordered:
            group["results"].sort(key=lambda item: float(item["final_score"]), reverse=True)
        return ordered

    def _build_semantic_context_clusters(
        self,
        query: str,
        results: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not results:
            return []

        file_ids = [int(item["file_id"]) for item in results]
        rows = self.database.fetch_chunk_embeddings_for_files(file_ids)

        vectors_by_file: dict[int, list[np.ndarray]] = defaultdict(list)
        text_by_file: dict[int, list[str]] = defaultdict(list)
        for row in rows:
            file_id = int(row["file_id"])
            emb = row.get("embedding")
            if emb:
                vectors_by_file[file_id].append(np.frombuffer(emb, dtype=np.float32))
            content = str(row.get("content") or "").strip()
            if content:
                text_by_file[file_id].append(content[:550])

        mean_vectors: dict[int, np.ndarray] = {}
        for file_id, vecs in vectors_by_file.items():
            if not vecs:
                continue
            matrix = np.stack(vecs, axis=0)
            mean_vectors[file_id] = np.mean(matrix, axis=0).astype(np.float32)

        with_vectors = [item for item in results if int(item["file_id"]) in mean_vectors]
        without_vectors = [item for item in results if int(item["file_id"]) not in mean_vectors]
        groups: list[list[dict[str, Any]]] = []

        if with_vectors:
            matrix = np.stack([mean_vectors[int(item["file_id"])] for item in with_vectors], axis=0)
            norms = np.linalg.norm(matrix, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            normalized = matrix / norms
            similarity = normalized @ normalized.T

            parent = list(range(len(with_vectors)))

            def find(x: int) -> int:
                while parent[x] != x:
                    parent[x] = parent[parent[x]]
                    x = parent[x]
                return x

            def union(a: int, b: int) -> None:
                ra = find(a)
                rb = find(b)
                if ra != rb:
                    parent[rb] = ra

            for i in range(len(with_vectors)):
                for j in range(i + 1, len(with_vectors)):
                    if float(similarity[i, j]) >= _COSINE_CLUSTER_THRESHOLD:
                        union(i, j)

            grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
            for i, item in enumerate(with_vectors):
                grouped[find(i)].append(item)
            groups.extend(grouped.values())

        for item in without_vectors:
            groups.append([item])

        clustered: list[dict[str, Any]] = []
        for group in groups:
            ordered_docs = sorted(group, key=lambda item: float(item["final_score"]), reverse=True)
            cluster_name = self._infer_context_cluster_name(query, ordered_docs, text_by_file)
            topics = self._infer_context_cluster_topics(ordered_docs, text_by_file)
            clustered.append(
                {
                    "cluster_name": cluster_name,
                    "topics": topics,
                    "documents": ordered_docs,
                    "best_score": float(ordered_docs[0]["final_score"]),
                }
            )

        clustered.sort(key=lambda item: item["best_score"], reverse=True)
        for item in clustered:
            item.pop("best_score", None)
        return clustered

    def _infer_context_cluster_name(
        self,
        query: str,
        docs: list[dict[str, Any]],
        text_by_file: dict[int, list[str]],
    ) -> str:
        query_terms = [
            token
            for token in re.findall(r"[a-zA-Z][a-zA-Z0-9_+-]{2,}", query.lower())
            if token not in _QUERY_STOP_WORDS
        ]
        query_lower = query.lower()
        if query_terms and "lab" in query_lower:
            return f"{' '.join(query_terms[:2]).title()} Lab Work"
        if query_terms and any(word in query_lower for word in ("project", "hackathon", "prototype")):
            return f"{' '.join(query_terms[:2]).title()} Project Work"

        context_counts: dict[str, int] = {}
        for item in docs:
            ctx = str(item.get("context_label") or "").strip()
            if not ctx:
                continue
            context_counts[ctx] = context_counts.get(ctx, 0) + 1
        if context_counts:
            best_context = max(context_counts.items(), key=lambda kv: kv[1])[0]
            if best_context and best_context.lower() != "general study material":
                return best_context

        merged = [query]
        for item in docs:
            merged.extend(
                [
                    str(item.get("cluster_label") or ""),
                    str(item.get("context_label") or ""),
                    str(item.get("file_name") or ""),
                ]
            )
            merged.extend(text_by_file.get(int(item["file_id"]), [])[:2])

        terms = top_terms(" ".join(merged), limit=3)
        if terms:
            return " ".join(term.title() for term in terms[:2])
        return "Related Work"

    def _infer_context_cluster_topics(
        self,
        docs: list[dict[str, Any]],
        text_by_file: dict[int, list[str]],
    ) -> list[str]:
        merged = []
        for item in docs:
            merged.extend(
                [
                    str(item.get("file_name") or ""),
                    str(item.get("cluster_label") or ""),
                    str(item.get("context_label") or ""),
                ]
            )
            merged.extend(text_by_file.get(int(item["file_id"]), [])[:2])
            first_chunk = ((item.get("top_chunks") or [{}])[0]).get("content")
            if first_chunk:
                merged.append(str(first_chunk))
        terms = top_terms(" ".join(merged), limit=6)
        return [term.replace("_", " ").title() for term in terms if term.strip()]
