from __future__ import annotations

import re
import threading
import time
from collections import defaultdict
from datetime import datetime
from typing import Any

from firebase_storage import FirebaseStorage
from insight_engine import InsightEngine
from utils import safe_duration

_CATEGORY_LABELS = {
    "coding": "Coding",
    "browsing": "Browsing",
    "documents": "Documents",
    "studying": "Studying",
    "communication": "Communication",
    "gaming": "Gaming",
    "other": "Other",
}

_QUESTION_STOPWORDS = {
    "what",
    "did",
    "i",
    "do",
    "for",
    "today",
    "yesterday",
    "this",
    "last",
    "month",
    "week",
    "how",
    "long",
    "was",
    "were",
    "my",
    "the",
    "on",
    "in",
    "a",
    "an",
    "to",
    "of",
    "documents",
    "document",
    "use",
    "used",
}


def _hhmm(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def _normalize_app_name(session: dict[str, Any]) -> str:
    app_name = str(session.get("app_name") or "").strip()
    process_name = str(session.get("process_name") or "").strip()
    if app_name and app_name.lower() != process_name.lower():
        return app_name
    if app_name:
        return app_name.removesuffix(".exe").replace("_", " ").title()
    if process_name:
        return process_name.removesuffix(".exe").replace("_", " ").title()
    return "Unknown App"


def _infer_days(question: str) -> int:
    q = question.lower()
    if re.search(r"\b(today|tonight|this morning|this afternoon)\b", q):
        return 1
    if "yesterday" in q:
        return 2
    if re.search(r"\b(this week|weekly|past week)\b", q):
        return 7
    if re.search(r"\b(last month|past month|this month)\b", q):
        return 30
    if re.search(r"\b(last year|this year)\b", q):
        return 365
    return 14


def _extract_query_terms(question: str, limit: int = 6) -> list[str]:
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9_+-]{2,}", question.lower())
    terms: list[str] = []
    seen: set[str] = set()
    for word in words:
        if word in _QUESTION_STOPWORDS or word in seen:
            continue
        seen.add(word)
        terms.append(word)
        if len(terms) >= limit:
            break
    return terms


def _session_text_blob(session: dict[str, Any]) -> str:
    fields = [
        session.get("app_name"),
        session.get("activity_name"),
        session.get("browser_name"),
        session.get("activity_type"),
        session.get("category"),
        session.get("domain"),
        session.get("document_name"),
        session.get("window_title"),
        session.get("video_title"),
        session.get("youtube_category"),
    ]
    return " ".join(str(item or "") for item in fields).lower()


class ActivityApiService:
    """API-ready activity timeline, ASK, and insights service."""

    def __init__(
        self,
        *,
        storage: FirebaseStorage,
        insight_engine: InsightEngine,
        user_id: str,
    ) -> None:
        self.storage = storage
        self.insight_engine = insight_engine
        self.user_id = user_id

    def get_timeline(self, days: int = 14) -> dict[str, Any]:
        days = max(1, int(days))
        start_time = time.time() - (days * 86400)
        sessions = self.storage.fetch_sessions(
            user_id=self.user_id,
            start_time=start_time,
            limit=5000,
        )

        buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for session in sessions:
            start_ts = float(session.get("start_time") or 0.0)
            end_ts = float(session.get("end_time") or start_ts)
            duration_seconds = max(0.0, float(session.get("duration_seconds") or end_ts - start_ts))
            date = str(session.get("date") or datetime.fromtimestamp(start_ts).strftime("%Y-%m-%d"))
            category = str(session.get("category") or "other").lower()
            app_name = _normalize_app_name(session)
            domain = str(session.get("domain") or "").strip()
            document_name = str(session.get("document_name") or "").strip()
            video_title = str(session.get("video_title") or "").strip()

            display_name = app_name
            if video_title:
                display_name = f"YouTube: {video_title} ({session.get('browser_name') or app_name})"
            elif str(session.get("activity_name") or "").strip():
                display_name = str(session.get("activity_name") or "").strip()

            buckets[date].append(
                {
                    "session_id": str(session.get("session_id") or ""),
                    "start_time": _hhmm(start_ts) if start_ts > 0 else "--:--",
                    "end_time": _hhmm(end_ts) if end_ts > 0 else "--:--",
                    "start_timestamp": start_ts,
                    "end_timestamp": end_ts,
                    "duration_seconds": duration_seconds,
                    "duration": safe_duration(duration_seconds),
                    "app_name": display_name,
                    "category": _CATEGORY_LABELS.get(category, category.title() or "Other"),
                    "domain": domain,
                    "document_name": document_name,
                    "activity_type": str(session.get("activity_type") or ""),
                    "youtube_category": str(session.get("youtube_category") or ""),
                }
            )

        timeline: list[dict[str, Any]] = []
        for date in sorted(buckets.keys(), reverse=True):
            entries = sorted(buckets[date], key=lambda item: float(item["start_timestamp"]))
            timeline.append({"date": date, "entries": entries})

        return {
            "banner": "Your Computer Memory",
            "days": days,
            "timeline": timeline,
            "total_sessions": len(sessions),
        }

    def ask(self, *, question: str) -> dict[str, Any]:
        normalized_q = (question or "").strip()
        days = _infer_days(normalized_q)
        start_time = time.time() - (days * 86400)
        sessions = self.storage.fetch_sessions(
            user_id=self.user_id,
            start_time=start_time,
            limit=5000,
        )
        if not sessions:
            return {
                "question": normalized_q,
                "answer": "No activity sessions were found for that time range yet.",
                "matches": [],
                "days": days,
            }

        q_lower = normalized_q.lower()
        terms = _extract_query_terms(normalized_q)

        matches = self._find_question_matches(q_lower=q_lower, terms=terms, sessions=sessions)
        if not matches and re.search(r"\bwhat did i do\b", q_lower):
            # Fallback to broad period summary instead of empty response.
            matches = sessions[:]

        answer = self._build_answer(question=normalized_q, q_lower=q_lower, days=days, matches=matches)
        serialized_matches = [self._serialize_match(item) for item in matches[:20]]
        return {
            "question": normalized_q,
            "answer": answer,
            "matches": serialized_matches,
            "days": days,
        }

    def get_insights(self, days: int = 14) -> dict[str, Any]:
        days = max(1, int(days))
        start_time = time.time() - (days * 86400)
        sessions = self.storage.fetch_sessions(
            user_id=self.user_id,
            start_time=start_time,
            limit=6000,
        )
        today = datetime.now().strftime("%Y-%m-%d")
        summary_cards = self.insight_engine.daily_summary_cards(sessions, target_date=today)
        insights = self.insight_engine.generate_insights(sessions)
        if insights:
            thread = threading.Thread(
                target=self.storage.save_insights,
                kwargs={"user_id": self.user_id, "insights": insights, "period": f"{days}d"},
                daemon=True,
                name="save-insights",
            )
            thread.start()
        return {
            "days": days,
            "summary_cards": summary_cards,
            "insights": insights,
        }

    def _find_question_matches(
        self,
        *,
        q_lower: str,
        terms: list[str],
        sessions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if "youtube" in q_lower:
            filtered = [
                item for item in sessions
                if "youtube" in _session_text_blob(item)
            ]
            return self._sort_by_time_desc(filtered)

        if re.search(r"\b(play|played|playing|game|gaming)\b", q_lower):
            filtered = [
                item for item in sessions
                if str(item.get("category") or "").lower() == "gaming"
            ]
            return self._sort_by_time_desc(filtered)

        if re.search(r"\b(code|coding|program|programming|debug)\b", q_lower):
            filtered = [
                item for item in sessions
                if str(item.get("category") or "").lower() == "coding"
            ]
            return self._sort_by_time_desc(filtered)

        if re.search(r"\b(document|documents|pdf|docx|study|paper|notes?)\b", q_lower):
            filtered = [
                item
                for item in sessions
                if str(item.get("document_name") or "").strip()
                or str(item.get("category") or "").lower() in {"documents", "studying"}
            ]
            if terms:
                filtered = [
                    item for item in filtered
                    if any(term in _session_text_blob(item) for term in terms)
                ]
            return self._sort_by_time_desc(filtered)

        if not terms:
            return self._sort_by_time_desc(sessions)
        filtered = [
            item for item in sessions
            if any(term in _session_text_blob(item) for term in terms)
        ]
        return self._sort_by_time_desc(filtered)

    def _build_answer(
        self,
        *,
        question: str,
        q_lower: str,
        days: int,
        matches: list[dict[str, Any]],
    ) -> str:
        if not matches:
            return f"I could not find matching activity for \"{question}\" in the last {days} days."

        total_seconds = sum(float(item.get("duration_seconds") or 0.0) for item in matches)
        total_duration = safe_duration(total_seconds)

        if "how long" in q_lower and "youtube" in q_lower:
            return f"You watched YouTube for {total_duration} in the selected period."
        if "how long" in q_lower and re.search(r"\b(code|coding|program)\b", q_lower):
            return f"You coded for {total_duration} in the selected period."
        if "how long" in q_lower:
            return f"You spent {total_duration} on the matched activity."

        if re.search(r"\b(play|played|playing|game|gaming)\b", q_lower):
            games = self._top_labels(matches, field="app_name", limit=3)
            if games:
                return f"You played {', '.join(games)} for a total of {total_duration}."
            return f"You played games for {total_duration}."

        if re.search(r"\b(document|documents|pdf|docx|paper|notes?)\b", q_lower):
            docs = self._top_labels(matches, field="document_name", limit=5)
            if docs:
                return f"You used these documents: {', '.join(docs)}."
            return f"I found document-related activity totaling {total_duration}."

        if re.search(r"\bwhat did i do\b", q_lower):
            by_category: dict[str, float] = defaultdict(float)
            for item in matches:
                category = str(item.get("category") or "other").lower()
                by_category[category] += float(item.get("duration_seconds") or 0.0)
            top = sorted(by_category.items(), key=lambda kv: kv[1], reverse=True)[:3]
            parts = [
                f"{_CATEGORY_LABELS.get(cat, cat.title())}: {safe_duration(sec)}"
                for cat, sec in top
            ]
            if parts:
                return f"Here is your activity summary: {'; '.join(parts)}."
            return f"You were active for {total_duration}."

        top_apps = self._top_labels(matches, field="app_name", limit=4)
        if top_apps:
            return f"I found {len(matches)} matching sessions. Main apps: {', '.join(top_apps)}."
        return f"I found {len(matches)} matching sessions totaling {total_duration}."

    def _serialize_match(self, session: dict[str, Any]) -> dict[str, Any]:
        start_ts = float(session.get("start_time") or 0.0)
        end_ts = float(session.get("end_time") or start_ts)
        duration_seconds = max(0.0, float(session.get("duration_seconds") or end_ts - start_ts))
        return {
            "session_id": str(session.get("session_id") or ""),
            "app_name": _normalize_app_name(session),
            "activity_name": str(session.get("activity_name") or ""),
            "process_name": str(session.get("process_name") or ""),
            "category": str(session.get("category") or "other"),
            "start_time": start_ts,
            "end_time": end_ts,
            "duration_seconds": duration_seconds,
            "duration": safe_duration(duration_seconds),
            "document_name": str(session.get("document_name") or ""),
            "domain": str(session.get("domain") or ""),
            "video_title": str(session.get("video_title") or ""),
            "youtube_category": str(session.get("youtube_category") or ""),
        }

    def _top_labels(
        self,
        sessions: list[dict[str, Any]],
        *,
        field: str,
        limit: int,
    ) -> list[str]:
        score: dict[str, float] = defaultdict(float)
        for item in sessions:
            raw = str(item.get(field) or "").strip()
            if not raw:
                continue
            score[raw] += float(item.get("duration_seconds") or 0.0)
        ranked = sorted(score.items(), key=lambda kv: kv[1], reverse=True)
        return [label for label, _ in ranked[: max(1, limit)]]

    def _sort_by_time_desc(self, sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(
            sessions,
            key=lambda item: float(item.get("start_time") or 0.0),
            reverse=True,
        )
