from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any

from utils import safe_duration


class InsightEngine:
    """Generate workflow insights from aggregated activity sessions."""

    def workflow_breakdown(self, sessions: list[dict[str, Any]]) -> dict[str, Any]:
        buckets = {
            "coding": 0.0,
            "browsing": 0.0,
            "youtube": 0.0,
            "study": 0.0,
            "gaming": 0.0,
        }
        study_hours: list[int] = []
        coding_dates: set[str] = set()
        youtube_tutorial_dates: set[str] = set()

        for session in sessions:
            category = str(session.get("category") or "other").lower()
            seconds = float(session.get("duration_seconds") or 0.0)
            if seconds <= 0:
                continue

            is_youtube = bool(str(session.get("video_title") or "").strip())
            if category == "coding":
                buckets["coding"] += seconds
                coding_dates.add(str(session.get("date") or ""))
            elif category == "gaming":
                buckets["gaming"] += seconds
            elif category in {"studying", "documents"}:
                buckets["study"] += seconds
                start_ts = float(session.get("start_time") or 0.0)
                if start_ts > 0:
                    study_hours.append(datetime.fromtimestamp(start_ts).hour)
            elif category == "browsing":
                if is_youtube:
                    buckets["youtube"] += seconds
                else:
                    buckets["browsing"] += seconds

            if is_youtube and str(session.get("youtube_category") or "").lower() in {"tutorial", "education"}:
                youtube_tutorial_dates.add(str(session.get("date") or ""))

        insights = list(self.generate_insights(sessions))
        if coding_dates & youtube_tutorial_dates:
            insights.append("You watched YouTube tutorials while coding.")

        productive_window = ""
        if len(study_hours) >= 3:
            median_hour = statistics.median(study_hours)
            if 10 <= median_hour <= 14:
                productive_window = "10:00-14:00"
            else:
                start_hour = max(0, int(median_hour) - 1)
                end_hour = min(23, int(median_hour) + 2)
                productive_window = f"{start_hour:02d}:00-{end_hour:02d}:00"

        deduped_insights: list[str] = []
        seen: set[str] = set()
        for item in insights:
            text = str(item or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            deduped_insights.append(text)

        return {
            "coding_time_seconds": buckets["coding"],
            "browsing_time_seconds": buckets["browsing"],
            "youtube_time_seconds": buckets["youtube"],
            "study_time_seconds": buckets["study"],
            "gaming_time_seconds": buckets["gaming"],
            "coding_time": safe_duration(buckets["coding"]),
            "browsing_time": safe_duration(buckets["browsing"]),
            "youtube_time": safe_duration(buckets["youtube"]),
            "study_time": safe_duration(buckets["study"]),
            "gaming_time": safe_duration(buckets["gaming"]),
            "productive_window": productive_window,
            "insights": deduped_insights,
        }

    def youtube_watch_patterns(self, sessions: list[dict[str, Any]]) -> dict[str, Any]:
        category_seconds: dict[str, float] = defaultdict(float)
        category_counts: Counter[str] = Counter()
        title_counts: Counter[str] = Counter()

        for session in sessions:
            title = str(session.get("video_title") or "").strip()
            if not title:
                continue
            seconds = float(session.get("duration_seconds") or 0.0)
            category = str(session.get("youtube_category") or "entertainment").strip().lower() or "entertainment"
            category_seconds[category] += seconds
            category_counts[category] += 1
            title_counts[title] += 1

        top_categories = [
            {
                "category": category,
                "duration_seconds": seconds,
                "duration": safe_duration(seconds),
                "sessions": int(category_counts[category]),
            }
            for category, seconds in sorted(category_seconds.items(), key=lambda item: item[1], reverse=True)
        ]
        top_titles = [
            {"title": title, "sessions": count}
            for title, count in title_counts.most_common(5)
        ]

        if len(top_categories) >= 2:
            summary = (
                f"You mainly watch {top_categories[0]['category']} and "
                f"{top_categories[1]['category']} videos."
            )
        elif top_categories:
            summary = f"You mainly watch {top_categories[0]['category']} videos."
        else:
            summary = "I do not have enough YouTube history yet."

        return {
            "summary": summary,
            "top_categories": top_categories[:5],
            "top_titles": top_titles,
            "total_sessions": sum(category_counts.values()),
        }

    def daily_summary_cards(
        self,
        sessions: list[dict[str, Any]],
        *,
        target_date: str | None = None,
    ) -> list[dict[str, Any]]:
        if target_date is None:
            target_date = datetime.now().strftime("%Y-%m-%d")

        buckets = {
            "coding": 0.0,
            "gaming": 0.0,
            "studying": 0.0,
            "browsing": 0.0,
        }

        for session in sessions:
            if str(session.get("date") or "") != target_date:
                continue
            category = str(session.get("category") or "other").lower()
            seconds = float(session.get("duration_seconds") or 0.0)
            if category == "coding":
                buckets["coding"] += seconds
            elif category == "gaming":
                buckets["gaming"] += seconds
            elif category in {"studying", "documents"}:
                buckets["studying"] += seconds
            elif category == "browsing":
                buckets["browsing"] += seconds

        return [
            {
                "title": "Coding",
                "duration_seconds": buckets["coding"],
                "duration": safe_duration(buckets["coding"]),
            },
            {
                "title": "Gaming",
                "duration_seconds": buckets["gaming"],
                "duration": safe_duration(buckets["gaming"]),
            },
            {
                "title": "Studying",
                "duration_seconds": buckets["studying"],
                "duration": safe_duration(buckets["studying"]),
            },
            {
                "title": "Browsing",
                "duration_seconds": buckets["browsing"],
                "duration": safe_duration(buckets["browsing"]),
            },
        ]

    def generate_insights(self, sessions: list[dict[str, Any]]) -> list[str]:
        if not sessions:
            return ["No activity has been captured yet."]

        insights: list[str] = []
        today = datetime.now().strftime("%Y-%m-%d")

        coding_today = sum(
            float(item.get("duration_seconds") or 0.0)
            for item in sessions
            if str(item.get("category") or "").lower() == "coding"
            and str(item.get("date") or "") == today
        )
        if coding_today > 0:
            insights.append(f"You coded for {safe_duration(coding_today)} today.")

        gaming_today = sum(
            float(item.get("duration_seconds") or 0.0)
            for item in sessions
            if str(item.get("category") or "").lower() == "gaming"
            and str(item.get("date") or "") == today
        )
        if gaming_today > 0:
            insights.append(f"You played games for {safe_duration(gaming_today)} today.")

        by_date: dict[str, dict[str, float]] = defaultdict(lambda: {"coding": 0.0, "chatgpt": 0.0})
        for item in sessions:
            date_key = str(item.get("date") or "")
            category = str(item.get("category") or "").lower()
            seconds = float(item.get("duration_seconds") or 0.0)
            domain = str(item.get("domain") or "").lower()
            if category == "coding":
                by_date[date_key]["coding"] += seconds
            if category == "browsing" and "chatgpt" in domain:
                by_date[date_key]["chatgpt"] += seconds

        if any(day["coding"] > 0 and day["chatgpt"] > 0 for day in by_date.values()):
            insights.append("You used ChatGPT while coding.")

        study_hours: list[int] = []
        for item in sessions:
            category = str(item.get("category") or "").lower()
            if category not in {"studying", "documents"}:
                continue
            ts = float(item.get("start_time") or 0.0)
            if ts <= 0:
                continue
            study_hours.append(datetime.fromtimestamp(ts).hour)
        if len(study_hours) >= 3:
            median_hour = statistics.median(study_hours)
            if 10 <= median_hour <= 14:
                insights.append("You tend to study between 10:00 and 14:00.")

        game_night_sessions = 0
        for item in sessions:
            category = str(item.get("category") or "").lower()
            if category != "gaming":
                continue
            ts = float(item.get("start_time") or 0.0)
            if ts <= 0:
                continue
            if datetime.fromtimestamp(ts).hour >= 21:
                game_night_sessions += 1
        if game_night_sessions >= 2:
            insights.append("You usually play games after 21:00.")

        if not insights:
            insights.append("Activity patterns are still forming. Keep using your computer normally.")
        return insights
