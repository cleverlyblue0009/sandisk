"""App usage statistics and activity suggestion service."""
from __future__ import annotations

import time
from collections import defaultdict
from typing import Any

from database import Database
from utils import safe_duration, top_terms

_CATEGORY_META_API: dict[str, dict[str, str]] = {
    "game": {"icon": "game", "label": "Games"},
    "browser": {"icon": "browser", "label": "Browsing"},
    "editor": {"icon": "editor", "label": "Coding"},
    "messaging": {"icon": "messaging", "label": "Messaging"},
    "office": {"icon": "office", "label": "Office"},
    "other": {"icon": "other", "label": "Other"},
}

# Existing frontend Activity tab expects these legacy categories.
_CATEGORY_META_LEGACY: dict[str, dict[str, str]] = {
    "game": {"icon": "game", "label": "Games"},
    "browser": {"icon": "browser", "label": "Browsing"},
    "editor": {"icon": "editor", "label": "Coding"},
    "chat": {"icon": "messaging", "label": "Chat"},
    "media": {"icon": "media", "label": "Media"},
    "torrent": {"icon": "downloads", "label": "Downloads"},
    "launcher": {"icon": "launcher", "label": "Launchers"},
    "system": {"icon": "system", "label": "System"},
}


def _canonical_category(category: str | None) -> str:
    c = (category or "").strip().lower()
    if c in {"game", "browser", "editor", "messaging", "office", "other"}:
        return c
    if c == "chat":
        return "messaging"
    if c in {"media", "torrent", "launcher", "system", ""}:
        return "other"
    return "other"


def _legacy_category(category: str | None) -> str:
    canonical = _canonical_category(category)
    if canonical == "messaging":
        return "chat"
    if canonical == "office":
        return "editor"
    if canonical == "other":
        return "system"
    return canonical


def _nice_name(process_name: str, app_name: str | None) -> str:
    if app_name and app_name.strip() and app_name.strip().lower() != process_name.lower():
        return app_name.strip()
    return process_name.removesuffix(".exe").replace("_", " ").title()


def _period_label(days: int) -> str:
    if days == 1:
        return "today"
    if days == 7:
        return "this week"
    return f"the last {days} days"


class StatsService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def get_stats(self, days: int = 14) -> dict[str, Any]:
        """Legacy payload used by /activity/stats (frontend Activity tab)."""
        start_time = time.time() - (max(1, days) * 86400)
        raw = self.database.get_activity_stats(start_time=start_time)

        for item in raw["by_process"]:
            sec = float(item["total_seconds"])
            category = _legacy_category(str(item.get("category") or "other"))
            item["duration"] = safe_duration(sec)
            item["total_hours"] = round(sec / 3600, 2)
            item["category"] = category
            item["icon"] = _CATEGORY_META_LEGACY.get(category, _CATEGORY_META_LEGACY["system"])["icon"]
            item["display_name"] = _nice_name(item["process_name"], item.get("app_name"))

        raw["total_duration"] = safe_duration(float(raw["total_seconds"]))
        raw["total_hours"] = round(float(raw["total_seconds"]) / 3600, 2)
        raw["days"] = days
        return raw

    def get_api_stats(self, days: int = 14) -> dict[str, Any]:
        """Canonical API payload used by /api/activity/stats."""
        start_time = time.time() - (max(1, days) * 86400)
        raw = self.database.get_activity_stats(start_time=start_time)
        max_sec = max((float(item["total_seconds"]) for item in raw["by_process"]), default=1.0)

        stats: list[dict[str, Any]] = []
        category_totals: dict[str, float] = defaultdict(float)

        for item in raw["by_process"]:
            sec = float(item["total_seconds"])
            category = _canonical_category(str(item.get("category") or "other"))
            category_totals[category] += sec
            display_name = _nice_name(item["process_name"], item.get("app_name"))
            meta = _CATEGORY_META_API.get(category, _CATEGORY_META_API["other"])
            stats.append(
                {
                    "app": display_name,
                    "app_name": display_name,
                    "process_name": item["process_name"],
                    "category": category,
                    "icon": meta["icon"],
                    "sessions": int(item.get("sessions") or 0),
                    "duration_seconds": sec,
                    "total_duration": safe_duration(sec),
                    "total_hours": round(sec / 3600, 2),
                    "pct": round((sec / max_sec) * 100) if max_sec > 0 else 0,
                }
            )

        categories = [
            {
                "category": cat,
                "label": _CATEGORY_META_API.get(cat, _CATEGORY_META_API["other"])["label"],
                "icon": _CATEGORY_META_API.get(cat, _CATEGORY_META_API["other"])["icon"],
                "duration_seconds": secs,
                "total_duration": safe_duration(secs),
                "total_hours": round(secs / 3600, 2),
            }
            for cat, secs in sorted(category_totals.items(), key=lambda kv: kv[1], reverse=True)
        ]

        stats.sort(key=lambda item: float(item["duration_seconds"]), reverse=True)
        return {
            "stats": stats,
            "categories": categories,
            "total_duration": safe_duration(float(raw["total_seconds"])),
            "total_hours": round(float(raw["total_seconds"]) / 3600, 2),
            "total_seconds": float(raw["total_seconds"]),
            "days": days,
        }

    def get_suggestions(self, days: int = 7) -> dict[str, Any]:
        """Generate plain-language activity suggestions from usage patterns."""
        days = max(1, int(days))
        period = _period_label(days)
        start_time = time.time() - (days * 86400)
        raw = self.database.get_activity_stats(start_time=start_time)
        sessions = raw.get("by_process", [])

        category_seconds: dict[str, float] = defaultdict(float)
        game_rows: list[dict[str, Any]] = []
        for row in sessions:
            category = _canonical_category(str(row.get("category") or "other"))
            seconds = float(row.get("total_seconds") or 0)
            if seconds <= 0:
                continue
            category_seconds[category] += seconds
            if category == "game":
                game_rows.append(row)

        suggestions: list[str] = []
        editor_seconds = category_seconds.get("editor", 0.0)
        if editor_seconds > 0:
            suggestions.append(f"You spent {safe_duration(editor_seconds)} coding {period}.")

        office_seconds = category_seconds.get("office", 0.0)
        if office_seconds > 0:
            suggestions.append(f"You spent {safe_duration(office_seconds)} in office apps {period}.")

        browser_seconds = category_seconds.get("browser", 0.0)
        if browser_seconds > 0:
            suggestions.append(f"You spent {safe_duration(browser_seconds)} browsing {period}.")

        if game_rows:
            top_game = max(game_rows, key=lambda item: float(item.get("total_seconds") or 0))
            game_name = _nice_name(str(top_game.get("process_name") or "game.exe"), top_game.get("app_name"))
            game_duration = safe_duration(float(top_game.get("total_seconds") or 0))
            suggestions.append(f"You played {game_name} for {game_duration} {period}.")

        doc_events = self.database.fetch_file_events(start_time=start_time)
        doc_names = [
            str(item.get("file_name") or "").strip()
            for item in doc_events
            if str(item.get("event_type") or "") in {"file_created", "file_modified"}
        ]
        unique_doc_names = sorted({name for name in doc_names if name})
        if unique_doc_names:
            topics = top_terms(" ".join(unique_doc_names), limit=2)
            if topics:
                topic_label = " ".join(term.title() for term in topics)
                suggestions.append(
                    f"You opened {len(unique_doc_names)} documents related to {topic_label}."
                )
            else:
                suggestions.append(f"You opened {len(unique_doc_names)} documents {period}.")

        if not suggestions:
            suggestions.append(f"No significant activity patterns were recorded for {period}.")

        return {
            "days": days,
            "period": period,
            "total_duration": safe_duration(float(raw.get("total_seconds") or 0)),
            "suggestions": suggestions,
        }
