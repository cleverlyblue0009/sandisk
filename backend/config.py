from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _default_scan_roots() -> list[Path]:
    home = Path.home()
    roots = [
        home / "Documents",
        home / "Downloads",
        home / "Desktop",
        home / "Pictures",
    ]
    return [root for root in roots if root.exists() and root.is_dir()]


@dataclass(frozen=True)
class Settings:
    app_name: str
    data_dir: Path
    db_path: Path
    faiss_index_path: Path
    embedding_model: str
    groq_model_query: str
    groq_api_key: str | None
    max_file_size_mb: int
    chunk_size_tokens: int
    chunk_overlap_tokens: int
    default_top_k: int
    default_result_limit: int
    scan_roots: list[Path]
    activity_poll_seconds: int
    session_gap_minutes: int
    timeline_days_default: int
    max_clusters: int


def get_settings() -> Settings:
    backend_dir = Path(__file__).resolve().parent
    default_data_dir = backend_dir / "data"
    data_dir = Path(os.getenv("APP_DATA_DIR", str(default_data_dir))).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    configured_roots = os.getenv("SCAN_ROOTS", "").strip()
    if configured_roots:
        scan_roots = [
            Path(item.strip()).expanduser().resolve()
            for item in configured_roots.split(";")
            if item.strip()
        ]
        scan_roots = [root for root in scan_roots if root.exists() and root.is_dir()]
    else:
        scan_roots = _default_scan_roots()

    return Settings(
        app_name="Personal Memory Assistant",
        data_dir=data_dir,
        db_path=Path(os.getenv("APP_DB_PATH", str(data_dir / "memory_assistant.db"))).expanduser().resolve(),
        faiss_index_path=Path(
            os.getenv("APP_FAISS_INDEX_PATH", str(data_dir / "memory_assistant.faiss"))
        ).expanduser().resolve(),
        embedding_model=os.getenv("EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2"),
        groq_model_query=os.getenv("GROQ_MODEL_QUERY", "llama-3.1-8b-instant"),
        groq_api_key=os.getenv("GROQ_API_KEY"),
        max_file_size_mb=int(os.getenv("MAX_FILE_SIZE_MB", "25")),
        chunk_size_tokens=int(os.getenv("CHUNK_SIZE_TOKENS", "650")),
        chunk_overlap_tokens=int(os.getenv("CHUNK_OVERLAP_TOKENS", "80")),
        default_top_k=int(os.getenv("DEFAULT_TOP_K", "15")),
        default_result_limit=int(os.getenv("DEFAULT_RESULT_LIMIT", "12")),
        scan_roots=scan_roots,
        activity_poll_seconds=int(os.getenv("ACTIVITY_POLL_SECONDS", "3")),
        session_gap_minutes=int(os.getenv("SESSION_GAP_MINUTES", "45")),
        timeline_days_default=int(os.getenv("TIMELINE_DAYS_DEFAULT", "14")),
        max_clusters=int(os.getenv("MAX_CLUSTERS", "8")),
    )
