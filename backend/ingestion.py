from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable

import numpy as np

from config import Settings
from database import Database
from embedding import EmbeddingEngine, FaissStore
from extractor import TextExtractor
from hashing import compute_sha256
from summarizer import extract_topics, summarize
from utils import (
    chunk_text,
    classify_file_type,
    count_tokens,
    file_extension,
    is_binary_metadata_only,
    is_supported_text_file,
    normalize_windows_path,
)

logger = logging.getLogger(__name__)


@dataclass
class ScanStats:
    total_files_seen: int = 0
    text_indexed: int = 0
    text_updated: int = 0
    text_unchanged: int = 0
    binary_metadata: int = 0
    unsupported: int = 0
    deleted: int = 0
    failed: int = 0

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


class IngestionService:
    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
        extractor: TextExtractor,
        embedding_engine: EmbeddingEngine,
        faiss_store: FaissStore,
    ) -> None:
        self.settings = settings
        self.database = database
        self.extractor = extractor
        self.embedding_engine = embedding_engine
        self.faiss_store = faiss_store
        self._lock = threading.RLock()

    def scan_directories(
        self,
        roots: Iterable[Path],
        progress_callback: Callable[[dict[str, int]], None] | None = None,
    ) -> dict[str, int]:
        stats = ScanStats()
        for root in roots:
            if not root.exists() or not root.is_dir():
                continue
            for candidate in root.rglob("*"):
                if not candidate.is_file():
                    continue
                stats.total_files_seen += 1
                try:
                    result = self.process_file(candidate, source="scan", event_type="file_modified")
                    if result == "indexed":
                        stats.text_indexed += 1
                    elif result == "updated":
                        stats.text_updated += 1
                    elif result == "unchanged":
                        stats.text_unchanged += 1
                    elif result == "binary_metadata":
                        stats.binary_metadata += 1
                    elif result == "unsupported":
                        stats.unsupported += 1
                except Exception:
                    logger.exception("Failed to index %s", candidate)
                    stats.failed += 1
                finally:
                    if progress_callback:
                        progress_callback(stats.to_dict())
        return stats.to_dict()

    def process_file(
        self,
        file_path: str | Path,
        *,
        source: str,
        event_type: str,
    ) -> str:
        with self._lock:
            path = Path(file_path).expanduser().resolve()
            if not path.exists() or not path.is_file():
                return "unsupported"

            normalized_path = normalize_windows_path(path)
            file_name = path.name
            extension = file_extension(path)
            file_type = classify_file_type(path)
            stat = path.stat()
            size_bytes = int(stat.st_size)
            modified_time = float(stat.st_mtime)
            created_time = float(stat.st_ctime)
            existing = self.database.get_file_by_path(normalized_path)

            if is_binary_metadata_only(path):
                # Binary/media archives are tracked as metadata only and never opened.
                file_id, _ = self.database.upsert_file(
                    file_path=normalized_path,
                    file_name=file_name,
                    file_type=file_type,
                    extension=extension,
                    size_bytes=size_bytes,
                    modified_time=modified_time,
                    created_time=created_time,
                    sha256=None,
                    is_binary=True,
                )
                if existing and int(existing.get("is_binary", 0)) == 0:
                    removed = self.database.clear_chunks_for_file(file_id)
                    self.faiss_store.remove(removed)
                self.database.record_file_event(
                    file_path=normalized_path,
                    file_name=file_name,
                    event_type=event_type,
                    source=source,
                    details="metadata-only",
                )
                return "binary_metadata"

            if not is_supported_text_file(path):
                return "unsupported"

            if size_bytes > self.settings.max_file_size_mb * 1024 * 1024:
                self.database.record_file_event(
                    file_path=normalized_path,
                    file_name=file_name,
                    event_type=event_type,
                    source=source,
                    details="skipped-size-limit",
                )
                return "unsupported"

            file_hash = compute_sha256(path)
            if existing and str(existing.get("sha256", "")) == file_hash:
                self.database.upsert_file(
                    file_path=normalized_path,
                    file_name=file_name,
                    file_type=file_type,
                    extension=extension,
                    size_bytes=size_bytes,
                    modified_time=modified_time,
                    created_time=created_time,
                    sha256=file_hash,
                    is_binary=False,
                )
                self.database.record_file_event(
                    file_path=normalized_path,
                    file_name=file_name,
                    event_type=event_type,
                    source=source,
                    details="unchanged",
                )
                return "unchanged"

            raw_text = self.extractor.extract_text(path).strip()
            if not raw_text:
                raw_text = f"{file_name}\n{normalized_path}"

            # Keep chunks in the requested 500-800 token range via settings defaults.
            chunks = chunk_text(
                raw_text,
                chunk_size_tokens=self.settings.chunk_size_tokens,
                overlap_tokens=self.settings.chunk_overlap_tokens,
            )
            if not chunks:
                chunks = [raw_text]

            vectors = self.embedding_engine.encode_texts(chunks)
            if vectors.shape[0] != len(chunks):
                raise RuntimeError("Embedding count mismatch")

            file_id, created = self.database.upsert_file(
                file_path=normalized_path,
                file_name=file_name,
                file_type=file_type,
                extension=extension,
                size_bytes=size_bytes,
                modified_time=modified_time,
                created_time=created_time,
                sha256=file_hash,
                is_binary=False,
            )
            removed = self.database.clear_chunks_for_file(file_id)
            self.faiss_store.remove(removed)

            chunk_rows: list[tuple[int, str, str, str, str, int, bytes, float]] = []
            for index, chunk in enumerate(chunks):
                embedding_blob = np.asarray(vectors[index], dtype=np.float32).tobytes()
                chunk_rows.append(
                    (
                        index,
                        normalized_path,
                        file_name,
                        file_type,
                        chunk,
                        count_tokens(chunk),
                        embedding_blob,
                        modified_time,
                    )
                )
            chunk_ids = self.database.insert_chunks(file_id=file_id, chunks=chunk_rows)

            # Use chunk IDs as FAISS vector IDs for direct metadata joins.
            self.faiss_store.add(vectors=vectors, ids=chunk_ids)
            mappings = [(chunk_id, file_id, chunk_id) for chunk_id in chunk_ids]
            self.database.insert_faiss_mappings(mappings)

            # Precompute summaries/topics at index time so search responses stay fast.
            summary_text = summarize(" ".join(chunks[:4]), max_sentences=3, max_chars=320)
            topics = extract_topics(" ".join(chunks[:4]), limit=6)
            self.database.upsert_file(
                file_path=normalized_path,
                file_name=file_name,
                file_type=file_type,
                extension=extension,
                size_bytes=size_bytes,
                modified_time=modified_time,
                created_time=created_time,
                sha256=file_hash,
                is_binary=False,
                summary=summary_text,
                topics_json=json.dumps(topics, ensure_ascii=False),
            )

            self.database.record_file_event(
                file_path=normalized_path,
                file_name=file_name,
                event_type="file_created" if created else event_type,
                source=source,
                details="reindexed",
            )
            return "indexed" if created else "updated"

    def delete_file(self, file_path: str | Path, *, source: str = "watchdog") -> bool:
        with self._lock:
            normalized_path = normalize_windows_path(file_path)
            removed_ids, file_name = self.database.delete_file_by_path(normalized_path)
            if removed_ids:
                self.faiss_store.remove(removed_ids)
            self.database.record_file_event(
                file_path=normalized_path,
                file_name=file_name or Path(normalized_path).name,
                event_type="file_deleted",
                source=source,
                details=None,
            )
            return bool(removed_ids)
