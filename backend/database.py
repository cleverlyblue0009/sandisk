from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


class Database:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._initialize()

    def _initialize(self) -> None:
        with self._lock, self._conn:
            self._conn.execute("PRAGMA foreign_keys = ON;")
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    path TEXT NOT NULL UNIQUE,
                    filename TEXT NOT NULL,
                    extension TEXT NOT NULL,
                    category TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    modified_time REAL NOT NULL,
                    indexed_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS chunks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_id INTEGER NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    token_count INTEGER NOT NULL,
                    FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE,
                    UNIQUE (file_id, chunk_index)
                );

                CREATE TABLE IF NOT EXISTS faiss_mapping (
                    faiss_id INTEGER PRIMARY KEY,
                    file_id INTEGER NOT NULL,
                    chunk_id INTEGER NOT NULL UNIQUE,
                    FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE,
                    FOREIGN KEY (chunk_id) REFERENCES chunks(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS app_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_files_path ON files(path);
                CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks(file_id);
                CREATE INDEX IF NOT EXISTS idx_faiss_file ON faiss_mapping(file_id);
                """
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def get_file_by_path(self, path: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM files WHERE path = ?", (path,)).fetchone()
        return _row_to_dict(row)

    def get_file_by_id(self, file_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
        return _row_to_dict(row)

    def insert_file(
        self,
        path: str,
        filename: str,
        extension: str,
        category: str,
        sha256: str,
        size_bytes: int,
        modified_time: float,
    ) -> int:
        now = time.time()
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                INSERT INTO files (
                    path, filename, extension, category, sha256, size_bytes, modified_time, indexed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (path, filename, extension, category, sha256, size_bytes, modified_time, now),
            )
            return int(cursor.lastrowid)

    def update_file(
        self,
        file_id: int,
        filename: str,
        extension: str,
        category: str,
        sha256: str,
        size_bytes: int,
        modified_time: float,
    ) -> None:
        now = time.time()
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE files
                SET filename = ?, extension = ?, category = ?, sha256 = ?,
                    size_bytes = ?, modified_time = ?, indexed_at = ?
                WHERE id = ?
                """,
                (filename, extension, category, sha256, size_bytes, modified_time, now, file_id),
            )

    def touch_file(self, file_id: int, size_bytes: int, modified_time: float) -> None:
        now = time.time()
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE files
                SET size_bytes = ?, modified_time = ?, indexed_at = ?
                WHERE id = ?
                """,
                (size_bytes, modified_time, now, file_id),
            )

    def clear_chunks_for_file(self, file_id: int) -> list[int]:
        with self._lock, self._conn:
            faiss_rows = self._conn.execute(
                "SELECT faiss_id FROM faiss_mapping WHERE file_id = ?",
                (file_id,),
            ).fetchall()
            faiss_ids = [int(row["faiss_id"]) for row in faiss_rows]
            self._conn.execute("DELETE FROM faiss_mapping WHERE file_id = ?", (file_id,))
            self._conn.execute("DELETE FROM chunks WHERE file_id = ?", (file_id,))
        return faiss_ids

    def delete_file_by_path(self, path: str) -> list[int]:
        with self._lock, self._conn:
            row = self._conn.execute("SELECT id FROM files WHERE path = ?", (path,)).fetchone()
            if row is None:
                return []
            file_id = int(row["id"])
            faiss_rows = self._conn.execute(
                "SELECT faiss_id FROM faiss_mapping WHERE file_id = ?",
                (file_id,),
            ).fetchall()
            faiss_ids = [int(r["faiss_id"]) for r in faiss_rows]
            self._conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
        return faiss_ids

    def insert_chunks(self, file_id: int, chunks: list[tuple[int, str, int]]) -> list[int]:
        chunk_ids: list[int] = []
        with self._lock, self._conn:
            for chunk_index, content, token_count in chunks:
                cursor = self._conn.execute(
                    """
                    INSERT INTO chunks (file_id, chunk_index, content, token_count)
                    VALUES (?, ?, ?, ?)
                    """,
                    (file_id, chunk_index, content, token_count),
                )
                chunk_ids.append(int(cursor.lastrowid))
        return chunk_ids

    def insert_faiss_mappings(self, mappings: list[tuple[int, int, int]]) -> None:
        if not mappings:
            return
        with self._lock, self._conn:
            self._conn.executemany(
                """
                INSERT OR REPLACE INTO faiss_mapping (faiss_id, file_id, chunk_id)
                VALUES (?, ?, ?)
                """,
                mappings,
            )

    def fetch_hits_by_faiss_ids(self, faiss_ids: list[int]) -> dict[int, dict[str, Any]]:
        if not faiss_ids:
            return {}
        placeholders = ", ".join(["?"] * len(faiss_ids))
        query = f"""
            SELECT
                fm.faiss_id,
                fm.file_id,
                fm.chunk_id,
                c.chunk_index,
                c.content,
                f.path,
                f.filename,
                f.extension,
                f.category,
                f.modified_time,
                f.indexed_at
            FROM faiss_mapping fm
            JOIN chunks c ON c.id = fm.chunk_id
            JOIN files f ON f.id = fm.file_id
            WHERE fm.faiss_id IN ({placeholders})
        """
        with self._lock:
            rows = self._conn.execute(query, tuple(faiss_ids)).fetchall()
        result: dict[int, dict[str, Any]] = {}
        for row in rows:
            result[int(row["faiss_id"])] = _row_to_dict(row) or {}
        return result

    def fetch_file_chunks(self, file_id: int, limit: int = 3) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT content
                FROM chunks
                WHERE file_id = ?
                ORDER BY chunk_index ASC
                LIMIT ?
                """,
                (file_id, limit),
            ).fetchall()
        return [str(row["content"]) for row in rows]

    def list_indexed_files(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, path, filename, extension, category, modified_time, indexed_at
                FROM files
                ORDER BY modified_time DESC
                """
            ).fetchall()
        return [_row_to_dict(row) or {} for row in rows]

    def get_counts(self) -> dict[str, int]:
        with self._lock:
            file_count = int(self._conn.execute("SELECT COUNT(*) FROM files").fetchone()[0])
            chunk_count = int(self._conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
            vector_count = int(self._conn.execute("SELECT COUNT(*) FROM faiss_mapping").fetchone()[0])
        return {"files": file_count, "chunks": chunk_count, "vectors": vector_count}

    def set_state(self, key: str, value: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO app_state (key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def get_state(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        return str(row["value"])
