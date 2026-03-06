from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

DOCUMENT_EXTENSIONS = {".pdf", ".docx", ".txt", ".md", ".csv", ".pptx", ".json"}
CODE_EXTENSIONS = {".py", ".js", ".java"}
SUPPORTED_TEXT_EXTENSIONS = DOCUMENT_EXTENSIONS | CODE_EXTENSIONS

SKIPPED_BINARY_EXTENSIONS = {
    ".exe",
    ".dll",
    ".iso",
    ".zip",
    ".rar",
    ".mp4",
    ".jpg",
    ".png",
}

STOP_WORDS = {
    "the",
    "and",
    "for",
    "with",
    "this",
    "that",
    "from",
    "into",
    "have",
    "your",
    "about",
    "file",
    "notes",
    "assignment",
    "project",
    "study",
    "course",
}


def normalize_windows_path(path: str | Path) -> str:
    resolved = str(Path(path).expanduser().resolve())
    return os.path.normcase(resolved)


def file_extension(path: str | Path) -> str:
    return Path(path).suffix.lower()


def is_supported_text_file(path: str | Path) -> bool:
    return file_extension(path) in SUPPORTED_TEXT_EXTENSIONS


def is_binary_metadata_only(path: str | Path) -> bool:
    return file_extension(path) in SKIPPED_BINARY_EXTENSIONS


def classify_file_type(path: str | Path) -> str:
    extension = file_extension(path)
    if extension in CODE_EXTENSIONS:
        return "code"
    if extension in {".csv"}:
        return "spreadsheet"
    if extension in {".pptx"}:
        return "presentation"
    if extension in {".pdf", ".docx", ".txt", ".md", ".json"}:
        return "document"
    return "binary"


def read_text_file(path: Path) -> str:
    for encoding in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            return path.read_text(encoding=encoding, errors="ignore")
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="ignore")


def read_json_file(path: Path) -> str:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        return json.dumps(data, indent=2, ensure_ascii=False)
    except Exception:
        return read_text_file(path)


def count_tokens(text: str) -> int:
    return len(text.split())


def chunk_text(text: str, chunk_size_tokens: int = 650, overlap_tokens: int = 80) -> list[str]:
    tokens = text.split()
    if not tokens:
        return []
    if len(tokens) <= chunk_size_tokens:
        return [" ".join(tokens)]

    chunks: list[str] = []
    start = 0
    while start < len(tokens):
        end = min(start + chunk_size_tokens, len(tokens))
        chunk_tokens = tokens[start:end]
        if chunk_tokens:
            chunks.append(" ".join(chunk_tokens))
        if end >= len(tokens):
            break
        start = max(0, end - overlap_tokens)
    return chunks


def now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def to_iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def safe_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, sec = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {sec}s"
    return f"{sec}s"


def keyword_score(keywords: Iterable[str], text: str) -> float:
    tokens = {token.lower() for token in keywords if token.strip()}
    if not tokens:
        return 0.0
    lowered = text.lower()
    hits = sum(1 for token in tokens if token in lowered)
    return hits / max(1, len(tokens))


def top_terms(text: str, limit: int = 5) -> list[str]:
    words = re.findall(r"[A-Za-z][A-Za-z0-9_+-]{2,}", text.lower())
    scores: dict[str, int] = {}
    for word in words:
        if word in STOP_WORDS:
            continue
        scores[word] = scores.get(word, 0) + 1
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    return [term for term, _ in ranked[:limit]]
