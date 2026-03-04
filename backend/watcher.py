from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from utils import is_supported_file, normalize_windows_path

logger = logging.getLogger(__name__)


class _DebouncedFileEventHandler(FileSystemEventHandler):
    def __init__(
        self,
        on_upsert: Callable[[str], None],
        on_delete: Callable[[str], None],
        debounce_seconds: float = 1.0,
    ) -> None:
        super().__init__()
        self.on_upsert = on_upsert
        self.on_delete = on_delete
        self.debounce_seconds = debounce_seconds
        self._lock = threading.Lock()
        self._last_seen: dict[str, float] = {}

    def _is_duplicate(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            previous = self._last_seen.get(key, 0.0)
            if now - previous < self.debounce_seconds:
                return True
            self._last_seen[key] = now
            return False

    def on_created(self, event: FileSystemEvent) -> None:
        self._handle_upsert(event, event_type="created")

    def on_modified(self, event: FileSystemEvent) -> None:
        self._handle_upsert(event, event_type="modified")

    def on_deleted(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        path = normalize_windows_path(event.src_path)
        dedupe_key = f"deleted:{path.lower()}"
        if self._is_duplicate(dedupe_key):
            return
        self.on_delete(path)

    def _handle_upsert(self, event: FileSystemEvent, event_type: str) -> None:
        if event.is_directory:
            return
        path = Path(event.src_path)
        if not is_supported_file(path):
            return

        normalized = normalize_windows_path(path)
        dedupe_key = f"{event_type}:{normalized.lower()}"
        if self._is_duplicate(dedupe_key):
            return
        self.on_upsert(normalized)


class DirectoryWatcher:
    def __init__(self, on_upsert: Callable[[str], None], on_delete: Callable[[str], None]) -> None:
        self.on_upsert = on_upsert
        self.on_delete = on_delete
        self._observer: Observer | None = None
        self._path: str | None = None
        self._lock = threading.RLock()

    @property
    def watched_path(self) -> str | None:
        return self._path

    def start(self, path: str) -> None:
        normalized = normalize_windows_path(path)
        with self._lock:
            self.stop()
            event_handler = _DebouncedFileEventHandler(self.on_upsert, self.on_delete, debounce_seconds=1.0)
            observer = Observer()
            observer.schedule(event_handler, normalized, recursive=True)
            observer.daemon = True
            observer.start()
            self._observer = observer
            self._path = normalized
            logger.info("Started watcher on %s", normalized)

    def stop(self) -> None:
        with self._lock:
            if self._observer:
                self._observer.stop()
                self._observer.join(timeout=3.0)
                self._observer = None
                logger.info("Stopped watcher")
            self._path = None
