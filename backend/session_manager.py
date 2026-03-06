from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from browser_activity import parse_browser_activity
from firebase_storage import FirebaseStorage
from foreground_tracker import ForegroundWindowEvent
from youtube_classifier import YouTubeClassifier

if TYPE_CHECKING:
    from database import Database

logger = logging.getLogger(__name__)

_SYSTEM_PROCESSES = {
    "svchost.exe",
    "runtimebroker.exe",
    "services.exe",
    "taskhostw.exe",
    "lsass.exe",
}

_CODING_PROCESSES = {"code.exe", "cursor.exe", "devenv.exe", "pycharm64.exe", "idea64.exe"}
_DOC_PROCESSES = {"wps.exe", "winword.exe", "powerpnt.exe", "excel.exe", "acrord32.exe"}
_COMM_PROCESSES = {"discord.exe", "whatsapp.exe", "teams.exe", "telegram.exe", "slack.exe"}
_GAME_PROCESSES = {"starrail.exe"}
_DOC_EXTENSIONS = {"pdf", "docx", "pptx", "xlsx"}
_STUDY_KEYWORDS = {"bioinformatics", "lab", "study", "research", "paper", "lecture"}

_PROCESS_NAME_OVERRIDES = {
    "code.exe": "VS Code",
    "msedge.exe": "Microsoft Edge",
    "chrome.exe": "Google Chrome",
    "firefox.exe": "Firefox",
    "wps.exe": "WPS Office",
    "winword.exe": "Microsoft Word",
    "discord.exe": "Discord",
    "whatsapp.exe": "WhatsApp",
}

_GAME_NAME_OVERRIDES = {
    "shf": "Silent Hill F",
    "starrail": "Honkai Star Rail",
}

_CATEGORY_TO_DB = {
    "coding": "editor",
    "browsing": "browser",
    "documents": "office",
    "studying": "office",
    "communication": "messaging",
    "gaming": "game",
    "other": "other",
}


def _extract_document(window_title: str) -> tuple[str | None, str | None]:
    if not window_title:
        return None, None
    match = re.search(r"([^\r\n\t|]+?\.(pdf|docx|pptx|xlsx))", window_title, flags=re.IGNORECASE)
    if not match:
        return None, None
    doc_name = str(match.group(1)).strip(" -|")
    extension = str(match.group(2)).lower()
    if extension not in _DOC_EXTENSIONS:
        return None, None
    return doc_name, extension


def _is_study_document(window_title: str, document_name: str | None) -> bool:
    text = f"{window_title} {document_name or ''}".lower()
    return any(keyword in text for keyword in _STUDY_KEYWORDS)


def _classify_category(
    process_name: str,
    file_extension: str | None,
    *,
    browser_category: str | None,
    has_browser_activity: bool,
) -> str:
    proc = process_name.lower()
    if proc in _CODING_PROCESSES:
        return "coding"
    if browser_category in {"coding", "studying", "communication", "documents", "gaming"}:
        return str(browser_category)
    if has_browser_activity:
        return "browsing"
    if proc in _COMM_PROCESSES:
        return "communication"
    if proc in _DOC_PROCESSES:
        return "documents"
    if proc in _GAME_PROCESSES or re.search(r"(?i)-win64-shipping\.exe$", process_name):
        return "gaming"
    if file_extension:
        return "documents"
    return "other"


def _clean_app_name(process_name: str) -> str:
    proc = process_name.lower()
    if proc in _PROCESS_NAME_OVERRIDES:
        return _PROCESS_NAME_OVERRIDES[proc]

    if re.search(r"(?i)-win64-shipping\.exe$", process_name):
        base = re.sub(r"(?i)-win64-shipping\.exe$", "", process_name)
        base = base.replace("_", " ").replace("-", " ").strip()
        normalized = re.sub(r"\s+", " ", base).strip()
        if normalized.lower() in _GAME_NAME_OVERRIDES:
            return _GAME_NAME_OVERRIDES[normalized.lower()]
        return normalized.title() if normalized else "Unknown Game"

    base = re.sub(r"(?i)\.exe$", "", process_name)
    base = base.replace("_", " ").replace("-", " ").strip()
    return re.sub(r"\s+", " ", base).title() or process_name


def _build_activity_label(
    *,
    base_app_name: str,
    category: str,
    document_name: str | None,
    file_extension: str | None,
    browser_activity_name: str,
    browser_name: str,
) -> str:
    if browser_activity_name and browser_name:
        return f"{browser_activity_name} ({browser_name})"
    if category == "coding":
        return f"Coding ({base_app_name})"
    if category == "studying":
        if document_name:
            return f"Studying ({document_name})"
        if file_extension:
            return f"Studying ({file_extension.upper()})"
        return f"Studying ({base_app_name})"
    if category == "documents":
        if document_name:
            return f"Documents ({document_name})"
        if file_extension:
            return f"Documents ({file_extension.upper()})"
        return f"Documents ({base_app_name})"
    if category == "communication":
        return f"Communication ({base_app_name})"
    if category == "browsing":
        return f"Web Browsing ({base_app_name})"
    return base_app_name


class SessionManager:
    """Convert foreground window events to persisted user activity sessions."""

    def __init__(
        self,
        *,
        storage: FirebaseStorage,
        user_id: str,
        database: Database | None = None,
        youtube_classifier: YouTubeClassifier | None = None,
    ) -> None:
        self.storage = storage
        self.user_id = user_id
        self.database = database
        self.youtube_classifier = youtube_classifier
        self._lock = threading.RLock()
        self._current_session: dict[str, Any] | None = None

    def handle_foreground_event(self, event: ForegroundWindowEvent) -> None:
        process_name = event.process_name.lower().strip()
        if not process_name or process_name in _SYSTEM_PROCESSES:
            return

        document_name, file_extension = _extract_document(event.window_title)
        browser_activity = parse_browser_activity(process_name, event.window_title)
        category = _classify_category(
            process_name,
            file_extension,
            browser_category=browser_activity.category if browser_activity else None,
            has_browser_activity=browser_activity is not None,
        )
        if category == "documents" and _is_study_document(event.window_title, document_name):
            category = "studying"

        browser_name = browser_activity.browser_name if browser_activity else ""
        browser_activity_name = browser_activity.activity_name if browser_activity else ""
        activity_type = browser_activity.category if browser_activity else category
        domain = browser_activity.site_name if browser_activity else ""
        video_title = ""
        youtube_category = ""

        if browser_activity is not None and browser_activity.is_youtube:
            video_title = browser_activity.video_title
            if video_title and self.youtube_classifier is not None:
                youtube_category = self.youtube_classifier.classify_youtube_video(video_title)
            else:
                youtube_category = "entertainment"
            activity_type = youtube_category
            category = "browsing"
            if video_title:
                browser_activity_name = f"YouTube: {video_title}"
            else:
                browser_activity_name = "YouTube"
            domain = "youtube"
        elif (
            browser_activity is not None
            and not browser_activity.site_known
            and self.youtube_classifier is not None
        ):
            site_classification = self.youtube_classifier.classify_unknown_website(
                browser_activity.site_name,
                event.window_title,
            )
            browser_activity_name = site_classification.activity_name
            activity_type = site_classification.activity_type
            if category == "browsing":
                category = site_classification.category

        base_app_name = _clean_app_name(event.process_name)
        app_name = _build_activity_label(
            base_app_name=base_app_name,
            category=category,
            document_name=document_name,
            file_extension=file_extension,
            browser_activity_name=browser_activity_name,
            browser_name=browser_name,
        )
        activity_name = app_name

        session_key = (
            process_name,
            category,
            base_app_name.lower(),
            activity_name.lower(),
            browser_name.lower(),
            (document_name or "").lower(),
            (domain or "").lower(),
        )

        with self._lock:
            if self._current_session is not None:
                if tuple(self._current_session.get("session_key", ())) == session_key:
                    return
                self._close_current_session(end_time=event.timestamp)

            now = float(event.timestamp)
            session = {
                "session_id": str(uuid.uuid4()),
                "app_name": base_app_name,
                "activity_name": activity_name,
                "browser_name": browser_name,
                "activity_type": activity_type,
                "base_app_name": base_app_name,
                "process_name": process_name,
                "executable_path": event.executable_path,
                "window_title": event.window_title,
                "category": category,
                "start_time": now,
                "end_time": now,
                "duration_seconds": 0.0,
                "date": datetime.fromtimestamp(now).strftime("%Y-%m-%d"),
                "domain": domain or "",
                "document_name": document_name or "",
                "file_extension": file_extension or "",
                "video_title": video_title,
                "youtube_category": youtube_category,
                "pid": int(event.pid),
                "source": "foreground",
                "updated_at": now,
                "session_key": session_key,
            }
            self._current_session = session
            self.storage.save_session(user_id=self.user_id, session=session)

    def stop(self) -> None:
        with self._lock:
            self._close_current_session(end_time=time.time())
        self.storage.sync_pending(user_id=self.user_id)

    def _close_current_session(self, *, end_time: float) -> None:
        if self._current_session is None:
            return

        session = dict(self._current_session)
        session.pop("session_key", None)
        session["end_time"] = max(float(end_time), float(session["start_time"]))
        session["duration_seconds"] = max(0.0, float(session["end_time"]) - float(session["start_time"]))
        session["updated_at"] = float(end_time)

        self.storage.save_session(user_id=self.user_id, session=session)

        if self.database is not None and session["duration_seconds"] > 0:
            db_category = _CATEGORY_TO_DB.get(str(session["category"]), "other")
            self.database.record_process_session(
                process_name=str(session["process_name"]),
                app_name=str(session["app_name"]),
                category=db_category,
                launcher_name=None,
                executable_name=str(session["process_name"]),
                pid=int(session.get("pid") or 0),
                start_time=float(session["start_time"]),
                end_time=float(session["end_time"]),
                date=str(session["date"]),
            )

        self._current_session = None
