from __future__ import annotations

import json
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from summarizer import chunks_to_summary
from utils import safe_duration

if TYPE_CHECKING:
    from assistant_personality import AssistantPersonality
    from database import Database
    from firebase_storage import FirebaseStorage
    from groq_client import GroqClient
    from insight_engine import InsightEngine
    from retrieval import RetrievalService

_QUESTION_STOPWORDS = {
    "what",
    "which",
    "when",
    "where",
    "did",
    "does",
    "was",
    "were",
    "doing",
    "for",
    "from",
    "about",
    "with",
    "used",
    "use",
    "using",
    "documents",
    "document",
    "files",
    "file",
    "today",
    "yesterday",
    "week",
    "month",
    "last",
    "this",
    "that",
    "my",
    "your",
    "show",
    "tell",
    "please",
}

_PROCESS_NAME_OVERRIDES = {
    "code.exe": "VS Code",
    "cursor.exe": "Cursor",
    "msedge.exe": "Microsoft Edge",
    "chrome.exe": "Google Chrome",
    "firefox.exe": "Firefox",
    "wps.exe": "WPS Office",
    "winword.exe": "Microsoft Word",
    "acrord32.exe": "Adobe Acrobat Reader",
    "discord.exe": "Discord",
}

_PART_OF_DAY_RANGES = {
    "morning": (6, 12),
    "afternoon": (12, 18),
    "evening": (18, 22),
    "night": (22, 24),
}


@dataclass(frozen=True)
class TimeScope:
    days: int
    label: str
    start_time: float
    end_time: float
    part_of_day: str = ""


def _lower(value: Any) -> str:
    return str(value or "").strip().lower()


def _display_application(session: dict[str, Any]) -> str:
    app_name = str(session.get("app_name") or "").strip()
    process_name = str(session.get("process_name") or "").strip().lower()
    if app_name:
        return app_name
    if process_name in _PROCESS_NAME_OVERRIDES:
        return _PROCESS_NAME_OVERRIDES[process_name]
    if process_name:
        return process_name.removesuffix(".exe").replace("_", " ").title()
    return "Unknown App"


def _session_label(session: dict[str, Any]) -> str:
    activity_name = str(session.get("activity_name") or "").strip()
    if activity_name:
        return activity_name

    video_title = str(session.get("video_title") or "").strip()
    if video_title:
        browser_name = str(session.get("browser_name") or "").strip() or _display_application(session)
        return f"YouTube ({browser_name})" if not video_title else f"YouTube: {video_title} ({browser_name})"

    document_name = str(session.get("document_name") or "").strip()
    category = _lower(session.get("category"))
    if document_name and category in {"documents", "studying"}:
        prefix = "Studying" if category == "studying" else "Documents"
        return f"{prefix} ({document_name})"

    return _display_application(session)


def _session_blob(session: dict[str, Any]) -> str:
    fields = [
        session.get("app_name"),
        session.get("activity_name"),
        session.get("browser_name"),
        session.get("activity_type"),
        session.get("process_name"),
        session.get("window_title"),
        session.get("category"),
        session.get("domain"),
        session.get("document_name"),
        session.get("video_title"),
        session.get("youtube_category"),
    ]
    return " ".join(str(item or "") for item in fields).lower()


def _extract_query_terms(question: str, limit: int = 8) -> list[str]:
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9_+-]{2,}", question.lower())
    seen: set[str] = set()
    terms: list[str] = []
    for word in words:
        if word in _QUESTION_STOPWORDS or word in seen:
            continue
        seen.add(word)
        terms.append(word)
        if len(terms) >= limit:
            break
    return terms


def _format_timestamp(ts: float) -> str:
    if ts <= 0:
        return ""
    return datetime.fromtimestamp(ts).strftime("%b %d, %H:%M")


def _format_usage_window(start_ts: float, end_ts: float) -> str:
    if start_ts <= 0:
        return ""
    start_dt = datetime.fromtimestamp(start_ts)
    end_dt = datetime.fromtimestamp(end_ts if end_ts > 0 else start_ts)
    return f"{start_dt.strftime('%b %d, %H:%M')}-{end_dt.strftime('%H:%M')}"


def _serialize_activity_session(session: dict[str, Any]) -> dict[str, Any]:
    start_ts = float(session.get("start_time") or 0.0)
    end_ts = float(session.get("end_time") or start_ts)
    duration_seconds = max(0.0, float(session.get("duration_seconds") or end_ts - start_ts))
    return {
        "session_id": str(session.get("session_id") or ""),
        "label": _session_label(session),
        "application_used": _display_application(session),
        "category": str(session.get("category") or "other"),
        "activity_type": str(session.get("activity_type") or ""),
        "start_time": start_ts,
        "end_time": end_ts,
        "start_label": _format_timestamp(start_ts),
        "end_label": _format_timestamp(end_ts),
        "time_window": _format_usage_window(start_ts, end_ts),
        "duration_seconds": duration_seconds,
        "duration": safe_duration(duration_seconds),
        "browser_name": str(session.get("browser_name") or ""),
        "domain": str(session.get("domain") or ""),
        "document_name": str(session.get("document_name") or ""),
        "video_title": str(session.get("video_title") or ""),
        "youtube_category": str(session.get("youtube_category") or ""),
        "window_title": str(session.get("window_title") or ""),
    }


def _parse_topics(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    text = str(raw or "").strip()
    if not text:
        return []
    try:
        loaded = json.loads(text)
    except Exception:
        return []
    if not isinstance(loaded, list):
        return []
    return [str(item).strip() for item in loaded if str(item).strip()]


class MemoryQueryEngine:
    """Unified memory retrieval across sessions, browser usage, YouTube, and documents."""

    def __init__(
        self,
        *,
        storage: "FirebaseStorage",
        database: "Database",
        retrieval_service: "RetrievalService",
        insight_engine: "InsightEngine",
        assistant_personality: "AssistantPersonality",
        groq_client: "GroqClient | None",
        user_id: str,
    ) -> None:
        self.storage = storage
        self.database = database
        self.retrieval_service = retrieval_service
        self.insight_engine = insight_engine
        self.assistant_personality = assistant_personality
        self.groq_client = groq_client
        self.user_id = user_id

    def classify_intent(self, question: str) -> str:
        q = question.lower().strip()
        if re.search(r"\b(document|documents|pdf|docx|paper|notes?|lab|assignment|report|bioinformatics)\b", q):
            return "document_search"
        if re.search(r"\b(youtube|videos?|watch|watched|watching)\b", q):
            return "youtube_analysis"
        if re.search(r"\b(workflow|routine|productivity|describe my workflow|what did i do|daily summary|how long)\b", q):
            return "workflow_summary"
        return "activity_history"

    def ask(self, *, question: str) -> dict[str, Any]:
        normalized_question = str(question or "").strip()
        intent = self.classify_intent(normalized_question)
        scope = self._build_time_scope(normalized_question, intent=intent)
        query_terms = _extract_query_terms(normalized_question)

        sessions = self.storage.fetch_sessions(
            user_id=self.user_id,
            start_time=scope.start_time,
            end_time=scope.end_time,
            limit=6000,
        )
        sessions = self._filter_part_of_day(sessions, scope.part_of_day)

        workflow = self.insight_engine.workflow_breakdown(sessions)
        youtube_analysis = self.insight_engine.youtube_watch_patterns(sessions)

        document_results: list[dict[str, Any]] = []
        if intent == "document_search":
            document_results = self._search_documents(
                query=normalized_question,
                sessions=sessions,
                query_terms=query_terms,
            )
        if not document_results:
            document_results = self._documents_from_sessions(
                sessions=sessions,
                query_terms=query_terms,
                require_terms=(intent == "document_search"),
            )

        relevant_sessions = self._relevant_sessions(
            sessions=sessions,
            intent=intent,
            query_terms=query_terms,
            documents=document_results,
        )
        activity_sessions = [_serialize_activity_session(item) for item in relevant_sessions[:12]]
        browser_sessions = self._browser_sessions(sessions=sessions, query_terms=query_terms, limit=8)
        youtube_sessions = self._youtube_sessions(sessions=sessions, limit=8)

        short_summary = self._build_short_summary(
            intent=intent,
            scope=scope,
            workflow=workflow,
            youtube_analysis=youtube_analysis,
            document_results=document_results,
            activity_sessions=activity_sessions,
            browser_sessions=browser_sessions,
        )
        structured_summary = [
            {"label": "Coding", "value": str(workflow.get("coding_time") or "0m")},
            {"label": "Browsing", "value": str(workflow.get("browsing_time") or "0m")},
            {"label": "YouTube", "value": str(workflow.get("youtube_time") or "0m")},
            {"label": "Study", "value": str(workflow.get("study_time") or "0m")},
            {"label": "Gaming", "value": str(workflow.get("gaming_time") or "0m")},
        ]

        assistant_input = {
            "intent": intent,
            "time_scope": scope.label,
            "short_summary": short_summary,
            "workflow_analysis": {
                "coding_time": workflow.get("coding_time"),
                "browsing_time": workflow.get("browsing_time"),
                "youtube_time": workflow.get("youtube_time"),
                "study_time": workflow.get("study_time"),
                "gaming_time": workflow.get("gaming_time"),
                "insights": workflow.get("insights") or [],
                "productive_window": workflow.get("productive_window") or "",
            },
            "documents": [
                {
                    "file_name": item.get("file_name"),
                    "summary": item.get("summary"),
                    "last_used": item.get("last_used"),
                    "application_used": item.get("application_used"),
                }
                for item in document_results[:5]
            ],
            "youtube_analysis": {
                "summary": youtube_analysis.get("summary") or "",
                "top_categories": youtube_analysis.get("top_categories") or [],
                "top_titles": youtube_analysis.get("top_titles") or [],
            },
            "browser_sessions": browser_sessions[:5],
            "activity_sessions": activity_sessions[:6],
        }
        assistant_response = self.assistant_personality.generate_response(
            user_query=normalized_question,
            structured_memory=assistant_input,
        )

        if not sessions and not document_results:
            assistant_response = "I could not find any recorded memory for that time range yet."
            short_summary = assistant_response

        return {
            "question": normalized_question,
            "intent": intent,
            "time_scope": {
                "days": scope.days,
                "label": scope.label,
                "part_of_day": scope.part_of_day,
            },
            "answer": assistant_response,
            "assistant_response": assistant_response,
            "short_summary": short_summary,
            "structured_summary": structured_summary,
            "related_documents": document_results,
            "activity_sessions": activity_sessions,
            "browser_sessions": browser_sessions,
            "youtube_activity": youtube_sessions,
            "workflow_analysis": workflow,
            "youtube_analysis": youtube_analysis,
            "matches": activity_sessions,
            "days": scope.days,
            "sources_queried": {
                "activity_sessions": True,
                "browser_activity": True,
                "youtube_activity": True,
                "document_index": intent == "document_search",
            },
        }

    def _build_time_scope(self, question: str, *, intent: str) -> TimeScope:
        q = question.lower()
        defaults = {
            "workflow_summary": 7,
            "youtube_analysis": 30,
            "document_search": 60,
            "activity_history": 14,
        }
        if re.search(r"\b(today|tonight|this morning|this afternoon|this evening)\b", q):
            days = 1
            label = "Today"
        elif "yesterday" in q:
            days = 2
            label = "Yesterday"
        elif re.search(r"\b(this week|past week|weekly)\b", q):
            days = 7
            label = "This Week"
        elif re.search(r"\b(last month|past month|this month)\b", q):
            days = 30
            label = "This Month"
        elif re.search(r"\b(last year|this year)\b", q):
            days = 365
            label = "This Year"
        else:
            days = defaults.get(intent, 14)
            label = f"Last {days} Days"

        part_of_day = ""
        for part in _PART_OF_DAY_RANGES:
            if part in q:
                part_of_day = part
                break

        end_time = time.time()
        start_time = end_time - (days * 86400)
        return TimeScope(days=days, label=label, start_time=start_time, end_time=end_time, part_of_day=part_of_day)

    def _filter_part_of_day(self, sessions: list[dict[str, Any]], part_of_day: str) -> list[dict[str, Any]]:
        if not part_of_day:
            return sessions
        start_hour, end_hour = _PART_OF_DAY_RANGES.get(part_of_day, (0, 24))
        filtered: list[dict[str, Any]] = []
        for session in sessions:
            start_ts = float(session.get("start_time") or 0.0)
            end_ts = float(session.get("end_time") or start_ts)
            start_dt = datetime.fromtimestamp(start_ts)
            end_dt = datetime.fromtimestamp(end_ts)
            if start_dt.hour < end_hour and end_dt.hour >= start_hour:
                filtered.append(session)
        return filtered

    def _search_documents(
        self,
        *,
        query: str,
        sessions: list[dict[str, Any]],
        query_terms: list[str],
    ) -> list[dict[str, Any]]:
        raw = self.retrieval_service.search(query=query, top_k=24, result_limit=6)
        results = [self._normalize_document_hit(item) for item in raw.get("results", [])]
        if not results:
            return []
        return self._correlate_documents(results=results, sessions=sessions, query_terms=query_terms)

    def _normalize_document_hit(self, item: dict[str, Any]) -> dict[str, Any]:
        summary = str(item.get("summary") or "").strip()
        topics = _parse_topics(item.get("topics"))
        snippets = [
            str(chunk.get("content") or "").strip()[:280]
            for chunk in (item.get("top_chunks") or [])
            if str(chunk.get("content") or "").strip()
        ][:3]
        if (not summary or not topics) and snippets:
            generated_summary, generated_topics = chunks_to_summary(
                snippets,
                groq_client=self.groq_client,
                file_name=str(item.get("file_name") or ""),
            )
            if not summary:
                summary = generated_summary
            if not topics:
                topics = generated_topics

        modified_time = float(item.get("modified_time") or 0.0)
        return {
            "file_id": int(item.get("file_id") or 0),
            "file_name": str(item.get("file_name") or ""),
            "file_path": str(item.get("file_path") or ""),
            "summary": summary,
            "topics": topics,
            "key_snippets": snippets,
            "score": float(item.get("final_score") or 0.0),
            "modified_time": modified_time,
            "modified_time_label": _format_timestamp(modified_time),
        }

    def _correlate_documents(
        self,
        *,
        results: list[dict[str, Any]],
        sessions: list[dict[str, Any]],
        query_terms: list[str],
    ) -> list[dict[str, Any]]:
        correlated: list[dict[str, Any]] = []
        for result in results:
            file_name = str(result.get("file_name") or "").strip()
            matching_sessions = self._matching_document_sessions(sessions=sessions, file_name=file_name)
            if not matching_sessions and query_terms:
                matching_sessions = [
                    item
                    for item in sessions
                    if str(item.get("document_name") or "").strip()
                    and any(term in _session_blob(item) for term in query_terms)
                ]

            last_used_session = matching_sessions[0] if matching_sessions else None
            correlated.append(
                {
                    **result,
                    "last_used_timestamp": float(last_used_session.get("end_time") or 0.0) if last_used_session else 0.0,
                    "last_used": _format_timestamp(float(last_used_session.get("end_time") or 0.0)) if last_used_session else result.get("modified_time_label", ""),
                    "last_used_window": _format_usage_window(
                        float(last_used_session.get("start_time") or 0.0),
                        float(last_used_session.get("end_time") or 0.0),
                    ) if last_used_session else "",
                    "application_used": _display_application(last_used_session) if last_used_session else "",
                    "used_sessions": [_serialize_activity_session(item) for item in matching_sessions[:5]],
                }
            )
        correlated.sort(
            key=lambda item: (float(item.get("last_used_timestamp") or 0.0), float(item.get("score") or 0.0)),
            reverse=True,
        )
        return correlated

    def _matching_document_sessions(self, *, sessions: list[dict[str, Any]], file_name: str) -> list[dict[str, Any]]:
        target = file_name.strip().lower()
        if not target:
            return []
        matches = [
            item
            for item in sessions
            if _lower(item.get("document_name")) == target
            or target in _lower(item.get("window_title"))
        ]
        return sorted(matches, key=lambda item: float(item.get("end_time") or 0.0), reverse=True)

    def _documents_from_sessions(
        self,
        *,
        sessions: list[dict[str, Any]],
        query_terms: list[str],
        require_terms: bool,
    ) -> list[dict[str, Any]]:
        document_sessions = [
            item
            for item in sessions
            if str(item.get("document_name") or "").strip()
        ]
        if require_terms and query_terms:
            document_sessions = [
                item
                for item in document_sessions
                if any(term in _session_blob(item) for term in query_terms)
            ]
        buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for session in document_sessions:
            buckets[str(session.get("document_name") or "").strip()].append(session)

        indexed_meta = self._indexed_documents_by_name(list(buckets.keys()))
        results: list[dict[str, Any]] = []
        for file_name, doc_sessions in buckets.items():
            if not file_name:
                continue
            doc_sessions.sort(key=lambda item: float(item.get("end_time") or 0.0), reverse=True)
            last_session = doc_sessions[0]
            meta = indexed_meta.get(file_name.lower(), {})
            summary = str(meta.get("summary") or "").strip()
            topics = _parse_topics(meta.get("topics_json"))
            snippets = []
            file_id = int(meta.get("id") or 0)
            if file_id > 0:
                snippets = [item[:280] for item in self.database.fetch_chunks_for_file(file_id, limit=3)]
            results.append(
                {
                    "file_id": file_id,
                    "file_name": file_name,
                    "file_path": str(meta.get("file_path") or ""),
                    "summary": summary,
                    "topics": topics,
                    "key_snippets": snippets,
                    "score": 0.0,
                    "last_used_timestamp": float(last_session.get("end_time") or 0.0),
                    "last_used": _format_timestamp(float(last_session.get("end_time") or 0.0)),
                    "last_used_window": _format_usage_window(
                        float(last_session.get("start_time") or 0.0),
                        float(last_session.get("end_time") or 0.0),
                    ),
                    "application_used": _display_application(last_session),
                    "used_sessions": [_serialize_activity_session(item) for item in doc_sessions[:5]],
                }
            )
        results.sort(key=lambda item: float(item.get("last_used_timestamp") or 0.0), reverse=True)
        return results[:6]

    def _indexed_documents_by_name(self, file_names: list[str]) -> dict[str, dict[str, Any]]:
        rows = self.database.fetch_files_by_names(file_names)
        return {str(row.get("file_name") or "").strip().lower(): row for row in rows}

    def _relevant_sessions(
        self,
        *,
        sessions: list[dict[str, Any]],
        intent: str,
        query_terms: list[str],
        documents: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if intent == "workflow_summary":
            return sorted(sessions, key=lambda item: float(item.get("start_time") or 0.0), reverse=True)

        if intent == "youtube_analysis":
            matches = [item for item in sessions if str(item.get("video_title") or "").strip()]
            return sorted(matches, key=lambda item: float(item.get("start_time") or 0.0), reverse=True)

        if intent == "document_search":
            doc_names = {
                str(item.get("file_name") or "").strip().lower()
                for item in documents
                if str(item.get("file_name") or "").strip()
            }
            matches = [
                item
                for item in sessions
                if str(item.get("document_name") or "").strip()
                and (
                    _lower(item.get("document_name")) in doc_names
                    or any(term in _session_blob(item) for term in query_terms)
                )
            ]
            return sorted(matches, key=lambda item: float(item.get("start_time") or 0.0), reverse=True)

        if query_terms:
            matches = [item for item in sessions if any(term in _session_blob(item) for term in query_terms)]
            if matches:
                return sorted(matches, key=lambda item: float(item.get("start_time") or 0.0), reverse=True)

        return sorted(sessions, key=lambda item: float(item.get("start_time") or 0.0), reverse=True)

    def _browser_sessions(
        self,
        *,
        sessions: list[dict[str, Any]],
        query_terms: list[str],
        limit: int,
    ) -> list[dict[str, Any]]:
        matches = [
            _serialize_activity_session(item)
            for item in sessions
            if (str(item.get("browser_name") or "").strip() or str(item.get("domain") or "").strip())
            and not str(item.get("video_title") or "").strip()
        ]
        if query_terms:
            filtered = [
                item
                for item in matches
                if any(term in " ".join(str(v or "") for v in item.values()).lower() for term in query_terms)
            ]
            if filtered:
                matches = filtered
        return matches[:limit]

    def _youtube_sessions(self, *, sessions: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        matches = [
            _serialize_activity_session(item)
            for item in sessions
            if str(item.get("video_title") or "").strip()
        ]
        return matches[:limit]

    def _build_short_summary(
        self,
        *,
        intent: str,
        scope: TimeScope,
        workflow: dict[str, Any],
        youtube_analysis: dict[str, Any],
        document_results: list[dict[str, Any]],
        activity_sessions: list[dict[str, Any]],
        browser_sessions: list[dict[str, Any]],
    ) -> str:
        if intent == "document_search":
            if not document_results:
                return f"I could not find matching documents in {scope.label.lower()}."
            top_doc = document_results[0]
            file_name = str(top_doc.get("file_name") or "that document")
            application_used = str(top_doc.get("application_used") or "").strip()
            last_used = str(top_doc.get("last_used_window") or top_doc.get("last_used") or "").strip()
            if application_used and last_used:
                return f"{file_name} was last used in {application_used} on {last_used}."
            if last_used:
                return f"{file_name} was last active on {last_used}."
            return f"I found {len(document_results)} related documents."

        if intent == "youtube_analysis":
            summary = str(youtube_analysis.get("summary") or "").strip()
            if summary:
                return summary
            return f"I found {len(activity_sessions)} YouTube sessions in {scope.label.lower()}."

        if intent == "workflow_summary":
            parts = []
            if workflow.get("coding_time_seconds", 0) > 0:
                parts.append(f"coded for {workflow['coding_time']}")
            if workflow.get("browsing_time_seconds", 0) > 0:
                parts.append(f"browsed for {workflow['browsing_time']}")
            if workflow.get("youtube_time_seconds", 0) > 0:
                parts.append(f"watched YouTube for {workflow['youtube_time']}")
            if workflow.get("study_time_seconds", 0) > 0:
                parts.append(f"studied for {workflow['study_time']}")
            if workflow.get("gaming_time_seconds", 0) > 0:
                parts.append(f"played games for {workflow['gaming_time']}")
            if parts:
                return f"In {scope.label.lower()}, you " + ", ".join(parts[:4]) + "."
            return f"I found {len(activity_sessions)} activity sessions in {scope.label.lower()}."

        if activity_sessions:
            lead = activity_sessions[0]
            return f"The most relevant activity was {lead['label']} around {lead['time_window']}."
        if browser_sessions:
            return f"I found {len(browser_sessions)} related browser sessions."
        return f"I could not find relevant activity in {scope.label.lower()}."
