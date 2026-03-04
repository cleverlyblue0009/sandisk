from __future__ import annotations

import logging
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from config import Settings
from database import Database
from embedding import EmbeddingEngine, FaissStore
from extractor import TextExtractor
from hashing import compute_sha256
from utils import categorize_file, chunk_text, count_tokens, is_supported_file, normalize_windows_path

logger = logging.getLogger(__name__)


@dataclass
class ScanStats:
    total_supported_files: int = 0
    scanned_files: int = 0
    indexed_files: int = 0
    updated_files: int = 0
    skipped_files: int = 0
    failed_files: int = 0

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


class IngestionService:
    def __init__(
        self,
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

    def _is_temp_or_locked_file(self, path: Path) -> bool:
        """Check if file is temporary or locked (Office temp files, etc)"""
        name = path.name.lower()
        # Skip Office temporary files (~$), system files (.), and common temp patterns
        return (
            name.startswith("~$")
            or name.startswith(".")
            or name.startswith("~")
            or name.endswith(".tmp")
            or name.endswith(".temp")
        )

    def _is_file_too_large(self, path: Path, max_size_mb: int = 500) -> bool:
        """Check if file exceeds memory-safe size limit"""
        try:
            size_mb = path.stat().st_size / (1024 * 1024)
            return size_mb > max_size_mb
        except (OSError, ValueError):
            return True

    def scan_directory(
        self,
        base_directory: str,
        progress_callback: Callable[[dict[str, int]], None] | None = None,
    ) -> dict[str, int]:
        base_path = Path(base_directory).expanduser().resolve()
        files = [
            path
            for path in base_path.rglob("*")
            if path.is_file() and is_supported_file(path)
        ]
        stats = ScanStats(total_supported_files=len(files))

        for file_path in files:
            try:
                # Skip temporary/locked files before processing
                if self._is_temp_or_locked_file(file_path):
                    stats.skipped_files += 1
                    logger.debug("Skipping temporary file: %s", file_path)
                    if progress_callback:
                        progress_callback(stats.to_dict())
                    continue

                # Skip extremely large files to prevent memory issues
                if self._is_file_too_large(file_path):
                    stats.skipped_files += 1
                    logger.warning("Skipping oversized file (>500MB): %s", file_path)
                    if progress_callback:
                        progress_callback(stats.to_dict())
                    continue

                result = self.process_file(file_path)
                stats.scanned_files += 1
                if result == "indexed":
                    stats.indexed_files += 1
                elif result == "updated":
                    stats.updated_files += 1
                elif result == "skipped":
                    stats.skipped_files += 1
            except MemoryError:
                stats.failed_files += 1
                logger.error("MemoryError processing file (too large): %s", file_path)
            except Exception:
                stats.failed_files += 1
                logger.exception("Failed to ingest file: %s", file_path)
            finally:
                if progress_callback:
                    progress_callback(stats.to_dict())

        return stats.to_dict()

    def process_file(self, file_path: str | Path) -> str:
        with self._lock:
            path = Path(file_path).expanduser().resolve()
            if not path.exists() or not path.is_file():
                return "skipped"
            if not is_supported_file(path):
                return "skipped"

            # Double-check for temp files
            if self._is_temp_or_locked_file(path):
                return "skipped"

            normalized_path = normalize_windows_path(path)
            extension = path.suffix.lower()
            category = categorize_file(path)
            
            try:
                file_hash = compute_sha256(path)
            except MemoryError:
                logger.error("MemoryError computing hash for: %s", path)
                raise
            
            file_size = int(path.stat().st_size)
            modified_time = float(path.stat().st_mtime)
            existing = self.database.get_file_by_path(normalized_path)

            if existing and existing["sha256"] == file_hash:
                self.database.touch_file(int(existing["id"]), size_bytes=file_size, modified_time=modified_time)
                return "skipped"

            try:
                extracted_text = self.extractor.extract_text(path)
            except MemoryError:
                logger.error("MemoryError extracting text from: %s", path)
                raise
            
            if not extracted_text.strip():
                extracted_text = path.name

            chunks = chunk_text(
                extracted_text,
                chunk_size_tokens=self.settings.chunk_size_tokens,
                overlap_tokens=self.settings.chunk_overlap_tokens,
            )
            if not chunks:
                chunks = [path.name]

            try:
                vectors = self.embedding_engine.encode_texts(chunks)
            except MemoryError:
                logger.error("MemoryError encoding embeddings for: %s", path)
                raise
            
            if vectors.shape[0] != len(chunks):
                raise RuntimeError("Embedding count mismatch")

            if existing:
                file_id = int(existing["id"])
                removed_faiss_ids = self.database.clear_chunks_for_file(file_id)
                self.faiss_store.remove(removed_faiss_ids)
                self.database.update_file(
                    file_id=file_id,
                    filename=path.name,
                    extension=extension,
                    category=category,
                    sha256=file_hash,
                    size_bytes=file_size,
                    modified_time=modified_time,
                )
                status = "updated"
            else:
                file_id = self.database.insert_file(
                    path=normalized_path,
                    filename=path.name,
                    extension=extension,
                    category=category,
                    sha256=file_hash,
                    size_bytes=file_size,
                    modified_time=modified_time,
                )
                status = "indexed"

            chunk_rows = [(idx, chunk, count_tokens(chunk)) for idx, chunk in enumerate(chunks)]
            chunk_ids = self.database.insert_chunks(file_id=file_id, chunks=chunk_rows)
            self.faiss_store.add(vectors=vectors, ids=chunk_ids)
            mappings = [(chunk_id, file_id, chunk_id) for chunk_id in chunk_ids]
            self.database.insert_faiss_mappings(mappings)
            return status

    def delete_file(self, file_path: str | Path) -> bool:
        with self._lock:
            normalized_path = normalize_windows_path(file_path)
            faiss_ids = self.database.delete_file_by_path(normalized_path)
            self.faiss_store.remove(faiss_ids)
            return bool(faiss_ids)