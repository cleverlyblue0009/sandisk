const API_BASE = import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000";

async function request(path, options = {}) {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: {
      "Content-Type": "application/json",
    },
    ...options,
  });

  if (!response.ok) {
    let detail = `Request failed (${response.status})`;
    try {
      const payload = await response.json();
      detail = payload.detail || detail;
    } catch {
      detail = response.statusText || detail;
    }
    throw new Error(detail);
  }
  return response.json();
}

export function startIndex(roots = null) {
  const body = roots?.length ? JSON.stringify({ roots }) : JSON.stringify({});
  return request("/index/start", { method: "POST", body });
}

export function getIndexStatus() {
  return request("/index/status");
}

export function queryMemory(query, topK = 40, resultLimit = 12) {
  return request("/query", {
    method: "POST",
    body: JSON.stringify({
      query,
      top_k: topK,
      result_limit: resultLimit,
    }),
  });
}

export function getTimeline(days = 14) {
  return request(`/timeline?days=${days}`);
}

export function getActivityStats(days = 14) {
  return request(`/activity/stats?days=${days}`);
}

export function getActivitySuggestions(days = 7) {
  return request(`/api/activity/suggestions?days=${days}`);
}

// ─── Voice ───────────────────────────────────────────────────────────────────

export function getVoiceStatus() {
  return request("/voice/status");
}

/** Send an audio Blob to the backend Whisper endpoint and return { text }. */
export async function transcribeAudio(audioBlob) {
  const formData = new FormData();
  const ext = audioBlob.type.includes("ogg")
    ? ".ogg"
    : audioBlob.type.includes("wav")
    ? ".wav"
    : ".webm";
  formData.append("audio", audioBlob, `recording${ext}`);
  const response = await fetch(`${API_BASE}/voice/transcribe`, {
    method: "POST",
    body: formData,
    // Do NOT set Content-Type — browser sets multipart boundary automatically.
  });
  if (!response.ok) {
    let detail = `Transcription failed (${response.status})`;
    try {
      const payload = await response.json();
      detail = payload.detail || detail;
    } catch {
      /* ignore */
    }
    throw new Error(detail);
  }
  return response.json();
}

/** Ask the backend to play a TTS response through the system speakers. */
export function speakText(text) {
  return request("/voice/speak", {
    method: "POST",
    body: JSON.stringify({ text }),
  });
}

// ─── Rich search (returns per-result summaries + topics) ─────────────────────

export function apiSearch(query, limit = 30) {
  return request("/api/search", {
    method: "POST",
    body: JSON.stringify({ query, limit }),
  });
}

// ─── Daily / weekly memory digest ────────────────────────────────────────────

export function getDailyDigest(days = 1, query = "") {
  const suffix = query ? `&query=${encodeURIComponent(query)}` : "";
  return request(`/api/digest?days=${days}${suffix}`);
}

export function reasonAboutMemory(query, days = null) {
  return request("/api/reason", {
    method: "POST",
    body: JSON.stringify({ query, days }),
  });
}

export function askMemory(question) {
  return request("/api/ask", {
    method: "POST",
    body: JSON.stringify({ question }),
  });
}

export function getInsights(days = 14) {
  return request(`/api/insights?days=${days}`);
}

export function getApiTimeline(days = 14) {
  return request(`/api/timeline?days=${days}`);
}
