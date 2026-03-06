from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from groq_client import GroqClient

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are a helpful personal memory assistant that analyzes a user's digital activity. "
    "Respond conversationally and sometimes include light humorous insights about the user's habits. "
    "Be friendly but concise. Do not fabricate information. Only use the provided data."
)


class AssistantPersonality:
    """Convert structured memory results into a natural-language assistant reply."""

    def __init__(self, groq_client: "GroqClient | None" = None) -> None:
        self.groq_client = groq_client

    def generate_response(self, *, user_query: str, structured_memory: dict[str, Any]) -> str:
        payload = json.dumps(structured_memory, ensure_ascii=False, indent=2)
        if self.groq_client is not None and self.groq_client.enabled and self.groq_client.client is not None:
            try:
                completion = self.groq_client.client.chat.completions.create(
                    model=self.groq_client.query_model,
                    temperature=0.4,
                    max_tokens=220,
                    messages=[
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {
                            "role": "user",
                            "content": (
                                "USER DATA:\n"
                                f"{payload}\n\n"
                                "USER QUESTION:\n"
                                f"{user_query}\n\n"
                                "INSTRUCTION:\n"
                                "Provide a helpful conversational response and optionally include "
                                "a short observation about the user's workflow."
                            ),
                        },
                    ],
                )
                content = (completion.choices[0].message.content or "").strip()
                if content:
                    return content
            except Exception:
                logger.exception("Groq assistant response generation failed")
        return self._fallback_response(user_query=user_query, structured_memory=structured_memory)

    def _fallback_response(self, *, user_query: str, structured_memory: dict[str, Any]) -> str:
        short_summary = str(structured_memory.get("short_summary") or "").strip()
        workflow = structured_memory.get("workflow_analysis") or {}
        documents = structured_memory.get("documents") or []
        youtube_analysis = structured_memory.get("youtube_analysis") or {}
        browser_sessions = structured_memory.get("browser_sessions") or []
        insights = workflow.get("insights") or []

        lines: list[str] = []
        if short_summary:
            lines.append(short_summary)
        elif user_query.strip():
            lines.append(f"Here is what I found for \"{user_query.strip()}\".")

        if documents:
            top_docs = ", ".join(str(item.get("file_name") or "") for item in documents[:2] if item.get("file_name"))
            if top_docs:
                lines.append(f"Relevant documents include {top_docs}.")

        top_categories = youtube_analysis.get("top_categories") or []
        if top_categories:
            readable = ", ".join(str(item.get("category") or "") for item in top_categories[:2] if item.get("category"))
            if readable:
                lines.append(f"Your recent YouTube history leans toward {readable}.")

        if not top_categories and browser_sessions:
            lines.append(f"I also found {len(browser_sessions)} related browser sessions.")

        if insights:
            lines.append(str(insights[0]))

        if not lines:
            return "I could not find enough activity to answer that yet."
        return " ".join(lines)
