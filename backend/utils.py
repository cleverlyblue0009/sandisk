from __future__ import annotations

import json
import os
from pathlib import Path

DOCUMENT_EXTENSIONS = {".pdf", ".txt", ".md", ".docx", ".json"}
CODE_EXTENSIONS = {".py", ".js", ".java", ".cpp"}
SPREADSHEET_EXTENSIONS = {".csv"}
PRESENTATION_EXTENSIONS = {".pptx"}

SUPPORTED_EXTENSIONS = (
    DOCUMENT_EXTENSIONS | CODE_EXTENSIONS | SPREADSHEET_EXTENSIONS | PRESENTATION_EXTENSIONS
)


def normalize_windows_path(path: str | Path) -> str:
    resolved = str(Path(path).expanduser().resolve())
    return os.path.normcase(resolved)


def is_supported_file(path: str | Path) -> bool:
    return Path(path).suffix.lower() in SUPPORTED_EXTENSIONS


def categorize_file(path: str | Path) -> str:
    extension = Path(path).suffix.lower()
    if extension in CODE_EXTENSIONS:
        return "code"
    if extension in SPREADSHEET_EXTENSIONS:
        return "spreadsheet"
    if extension in PRESENTATION_EXTENSIONS:
        return "presentation"
    return "document"


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
