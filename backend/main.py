from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from config import Settings, get_settings
from database import Database
from embedding import EmbeddingEngine, FaissStore
from explanation import ExplanationService
from extractor import TextExtractor
from groq_client import GroqClient
from ingestion import IngestionService
from retrieval import RetrievalService
from utils import normalize_windows_path
from watcher import DirectoryWatcher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("memory-assistant")

settings: Settings = get_settings()
database = Database(settings.db_path)
extractor = TextExtractor(max_file_size_mb=settings.max_file_size_mb)
embedding_engine = EmbeddingEngine(settings.embedding_model)
faiss_store = FaissStore(settings.faiss_index_path, embedding_engine.dimension)
groq_client = GroqClient(
    api_key=settings.groq_api_key,
    query_model=settings.groq_model_query,
    summary_model=settings.groq_model_summary,
)
explanation_service = ExplanationService(groq_client)
ingestion_service = IngestionService(
    settings=settings,
    database=database,
    extractor=extractor,
    embedding_engine=embedding_engine,
    faiss_store=faiss_store,
)
retrieval_service = RetrievalService(
    settings=settings,
    database=database,
    embedding_engine=embedding_engine,
    faiss_store=faiss_store,
    groq_client=groq_client,
    explanation_service=explanation_service,
)

app = FastAPI(title=settings.app_name, version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_status_lock = threading.Lock()
_index_status: dict[str, Any] = {
    "selected_directory": None,
    "is_indexing": False,
    "total_supported_files": 0,
    "scanned_files": 0,
    "indexed_files": 0,
    "updated_files": 0,
    "skipped_files": 0,
    "failed_files": 0,
    "last_index_completed_at": None,
    "watcher_active": False,
    "watcher_directory": None,
}


class DirectorySelectionRequest(BaseModel):
    directory: str = Field(..., min_length=1, description="Windows directory to index")


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1)
    top_k: int | None = Field(default=None, ge=1, le=100)
    result_limit: int | None = Field(default=None, ge=1, le=50)


def _update_status(updates: dict[str, Any]) -> None:
    with _status_lock:
        _index_status.update(updates)


def _scan_in_background(directory: str) -> None:
    logger.info("Starting initial scan for %s", directory)
    _update_status(
        {
            "is_indexing": True,
            "total_supported_files": 0,
            "scanned_files": 0,
            "indexed_files": 0,
            "updated_files": 0,
            "skipped_files": 0,
            "failed_files": 0,
            "watcher_active": False,
            "watcher_directory": None,
        }
    )

    def progress_callback(stats: dict[str, int]) -> None:
        _update_status(stats)

    try:
        stats = ingestion_service.scan_directory(directory, progress_callback=progress_callback)
        _update_status(stats)
        watcher.start(directory)
        _update_status(
            {
                "is_indexing": False,
                "last_index_completed_at": time.time(),
                "watcher_active": True,
                "watcher_directory": watcher.watched_path,
            }
        )
        logger.info("Initial scan completed for %s", directory)
    except Exception:
        logger.exception("Initial scan failed")
        _update_status(
            {
                "is_indexing": False,
                "watcher_active": False,
                "watcher_directory": None,
            }
        )


def _start_scan(directory: str) -> None:
    thread = threading.Thread(target=_scan_in_background, args=(directory,), daemon=True)
    thread.start()


def _on_file_upsert(path: str) -> None:
    try:
        status = ingestion_service.process_file(path)
        logger.info("Watcher upsert processed: %s (%s)", path, status)
    except Exception:
        logger.exception("Watcher failed to process file %s", path)


def _on_file_delete(path: str) -> None:
    try:
        deleted = ingestion_service.delete_file(path)
        logger.info("Watcher delete processed: %s (deleted=%s)", path, deleted)
    except Exception:
        logger.exception("Watcher failed to delete file %s", path)


watcher = DirectoryWatcher(on_upsert=_on_file_upsert, on_delete=_on_file_delete)


@app.on_event("startup")
def startup_event() -> None:
    remembered_directory = database.get_state("selected_directory")
    if remembered_directory and Path(remembered_directory).exists():
        _update_status({"selected_directory": remembered_directory})
        watcher.start(remembered_directory)
        _update_status({"watcher_active": True, "watcher_directory": watcher.watched_path})
        logger.info("Resumed watcher for persisted directory: %s", remembered_directory)


@app.on_event("shutdown")
def shutdown_event() -> None:
    watcher.stop()
    faiss_store.save()
    database.close()


@app.get("/health")
def health() -> dict[str, Any]:
    counts = database.get_counts()
    return {
        "status": "ok",
        "indexed_files": counts["files"],
        "indexed_chunks": counts["chunks"],
        "indexed_vectors": counts["vectors"],
        "faiss_total": faiss_store.ntotal,
    }


@app.post("/api/directory/select")
def select_directory(payload: DirectorySelectionRequest) -> dict[str, Any]:
    directory = normalize_windows_path(payload.directory)
    path = Path(directory)
    if not path.exists() or not path.is_dir():
        raise HTTPException(status_code=400, detail="Directory does not exist or is invalid")

    watcher.stop()
    database.set_state("selected_directory", directory)
    _update_status({"selected_directory": directory})
    _start_scan(directory)
    return {"message": "Directory accepted. Initial indexing started.", "directory": directory}


@app.get("/api/index/status")
def index_status() -> dict[str, Any]:
    with _status_lock:
        snapshot = dict(_index_status)
    snapshot["counts"] = database.get_counts()
    snapshot["faiss_total"] = faiss_store.ntotal
    return snapshot


@app.get("/api/files")
def list_files() -> dict[str, Any]:
    return {"files": database.list_indexed_files()}


@app.post("/api/search")
def search(payload: SearchRequest) -> dict[str, Any]:
    selected_directory = database.get_state("selected_directory")
    if not selected_directory:
        raise HTTPException(status_code=400, detail="No directory selected yet")
    if faiss_store.ntotal == 0:
        return {"query": payload.query, "analysis": {}, "results": []}

    return retrieval_service.search(
        query=payload.query,
        top_k=payload.top_k,
        result_limit=payload.result_limit,
    )
