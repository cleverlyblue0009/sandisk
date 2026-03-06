from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from firebase_storage import FirebaseStorage

if TYPE_CHECKING:
    from database import Database

logger = logging.getLogger(__name__)

_DEMO_META_KEY = "demo_history_generated_v2"
_CATEGORY_TO_DB = {
    "coding": "editor",
    "browsing": "browser",
    "documents": "office",
    "studying": "office",
    "communication": "messaging",
    "gaming": "game",
    "other": "other",
}

_YOUTUBE_DEMO_VIDEOS: list[tuple[str, str]] = [
    ("How AES Encryption Works - Computerphile", "tutorial"),
    ("Silent Hill F Gameplay Walkthrough", "gameplay"),
    ("LoFi Beats to Study To", "music"),
    ("CRISPR Explained in 5 Minutes", "education"),
    ("Inside Linux Kernel Scheduling - Documentary Short", "documentary"),
]


class DemoDataGenerator:
    """Generate 30 days of demo history for hackathon showcases."""

    def __init__(
        self,
        *,
        storage: FirebaseStorage,
        user_id: str,
        database: Database | None = None,
    ) -> None:
        self.storage = storage
        self.user_id = user_id
        self.database = database

    def ensure_demo_history(self) -> None:
        if self.storage.get_meta(_DEMO_META_KEY) == "1":
            return

        if self.storage.count_sessions(self.user_id) > 0:
            self.storage.set_meta(_DEMO_META_KEY, "1")
            return

        sessions = self._build_demo_sessions(days=30)
        for session in sessions:
            self.storage.save_session(user_id=self.user_id, session=session)
            self._mirror_to_sqlite(session)

        self.storage.set_meta(_DEMO_META_KEY, "1")
        logger.info("Demo history generated: %s sessions", len(sessions))

    def _mirror_to_sqlite(self, session: dict[str, Any]) -> None:
        if self.database is None:
            return
        self.database.record_process_session(
            process_name=str(session["process_name"]).lower(),
            app_name=str(session["app_name"]),
            category=_CATEGORY_TO_DB.get(str(session["category"]), "other"),
            launcher_name=None,
            executable_name=str(session["process_name"]),
            pid=int(session.get("pid") or 0),
            start_time=float(session["start_time"]),
            end_time=float(session["end_time"]),
            date=str(session["date"]),
        )

    def _build_demo_sessions(self, *, days: int) -> list[dict[str, Any]]:
        now = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        sessions: list[dict[str, Any]] = []
        study_documents = [
            ("Bioinformatics.pdf", "WPS Office", "wps.exe", r"C:\Program Files\WPS Office\office6\wps.exe"),
            ("Cryptography_Lab.docx", "Microsoft Word", "winword.exe", r"C:\Program Files\Microsoft Office\root\Office16\WINWORD.EXE"),
            ("Genome_Assembly_Notes.docx", "Microsoft Word", "winword.exe", r"C:\Program Files\Microsoft Office\root\Office16\WINWORD.EXE"),
        ]
        coding_tabs = [
            ("GitHub", "Development (Chrome)", "github"),
            ("Stack Overflow", "Programming Help (Chrome)", "stackoverflow"),
            ("ChatGPT", "ChatGPT (Chrome)", "chatgpt"),
            ("MDN Web Docs", "Documentation (Chrome)", "documentation"),
        ]

        for offset in range(days):
            day = now - timedelta(days=(days - 1 - offset))

            # Work block: VS Code.
            if day.weekday() < 5:
                sessions.append(
                    self._make_session(
                        day=day,
                        start_hour=9,
                        start_minute=30,
                        duration_minutes=210,
                        app_name="VS Code",
                        process_name="code.exe",
                        executable_path=r"C:\Program Files\Microsoft VS Code\Code.exe",
                        window_title="memory_query_engine.py - Visual Studio Code",
                        category="coding",
                        activity_name="Coding (VS Code)",
                        activity_type="coding",
                    )
                )

                site_name, activity_name, domain = coding_tabs[offset % len(coding_tabs)]
                sessions.append(
                    self._make_session(
                        day=day,
                        start_hour=12,
                        start_minute=0,
                        duration_minutes=45,
                        app_name="Google Chrome",
                        process_name="chrome.exe",
                        executable_path=r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                        window_title=f"{site_name} - Google Chrome",
                        category="browsing",
                        domain=domain,
                        browser_name="Chrome",
                        activity_name=activity_name,
                        activity_type=domain,
                    )
                )

            # Document study blocks.
            if offset % 3 == 0:
                document_name, app_name, process_name, executable_path = study_documents[offset % len(study_documents)]
                sessions.append(
                    self._make_session(
                        day=day,
                        start_hour=14,
                        start_minute=0,
                        duration_minutes=75,
                        app_name=app_name,
                        process_name=process_name,
                        executable_path=executable_path,
                        window_title=f"{document_name} - {app_name}",
                        category="studying",
                        activity_name=f"Studying ({document_name})",
                        activity_type="studying",
                        document_name=document_name,
                        file_extension=document_name.rsplit(".", 1)[-1].lower(),
                    )
                )

            # Casual browsing + YouTube.
            if offset % 2 == 0:
                video_title, youtube_category = _YOUTUBE_DEMO_VIDEOS[offset % len(_YOUTUBE_DEMO_VIDEOS)]
                sessions.append(
                    self._make_session(
                        day=day,
                        start_hour=18,
                        start_minute=0,
                        duration_minutes=35,
                        app_name="Google Chrome",
                        process_name="chrome.exe",
                        executable_path=r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                        window_title=f"{video_title} - YouTube - Google Chrome",
                        category="browsing",
                        domain="youtube",
                        browser_name="Chrome",
                        activity_name=f"YouTube: {video_title} (Chrome)",
                        activity_type=youtube_category,
                        video_title=video_title,
                        youtube_category=youtube_category,
                    )
                )

            if offset % 5 == 0:
                sessions.append(
                    self._make_session(
                        day=day,
                        start_hour=19,
                        start_minute=10,
                        duration_minutes=40,
                        app_name="Google Chrome",
                        process_name="chrome.exe",
                        executable_path=r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                        window_title="MangaDex - Google Chrome",
                        category="browsing",
                        domain="manga",
                        browser_name="Chrome",
                        activity_name="Manga Reading (Chrome)",
                        activity_type="manga reading",
                    )
                )

            # Evening gaming.
            if offset % 3 == 0:
                sessions.append(
                    self._make_session(
                        day=day,
                        start_hour=21,
                        start_minute=0,
                        duration_minutes=120,
                        app_name="Silent Hill F",
                        process_name="Shf-Win64-Shipping.exe",
                        executable_path=r"D:\Games\SilentHillF\Shf-Win64-Shipping.exe",
                        window_title="Silent Hill F",
                        category="gaming",
                        activity_name="Gaming (Silent Hill F)",
                        activity_type="gaming",
                    )
                )

        sessions.sort(key=lambda item: float(item["start_time"]))
        return sessions

    def _make_session(
        self,
        *,
        day: datetime,
        start_hour: int,
        start_minute: int,
        duration_minutes: int,
        app_name: str,
        process_name: str,
        executable_path: str,
        window_title: str,
        category: str,
        domain: str = "",
        document_name: str = "",
        file_extension: str = "",
        browser_name: str = "",
        activity_name: str = "",
        activity_type: str = "",
        video_title: str = "",
        youtube_category: str = "",
    ) -> dict[str, Any]:
        start_dt = day.replace(hour=start_hour, minute=start_minute)
        end_dt = start_dt + timedelta(minutes=duration_minutes)
        start_ts = start_dt.timestamp()
        end_ts = end_dt.timestamp()
        return {
            "session_id": str(uuid.uuid4()),
            "app_name": app_name,
            "activity_name": activity_name,
            "browser_name": browser_name,
            "activity_type": activity_type,
            "process_name": process_name,
            "executable_path": executable_path,
            "window_title": window_title,
            "category": category,
            "start_time": start_ts,
            "end_time": end_ts,
            "duration_seconds": float(max(0.0, end_ts - start_ts)),
            "date": start_dt.strftime("%Y-%m-%d"),
            "domain": domain,
            "document_name": document_name,
            "file_extension": file_extension,
            "video_title": video_title,
            "youtube_category": youtube_category,
            "source": "demo",
            "pid": 0,
            "updated_at": end_ts,
        }
