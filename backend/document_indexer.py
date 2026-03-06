from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

import faiss
import numpy as np
from docx import Document as DocxDocument

from embedding import EmbeddingEngine

try:
    from pdfminer.high_level import extract_text as pdf_extract_text
except Exception:  # pragma: no cover - optional dependency in some envs
    pdf_extract_text = None

logger = logging.getLogger(__name__)


def _now() -> float:
    return time.time()


class DocumentIndexer:
    """Semantic PDF/DOCX indexer backed by FAISS."""

    def __init__(
        self,
        *,
        data_dir: Path,
        embedding_engine: EmbeddingEngine,
        chunk_size_words: int = 220,
        chunk_overlap_words: int = 40,
    ) -> None:
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.embedding_engine = embedding_engine
        self.chunk_size_words = max(80, int(chunk_size_words))
        self.chunk_overlap_words = max(0, int(chunk_overlap_words))

        self.index_path = self.data_dir / "document_memory.faiss"
        self.meta_path = self.data_dir / "document_memory_meta.json"

        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

        self._index = self._load_or_create_index()
        self._chunks: list[dict[str, Any]] = self._load_metadata()

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._chunks)

    def start_background_indexing(self, roots: list[Path], interval_seconds: int = 900) -> None:
        """Continuously refreshes the index in background without UI coupling."""
        roots = [path for path in roots if path.exists() and path.is_dir()]
        if not roots:
            return

        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run_index_loop,
                args=(roots, max(120, int(interval_seconds))),
                daemon=True,
                name="document-indexer",
            )
            self._thread.start()
        logger.info("Document indexer background loop started for %s roots", len(roots))

    def stop(self) -> None:
        with self._lock:
            self._stop_event.set()
            thread = self._thread
            self._thread = None
        if thread is not None:
            thread.join(timeout=2)

    def reindex_now(self, roots: list[Path]) -> dict[str, int]:
        return self._rebuild_index(roots)

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        text = query.strip()
        if not text:
            return []

        with self._lock:
            if not self._chunks or self._index.ntotal <= 0:
                return []

            query_vec = self.embedding_engine.encode_texts([text]).astype(np.float32)
            top_k = min(max(12, limit * 6), len(self._chunks))
            scores, indices = self._index.search(query_vec, top_k)

            aggregated: dict[str, dict[str, Any]] = {}
            for score, idx in zip(scores[0].tolist(), indices[0].tolist()):
                if idx < 0 or idx >= len(self._chunks):
                    continue
                item = self._chunks[idx]
                path = str(item["file_path"])
                bucket = aggregated.setdefault(
                    path,
                    {
                        "file_name": str(item["file_name"]),
                        "file_path": path,
                        "summary": str(item["summary"]),
                        "key_snippets": [],
                        "score": float(score),
                        "date_last_used": "",
                        "modified_time": float(item.get("modified_time") or 0.0),
                    },
                )
                bucket["score"] = max(float(bucket["score"]), float(score))
                snippet = str(item.get("chunk_text") or "").strip()
                if snippet and snippet not in bucket["key_snippets"] and len(bucket["key_snippets"]) < 3:
                    bucket["key_snippets"].append(snippet[:280])

            ranked = sorted(aggregated.values(), key=lambda row: float(row["score"]), reverse=True)
            return ranked[: max(1, int(limit))]

    def _run_index_loop(self, roots: list[Path], interval_seconds: int) -> None:
        # First pass immediately for demo readiness.
        try:
            self._rebuild_index(roots)
        except Exception:
            logger.exception("Initial document indexing failed")

        while not self._stop_event.wait(interval_seconds):
            try:
                self._rebuild_index(roots)
            except Exception:
                logger.exception("Background document re-indexing failed")

    def _rebuild_index(self, roots: list[Path]) -> dict[str, int]:
        files = self._collect_files(roots)
        all_chunks: list[dict[str, Any]] = []
        texts: list[str] = []
        scanned = 0

        for path in files:
            scanned += 1
            extracted = self._extract_text(path)
            if not extracted:
                continue
            summary = self._summarize(extracted)
            for chunk in self._chunk_text(extracted):
                cleaned = chunk.strip()
                if not cleaned:
                    continue
                texts.append(cleaned)
                all_chunks.append(
                    {
                        "file_path": str(path),
                        "file_name": path.name,
                        "summary": summary,
                        "chunk_text": cleaned,
                        "modified_time": float(path.stat().st_mtime),
                    }
                )

        if texts:
            vectors = self.embedding_engine.encode_texts(texts).astype(np.float32)
            index = faiss.IndexFlatIP(vectors.shape[1])
            index.add(vectors)
        else:
            index = faiss.IndexFlatIP(self.embedding_engine.dimension)

        with self._lock:
            self._index = index
            self._chunks = all_chunks
            self._persist()

        logger.info("Document index rebuilt: files=%s chunks=%s", scanned, len(all_chunks))
        return {"files_scanned": scanned, "chunks_indexed": len(all_chunks)}

    def _collect_files(self, roots: list[Path]) -> list[Path]:
        files: list[Path] = []
        for root in roots:
            if not root.exists() or not root.is_dir():
                continue
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                suffix = path.suffix.lower()
                if suffix in {".pdf", ".docx"}:
                    files.append(path)
        return files

    def _extract_text(self, path: Path) -> str:
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            return self._extract_pdf(path)
        if suffix == ".docx":
            return self._extract_docx(path)
        return ""

    def _extract_pdf(self, path: Path) -> str:
        if pdf_extract_text is not None:
            try:
                return str(pdf_extract_text(str(path)) or "").strip()
            except Exception:
                logger.debug("pdfminer extraction failed for %s", path, exc_info=True)

        # Fallback keeps indexing functional if pdfminer isn't installed.
        try:
            import fitz

            with fitz.open(str(path)) as doc:
                return "\n".join(page.get_text("text") for page in doc).strip()
        except Exception:
            logger.debug("PDF fallback extraction failed for %s", path, exc_info=True)
            return ""

    def _extract_docx(self, path: Path) -> str:
        try:
            doc = DocxDocument(str(path))
        except Exception:
            logger.debug("DOCX extraction failed for %s", path, exc_info=True)
            return ""
        lines = [paragraph.text.strip() for paragraph in doc.paragraphs if paragraph.text.strip()]
        return "\n".join(lines).strip()

    def _chunk_text(self, text: str) -> list[str]:
        words = text.split()
        if not words:
            return []
        if len(words) <= self.chunk_size_words:
            return [" ".join(words)]

        chunks: list[str] = []
        start = 0
        while start < len(words):
            end = min(start + self.chunk_size_words, len(words))
            chunk_words = words[start:end]
            chunks.append(" ".join(chunk_words))
            if end >= len(words):
                break
            start = max(0, end - self.chunk_overlap_words)
        return chunks

    def _summarize(self, text: str, max_chars: int = 300) -> str:
        clean = " ".join(text.split())
        if len(clean) <= max_chars:
            return clean
        # Keep a meaningful sentence boundary when possible.
        candidate = clean[:max_chars]
        cut = candidate.rfind(". ")
        if cut >= 120:
            return candidate[: cut + 1].strip()
        return candidate.rsplit(" ", 1)[0].strip() + "..."

    def _load_or_create_index(self) -> faiss.IndexFlatIP:
        if self.index_path.exists():
            try:
                loaded = faiss.read_index(str(self.index_path))
                if loaded.d == self.embedding_engine.dimension:
                    return loaded  # type: ignore[return-value]
            except Exception:
                logger.debug("Failed to load document index from disk", exc_info=True)
        return faiss.IndexFlatIP(self.embedding_engine.dimension)

    def _load_metadata(self) -> list[dict[str, Any]]:
        if not self.meta_path.exists():
            return []
        try:
            payload = json.loads(self.meta_path.read_text(encoding="utf-8"))
            if isinstance(payload, list):
                return [item for item in payload if isinstance(item, dict)]
        except Exception:
            logger.debug("Failed to load document metadata", exc_info=True)
        return []

    def _persist(self) -> None:
        try:
            faiss.write_index(self._index, str(self.index_path))
            self.meta_path.write_text(json.dumps(self._chunks, ensure_ascii=False), encoding="utf-8")
        except Exception:
            logger.exception("Failed to persist document index")
