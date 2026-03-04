from __future__ import annotations

from datetime import datetime
from typing import Any

from config import Settings
from database import Database
from embedding import EmbeddingEngine, FaissStore
from explanation import ExplanationService
from groq_client import GroqClient
from ranking import rank_file_candidates


class RetrievalService:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        embedding_engine: EmbeddingEngine,
        faiss_store: FaissStore,
        groq_client: GroqClient,
        explanation_service: ExplanationService,
    ) -> None:
        self.settings = settings
        self.database = database
        self.embedding_engine = embedding_engine
        self.faiss_store = faiss_store
        self.groq_client = groq_client
        self.explanation_service = explanation_service

    def search(self, query: str, top_k: int | None = None, result_limit: int | None = None) -> dict[str, Any]:
        top_k = top_k or self.settings.default_top_k
        result_limit = result_limit or self.settings.default_result_limit
        analysis = self.groq_client.analyze_query(query)
        expanded_query = analysis.get("expanded_query", query) or query
        keywords = analysis.get("keywords", [])

        query_embedding = self.embedding_engine.encode_texts([expanded_query])
        distances, faiss_ids = self.faiss_store.search(query_embedding, top_k=top_k)
        hit_pairs = [
            (faiss_id, distance)
            for faiss_id, distance in zip(faiss_ids, distances)
            if faiss_id != -1
        ]
        if not hit_pairs:
            return {
                "query": query,
                "analysis": analysis,
                "results": [],
            }

        unique_faiss_ids = [faiss_id for faiss_id, _ in hit_pairs]
        hit_lookup = self.database.fetch_hits_by_faiss_ids(unique_faiss_ids)

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
                        "path": hit["path"],
                        "filename": hit["filename"],
                        "extension": hit["extension"],
                        "category": hit["category"],
                        "modified_time": float(hit["modified_time"]),
                    },
                    "distances": [],
                    "chunks": [],
                },
            )
            bucket["distances"].append(float(distance))
            if len(bucket["chunks"]) < 3:
                bucket["chunks"].append(
                    {
                        "chunk_id": int(hit["chunk_id"]),
                        "chunk_index": int(hit["chunk_index"]),
                        "content": str(hit["content"])[:600],
                    }
                )

        ranked = rank_file_candidates(candidates, keywords=keywords)
        ranked = ranked[:result_limit]
        annotated = self.explanation_service.annotate_results(query, ranked)

        response_results: list[dict[str, Any]] = []
        for item in annotated:
            metadata = item["metadata"]
            response_results.append(
                {
                    "file_id": item["file_id"],
                    "path": metadata["path"],
                    "filename": metadata["filename"],
                    "extension": metadata["extension"],
                    "category": metadata["category"],
                    "modified_time": datetime.fromtimestamp(metadata["modified_time"]).isoformat(),
                    "summary": item.get("summary", ""),
                    "explanation": item.get("explanation", ""),
                    "top_chunks": item["top_chunks"],
                    "distance_stats": item["distance_stats"],
                    "score_breakdown": item["score_breakdown"],
                    "final_score": round(float(item["final_score"]), 4),
                }
            )

        return {
            "query": query,
            "analysis": analysis,
            "results": response_results,
        }
