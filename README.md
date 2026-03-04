# Windows Personal Memory Assistant

Production-grade hackathon project that creates a semantic memory layer over Windows File Explorer.

## Features
- Recursive indexing of a selected Windows directory
- SHA-256 based change detection (skip unchanged files)
- Real-time monitoring with `watchdog` (`created`, `modified`, `deleted`)
- Multi-format extraction (`pdf`, `docx`, `pptx`, `txt`, `md`, `csv`, `json`, `py`, `js`, `java`, `cpp`)
- Chunk-level embeddings (`all-MiniLM-L6-v2`)
- FAISS vector storage (`IndexFlatL2` via `IndexIDMap2`)
- SQLite metadata + FAISS ID mapping
- Natural-language retrieval with Groq query understanding
- Weighted ranking with transparent score breakdown
- Groq-generated summary + retrieval explanation per result
- React + Tailwind dashboard UI

## Project Structure
```text
backend/
  main.py
  config.py
  database.py
  hashing.py
  ingestion.py
  extractor.py
  embedding.py
  retrieval.py
  ranking.py
  groq_client.py
  explanation.py
  watcher.py
  utils.py
  requirements.txt
  .env.example
frontend/
  index.html
  package.json
  postcss.config.js
  tailwind.config.js
  vite.config.js
  src/
    components/
      DirectorySelector.jsx
      SearchBar.jsx
      ResultCard.jsx
      ScoreBreakdown.jsx
    pages/
      Dashboard.jsx
    services/
      api.js
    main.jsx
    index.css
README.md
```

## Backend Setup (Windows)
1. Open PowerShell in project root.
2. Create and activate virtual environment:
   ```powershell
   cd backend
   py -3.11 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```
3. Install dependencies:
   ```powershell
   pip install -r requirements.txt
   ```
4. Configure environment:
   ```powershell
   Copy-Item .env.example .env
   ```
   Edit `.env` and set `GROQ_API_KEY`.
5. Run API:
   ```powershell
   uvicorn main:app --reload --host 127.0.0.1 --port 8000
   ```

## Frontend Setup (Windows)
1. Open second PowerShell terminal:
   ```powershell
   cd frontend
   npm install
   npm run dev
   ```
2. Open `http://127.0.0.1:5173`.

## How It Works
1. Select a base directory from dashboard.
2. Backend performs initial recursive scan.
3. Each supported file is hashed (`SHA-256`).
4. If hash unchanged, re-embedding is skipped.
5. If changed/new:
   - text is extracted by file type
   - text is chunked into ~650-token chunks with overlap
   - chunk embeddings are generated
   - embeddings are added to FAISS
   - metadata + FAISS mappings are stored in SQLite
6. After initial scan, `watchdog` starts monitoring real-time file changes.

## File Type Categories
- `document`: `.pdf`, `.txt`, `.md`, `.docx`, `.json`
- `code`: `.py`, `.js`, `.java`, `.cpp`
- `spreadsheet`: `.csv`
- `presentation`: `.pptx`

Unsupported files are skipped safely.

## Query Pipeline
1. Groq analyzes user query:
   - intent
   - expanded query
   - keywords
   - time hints
2. Expanded query is embedded.
3. FAISS returns top chunk matches.
4. Chunk hits are aggregated per file.
5. Ranking computes final score.
6. Groq generates:
   - 2-sentence summary
   - 1-sentence explanation

## Ranking Formula
```text
Final Score =
  0.65 * semantic_score
  + 0.25 * recency_score
  + 0.10 * keyword_match_score
```

Where:
- `semantic_score`: normalized inverse FAISS distance
- `recency_score`: exponential decay from file modified date
- `keyword_match_score`: keyword hits in filename/path/type metadata

Every result returns full score breakdown plus weighted components.

## API Endpoints
- `GET /health`
- `POST /api/directory/select`
- `GET /api/index/status`
- `GET /api/files`
- `POST /api/search`

## Example Test Queries
- `Could you retrieve the stuff I used for OS exam?`
- `Find my networking lab code from last month`
- `Show presentation slides about distributed systems`
- `What notes did I keep for machine learning revision?`

## Notes
- This implementation is Windows-first and normalizes paths for Windows behavior.
- If `GROQ_API_KEY` is missing, deterministic fallbacks keep the app operational, but Groq output quality is reduced.
