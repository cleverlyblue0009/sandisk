from __future__ import annotations

import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from database import Database
from utils import safe_duration, top_terms

_CATEGORY_ICONS: dict[str, str] = {
    "game": "game",
    "browser": "browser",
    "editor": "editor",
    "messaging": "messaging",
    "office": "office",
    "other": "other",
    # Backward compatibility with older category values.
    "chat": "messaging",
    "media": "media",
    "torrent": "downloads",
    "launcher": "launcher",
    "system": "system",
}


def _date_key(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _hhmm(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M")


def _legacy_category(category: str | None) -> str:
    c = (category or "").strip().lower()
    if c == "messaging":
        return "chat"
    if c == "office":
        return "editor"
    if c == "other":
        return "system"
    return c or "system"


class MemoryTimelineService:
    def __init__(self, database: Database, session_gap_minutes: int = 45) -> None:
        self.database = database
        self.session_gap_seconds = max(10, session_gap_minutes * 60)

    def get_timeline(self, days: int) -> dict[str, Any]:
        """Timeline payload used by the existing frontend Timeline tab."""
        start_time = time.time() - (max(1, days) * 86400)
        file_events = self.database.fetch_file_events(start_time=start_time)
        process_activity = self.database.fetch_process_activity(start_time=start_time)
        file_meta = {item["file_path"]: item for item in self.database.fetch_clustered_files()}

        timeline_buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)

        for event in file_events:
            path = str(event["file_path"])
            meta = file_meta.get(path, {})
            entry = self._file_event_entry(event, meta)
            timeline_buckets[_date_key(float(event["event_time"]))].append(entry)

        for activity in process_activity:
            entry = self._process_entry(activity)
            timeline_buckets[_date_key(float(activity["start_time"]))].append(entry)

        timeline = []
        for day in sorted(timeline_buckets.keys(), reverse=True):
            entries = sorted(timeline_buckets[day], key=lambda item: item["timestamp"])
            timeline.append({"date": day, "entries": entries})

        sessions = self._build_semantic_sessions(file_events, file_meta)
        return {"days": days, "timeline": timeline, "sessions": sessions}

    def get_activity_timeline_entries(self, days: int) -> list[dict[str, Any]]:
        """Chronological app timeline used by GET /api/timeline."""
        start_time = time.time() - (max(1, days) * 86400)
        process_activity = self.database.fetch_process_activity(start_time=start_time)
        entries: list[dict[str, Any]] = []

        for activity in process_activity:
            process_name = str(activity.get("process_name") or "unknown.exe")
            app_name = str(activity.get("app_name") or "").strip()
            if not app_name or app_name.lower() == process_name.lower():
                app_name = process_name.removesuffix(".exe").replace("_", " ").title()

            category = str(activity.get("category") or "other")
            start_ts = float(activity.get("start_time") or 0)
            end_ts = float(activity.get("end_time") or start_ts)
            duration_seconds = max(0.0, end_ts - start_ts)
            date = str(activity.get("date") or _date_key(start_ts))

            entries.append(
                {
                    "date": date,
                    "start_time": _hhmm(start_ts),
                    "end_time": _hhmm(end_ts),
                    "app_name": app_name,
                    "process_name": process_name,
                    "category": category,
                    "duration": safe_duration(duration_seconds),
                    "duration_seconds": duration_seconds,
                    "start_timestamp": start_ts,
                    "end_timestamp": end_ts,
                    "icon": _CATEGORY_ICONS.get(category, "other"),
                }
            )

        # Most recent first while remaining chronological.
        entries.sort(key=lambda item: float(item["start_timestamp"]), reverse=True)
        return entries

    def _file_event_entry(self, event: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
        event_type = str(event["event_type"])
        file_name = str(event["file_name"])
        title = f"Touched {file_name}"
        if event_type == "download":
            title = f"Downloaded {file_name}"
        elif event_type == "file_created":
            title = f"Opened {file_name}"
        elif event_type == "file_modified":
            title = f"Edited {file_name}"
        elif event_type == "file_deleted":
            title = f"Deleted {file_name}"

        return {
            "type": "file",
            "event_type": event_type,
            "title": title,
            "timestamp": float(event["event_time"]),
            "timestamp_iso": datetime.fromtimestamp(float(event["event_time"]), tz=timezone.utc).isoformat(),
            "file_name": file_name,
            "file_path": str(event["file_path"]),
            "cluster": meta.get("cluster_label"),
            "context": meta.get("context_label"),
            "source": event.get("source"),
        }

    def _process_entry(self, activity: dict[str, Any]) -> dict[str, Any]:
        process_name = str(activity["process_name"])
        duration_seconds = float(activity["duration_seconds"])
        app_name = str(activity.get("app_name") or "").strip()
        if not app_name or app_name.lower() == process_name.lower():
            app_name = process_name.removesuffix(".exe").replace("_", " ").title()
        category = _legacy_category(str(activity.get("category") or "other"))
        icon = _CATEGORY_ICONS.get(category, "other")
        start_ts = float(activity["start_time"])
        end_ts = float(activity.get("end_time", start_ts + duration_seconds))
        return {
            "type": "app",
            "title": f"Used {app_name}",
            "app_name": app_name,
            "process_name": process_name,
            "category": category,
            "icon": icon,
            "timestamp": start_ts,
            "end_timestamp": end_ts,
            "timestamp_iso": datetime.fromtimestamp(start_ts, tz=timezone.utc).isoformat(),
            "duration_seconds": duration_seconds,
            "duration": safe_duration(duration_seconds),
        }

    def _build_semantic_sessions(
        self,
        file_events: list[dict[str, Any]],
        file_meta: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        # Sessions are formed when related files are touched within a rolling time window.
        signal_events = [
            item
            for item in file_events
            if str(item.get("event_type")) in {"file_created", "file_modified"}
        ]
        if not signal_events:
            return []

        sorted_events = sorted(signal_events, key=lambda item: float(item["event_time"]))
        sessions: list[dict[str, Any]] = []
        active: dict[str, Any] | None = None

        for event in sorted_events:
            ts = float(event["event_time"])
            meta = file_meta.get(str(event["file_path"]), {})
            cluster = str(meta.get("cluster_label") or "General Study Material")
            context = str(meta.get("context_label") or "General Study Material")
            session_key = f"{cluster}::{context}"

            if (
                active is None
                or ts - float(active["end_time"]) > self.session_gap_seconds
                or active["session_key"] != session_key
            ):
                if active is not None:
                    sessions.append(self._finalize_session(active))
                active = {
                    "session_key": session_key,
                    "cluster": cluster,
                    "context": context,
                    "start_time": ts,
                    "end_time": ts,
                    "files": {str(event["file_name"])},
                    "topic_text": [str(event["file_name"]), cluster],
                }
            else:
                active["end_time"] = ts
                active["files"].add(str(event["file_name"]))
                active["topic_text"].append(str(event["file_name"]))

        if active is not None:
            sessions.append(self._finalize_session(active))

        sessions.sort(key=lambda item: item["start_time"], reverse=True)
        return sessions

    def _finalize_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        terms = top_terms(" ".join(payload["topic_text"]), limit=4)
        topics = [term.replace("_", " ") for term in terms] or [payload["cluster"]]
        duration = max(0.0, float(payload["end_time"]) - float(payload["start_time"]))
        return {
            "session_name": f"{payload['cluster']} - {payload['context']}",
            "cluster": payload["cluster"],
            "context": payload["context"],
            "start_time": float(payload["start_time"]),
            "end_time": float(payload["end_time"]),
            "duration_seconds": duration,
            "duration": safe_duration(duration),
            "files": sorted(payload["files"]),
            "topics": topics,
        }
