"""Foreground window activity tracker for Windows.

Tracks only the app currently focused by the user (foreground window),
polling every few seconds and writing completed sessions to SQLite.
"""
from __future__ import annotations

import ctypes
import logging
import threading
import time
from dataclasses import dataclass

import psutil

from database import Database

logger = logging.getLogger(__name__)

# Required system process filter.
_SYSTEM_PROCESSES = frozenset(
    {
        "svchost.exe",
        "runtimebroker.exe",
        "services.exe",
        "taskhostw.exe",
        "lsass.exe",
    }
)

_BROWSER_PROCESSES = frozenset({"msedge.exe", "chrome.exe", "firefox.exe"})
_EDITOR_PROCESSES = frozenset({"code.exe"})
_GAME_PROCESSES = frozenset({"starrail.exe"})
_MESSAGING_PROCESSES = frozenset({"whatsapp.exe"})
_OFFICE_PROCESSES = frozenset({"wps.exe", "winword.exe"})

_DISPLAY_NAME_OVERRIDES: dict[str, str] = {
    "code.exe": "VS Code",
    "msedge.exe": "Microsoft Edge",
    "chrome.exe": "Google Chrome",
    "firefox.exe": "Firefox",
    "starrail.exe": "Honkai Star Rail",
    "whatsapp.exe": "WhatsApp",
    "wps.exe": "WPS Office",
    "winword.exe": "Microsoft Word",
}

_user32 = getattr(ctypes, "windll", None)
if _user32 is not None:
    _user32 = _user32.user32
    _user32.GetForegroundWindow.restype = ctypes.c_void_p
    _user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    _user32.GetWindowThreadProcessId.restype = ctypes.c_uint


def _get_foreground_pid() -> int | None:
    """Return PID for the active foreground window."""
    if _user32 is None:
        return None
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return None
    pid = ctypes.c_ulong(0)
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return None
    return int(pid.value)


def _classify_app(executable_name: str) -> str:
    proc = executable_name.lower()
    if proc in _EDITOR_PROCESSES:
        return "editor"
    if proc in _BROWSER_PROCESSES:
        return "browser"
    if proc in _GAME_PROCESSES or proc.endswith("shipping.exe"):
        return "game"
    if proc in _MESSAGING_PROCESSES:
        return "messaging"
    if proc in _OFFICE_PROCESSES:
        return "office"
    return "other"


def _pretty_app_name(executable_name: str) -> str:
    proc = executable_name.lower()
    if proc in _DISPLAY_NAME_OVERRIDES:
        return _DISPLAY_NAME_OVERRIDES[proc]
    return executable_name.removesuffix(".exe").replace("_", " ").replace("-", " ").title()


@dataclass
class _ActiveSession:
    pid: int
    process_name: str
    executable_name: str
    app_name: str
    category: str
    start_time: float


class ActivityTracker:
    """Polls the foreground app and records completed sessions."""

    def __init__(self, database: Database, poll_seconds: int = 3) -> None:
        self.database = database
        self.poll_seconds = max(1, int(poll_seconds))
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._active: _ActiveSession | None = None

    def start(self) -> None:
        if _user32 is None:
            logger.warning("Foreground tracking requires Windows APIs; tracker is inactive.")
            return
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                daemon=True,
                name="activity-tracker",
            )
            self._thread.start()
            logger.info("Activity tracker started (foreground polling every %ss)", self.poll_seconds)

    def stop(self) -> None:
        with self._lock:
            self._stop_event.set()
            thread = self._thread
            self._thread = None
        if thread is not None:
            thread.join(timeout=self.poll_seconds + 2)
        self._flush_active_session()
        logger.info("Activity tracker stopped")

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception:
                logger.exception("Foreground activity sampling failed")
            self._stop_event.wait(self.poll_seconds)

    def _tick(self) -> None:
        now = time.time()
        next_session, should_apply = self._read_foreground_session(now)
        if not should_apply:
            return

        with self._lock:
            current = self._active

            if next_session is None:
                if current is not None:
                    self._close_session(current, now)
                    self._active = None
                return

            if current and current.pid == next_session.pid and current.process_name == next_session.process_name:
                return

            if current is not None:
                self._close_session(current, now)

            self._active = next_session

    def _read_foreground_session(self, now: float) -> tuple[_ActiveSession | None, bool]:
        pid = _get_foreground_pid()
        if pid is None:
            return None, True

        try:
            process = psutil.Process(pid)
            executable_name = str(process.name() or "").strip()
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            return None, True
        except psutil.AccessDenied:
            logger.debug("Access denied for foreground process pid=%s", pid)
            return None, False

        if not executable_name:
            return None, False

        normalized = executable_name.lower()
        if normalized in _SYSTEM_PROCESSES:
            return None, True

        return (
            _ActiveSession(
                pid=pid,
                process_name=normalized,
                executable_name=executable_name,
                app_name=_pretty_app_name(executable_name),
                category=_classify_app(executable_name),
                start_time=now,
            ),
            True,
        )

    def _close_session(self, session: _ActiveSession, end_time: float) -> None:
        if end_time <= session.start_time:
            return
        self.database.record_process_session(
            process_name=session.process_name,
            app_name=session.app_name,
            category=session.category,
            launcher_name=None,
            executable_name=session.executable_name,
            pid=session.pid,
            start_time=session.start_time,
            end_time=end_time,
        )

    def _flush_active_session(self) -> None:
        now = time.time()
        with self._lock:
            if self._active is not None:
                self._close_session(self._active, now)
                self._active = None
