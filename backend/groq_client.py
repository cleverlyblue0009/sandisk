from __future__ import annotations

import json
import logging
import re
from typing import Any

from groq import Groq

logger = logging.getLogger(__name__)


class GroqClient:
    def __init__(self, api_key: str | None, query_model: str) -> None:
        self.api_key = api_key
        self.query_model = query_model
        self.enabled = bool(api_key)
        self.client = Groq(api_key=api_key) if self.enabled else None
        if not self.enabled:
            logger.warning("GROQ_API_KEY is missing. Falling back to deterministic query expansion.")

    def _parse_json(self, payload: str) -> dict[str, Any]:
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", payload, flags=re.DOTALL)
            if not match:
                return {}
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                return {}

    def analyze_query(self, query: str) -> dict[str, Any]:
        if not self.enabled or self.client is None:
            return self._fallback_analysis(query)

        system_prompt = (
            "You expand user memory-search queries for a semantic desktop assistant. "
            "Return strict JSON with keys: intent, expanded_query, keywords, time_hints."
        )
        user_prompt = (
            f"Query: {query}\n"
            "Expand the query for related terms and extract compact keywords. "
            "If a time phrase appears (yesterday, last month), include it in time_hints."
        )

        try:
            completion = self.client.chat.completions.create(
                model=self.query_model,
                temperature=0.1,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )
            content = completion.choices[0].message.content or "{}"
            parsed = self._parse_json(content)
            if not parsed:
                return self._fallback_analysis(query)
            return {
                "intent": str(parsed.get("intent", "")).strip(),
                "expanded_query": str(parsed.get("expanded_query", query)).strip() or query,
                "keywords": [str(k).strip() for k in parsed.get("keywords", []) if str(k).strip()],
                "time_hints": [str(t).strip() for t in parsed.get("time_hints", []) if str(t).strip()],
            }
        except Exception:
            logger.exception("Groq query expansion failed")
            return self._fallback_analysis(query)

    def _fallback_analysis(self, query: str) -> dict[str, Any]:
        tokens = [
            token.strip(" ,.!?").lower()
            for token in query.split()
            if len(token.strip(" ,.!?")) > 2
        ]
        return {
            "intent": "fallback-semantic-search",
            "expanded_query": query,
            "keywords": tokens[:10],
            "time_hints": [item for item in ("yesterday", "today", "last month", "recent") if item in query.lower()],
        }
