from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from utils import now_ts


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
        self.schema_reset = False
        self._initialize()

    def _initialize(self) -> None:
        with self._lock, self._conn:
            self._conn.execute("PRAGMA foreign_keys = ON;")
            self._maybe_reset_legacy_schema()
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_path TEXT NOT NULL UNIQUE,
                    file_name TEXT NOT NULL,
                    file_type TEXT NOT NULL,
                    extension TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    modified_time REAL NOT NULL,
                    created_time REAL NOT NULL,
                    last_indexed_time REAL NOT NULL,
                    sha256 TEXT,
                    is_binary INTEGER NOT NULL DEFAULT 0,
                    summary TEXT,
                    topics_json TEXT,
                    cluster_id INTEGER,
                    cluster_label TEXT,
                    context_label TEXT
                );

                CREATE TABLE IF NOT EXISTS chunks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_id INTEGER NOT NULL,
                    file_path TEXT NOT NULL,
                    file_name TEXT NOT NULL,
                    file_type TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    token_count INTEGER NOT NULL,
                    embedding BLOB NOT NULL,
                    timestamp REAL NOT NULL,
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

                CREATE TABLE IF NOT EXISTS file_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_path TEXT NOT NULL,
                    file_name TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    event_time REAL NOT NULL,
                    source TEXT NOT NULL,
                    details TEXT
                );

                CREATE TABLE IF NOT EXISTS process_activity (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    process_name TEXT NOT NULL,
                    app_name TEXT,
                    category TEXT NOT NULL DEFAULT 'system',
                    launcher_name TEXT,
                    executable_name TEXT,
                    pid INTEGER NOT NULL,
                    start_time REAL NOT NULL,
                    end_time REAL NOT NULL,
                    duration_seconds REAL NOT NULL,
                    date TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS app_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_files_path ON files(file_path);
                CREATE INDEX IF NOT EXISTS idx_files_cluster ON files(cluster_id);
                CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks(file_id);
                CREATE INDEX IF NOT EXISTS idx_faiss_file ON faiss_mapping(file_id);
                CREATE INDEX IF NOT EXISTS idx_file_events_time ON file_events(event_time);
                CREATE INDEX IF NOT EXISTS idx_file_events_type_time ON file_events(event_type, event_time DESC);
                CREATE INDEX IF NOT EXISTS idx_process_activity_time ON process_activity(start_time, end_time);
                """
            )
        # Idempotent migrations add new columns to existing databases
        # without triggering a full schema reset.
        self._migrate_files()
        self._migrate_process_activity()

    def _maybe_reset_legacy_schema(self) -> None:
        tables = {
            str(row["name"])
            for row in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if "files" not in tables:
            return

        file_columns = {
            str(row["name"])
            for row in self._conn.execute("PRAGMA table_info(files)").fetchall()
        }
        required_file_columns = {"file_path", "file_name", "file_type", "extension", "is_binary"}
        chunks_columns = set()
        if "chunks" in tables:
            chunks_columns = {
                str(row["name"])
                for row in self._conn.execute("PRAGMA table_info(chunks)").fetchall()
            }
        required_chunk_columns = {"file_path", "file_name", "file_type", "embedding"}

        if required_file_columns.issubset(file_columns) and required_chunk_columns.issubset(chunks_columns):
            return

        self.schema_reset = True
        # The previous project version used an incompatible schema.
        # Resetting ensures current queries and indexes stay consistent.
        self._conn.executescript(
            """
            DROP TABLE IF EXISTS faiss_mapping;
            DROP TABLE IF EXISTS chunks;
            DROP TABLE IF EXISTS files;
            DROP TABLE IF EXISTS file_events;
            DROP TABLE IF EXISTS process_activity;
            DROP TABLE IF EXISTS app_state;
            """
        )

    def _migrate_files(self) -> None:
        """Add summary/topic columns to files if missing."""
        existing = {
            str(row["name"])
            for row in self._conn.execute(
                "PRAGMA table_info(files)"
            ).fetchall()
        }
        migrations = [
            ("summary", "TEXT"),
            ("topics_json", "TEXT"),
        ]
        with self._lock, self._conn:
            for col, definition in migrations:
                if col not in existing:
                    try:
                        self._conn.execute(
                            f"ALTER TABLE files ADD COLUMN {col} {definition}"
                        )
                    except Exception:
                        pass

    def _migrate_process_activity(self) -> None:
        """Add activity metadata columns to process_activity if missing.

        Uses ALTER TABLE ADD COLUMN so existing rows are preserved.
        The IF NOT EXISTS guard is emulated via PRAGMA table_info check
        because SQLite does not support ALTER TABLE ADD COLUMN IF NOT EXISTS.
        """
        existing = {
            str(row["name"])
            for row in self._conn.execute(
                "PRAGMA table_info(process_activity)"
            ).fetchall()
        }
        migrations = [
            ("app_name", "TEXT"),
            ("category", "TEXT NOT NULL DEFAULT 'system'"),
            ("launcher_name", "TEXT"),
            ("executable_name", "TEXT"),
            ("date", "TEXT NOT NULL DEFAULT ''"),
        ]
        with self._lock, self._conn:
            for col, definition in migrations:
                if col not in existing:
                    try:
                        self._conn.execute(
                            f"ALTER TABLE process_activity ADD COLUMN {col} {definition}"
                        )
                    except Exception:
                        pass  # Concurrent write or already exists - safe to ignore

            self._conn.execute(
                """
                UPDATE process_activity
                SET date = strftime('%Y-%m-%d', start_time, 'unixepoch', 'localtime')
                WHERE COALESCE(date, '') = ''
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_process_activity_date
                ON process_activity(date, start_time)
                """
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def get_file_by_path(self, file_path: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM files WHERE file_path = ?", (file_path,)).fetchone()
        return _row_to_dict(row)

    def upsert_file(
        self,
        *,
        file_path: str,
        file_name: str,
        file_type: str,
        extension: str,
        size_bytes: int,
        modified_time: float,
        created_time: float,
        sha256: str | None,
        is_binary: bool,
        summary: str | None = None,
        topics_json: str | None = None,
    ) -> tuple[int, bool]:
        with self._lock, self._conn:
            existing = self._conn.execute(
                "SELECT id FROM files WHERE file_path = ?",
                (file_path,),
            ).fetchone()
            indexed_at = now_ts()
            if existing:
                file_id = int(existing["id"])
                self._conn.execute(
                    """
                    UPDATE files
                    SET file_name = ?, file_type = ?, extension = ?, size_bytes = ?,
                        modified_time = ?, created_time = ?, last_indexed_time = ?,
                        sha256 = ?, is_binary = ?,
                        summary = COALESCE(?, summary),
                        topics_json = COALESCE(?, topics_json)
                    WHERE id = ?
                    """,
                    (
                        file_name,
                        file_type,
                        extension,
                        size_bytes,
                        modified_time,
                        created_time,
                        indexed_at,
                        sha256,
                        int(is_binary),
                        summary,
                        topics_json,
                        file_id,
                    ),
                )
                return file_id, False

            cursor = self._conn.execute(
                """
                INSERT INTO files (
                    file_path, file_name, file_type, extension, size_bytes, modified_time,
                    created_time, last_indexed_time, sha256, is_binary, summary, topics_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    file_path,
                    file_name,
                    file_type,
                    extension,
                    size_bytes,
                    modified_time,
                    created_time,
                    indexed_at,
                    sha256,
                    int(is_binary),
                    summary,
                    topics_json,
                ),
            )
            return int(cursor.lastrowid), True

    def update_file_cluster(
        self,
        *,
        file_id: int,
        cluster_id: int | None,
        cluster_label: str | None,
        context_label: str | None,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE files
                SET cluster_id = ?, cluster_label = ?, context_label = ?
                WHERE id = ?
                """,
                (cluster_id, cluster_label, context_label, file_id),
            )

    def clear_all_clusters(self) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE files SET cluster_id = NULL, cluster_label = NULL, context_label = NULL WHERE is_binary = 0"
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

    def insert_chunks(
        self,
        *,
        file_id: int,
        chunks: list[tuple[int, str, str, str, str, int, bytes, float]],
    ) -> list[int]:
        chunk_ids: list[int] = []
        with self._lock, self._conn:
            for (
                chunk_index,
                file_path,
                file_name,
                file_type,
                content,
                token_count,
                embedding_blob,
                timestamp,
            ) in chunks:
                cursor = self._conn.execute(
                    """
                    INSERT INTO chunks (
                        file_id, file_path, file_name, file_type, chunk_index, content, token_count, embedding, timestamp
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        file_id,
                        file_path,
                        file_name,
                        file_type,
                        chunk_index,
                        content,
                        token_count,
                        embedding_blob,
                        timestamp,
                    ),
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

    def delete_file_by_path(self, file_path: str) -> tuple[list[int], str | None]:
        with self._lock, self._conn:
            row = self._conn.execute("SELECT id, file_name FROM files WHERE file_path = ?", (file_path,)).fetchone()
            if row is None:
                return [], None
            file_id = int(row["id"])
            file_name = str(row["file_name"])
            faiss_rows = self._conn.execute(
                "SELECT faiss_id FROM faiss_mapping WHERE file_id = ?",
                (file_id,),
            ).fetchall()
            faiss_ids = [int(item["faiss_id"]) for item in faiss_rows]
            self._conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
        return faiss_ids, file_name

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
                c.timestamp AS chunk_timestamp,
                f.file_path,
                f.file_name,
                f.extension,
                f.file_type,
                f.modified_time,
                f.summary,
                f.topics_json,
                f.cluster_id,
                f.cluster_label,
                f.context_label
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

    def fetch_text_file_embeddings(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT
                    f.id AS file_id,
                    f.file_name,
                    f.file_path,
                    f.file_type,
                    f.extension,
                    f.modified_time,
                    c.content,
                    c.chunk_index,
                    c.embedding
                FROM files f
                JOIN chunks c ON c.file_id = f.id
                WHERE f.is_binary = 0
                ORDER BY f.id ASC, c.chunk_index ASC
                """
            ).fetchall()
        return [_row_to_dict(row) or {} for row in rows]

    def fetch_chunks_for_file(self, file_id: int, limit: int = 4) -> list[str]:
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

    def fetch_chunk_embeddings_for_files(self, file_ids: list[int]) -> list[dict[str, Any]]:
        if not file_ids:
            return []
        placeholders = ", ".join(["?"] * len(file_ids))
        query = f"""
            SELECT
                c.file_id,
                c.chunk_index,
                c.content,
                c.embedding
            FROM chunks c
            WHERE c.file_id IN ({placeholders})
            ORDER BY c.file_id ASC, c.chunk_index ASC
        """
        with self._lock:
            rows = self._conn.execute(query, tuple(file_ids)).fetchall()
        return [_row_to_dict(row) or {} for row in rows]

    def fetch_clustered_files(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT
                    id,
                    file_path,
                    file_name,
                    file_type,
                    extension,
                    modified_time,
                    summary,
                    topics_json,
                    cluster_id,
                    cluster_label,
                    context_label,
                    is_binary
                FROM files
                ORDER BY modified_time DESC
                """
            ).fetchall()
        return [_row_to_dict(row) or {} for row in rows]

    def fetch_files_by_paths(self, paths: list[str]) -> list[dict[str, Any]]:
        if not paths:
            return []
        placeholders = ", ".join(["?"] * len(paths))
        query = f"""
            SELECT
                id,
                file_path,
                file_name,
                file_type,
                extension,
                modified_time,
                summary,
                topics_json,
                cluster_id,
                cluster_label,
                context_label,
                is_binary
            FROM files
            WHERE file_path IN ({placeholders})
        """
        with self._lock:
            rows = self._conn.execute(query, tuple(paths)).fetchall()
        return [_row_to_dict(row) or {} for row in rows]

    def fetch_files_by_names(self, file_names: list[str]) -> list[dict[str, Any]]:
        cleaned = [str(name).strip() for name in file_names if str(name).strip()]
        if not cleaned:
            return []
        placeholders = ", ".join(["?"] * len(cleaned))
        query = f"""
            SELECT
                id,
                file_path,
                file_name,
                file_type,
                extension,
                modified_time,
                summary,
                topics_json,
                cluster_id,
                cluster_label,
                context_label,
                is_binary
            FROM files
            WHERE LOWER(file_name) IN ({placeholders})
        """
        params = tuple(name.lower() for name in cleaned)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [_row_to_dict(row) or {} for row in rows]

    def record_file_event(
        self,
        *,
        file_path: str,
        file_name: str,
        event_type: str,
        source: str,
        details: str | None = None,
        event_time: float | None = None,
    ) -> None:
        event_time = event_time if event_time is not None else now_ts()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO file_events (file_path, file_name, event_type, event_time, source, details)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (file_path, file_name, event_type, event_time, source, details),
            )

    def fetch_file_events(self, *, start_time: float) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, file_path, file_name, event_type, event_time, source, details
                FROM file_events
                WHERE event_time >= ?
                ORDER BY event_time ASC
                """,
                (start_time,),
            ).fetchall()
        return [_row_to_dict(row) or {} for row in rows]

    def fetch_recent_file_events(
        self,
        *,
        event_type: str,
        start_time: float,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, file_path, file_name, event_type, event_time, source, details
                FROM file_events
                WHERE event_type = ? AND event_time >= ?
                ORDER BY event_time DESC
                LIMIT ?
                """,
                (event_type, start_time, max(1, int(limit))),
            ).fetchall()
        return [_row_to_dict(row) or {} for row in rows]

    def record_process_session(
        self,
        *,
        process_name: str,
        app_name: str | None = None,
        category: str = "system",
        launcher_name: str | None = None,
        executable_name: str | None = None,
        pid: int,
        start_time: float,
        end_time: float,
        date: str | None = None,
    ) -> None:
        duration = max(0.0, end_time - start_time)
        if not date:
            date = datetime.fromtimestamp(start_time).strftime("%Y-%m-%d")
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO process_activity
                    (process_name, app_name, category, launcher_name, executable_name, pid, start_time, end_time, duration_seconds, date)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    process_name,
                    app_name,
                    category,
                    launcher_name,
                    executable_name,
                    int(pid),
                    start_time,
                    end_time,
                    duration,
                    date,
                ),
            )

    def fetch_process_activity(self, *, start_time: float) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, process_name,
                       COALESCE(app_name, process_name) AS app_name,
                       COALESCE(category, 'system') AS category,
                       launcher_name,
                       COALESCE(executable_name, process_name) AS executable_name,
                       COALESCE(date, strftime('%Y-%m-%d', start_time, 'unixepoch', 'localtime')) AS date,
                       pid, start_time, end_time, duration_seconds
                FROM process_activity
                WHERE end_time >= ?
                ORDER BY start_time ASC
                """,
                (start_time,),
            ).fetchall()
        return [_row_to_dict(row) or {} for row in rows]

    def get_activity_stats(self, *, start_time: float) -> dict[str, Any]:
        with self._lock:
            total_seconds = float(
                self._conn.execute(
                    "SELECT COALESCE(SUM(duration_seconds), 0) FROM process_activity WHERE end_time >= ?",
                    (start_time,),
                ).fetchone()[0]
            )
            rows = self._conn.execute(
                """
                SELECT
                    process_name,
                    MAX(COALESCE(app_name, process_name)) AS app_name,
                    MAX(COALESCE(category, 'system'))      AS category,
                    MAX(launcher_name)                     AS launcher_name,
                    MAX(COALESCE(executable_name, process_name)) AS executable_name,
                    SUM(duration_seconds)                  AS total_seconds,
                    COUNT(*)                               AS sessions
                FROM process_activity
                WHERE end_time >= ?
                GROUP BY process_name
                ORDER BY total_seconds DESC
                LIMIT 20
                """,
                (start_time,),
            ).fetchall()

        by_process = [
            {
                "process_name": str(row["process_name"]),
                "app_name": str(row["app_name"]) if row["app_name"] else None,
                "category": str(row["category"]),
                "launcher_name": str(row["launcher_name"]) if row["launcher_name"] else None,
                "executable_name": str(row["executable_name"]) if row["executable_name"] else str(row["process_name"]),
                "total_seconds": float(row["total_seconds"]),
                "sessions": int(row["sessions"]),
            }
            for row in rows
        ]
        return {"total_seconds": total_seconds, "by_process": by_process}

    def get_index_counts(self) -> dict[str, int]:
        with self._lock:
            files_total = int(self._conn.execute("SELECT COUNT(*) FROM files").fetchone()[0])
            text_files = int(self._conn.execute("SELECT COUNT(*) FROM files WHERE is_binary = 0").fetchone()[0])
            binary_files = int(self._conn.execute("SELECT COUNT(*) FROM files WHERE is_binary = 1").fetchone()[0])
            chunks = int(self._conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
            vectors = int(self._conn.execute("SELECT COUNT(*) FROM faiss_mapping").fetchone()[0])
        return {
            "files_total": files_total,
            "text_files": text_files,
            "binary_files": binary_files,
            "chunks": chunks,
            "vectors": vectors,
        }

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
