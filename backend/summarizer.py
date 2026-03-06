"""Lightweight extractive document summariser and keyword extractor.

No external LLM required — uses frequency-based sentence scoring so the
system stays fully offline.  When a Groq client is provided the module
falls back to LLM-generated summaries for higher quality.
"""
from __future__ import annotations

import re
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from groq_client import GroqClient

from utils import top_terms

logger = logging.getLogger(__name__)


# ─── Extractive helpers ───────────────────────────────────────────────────────

_SENTENCE_SPLIT = re.compile(r'(?<=[.!?])\s+')
_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "are", "was", "were", "be", "been",
    "being", "have", "has", "had", "do", "does", "did", "will", "would",
    "could", "should", "may", "might", "shall", "that", "this", "these",
    "those", "it", "its", "they", "them", "their", "we", "our", "you",
    "your", "he", "she", "his", "her", "i", "me", "my", "not", "no",
    "as", "if", "then", "than", "also", "just", "more", "can", "all",
})


def _score_sentence(sentence: str, word_freq: dict[str, int]) -> float:
    """Score a sentence by the sum of its content-word frequencies."""
    words = re.findall(r'\b[a-z]{3,}\b', sentence.lower())
    content = [w for w in words if w not in _STOPWORDS]
    if not content:
        return 0.0
    return sum(word_freq.get(w, 0) for w in content) / len(content)


def summarize(
    text: str,
    max_sentences: int = 3,
    max_chars: int = 300,
) -> str:
    """Return a short extractive summary from *text*.

    Selects sentences with the highest content-word frequency scores,
    then returns them in their original order.
    """
    if not text or not text.strip():
        return ""

    # Sentence tokenisation
    raw = _SENTENCE_SPLIT.split(text.strip())
    sentences = [s.strip() for s in raw if len(s.strip()) > 25]
    if not sentences:
        return text.strip()[:max_chars]

    # Word frequency map over the whole text
    words = re.findall(r'\b[a-z]{3,}\b', text.lower())
    freq: dict[str, int] = {}
    for w in words:
        if w not in _STOPWORDS:
            freq[w] = freq.get(w, 0) + 1

    # Score and pick top sentences
    scored = sorted(
        enumerate(sentences),
        key=lambda idx_s: _score_sentence(idx_s[1], freq),
        reverse=True,
    )
    top_indices = sorted(i for i, _ in scored[:max_sentences])
    summary = " ".join(sentences[i] for i in top_indices)

    if len(summary) > max_chars:
        summary = summary[:max_chars].rsplit(" ", 1)[0] + "…"
    return summary


def extract_topics(text: str, limit: int = 5) -> list[str]:
    """Extract top *limit* topic terms from *text*, title-cased."""
    terms = top_terms(text, limit=limit)
    return [t.replace("_", " ").title() for t in terms]


# ─── Chunk-level helpers ─────────────────────────────────────────────────────

def chunks_to_summary(
    chunks: list[str],
    *,
    max_chars: int = 300,
    max_topics: int = 5,
    groq_client: "GroqClient | None" = None,
    file_name: str = "",
) -> tuple[str, list[str]]:
    """Derive (summary, topics) from a list of retrieved text chunks.

    Tries Groq LLM summarisation first (if client available and key set),
    falls back to extractive summarisation.

    Parameters
    ----------
    chunks:
        Ordered list of text chunk strings (most relevant first).
    max_chars:
        Maximum character length for the returned summary.
    max_topics:
        Maximum number of topic keywords to return.
    groq_client:
        Optional GroqClient; used for LLM summarisation when configured.
    file_name:
        Original file name — prepended to the text so LLM has context.
    """
    if not chunks:
        return "", []

    combined = " ".join(chunks)

    # ── Groq LLM summarisation (optional) ────────────────────────────────────
    if groq_client is not None and groq_client.enabled:
        try:
            summary = _groq_summarize(
                groq_client,
                combined,
                file_name=file_name,
                max_chars=max_chars,
            )
            topics = extract_topics(combined, limit=max_topics)
            return summary, topics
        except Exception:
            logger.debug("Groq summarisation failed — falling back to extractive", exc_info=True)

    # ── Extractive fallback ───────────────────────────────────────────────────
    summary = summarize(combined, max_sentences=3, max_chars=max_chars)
    topics = extract_topics(combined, limit=max_topics)
    return summary, topics


def _groq_summarize(
    client: "GroqClient",
    text: str,
    *,
    file_name: str,
    max_chars: int,
) -> str:
    """Call Groq to produce a one-paragraph document summary."""
    system = (
        "You summarise document excerpts for a personal memory assistant. "
        "Return ONLY the summary — no preamble, no labels. "
        f"Keep it under {max_chars} characters."
    )
    snippet = text[:2000]  # Avoid token limits
    user = f"File: {file_name}\n\nExcerpt:\n{snippet}\n\nSummarise what this document discusses."

    completion = client.client.chat.completions.create(  # type: ignore[union-attr]
        model=client.query_model,
        temperature=0.1,
        max_tokens=120,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return (completion.choices[0].message.content or "").strip()[:max_chars]
