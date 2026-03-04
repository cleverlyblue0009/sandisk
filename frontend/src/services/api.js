const API_BASE = import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000";

async function request(path, options = {}) {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: {
      "Content-Type": "application/json"
    },
    ...options
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

export function selectDirectory(directory) {
  return request("/api/directory/select", {
    method: "POST",
    body: JSON.stringify({ directory })
  });
}

export function getIndexStatus() {
  return request("/api/index/status");
}

export function searchMemory(query, topK = 20, resultLimit = 10) {
  return request("/api/search", {
    method: "POST",
    body: JSON.stringify({
      query,
      top_k: topK,
      result_limit: resultLimit
    })
  });
}
