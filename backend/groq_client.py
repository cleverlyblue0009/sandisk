from __future__ import annotations

import json
import logging
import re
from typing import Any

from groq import Groq

logger = logging.getLogger(__name__)


class GroqClient:
    def __init__(self, api_key: str | None, query_model: str, summary_model: str) -> None:
        self.api_key = api_key
        self.query_model = query_model
        self.summary_model = summary_model
        self.enabled = bool(api_key)
        self.client = Groq(api_key=api_key) if self.enabled else None
        if not self.enabled:
            logger.warning("GROQ_API_KEY not set: running with deterministic fallback outputs")

    def _parse_json(self, payload: str) -> dict[str, Any]:
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", payload, re.DOTALL)
            if not match:
                return {}
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                return {}

    def _json_completion(self, model: str, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        if not self.enabled or self.client is None:
            return {}

        try:
            response = self.client.chat.completions.create(
                model=model,
                temperature=0.1,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )
            content = response.choices[0].message.content or "{}"
            return self._parse_json(content)
        except Exception:
            logger.exception("Groq completion failed")
            return {}

    def analyze_query(self, query: str) -> dict[str, Any]:
        system_prompt = (
            "You analyze search queries for a personal memory assistant. "
            "Return strict JSON with keys: intent (string), expanded_query (string), "
            "keywords (array of strings), time_hints (array of strings)."
        )
        user_prompt = (
            "Analyze this user query for semantic file retrieval.\n"
            f"Query: {query}\n"
            "Extract search intent, expand to likely academic/work terms, include concise keywords "
            "and any time hints."
        )

        parsed = self._json_completion(self.query_model, system_prompt, user_prompt)
        if parsed:
            return {
                "intent": str(parsed.get("intent", "")).strip(),
                "expanded_query": str(parsed.get("expanded_query", query)).strip() or query,
                "keywords": [str(keyword).strip() for keyword in parsed.get("keywords", []) if str(keyword).strip()],
                "time_hints": [
                    str(time_hint).strip()
                    for time_hint in parsed.get("time_hints", [])
                    if str(time_hint).strip()
                ],
            }

        fallback_keywords = [
            token.strip(" ,.!?").lower()
            for token in query.split()
            if len(token.strip(" ,.!?")) > 2
        ]
        return {
            "intent": "Fallback lexical intent extraction",
            "expanded_query": query,
            "keywords": fallback_keywords[:8],
            "time_hints": [],
        }

    def summarize_result(
        self,
        query: str,
        file_name: str,
        file_type: str,
        file_path: str,
        context_snippets: list[str],
    ) -> dict[str, str]:
        condensed_context = "\n".join(context_snippets)[:2400]
        system_prompt = (
            "You generate concise retrieval summaries for a semantic desktop search assistant. "
            "Return strict JSON with keys summary and explanation."
        )
        user_prompt = (
            f"User query: {query}\n"
            f"File name: {file_name}\n"
            f"File type: {file_type}\n"
            f"Path: {file_path}\n"
            f"Content snippets:\n{condensed_context}\n\n"
            "Write:\n"
            "1) summary: exactly 2 sentences summarizing likely file content.\n"
            "2) explanation: exactly 1 sentence explaining retrieval reason, mentioning semantics/keywords/recency."
        )

        parsed = self._json_completion(self.summary_model, system_prompt, user_prompt)
        summary = str(parsed.get("summary", "")).strip()
        explanation = str(parsed.get("explanation", "")).strip()

        if summary and explanation:
            return {"summary": summary, "explanation": explanation}

        fallback_summary = (
            f"{file_name} is a {file_type} file retrieved from your indexed workspace. "
            "It appears semantically related to the query based on stored content chunks."
        )
        fallback_explanation = (
            "This file was retrieved due to semantic overlap with your query and ranking signals from recency/keywords."
        )
        return {"summary": fallback_summary, "explanation": fallback_explanation}
