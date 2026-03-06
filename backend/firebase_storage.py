from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class FirebaseStorage:
    """Firestore-first storage with local SQLite fallback cache."""

    def __init__(self, cache_db_path: Path | str) -> None:
        self.cache_db_path = Path(cache_db_path).expanduser().resolve()
        self.cache_db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.cache_db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._firestore = self._init_firestore_client()
        self._initialize_cache_schema()
        self._migrate_cache_schema()

    @property
    def firestore_enabled(self) -> bool:
        return self._firestore is not None

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _init_firestore_client(self) -> Any | None:
        try:
            import firebase_admin
            from firebase_admin import credentials, firestore
        except Exception:
            logger.warning("firebase-admin is unavailable; using local cache fallback.")
            return None

        try:
            if not firebase_admin._apps:
                cred_path = (
                    os.getenv("FIREBASE_CREDENTIALS_JSON", "").strip()
                    or os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
                )
                if cred_path and Path(cred_path).exists():
                    cred = credentials.Certificate(cred_path)
                    firebase_admin.initialize_app(cred)
                else:
                    firebase_admin.initialize_app()
            return firestore.client()
        except Exception as exc:
            logger.warning("Firestore init failed, continuing with local cache only: %s", exc)
            return None

    def _initialize_cache_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS activity_sessions_cache (
                    session_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    app_name TEXT NOT NULL,
                    activity_name TEXT,
                    browser_name TEXT,
                    activity_type TEXT,
                    process_name TEXT NOT NULL,
                    executable_path TEXT,
                    window_title TEXT,
                    category TEXT NOT NULL,
                    start_time REAL NOT NULL,
                    end_time REAL NOT NULL,
                    duration_seconds REAL NOT NULL,
                    date TEXT NOT NULL,
                    domain TEXT,
                    document_name TEXT,
                    file_extension TEXT,
                    video_title TEXT,
                    youtube_category TEXT,
                    source TEXT NOT NULL DEFAULT 'foreground',
                    synced INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS insights_cache (
                    insight_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    insight TEXT NOT NULL,
                    period TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    synced INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_sessions_user_date
                    ON activity_sessions_cache(user_id, date, start_time DESC);
                CREATE INDEX IF NOT EXISTS idx_sessions_sync
                    ON activity_sessions_cache(synced, updated_at);
                CREATE INDEX IF NOT EXISTS idx_insights_sync
                    ON insights_cache(synced, created_at);
                """
            )

    def _migrate_cache_schema(self) -> None:
        with self._lock, self._conn:
            existing = {
                str(row["name"])
                for row in self._conn.execute(
                    "PRAGMA table_info(activity_sessions_cache)"
                ).fetchall()
            }
            migrations = [
                ("activity_name", "TEXT"),
                ("browser_name", "TEXT"),
                ("activity_type", "TEXT"),
                ("video_title", "TEXT"),
                ("youtube_category", "TEXT"),
            ]
            for column, definition in migrations:
                if column in existing:
                    continue
                self._conn.execute(
                    f"ALTER TABLE activity_sessions_cache ADD COLUMN {column} {definition}"
                )

    def set_meta(self, key: str, value: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO meta (key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        return str(row["value"])

    def count_sessions(self, user_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS total FROM activity_sessions_cache WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return int(row["total"] if row else 0)

    def save_session(self, *, user_id: str, session: dict[str, Any]) -> None:
        payload = self._normalize_session_payload(user_id=user_id, session=session)
        synced = 1 if self._try_sync_session(payload) else 0

        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO activity_sessions_cache (
                    session_id, user_id, app_name, activity_name, browser_name, activity_type,
                    process_name, executable_path, window_title,
                    category, start_time, end_time, duration_seconds, date, domain,
                    document_name, file_extension, video_title, youtube_category,
                    source, synced, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    user_id = excluded.user_id,
                    app_name = excluded.app_name,
                    activity_name = excluded.activity_name,
                    browser_name = excluded.browser_name,
                    activity_type = excluded.activity_type,
                    process_name = excluded.process_name,
                    executable_path = excluded.executable_path,
                    window_title = excluded.window_title,
                    category = excluded.category,
                    start_time = excluded.start_time,
                    end_time = excluded.end_time,
                    duration_seconds = excluded.duration_seconds,
                    date = excluded.date,
                    domain = excluded.domain,
                    document_name = excluded.document_name,
                    file_extension = excluded.file_extension,
                    video_title = excluded.video_title,
                    youtube_category = excluded.youtube_category,
                    source = excluded.source,
                    synced = excluded.synced,
                    updated_at = excluded.updated_at
                """,
                (
                    payload["session_id"],
                    payload["user_id"],
                    payload["app_name"],
                    payload["activity_name"],
                    payload["browser_name"],
                    payload["activity_type"],
                    payload["process_name"],
                    payload["executable_path"],
                    payload["window_title"],
                    payload["category"],
                    payload["start_time"],
                    payload["end_time"],
                    payload["duration_seconds"],
                    payload["date"],
                    payload["domain"],
                    payload["document_name"],
                    payload["file_extension"],
                    payload["video_title"],
                    payload["youtube_category"],
                    payload["source"],
                    synced,
                    payload["updated_at"],
                ),
            )

    def fetch_sessions(
        self,
        *,
        user_id: str,
        start_time: float | None = None,
        end_time: float | None = None,
        limit: int = 3000,
    ) -> list[dict[str, Any]]:
        query = """
            SELECT
                session_id, user_id, app_name, activity_name, browser_name, activity_type,
                process_name, executable_path, window_title,
                category, start_time, end_time, duration_seconds, date, domain,
                document_name, file_extension, video_title, youtube_category,
                source, synced, updated_at
            FROM activity_sessions_cache
            WHERE user_id = ?
        """
        params: list[Any] = [user_id]

        if start_time is not None:
            query += " AND end_time >= ?"
            params.append(float(start_time))
        if end_time is not None:
            query += " AND start_time <= ?"
            params.append(float(end_time))

        query += " ORDER BY start_time DESC LIMIT ?"
        params.append(max(1, int(limit)))

        with self._lock:
            rows = self._conn.execute(query, tuple(params)).fetchall()
        return [{k: row[k] for k in row.keys()} for row in rows]

    def save_insights(self, *, user_id: str, insights: list[str], period: str) -> None:
        now = time.time()
        for idx, message in enumerate(insights):
            text = str(message).strip()
            if not text:
                continue
            insight_id = f"{int(now)}-{idx}-{abs(hash(text)) % 1000000}"
            synced = 1 if self._try_sync_insight(user_id=user_id, insight_id=insight_id, text=text, period=period, created_at=now) else 0
            with self._lock, self._conn:
                self._conn.execute(
                    """
                    INSERT INTO insights_cache (insight_id, user_id, insight, period, created_at, synced)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(insight_id) DO UPDATE SET
                        user_id = excluded.user_id,
                        insight = excluded.insight,
                        period = excluded.period,
                        created_at = excluded.created_at,
                        synced = excluded.synced
                    """,
                    (insight_id, user_id, text, period, now, synced),
                )

    def fetch_insights(self, *, user_id: str, days: int = 30) -> list[dict[str, Any]]:
        start_time = time.time() - (max(1, days) * 86400)
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT insight_id, user_id, insight, period, created_at, synced
                FROM insights_cache
                WHERE user_id = ? AND created_at >= ?
                ORDER BY created_at DESC
                LIMIT 200
                """,
                (user_id, start_time),
            ).fetchall()
        return [{k: row[k] for k in row.keys()} for row in rows]

    def sync_pending(self, *, user_id: str | None = None, limit: int = 100) -> None:
        if not self.firestore_enabled:
            return

        sessions_query = """
            SELECT * FROM activity_sessions_cache
            WHERE synced = 0
        """
        params: list[Any] = []
        if user_id:
            sessions_query += " AND user_id = ?"
            params.append(user_id)
        sessions_query += " ORDER BY updated_at ASC LIMIT ?"
        params.append(max(1, int(limit)))

        with self._lock:
            session_rows = self._conn.execute(sessions_query, tuple(params)).fetchall()

        for row in session_rows:
            payload = {k: row[k] for k in row.keys()}
            if self._try_sync_session(payload):
                with self._lock, self._conn:
                    self._conn.execute(
                        "UPDATE activity_sessions_cache SET synced = 1 WHERE session_id = ?",
                        (str(payload["session_id"]),),
                    )

        insights_query = """
            SELECT insight_id, user_id, insight, period, created_at
            FROM insights_cache
            WHERE synced = 0
            ORDER BY created_at ASC
            LIMIT ?
        """
        with self._lock:
            insight_rows = self._conn.execute(insights_query, (max(1, int(limit)),)).fetchall()

        for row in insight_rows:
            if self._try_sync_insight(
                user_id=str(row["user_id"]),
                insight_id=str(row["insight_id"]),
                text=str(row["insight"]),
                period=str(row["period"]),
                created_at=float(row["created_at"]),
            ):
                with self._lock, self._conn:
                    self._conn.execute(
                        "UPDATE insights_cache SET synced = 1 WHERE insight_id = ?",
                        (str(row["insight_id"]),),
                    )

    def _normalize_session_payload(self, *, user_id: str, session: dict[str, Any]) -> dict[str, Any]:
        now = time.time()
        return {
            "session_id": str(session.get("session_id") or ""),
            "user_id": str(user_id),
            "app_name": str(session.get("app_name") or "Unknown App"),
            "activity_name": str(session.get("activity_name") or ""),
            "browser_name": str(session.get("browser_name") or ""),
            "activity_type": str(session.get("activity_type") or ""),
            "process_name": str(session.get("process_name") or "unknown.exe"),
            "executable_path": str(session.get("executable_path") or ""),
            "window_title": str(session.get("window_title") or ""),
            "category": str(session.get("category") or "other"),
            "start_time": float(session.get("start_time") or now),
            "end_time": float(session.get("end_time") or session.get("start_time") or now),
            "duration_seconds": float(session.get("duration_seconds") or 0.0),
            "date": str(session.get("date") or ""),
            "domain": str(session.get("domain") or ""),
            "document_name": str(session.get("document_name") or ""),
            "file_extension": str(session.get("file_extension") or ""),
            "video_title": str(session.get("video_title") or ""),
            "youtube_category": str(session.get("youtube_category") or ""),
            "source": str(session.get("source") or "foreground"),
            "updated_at": float(session.get("updated_at") or now),
        }

    def _try_sync_session(self, payload: dict[str, Any]) -> bool:
        if not self.firestore_enabled:
            return False
        try:
            doc_id = str(payload["session_id"])
            if not doc_id:
                return False

            now = time.time()
            firestore_payload = dict(payload)
            firestore_payload["updated_at"] = now

            self._firestore.collection("activity_sessions").document(doc_id).set(firestore_payload, merge=True)
            self._firestore.collection("users").document(str(payload["user_id"])).set(
                {
                    "updated_at": now,
                    "last_activity_at": float(payload["end_time"]),
                },
                merge=True,
            )
            if payload.get("document_name"):
                self._firestore.collection("documents").document(doc_id).set(
                    {
                        "session_id": doc_id,
                        "user_id": str(payload["user_id"]),
                        "document_name": str(payload["document_name"]),
                        "file_extension": str(payload["file_extension"]),
                        "window_title": str(payload["window_title"]),
                        "date": str(payload["date"]),
                        "updated_at": now,
                    },
                    merge=True,
                )
            return True
        except Exception as exc:
            logger.debug("Firestore session sync failed: %s", exc)
            return False

    def _try_sync_insight(
        self,
        *,
        user_id: str,
        insight_id: str,
        text: str,
        period: str,
        created_at: float,
    ) -> bool:
        if not self.firestore_enabled:
            return False
        try:
            self._firestore.collection("insights").document(insight_id).set(
                {
                    "insight_id": insight_id,
                    "user_id": user_id,
                    "insight": text,
                    "period": period,
                    "created_at": created_at,
                },
                merge=True,
            )
            return True
        except Exception as exc:
            logger.debug("Firestore insight sync failed: %s", exc)
            return False
