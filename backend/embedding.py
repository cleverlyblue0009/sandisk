from __future__ import annotations

import logging
import threading
from pathlib import Path

import faiss
import numpy as np
from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)


class EmbeddingEngine:
    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        self.model = SentenceTransformer(model_name)
        self.dimension = int(self.model.get_sentence_embedding_dimension())

    def encode_texts(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.empty((0, self.dimension), dtype=np.float32)
        vectors = self.model.encode(
            texts,
            convert_to_numpy=True,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return np.asarray(vectors, dtype=np.float32)


class FaissStore:
    def __init__(self, index_path: Path, dimension: int) -> None:
        self.index_path = index_path
        self.dimension = dimension
        self._lock = threading.RLock()
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        self._index = self._load_or_create_index()

    def _create_index(self) -> faiss.IndexIDMap2:
        return faiss.IndexIDMap2(faiss.IndexFlatL2(self.dimension))

    def _load_or_create_index(self) -> faiss.IndexIDMap2:
        if not self.index_path.exists():
            return self._create_index()

        try:
            loaded = faiss.read_index(str(self.index_path))
            if loaded.d != self.dimension:
                logger.warning(
                    "FAISS dimension mismatch (stored=%s, expected=%s); rebuilding empty index",
                    loaded.d,
                    self.dimension,
                )
                return self._create_index()
            if isinstance(loaded, faiss.IndexIDMap2):
                return loaded

            wrapped = faiss.IndexIDMap2(loaded)
            return wrapped
        except Exception:
            logger.exception("Failed to load FAISS index from disk; starting empty index")
            return self._create_index()

    @property
    def ntotal(self) -> int:
        with self._lock:
            return int(self._index.ntotal)

    def add(self, vectors: np.ndarray, ids: list[int]) -> None:
        if vectors.size == 0 or not ids:
            return
        if vectors.shape[0] != len(ids):
            raise ValueError("Vectors and IDs length mismatch")

        ids_np = np.asarray(ids, dtype=np.int64)
        with self._lock:
            self._index.add_with_ids(np.asarray(vectors, dtype=np.float32), ids_np)
            self.save()

    def remove(self, ids: list[int]) -> int:
        if not ids:
            return 0
        ids_np = np.asarray(ids, dtype=np.int64)
        with self._lock:
            try:
                removed = int(self._index.remove_ids(ids_np))
            except Exception:
                logger.exception("Failed to remove vectors from FAISS index")
                removed = 0
            if removed > 0:
                self.save()
            return removed

    def search(self, query_vector: np.ndarray, top_k: int) -> tuple[list[float], list[int]]:
        if query_vector.ndim == 1:
            query_vector = query_vector.reshape(1, -1)

        with self._lock:
            if self._index.ntotal == 0:
                return [], []
            distances, ids = self._index.search(np.asarray(query_vector, dtype=np.float32), top_k)
        return distances[0].tolist(), [int(value) for value in ids[0].tolist()]

    def reset(self) -> None:
        with self._lock:
            self._index = self._create_index()
            self.save()

    def save(self) -> None:
        with self._lock:
            faiss.write_index(self._index, str(self.index_path))
