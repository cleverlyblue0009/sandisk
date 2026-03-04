from __future__ import annotations

from typing import Any

from groq_client import GroqClient


class ExplanationService:
    def __init__(self, groq_client: GroqClient, max_results: int | None = None) -> None:
        self.groq_client = groq_client
        self.max_results = max_results

    def annotate_results(self, query: str, ranked_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not ranked_results:
            return ranked_results

        for index, result in enumerate(ranked_results):
            if self.max_results is not None and index >= self.max_results:
                result["summary"] = "Summary omitted for lower-ranked result."
                result["explanation"] = "This result remains available due to score ranking."
                continue

            metadata = result["metadata"]
            snippet_payload = [chunk["content"] for chunk in result.get("top_chunks", [])][:3]
            llm_output = self.groq_client.summarize_result(
                query=query,
                file_name=str(metadata.get("filename", "")),
                file_type=str(metadata.get("category", "")),
                file_path=str(metadata.get("path", "")),
                context_snippets=snippet_payload,
            )
            result["summary"] = llm_output["summary"]
            result["explanation"] = llm_output["explanation"]
        return ranked_results
