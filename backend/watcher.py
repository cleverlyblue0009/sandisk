from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable, Iterable

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from utils import is_binary_metadata_only, is_supported_text_file, normalize_windows_path

logger = logging.getLogger(__name__)


def _is_trackable(path: Path) -> bool:
    return is_supported_text_file(path) or is_binary_metadata_only(path)


class _DebouncedFileEventHandler(FileSystemEventHandler):
    def __init__(
        self,
        *,
        on_created: Callable[[str], None],
        on_modified: Callable[[str], None],
        on_deleted: Callable[[str], None],
        debounce_seconds: float = 1.0,
    ) -> None:
        super().__init__()
        self._on_created_callback = on_created
        self._on_modified_callback = on_modified
        self._on_deleted_callback = on_deleted
        self.debounce_seconds = debounce_seconds
        self._lock = threading.Lock()
        self._last_seen: dict[str, float] = {}

    def _is_duplicate(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            last = self._last_seen.get(key, 0.0)
            if now - last < self.debounce_seconds:
                return True
            self._last_seen[key] = now
            return False

    def on_created(self, event: FileSystemEvent) -> None:
        self._handle_upsert(event, "created")

    def on_modified(self, event: FileSystemEvent) -> None:
        self._handle_upsert(event, "modified")

    def on_deleted(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        path = Path(event.src_path)
        if not _is_trackable(path):
            return
        normalized = normalize_windows_path(path)
        key = f"deleted:{normalized.lower()}"
        if self._is_duplicate(key):
            return
        self._on_deleted_callback(normalized)

    def _handle_upsert(self, event: FileSystemEvent, tag: str) -> None:
        if event.is_directory:
            return
        path = Path(event.src_path)
        if not _is_trackable(path):
            return
        normalized = normalize_windows_path(path)
        key = f"{tag}:{normalized.lower()}"
        if self._is_duplicate(key):
            return
        if tag == "created":
            self._on_created_callback(normalized)
        else:
            self._on_modified_callback(normalized)


class DirectoryWatcher:
    def __init__(
        self,
        *,
        on_created: Callable[[str], None],
        on_modified: Callable[[str], None],
        on_deleted: Callable[[str], None],
    ) -> None:
        self.on_created = on_created
        self.on_modified = on_modified
        self.on_deleted = on_deleted
        self._observer: Observer | None = None
        self._paths: list[str] = []
        self._lock = threading.RLock()

    @property
    def watched_paths(self) -> list[str]:
        return list(self._paths)

    def start(self, paths: Iterable[str | Path]) -> None:
        normalized_paths = []
        for path in paths:
            candidate = Path(path).expanduser().resolve()
            if candidate.exists() and candidate.is_dir():
                normalized_paths.append(normalize_windows_path(candidate))
        if not normalized_paths:
            return

        with self._lock:
            self.stop()
            handler = _DebouncedFileEventHandler(
                on_created=self.on_created,
                on_modified=self.on_modified,
                on_deleted=self.on_deleted,
            )
            observer = Observer()
            for root in normalized_paths:
                observer.schedule(handler, root, recursive=True)
                logger.info("Watching root: %s", root)
            observer.daemon = True
            observer.start()
            self._observer = observer
            self._paths = normalized_paths
            logger.info("Watcher started for %s roots", len(normalized_paths))

    def stop(self) -> None:
        with self._lock:
            if self._observer is not None:
                self._observer.stop()
                self._observer.join(timeout=5.0)
                self._observer = None
                logger.info("Watcher stopped")
            self._paths = []
