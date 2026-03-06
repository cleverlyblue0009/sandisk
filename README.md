# Personal Memory Assistant

Semantic desktop memory layer for Windows.
Indexes files by meaning and time, tracks app activity, answers natural-language queries, and supports **voice input + voice response**.

## Implemented Architecture

1. Python Backend (FastAPI)
2. Semantic Memory Engine (SentenceTransformers + FAISS)
3. Activity Tracking Engine (psutil + SQLite) with app categorisation
4. Voice Engine (faster-whisper STT + pyttsx3 TTS)
5. Desktop Assistant UI (Electron + React — tabbed, floating bubble)

## Project Structure

```text
sandisk/
  backend/
    main.py               # FastAPI app — all HTTP endpoints
    config.py             # Settings from env vars
    database.py           # SQLite abstraction (files, chunks, activity)
    ingestion.py          # File scanning, chunking, embedding
    extractor.py          # PDF / DOCX / PPTX / TXT text extraction
    embedding.py          # SentenceTransformers + FAISS store
    semantic_clustering.py# KMeans topic clustering + context inference
    retrieval.py          # Semantic search pipeline
    ranking.py            # 0.65 semantic + 0.25 recency + 0.10 keyword
    timeline.py           # Memory timeline + semantic work sessions
    activity_tracker.py   # psutil process monitoring every 5 s
    voice.py              # STT (faster-whisper) + TTS (pyttsx3)
    watcher.py            # watchdog live file-system events
    groq_client.py        # Groq LLM query expansion (fallback: heuristic)
    hashing.py            # SHA-256 change detection
    utils.py              # Shared helpers
    requirements.txt
    data/
      memory_assistant.db
      memory_assistant.faiss
  frontend/
    electron/
      main.cjs            # Electron window (always-on-top, bottom-right)
      preload.cjs         # contextBridge IPC bridge
    src/
      App.jsx             # Tabbed floating UI (Search / Timeline / Activity)
      main.jsx
      index.css           # Space Grotesk design system
      services/
        api.js            # fetch wrappers for all backend endpoints
    package.json
    vite.config.js
```

## Key Features

- Automatic recursive indexing of:
  - `Documents`
  - `Downloads`
  - `Desktop`
  - `Pictures`
- Supported text extraction:
  - `.pdf .docx .txt .md .csv .pptx .json .py .js .java`
- Metadata-only binary handling:
  - `.exe .dll .iso .zip .rar .mp4 .jpg .png`
- Chunking for large text:
  - ~650 token chunks with overlap (inside 500-800 target range)
- Real-time file updates via `watchdog`:
  - `file_created`, `file_modified`, `file_deleted`
- Semantic engine:
  - `sentence-transformers/all-MiniLM-L6-v2`
  - FAISS vector retrieval
  - KMeans semantic clustering
- Context inference:
  - `Exam Preparation`, `Coursework`, `Projects`, `General Study Material`
- Query pipeline:
  1. Query input
  2. Groq query expansion
  3. Embedding generation
  4. FAISS retrieval
  5. Ranking with formula:
     - `0.65 semantic + 0.25 recency + 0.10 keyword`
- Activity tracker:
  - Running process tracking every 5 seconds
  - Start/end and duration persistence in SQLite
- Memory timeline:
  - Combines file events + process activity + semantic cluster metadata
- Semantic recall sessions:
  - Auto groups related file activity into sessions by topic/time window
- Floating assistant:
  - Bubble-style Electron window that expands to chat
- Windows start on boot:
  - Electron `app.setLoginItemSettings({ openAtLogin: true })`

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/query` | Semantic document search |
| `GET`  | `/timeline` | Memory timeline (file events + process sessions) |
| `GET`  | `/activity/stats` | Per-process usage with category labels |
| `GET`  | `/index/status` | Indexing progress + FAISS counts |
| `POST` | `/index/start` | Trigger directory scan |
| `GET`  | `/health` | Health check |
| `GET`  | `/voice/status` | STT/TTS availability |
| `POST` | `/voice/transcribe` | Whisper STT — upload audio → text |
| `POST` | `/voice/speak` | pyttsx3 TTS — text → system speakers |

### Voice Input (UI)
The frontend uses the **browser Web Speech API** (Chrome/Edge) for zero-latency voice input.
The `/voice/transcribe` endpoint provides an offline fallback using faster-whisper.

### Voice Response
The frontend uses **browser SpeechSynthesis** (toggle 🔊/🔇 in the header).
The `/voice/speak` endpoint pipes text through pyttsx3 on the host OS.

## Run Backend (Windows)

```powershell
cd backend
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

Set `GROQ_API_KEY` in `backend/.env` for LLM query expansion.  
If missing, deterministic fallback query expansion is used.

## Run Desktop Assistant (Windows)

```powershell
cd frontend
npm install
npm run dev:desktop
```

For production UI bundle:

```powershell
npm run build
npm start
```

## Example Queries

Text or voice — the UI auto-routes each query to the correct pipeline.

| Query | Routed to |
|-------|-----------|
| "What documents did I use for OS exam?" | Document search |
| "What software engineering materials do I have?" | Document search |
| "What did I play last month?" | Activity stats |
| "Show my gaming time this week" | Activity stats |
| "What did I work on yesterday?" | Timeline |
| "Show my recent timeline" | Timeline |
| "What projects did I work on recently?" | Document search |
