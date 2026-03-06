from __future__ import annotations

import ctypes
import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable

import psutil

logger = logging.getLogger(__name__)

_user32 = getattr(ctypes, "windll", None)
if _user32 is not None:
    _user32 = _user32.user32
    _user32.GetForegroundWindow.restype = ctypes.c_void_p
    _user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    _user32.GetWindowThreadProcessId.restype = ctypes.c_uint
    _user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
    _user32.GetWindowTextLengthW.restype = ctypes.c_int
    _user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    _user32.GetWindowTextW.restype = ctypes.c_int


@dataclass
class ForegroundWindowEvent:
    pid: int
    process_name: str
    executable_path: str
    window_title: str
    timestamp: float


def _read_window_title(hwnd: int) -> str:
    if _user32 is None or not hwnd:
        return ""
    length = int(_user32.GetWindowTextLengthW(hwnd))
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, buffer, len(buffer))
    return str(buffer.value or "").strip()


class ForegroundTracker:
    """Track foreground window every N seconds using Windows APIs only."""

    def __init__(
        self,
        on_event: Callable[[ForegroundWindowEvent], None],
        poll_seconds: int = 2,
    ) -> None:
        self.on_event = on_event
        self.poll_seconds = max(1, int(poll_seconds))
        self._stop_event = threading.Event()
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if _user32 is None:
            logger.warning("Windows foreground APIs are unavailable; tracker not started.")
            return
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                daemon=True,
                name="foreground-tracker",
            )
            self._thread.start()
            logger.info("Foreground tracker started (poll=%ss)", self.poll_seconds)

    def stop(self) -> None:
        with self._lock:
            self._stop_event.set()
            thread = self._thread
            self._thread = None
        if thread is not None:
            thread.join(timeout=self.poll_seconds + 2)
        logger.info("Foreground tracker stopped")

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                event = self._sample_foreground_event()
                if event is not None:
                    self.on_event(event)
            except Exception:
                logger.exception("Foreground sampling failed")
            self._stop_event.wait(self.poll_seconds)

    def _sample_foreground_event(self) -> ForegroundWindowEvent | None:
        if _user32 is None:
            return None

        hwnd = _user32.GetForegroundWindow()
        if not hwnd:
            return None

        pid_ref = ctypes.c_ulong(0)
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_ref))
        pid = int(pid_ref.value or 0)
        if pid <= 0:
            return None

        try:
            proc = psutil.Process(pid)
            process_name = str(proc.name() or "").strip()
            executable_path = str(proc.exe() or "").strip()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
            return None

        if not process_name:
            return None

        return ForegroundWindowEvent(
            pid=pid,
            process_name=process_name,
            executable_path=executable_path,
            window_title=_read_window_title(hwnd),
            timestamp=time.time(),
        )
