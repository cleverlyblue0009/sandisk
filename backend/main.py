from __future__ import annotations

import copy
import logging
import json
import os
import re
import threading
import time
from collections import OrderedDict, defaultdict
from datetime import datetime, timezone as tz
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from activity_api import ActivityApiService
from assistant_personality import AssistantPersonality
from demo_data_generator import DemoDataGenerator
from firebase_storage import FirebaseStorage
from foreground_tracker import ForegroundTracker
from insight_engine import InsightEngine
from memory_query_engine import MemoryQueryEngine
from session_manager import SessionManager
from stats_service import StatsService
from summarizer import chunks_to_summary
from youtube_classifier import YouTubeClassifier
from voice import VoiceService
from config import Settings, get_settings
from database import Database
from embedding import EmbeddingEngine, FaissStore
from extractor import TextExtractor
from groq_client import GroqClient
from ingestion import IngestionService
from retrieval import RetrievalService
from semantic_clustering import SemanticClusteringEngine
from timeline import MemoryTimelineService
from utils import now_ts, safe_duration, top_terms
from watcher import DirectoryWatcher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("personal-memory-assistant")

settings: Settings = get_settings()
database = Database(settings.db_path)
extractor = TextExtractor(max_file_size_mb=settings.max_file_size_mb)
embedding_engine = EmbeddingEngine(settings.embedding_model)
faiss_store = FaissStore(settings.faiss_index_path, embedding_engine.dimension)
if database.schema_reset:
    faiss_store.reset()
groq_client = GroqClient(api_key=settings.groq_api_key, query_model=settings.groq_model_query)
clustering_engine = SemanticClusteringEngine(max_clusters=settings.max_clusters)
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
)
timeline_service = MemoryTimelineService(database=database, session_gap_minutes=settings.session_gap_minutes)
voice_service = VoiceService(whisper_model_size="tiny")
stats_service = StatsService(database=database)
memory_user_id = os.getenv("MEMORY_USER_ID", "local-user")
firebase_storage = FirebaseStorage(settings.data_dir / "memory_cache.db")
session_manager = SessionManager(
    storage=firebase_storage,
    user_id=memory_user_id,
    database=database,
    youtube_classifier=YouTubeClassifier(groq_client=groq_client),
)
foreground_tracker = ForegroundTracker(
    on_event=session_manager.handle_foreground_event,
    poll_seconds=2,
)
insight_engine = InsightEngine()
assistant_personality = AssistantPersonality(groq_client=groq_client)
memory_query_engine = MemoryQueryEngine(
    storage=firebase_storage,
    database=database,
    retrieval_service=retrieval_service,
    insight_engine=insight_engine,
    assistant_personality=assistant_personality,
    groq_client=groq_client,
    user_id=memory_user_id,
)
activity_api_service = ActivityApiService(
    storage=firebase_storage,
    insight_engine=insight_engine,
    user_id=memory_user_id,
)
demo_data_generator = DemoDataGenerator(
    storage=firebase_storage,
    user_id=memory_user_id,
    database=database,
)

app = FastAPI(title=settings.app_name, version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class QueryRequest(BaseModel):
    query: str = Field(..., min_length=1)
    top_k: int | None = Field(default=None, ge=1, le=200)
    result_limit: int | None = Field(default=None, ge=1, le=100)


class IndexStartRequest(BaseModel):
    roots: list[str] | None = Field(default=None)


class SpeakRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=1000)



_status_lock = threading.RLock()
_status: dict[str, Any] = {
    "is_indexing": False,
    "watcher_active": False,
    "watcher_roots": [],
    "scan_roots": [str(path) for path in settings.scan_roots],
    "last_scan_started_at": None,
    "last_scan_completed_at": None,
    "scan_stats": {},
    "last_cluster_summary": {},
}

_cluster_refresh_timer: threading.Timer | None = None
_cluster_refresh_lock = threading.RLock()
_activity_cache_lock = threading.RLock()
_activity_query_cache: OrderedDict[str, dict[str, Any]] = OrderedDict()
_ACTIVITY_CACHE_SIZE = 128


def _update_status(values: dict[str, Any]) -> None:
    with _status_lock:
        _status.update(values)


def _scan_roots_to_paths(roots: list[str] | None = None) -> list[Path]:
    if roots:
        candidates = [Path(item).expanduser().resolve() for item in roots]
    else:
        candidates = settings.scan_roots
    return [path for path in candidates if path.exists() and path.is_dir()]


def _refresh_clusters() -> None:
    summary = clustering_engine.refresh_clusters(database)
    _update_status({"last_cluster_summary": summary})


def _schedule_cluster_refresh(delay_seconds: float = 3.0) -> None:
    global _cluster_refresh_timer
    with _cluster_refresh_lock:
        if _cluster_refresh_timer is not None:
            _cluster_refresh_timer.cancel()
        timer = threading.Timer(delay_seconds, _refresh_clusters)
        timer.daemon = True
        timer.start()
        _cluster_refresh_timer = timer


def _run_index_scan(roots: list[Path]) -> None:
    started_at = now_ts()
    _update_status(
        {
            "is_indexing": True,
            "scan_roots": [str(path) for path in roots],
            "last_scan_started_at": started_at,
            "scan_stats": {},
        }
    )
    try:
        stats = ingestion_service.scan_directories(
            roots,
            progress_callback=lambda payload: _update_status({"scan_stats": payload}),
        )
        cluster_summary = clustering_engine.refresh_clusters(database)
        watcher.start(roots)
        _update_status(
            {
                "is_indexing": False,
                "watcher_active": bool(watcher.watched_paths),
                "watcher_roots": watcher.watched_paths,
                "scan_stats": stats,
                "last_cluster_summary": cluster_summary,
                "last_scan_completed_at": now_ts(),
            }
        )
        logger.info("Indexing completed. roots=%s", [str(path) for path in roots])
    except Exception:
        logger.exception("Indexing failed")
        _update_status({"is_indexing": False})


def _start_indexing(roots: list[Path]) -> bool:
    with _status_lock:
        if _status["is_indexing"]:
            return False
        _status["is_indexing"] = True

    thread = threading.Thread(target=_run_index_scan, args=(roots,), daemon=True, name="index-scan")
    thread.start()
    return True


def _on_file_created(path: str) -> None:
    try:
        _record_download_if_needed(Path(path))
        ingestion_service.process_file(path, source="watchdog", event_type="file_created")
        _schedule_cluster_refresh()
    except Exception:
        logger.exception("Watcher create handler failed for %s", path)


def _on_file_modified(path: str) -> None:
    try:
        ingestion_service.process_file(path, source="watchdog", event_type="file_modified")
        _schedule_cluster_refresh()
    except Exception:
        logger.exception("Watcher modify handler failed for %s", path)


def _on_file_deleted(path: str) -> None:
    try:
        ingestion_service.delete_file(path, source="watchdog")
        _schedule_cluster_refresh()
    except Exception:
        logger.exception("Watcher delete handler failed for %s", path)


_DOWNLOAD_EXTENSIONS = frozenset({
    ".exe", ".msi", ".zip", ".rar", ".7z", ".tar", ".gz", ".iso",
    ".dmg", ".pkg", ".deb", ".rpm", ".apk",
})


def _record_download_if_needed(p: Path) -> None:
    """Record a download event when a binary drops into the Downloads folder."""
    try:
        downloads_dir = Path.home() / "Downloads"
        p.relative_to(downloads_dir)  # raises ValueError if not inside Downloads
    except ValueError:
        return
    if p.suffix.lower() not in _DOWNLOAD_EXTENSIONS:
        return
    database.record_file_event(
        file_path=str(p),
        file_name=p.name,
        event_type="download",
        source="watcher",
    )
    logger.info("Download detected: %s", p.name)


watcher = DirectoryWatcher(
    on_created=_on_file_created,
    on_modified=_on_file_modified,
    on_deleted=_on_file_deleted,
)

_ACTIVITY_QUERY_PATTERN = re.compile(
    r"\b(play|played|playing|game|games|gaming|download|downloaded|spent|did\s+i\s+do)\b",
    flags=re.IGNORECASE,
)
_DOCUMENT_QUERY_PATTERN = re.compile(
    r"\b(document|documents|notes?|lab|paper|study|research)\b",
    flags=re.IGNORECASE,
)


def _route_query_intent(query: str) -> str:
    if _ACTIVITY_QUERY_PATTERN.search(query):
        return "activity"
    if _DOCUMENT_QUERY_PATTERN.search(query):
        return "document"
    return "document"


def _activity_cache_get(key: str) -> dict[str, Any] | None:
    with _activity_cache_lock:
        value = _activity_query_cache.get(key)
        if value is None:
            return None
        _activity_query_cache.move_to_end(key)
        return copy.deepcopy(value)


def _activity_cache_put(key: str, value: dict[str, Any]) -> None:
    with _activity_cache_lock:
        _activity_query_cache[key] = copy.deepcopy(value)
        _activity_query_cache.move_to_end(key)
        while len(_activity_query_cache) > _ACTIVITY_CACHE_SIZE:
            _activity_query_cache.popitem(last=False)


def _build_activity_query_payload(query: str, days: int) -> dict[str, Any]:
    cache_key = f"{query.strip().lower()}|days={days}"
    cached = _activity_cache_get(cache_key)
    if cached is not None:
        cached["cache_hit"] = True
        return cached

    start_time = time.time() - (max(1, days) * 86400)
    stats = stats_service.get_stats(days=days)
    downloads = database.fetch_recent_file_events(
        event_type="download",
        start_time=start_time,
        limit=15,
    )
    games = [
        item
        for item in stats.get("by_process", [])
        if str(item.get("category") or "system") == "game"
    ]

    payload = {
        "query": query,
        "intent": "activity",
        "days": days,
        "total_duration": stats.get("total_duration", "0s"),
        "total_hours": stats.get("total_hours", 0),
        "total_seconds": stats.get("total_seconds", 0),
        "by_process": stats.get("by_process", []),
        "games": games,
        "downloads": [
            {
                "file_name": str(item.get("file_name") or ""),
                "file_path": str(item.get("file_path") or ""),
                "timestamp": float(item.get("event_time") or 0),
                "timestamp_iso": datetime.fromtimestamp(
                    float(item.get("event_time") or 0), tz=tz.utc
                ).isoformat(),
            }
            for item in downloads
        ],
        "results": [],
        "grouped_results": [],
        "cache_hit": False,
    }
    _activity_cache_put(cache_key, payload)
    return payload


@app.on_event("startup")
def startup_event() -> None:
    demo_data_generator.ensure_demo_history()
    foreground_tracker.start()
    roots = _scan_roots_to_paths()
    if roots:
        # Start initial indexing automatically so memory stays fresh after app launch.
        _start_indexing(roots)
    else:
        logger.warning("No default scan roots found. Set SCAN_ROOTS if needed.")


@app.on_event("shutdown")
def shutdown_event() -> None:
    global _cluster_refresh_timer
    with _cluster_refresh_lock:
        if _cluster_refresh_timer is not None:
            _cluster_refresh_timer.cancel()
            _cluster_refresh_timer = None
    watcher.stop()
    foreground_tracker.stop()
    session_manager.stop()
    firebase_storage.sync_pending(user_id=memory_user_id)
    firebase_storage.close()
    faiss_store.save()
    database.close()


@app.get("/health")
def health() -> dict[str, Any]:
    counts = database.get_index_counts()
    return {
        "status": "ok",
        "model": settings.embedding_model,
        "counts": counts,
        "faiss_total": faiss_store.ntotal,
    }


@app.post("/index/start")
def index_start(payload: IndexStartRequest | None = None) -> dict[str, Any]:
    requested = payload.roots if payload else None
    roots = _scan_roots_to_paths(requested)
    if not roots:
        raise HTTPException(status_code=400, detail="No valid directories to index.")
    started = _start_indexing(roots)
    if not started:
        raise HTTPException(status_code=409, detail="Indexing is already in progress.")
    return {"message": "Indexing started", "roots": [str(path) for path in roots]}


@app.get("/index/status")
def index_status() -> dict[str, Any]:
    with _status_lock:
        snapshot = dict(_status)
    snapshot["counts"] = database.get_index_counts()
    snapshot["faiss_total"] = faiss_store.ntotal
    return snapshot


@app.post("/query")
def query_memory(payload: QueryRequest) -> dict[str, Any]:
    started = time.perf_counter()
    intent = _route_query_intent(payload.query)

    if intent == "activity":
        days = _infer_days_from_query(payload.query, default=7)
        activity_payload = _build_activity_query_payload(payload.query, days=days)
        activity_payload["analysis"] = {"intent": "activity", "router": "keyword-router"}
        activity_payload["expanded_query"] = payload.query
        activity_payload["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
        return activity_payload

    if faiss_store.ntotal == 0:
        return {
            "query": payload.query,
            "expanded_query": payload.query,
            "analysis": {"intent": "document", "router": "keyword-router"},
            "results": [],
            "grouped_results": [],
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        }

    result = retrieval_service.search(
        query=payload.query,
        top_k=payload.top_k,
        result_limit=payload.result_limit,
    )
    result["analysis"] = dict(result.get("analysis", {}))
    result["analysis"]["router"] = "keyword-router"
    result["analysis"]["intent"] = "document"
    result["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
    return result


@app.get("/activity/stats")
def activity_stats(days: int = Query(default=14, ge=1, le=90)) -> dict[str, Any]:
    """Existing stats endpoint - used by the React frontend Activity tab."""
    return stats_service.get_stats(days=days)


# Voice endpoints


@app.get("/voice/status")
def voice_status() -> dict[str, Any]:
    """Report STT/TTS availability so the UI can adapt its controls."""
    return {
        "stt_available": voice_service.stt_available,
        "tts_available": voice_service.tts_available,
    }


@app.post("/voice/transcribe")
async def voice_transcribe(audio: UploadFile = File(...)) -> dict[str, Any]:
    """Transcribe an uploaded audio file (webm/wav/ogg) to text via Whisper."""
    if not voice_service.stt_available:
        raise HTTPException(
            status_code=503,
            detail="STT unavailable. Install faster-whisper and restart the server.",
        )
    audio_bytes = await audio.read()
    suffix = Path(audio.filename or "audio.webm").suffix or ".webm"
    try:
        text = voice_service.transcribe(audio_bytes, audio_suffix=suffix)
    except Exception as err:
        raise HTTPException(status_code=500, detail=f"Transcription failed: {err}") from err
    return {"text": text}


@app.post("/voice/speak")
def voice_speak(payload: SpeakRequest) -> dict[str, Any]:
    """Play a TTS response through the system speakers (non-blocking)."""
    voice_service.speak(payload.text)
    return {"ok": True, "tts_available": voice_service.tts_available}


@app.get("/timeline")
def timeline(days: int = Query(default=settings.timeline_days_default, ge=1, le=90)) -> dict[str, Any]:
    return timeline_service.get_timeline(days=days)


# /api/ endpoints - clean-format aliases for external consumers

class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1)
    limit: int | None = Field(default=15, ge=1, le=50)


class ReasonRequest(BaseModel):
    query: str = Field(..., min_length=1)
    days: int | None = Field(default=None, ge=1, le=90)


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1)


_SKIP_ACTIVITY_CATEGORIES = {"system", "launcher"}
_CATEGORY_LABELS = {
    "editor": "Coding",
    "browser": "Browsing",
    "game": "Gaming",
    "chat": "Chat",
    "media": "Media",
    "torrent": "Downloads",
}
_QUERY_NOISE = {
    "what",
    "did",
    "do",
    "for",
    "from",
    "with",
    "my",
    "on",
    "in",
    "about",
    "today",
    "yesterday",
    "week",
    "month",
    "files",
    "documents",
    "document",
    "work",
    "worked",
    "used",
    "spent",
    "show",
    "explain",
}


def _infer_days_from_query(query: str, default: int = 14) -> int:
    q = query.lower()
    if re.search(r"\b(today|tonight|this morning)\b", q):
        return 1
    if "yesterday" in q:
        return 2
    if re.search(r"\b(this week|past week|weekly|explain my week)\b", q):
        return 7
    if re.search(r"\b(last month|past month|this month)\b", q):
        return 30
    if re.search(r"\b(last year|this year)\b", q):
        return 365
    return max(1, default)


def _period_label(query: str, days: int) -> str:
    q = query.lower()
    if "yesterday" in q:
        return "Yesterday"
    if "today" in q or days == 1:
        return "Today"
    if "week" in q or days == 7:
        return "This Week"
    if "month" in q or days == 30:
        return "This Month"
    return f"Last {days} Days"


def _query_focus_terms(query: str, limit: int = 4) -> list[str]:
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9_+-]{2,}", query.lower())
    terms: list[str] = []
    seen: set[str] = set()
    for word in words:
        if word in _QUERY_NOISE or word in seen:
            continue
        seen.add(word)
        terms.append(word)
        if len(terms) >= limit:
            break
    return terms


def _merge_windows(windows: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if not windows:
        return []
    ordered = sorted(windows, key=lambda item: item[0])
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        prev_start, prev_end = merged[-1]
        if start <= prev_end:
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))
    return merged


def _build_document_result(item: dict[str, Any]) -> dict[str, Any]:
    chunks = [c["content"] for c in (item.get("top_chunks") or []) if c.get("content")]
    summary = str(item.get("summary") or "").strip()
    raw_topics = item.get("topics")
    if isinstance(raw_topics, list):
        topics = [str(topic) for topic in raw_topics if str(topic).strip()]
    else:
        topics = []
    if not topics:
        try:
            parsed_topics = json.loads(str(item.get("topics_json") or "[]"))
            if isinstance(parsed_topics, list):
                topics = [str(topic) for topic in parsed_topics if str(topic).strip()]
        except Exception:
            topics = []
    if not summary or not topics:
        generated_summary, generated_topics = chunks_to_summary(
            chunks,
            groq_client=groq_client,
            file_name=item.get("file_name", ""),
        )
        if not summary:
            summary = generated_summary
        if not topics:
            topics = generated_topics
    mod_ts = float(item.get("modified_time") or 0)
    last_opened = (
        datetime.fromtimestamp(mod_ts, tz=tz.utc).strftime("%Y-%m-%d")
        if mod_ts > 0
        else ""
    )
    key_snippets = [text.strip() for text in chunks[:3] if text.strip()]
    return {
        "file_id": int(item.get("file_id") or 0),
        "file": item.get("file_name", ""),
        "file_path": item.get("file_path", ""),
        "file_type": item.get("file_type", ""),
        "extension": item.get("extension", ""),
        "last_opened": last_opened,
        "last_modified": (
            datetime.fromtimestamp(mod_ts, tz=tz.utc).isoformat() if mod_ts > 0 else ""
        ),
        "cluster": item.get("cluster_label", ""),
        "context": item.get("context_label", ""),
        "summary": summary,
        "topics": topics,
        "key_snippets": key_snippets,
        "score": item.get("final_score", 0),
        "score_breakdown": item.get("score_breakdown", {}),
    }


def _build_search_conversation(query: str, results: list[dict[str, Any]]) -> tuple[str, str]:
    if not results:
        return (
            f"I could not find documents related to \"{query}\" yet.",
            "No matching documents were found.",
        )

    lines = [f"I found {len(results)} documents related to \"{query}\"."]
    lines.append("Top matches include:")
    for doc in results[:3]:
        brief = (doc.get("summary") or "").strip()
        if len(brief) > 120:
            brief = brief[:117].rsplit(" ", 1)[0] + "..."
        if brief:
            lines.append(f"- {doc['file']}: {brief}")
        else:
            lines.append(f"- {doc['file']}")

    voice = f"I found {len(results)} related documents. Top match is {results[0]['file']}."
    return "\n".join(lines), voice


def _map_context_clusters(
    raw_clusters: list[dict[str, Any]],
    docs_by_id: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    clusters: list[dict[str, Any]] = []
    for cluster in raw_clusters:
        docs: list[dict[str, Any]] = []
        for item in cluster.get("documents", []):
            file_id = int(item.get("file_id") or 0)
            mapped = docs_by_id.get(file_id)
            if mapped:
                docs.append(mapped)
        if not docs:
            continue
        clusters.append(
            {
                "cluster_name": str(cluster.get("cluster_name") or "Related Work"),
                "topics": [str(topic) for topic in cluster.get("topics", []) if str(topic).strip()],
                "documents": docs,
            }
        )
    return clusters


def _build_digest_payload(days: int, query: str = "") -> dict[str, Any]:
    start = time.time() - (max(1, days) * 86400)
    stats_raw = stats_service.get_stats(days=days)
    stats_api = stats_service.get_api_stats(days=days)
    file_events = database.fetch_file_events(start_time=start)

    downloads = [e for e in file_events if str(e.get("event_type")) == "download"]
    doc_events = [
        e for e in file_events
        if str(e.get("event_type")) in {"file_created", "file_modified"}
    ]

    unique_doc_paths: list[str] = []
    seen_paths: set[str] = set()
    for event in sorted(doc_events, key=lambda item: float(item["event_time"]), reverse=True):
        path = str(event["file_path"])
        if path in seen_paths:
            continue
        seen_paths.add(path)
        unique_doc_paths.append(path)
        if len(unique_doc_paths) >= 18:
            break

    file_meta = {row["file_path"]: row for row in database.fetch_files_by_paths(unique_doc_paths)}
    documents_edited: list[dict[str, Any]] = []
    topic_seed: list[str] = []

    for path in unique_doc_paths[:15]:
        meta = file_meta.get(path, {})
        file_name = str(meta.get("file_name") or Path(path).name)
        file_id = int(meta.get("id") or 0)
        summary = str(meta.get("summary") or "").strip()
        topics: list[str] = []
        try:
            parsed_topics = json.loads(str(meta.get("topics_json") or "[]"))
            if isinstance(parsed_topics, list):
                topics = [str(topic) for topic in parsed_topics if str(topic).strip()]
        except Exception:
            topics = []
        if not summary or not topics:
            chunks = database.fetch_chunks_for_file(file_id, limit=3) if file_id > 0 else []
            generated_summary, generated_topics = chunks_to_summary(
                chunks,
                groq_client=groq_client,
                file_name=file_name,
            )
            if not summary:
                summary = generated_summary
            if not topics:
                topics = generated_topics
        topic_seed.extend(topics)
        topic_seed.append(file_name)
        mod_ts = float(meta.get("modified_time") or 0)
        documents_edited.append(
            {
                "file_name": file_name,
                "file_path": path,
                "last_modified": datetime.fromtimestamp(mod_ts, tz=tz.utc).isoformat() if mod_ts > 0 else "",
                "summary": summary,
                "topics": topics,
            }
        )

    cat_seconds: dict[str, float] = defaultdict(float)
    cat_apps: dict[str, list[str]] = defaultdict(list)
    for item in sorted(
        stats_raw.get("by_process", []),
        key=lambda row: float(row.get("total_seconds", 0)),
        reverse=True,
    ):
        cat = str(item.get("category") or "system")
        if cat in _SKIP_ACTIVITY_CATEGORIES:
            continue
        sec = float(item.get("total_seconds", 0))
        if sec <= 0:
            continue
        cat_seconds[cat] += sec
        app_name = str(item.get("display_name") or item.get("app_name") or item.get("process_name") or "").strip()
        if app_name and app_name not in cat_apps[cat] and len(cat_apps[cat]) < 3:
            cat_apps[cat].append(app_name)

    category_breakdown = [
        {
            "category": cat,
            "label": _CATEGORY_LABELS.get(cat, cat.title()),
            "duration_seconds": secs,
            "duration": safe_duration(secs),
            "apps": cat_apps.get(cat, []),
        }
        for cat, secs in sorted(cat_seconds.items(), key=lambda kv: kv[1], reverse=True)
    ]
    category_summary = [
        f"{item['label']}: {item['duration']}"
        + (f" ({', '.join(item['apps'][:2])})" if item["apps"] else "")
        for item in category_breakdown
    ]

    most_used_apps = [
        {
            "app": str(item.get("display_name") or item.get("app_name") or item.get("process_name") or ""),
            "duration": str(item.get("duration") or safe_duration(float(item.get("total_seconds", 0)))),
            "category": str(item.get("category") or "system"),
        }
        for item in stats_raw.get("by_process", [])
        if str(item.get("category") or "system") not in _SKIP_ACTIVITY_CATEGORIES
    ][:6]

    topic_source = " ".join(topic_seed + [doc["summary"] for doc in documents_edited if doc["summary"]])
    main_topics = [term.replace("_", " ").title() for term in top_terms(topic_source, limit=8)]

    period = _period_label(query, days)
    summary_title = "Weekly Memory" if days == 7 else f"{period} Summary"
    lines = [summary_title]
    if category_breakdown:
        for row in category_breakdown[:3]:
            lines.append(f"{row['label']}: {row['duration']}")
    if downloads:
        lines.append(f"Downloads: {', '.join(str(d['file_name']) for d in downloads[-3:])}")
    if documents_edited:
        lines.append(f"Documents edited: {', '.join(doc['file_name'] for doc in documents_edited[:3])}")
    assistant_response = "\n".join(lines)

    if category_breakdown:
        top_row = category_breakdown[0]
        voice_summary = (
            f"{period}, your top activity was {top_row['label'].lower()} for {top_row['duration']}."
        )
    elif most_used_apps:
        voice_summary = f"{period}, your most used app was {most_used_apps[0]['app']}."
    else:
        voice_summary = f"No activity was recorded for {period.lower()}."

    return {
        "days": days,
        "summary_title": summary_title,
        "assistant_response": assistant_response,
        "voice_summary": voice_summary,
        "total_apps": len(stats_api.get("stats", [])),
        "total_hours": stats_api.get("total_hours", 0),
        "total_duration": stats_api.get("total_duration", "0s"),
        "category_summary": category_summary,
        "category_breakdown": category_breakdown,
        "docs_worked_on": [doc["file_name"] for doc in documents_edited],
        "documents_edited": documents_edited,
        "downloads": [str(d["file_name"]) for d in downloads[-15:]],
        "downloads_detailed": [
            {
                "file_name": str(d["file_name"]),
                "file_path": str(d["file_path"]),
                "timestamp": float(d["event_time"]),
                "timestamp_iso": datetime.fromtimestamp(float(d["event_time"]), tz=tz.utc).isoformat(),
                "file_type": Path(str(d["file_name"])).suffix.lower().lstrip("."),
            }
            for d in downloads[-15:]
        ],
        "most_used_apps": most_used_apps,
        "main_topics": main_topics,
        "stats": stats_api.get("stats", [])[:15],
        "categories": stats_api.get("categories", []),
    }


def _build_cross_memory_reasoning(query: str, days: int) -> dict[str, Any]:
    start = time.time() - (max(1, days) * 86400)
    raw = retrieval_service.search(query=query, top_k=48, result_limit=12) if faiss_store.ntotal > 0 else {"results": []}
    docs = [_build_document_result(item) for item in raw.get("results", [])]
    docs_by_path = {doc["file_path"] for doc in docs if doc.get("file_path")}

    file_events = database.fetch_file_events(start_time=start)
    relevant_doc_events = [
        event
        for event in file_events
        if str(event.get("event_type")) in {"file_created", "file_modified"}
        and str(event.get("file_path")) in docs_by_path
    ]

    windows = [
        (float(event["event_time"]) - 45 * 60, float(event["event_time"]) + 45 * 60)
        for event in relevant_doc_events
    ]
    if not windows:
        now = time.time()
        windows = [(start, now)]
    windows = _merge_windows(windows)

    process_activity = database.fetch_process_activity(start_time=start)
    app_seconds: dict[tuple[str, str], float] = defaultdict(float)
    total_focus_seconds = 0.0
    for session in process_activity:
        category = str(session.get("category") or "system")
        if category in _SKIP_ACTIVITY_CATEGORIES:
            continue
        s_start = float(session.get("start_time") or 0)
        s_end = float(session.get("end_time") or s_start)
        overlap = 0.0
        for w_start, w_end in windows:
            overlap += max(0.0, min(s_end, w_end) - max(s_start, w_start))
        if overlap <= 0:
            continue
        app_name = str(session.get("app_name") or session.get("process_name") or "Unknown").strip()
        key = (app_name, category)
        app_seconds[key] += overlap
        total_focus_seconds += overlap

    apps_used = [
        {
            "app": app,
            "category": category,
            "duration_seconds": seconds,
            "duration": safe_duration(seconds),
        }
        for (app, category), seconds in sorted(app_seconds.items(), key=lambda kv: kv[1], reverse=True)
    ][:6]

    focus_terms = _query_focus_terms(query, limit=3)
    focus_label = " ".join(focus_terms).title() if focus_terms else "this work"
    topic_seed = " ".join(
        " ".join(doc.get("topics") or []) + " " + str(doc.get("summary") or "")
        for doc in docs[:10]
    )
    topics = [term.replace("_", " ").title() for term in top_terms(topic_seed, limit=8)]

    period = _period_label(query, days)
    lines = [
        f"{period} you worked on {focus_label}.",
        f"Time spent: {safe_duration(total_focus_seconds)}.",
    ]
    if apps_used:
        lines.append(f"Apps used: {', '.join(app['app'] for app in apps_used[:4])}.")
    if docs:
        lines.append(f"Documents edited: {', '.join(doc['file'] for doc in docs[:4])}.")
    if topics:
        lines.append(f"Topics detected: {', '.join(topics[:5])}.")
    assistant_response = "\n".join(lines)

    voice_summary = (
        f"{period}, you spent {safe_duration(total_focus_seconds)} on {focus_label}."
        if total_focus_seconds > 0
        else f"{period}, I found document activity but no app usage overlap for {focus_label}."
    )

    return {
        "query": query,
        "days": days,
        "period": period,
        "focus": focus_label,
        "time_spent_seconds": total_focus_seconds,
        "time_spent": safe_duration(total_focus_seconds),
        "apps_used": apps_used,
        "documents_edited": docs[:10],
        "topics": topics,
        "assistant_response": assistant_response,
        "voice_summary": voice_summary,
    }


@app.get("/api/timeline")
def api_timeline(days: int = Query(default=14, ge=1, le=90)) -> dict[str, Any]:
    """Timeline grouped by date from persisted activity sessions."""
    return activity_api_service.get_timeline(days=days)


@app.get("/api/activity/stats")
def api_activity_stats(days: int = Query(default=14, ge=1, le=90)) -> dict[str, Any]:
    """Aggregated per-app usage stats in clean format.

    Returns ``stats`` (per-process rows), ``categories`` (rolled-up by type),
    and overall ``total_duration``.
    """
    return stats_service.get_api_stats(days=days)


@app.get("/api/activity/suggestions")
def api_activity_suggestions(days: int = Query(default=7, ge=1, le=90)) -> dict[str, Any]:
    """Usage-pattern suggestions generated from app and document activity."""
    return stats_service.get_suggestions(days=days)


@app.post("/api/ask")
def api_ask(payload: AskRequest) -> dict[str, Any]:
    """Natural language memory query endpoint for the ASK tab."""
    return memory_query_engine.ask(question=payload.question)


@app.get("/api/insights")
def api_insights(days: int = Query(default=14, ge=1, le=90)) -> dict[str, Any]:
    """Insight cards and workflow patterns for the INSIGHTS tab."""
    return activity_api_service.get_insights(days=days)


@app.post("/api/search")
def api_search(payload: SearchRequest) -> dict[str, Any]:
    """Semantic document search with conversational and clustered responses."""
    if faiss_store.ntotal == 0:
        return {
            "query": payload.query,
            "results": [],
            "total": 0,
            "context_clusters": [],
            "assistant_response": f"I could not find documents related to \"{payload.query}\" yet.",
            "voice_summary": "No matching documents were found.",
        }

    raw = retrieval_service.search(
        query=payload.query,
        top_k=15,
        result_limit=payload.limit,
    )

    results = [_build_document_result(item) for item in raw.get("results", [])]
    docs_by_id = {int(item["file_id"]): item for item in results}
    context_clusters = _map_context_clusters(raw.get("context_clusters", []), docs_by_id)
    assistant_response, voice_summary = _build_search_conversation(payload.query, results)

    return {
        "query": payload.query,
        "expanded_query": raw.get("expanded_query", payload.query),
        "results": results,
        "total": len(results),
        "context_clusters": context_clusters,
        "assistant_response": assistant_response,
        "voice_summary": voice_summary,
    }


@app.post("/api/search_documents")
def api_search_documents(payload: SearchRequest) -> dict[str, Any]:
    """Document search endpoint requested by external clients."""
    if faiss_store.ntotal == 0:
        return {
            "query": payload.query,
            "results": [],
            "total": 0,
        }

    raw = retrieval_service.search(
        query=payload.query,
        top_k=24,
        result_limit=payload.limit,
    )
    results: list[dict[str, Any]] = []
    for item in raw.get("results", []):
        normalized = _build_document_result(item)
        results.append(
            {
                "file_name": str(normalized.get("file") or ""),
                "summary": str(normalized.get("summary") or ""),
                "key_snippets": normalized.get("key_snippets") or [],
                "last_used_date": str(normalized.get("last_opened") or ""),
                "file_path": str(normalized.get("file_path") or ""),
                "score": float(normalized.get("score") or 0.0),
            }
        )

    return {
        "query": payload.query,
        "results": results,
        "total": len(results),
    }


@app.get("/api/digest")
def api_digest(
    days: int = Query(default=1, ge=1, le=30),
    query: str = Query(default=""),
) -> dict[str, Any]:
    return _build_digest_payload(days=days, query=query)


@app.post("/api/reason")
def api_reason(payload: ReasonRequest) -> dict[str, Any]:
    days = payload.days or _infer_days_from_query(payload.query, default=2)
    return _build_cross_memory_reasoning(payload.query, days=days)
