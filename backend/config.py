from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    app_name: str
    data_dir: Path
    db_path: Path
    faiss_index_path: Path
    embedding_model: str
    groq_model_query: str
    groq_model_summary: str
    groq_api_key: str | None
    max_file_size_mb: int
    chunk_size_tokens: int
    chunk_overlap_tokens: int
    default_top_k: int
    default_result_limit: int


def get_settings() -> Settings:
    backend_dir = Path(__file__).resolve().parent
    default_data_dir = backend_dir / "data"
    data_dir = Path(os.getenv("APP_DATA_DIR", str(default_data_dir))).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    return Settings(
        app_name="Windows Personal Memory Assistant",
        data_dir=data_dir,
        db_path=Path(os.getenv("APP_DB_PATH", str(data_dir / "memory_assistant.db"))).expanduser().resolve(),
        faiss_index_path=Path(
            os.getenv("APP_FAISS_INDEX_PATH", str(data_dir / "memory_assistant.faiss"))
        ).expanduser().resolve(),
        embedding_model=os.getenv("EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2"),
        groq_model_query=os.getenv("GROQ_MODEL_QUERY", "llama-3.1-8b-instant"),
        groq_model_summary=os.getenv("GROQ_MODEL_SUMMARY", "llama-3.1-70b-versatile"),
        groq_api_key=os.getenv("GROQ_API_KEY"),
        max_file_size_mb=int(os.getenv("MAX_FILE_SIZE_MB", "25")),
        chunk_size_tokens=int(os.getenv("CHUNK_SIZE_TOKENS", "650")),
        chunk_overlap_tokens=int(os.getenv("CHUNK_OVERLAP_TOKENS", "80")),
        default_top_k=int(os.getenv("DEFAULT_TOP_K", "20")),
        default_result_limit=int(os.getenv("DEFAULT_RESULT_LIMIT", "10")),
    )
