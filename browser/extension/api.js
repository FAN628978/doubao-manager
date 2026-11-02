const DEFAULT_API_BASE = "http://127.0.0.1:8765";

async function settings() {
  const stored = await chrome.storage.local.get(["apiBase", "apiKey"]);
  return {
    apiBase: String(stored.apiBase || DEFAULT_API_BASE).replace(/\/$/, ""),
    apiKey: String(stored.apiKey || "")
  };
}

export async function localApi(path, options = {}) {
  const config = await settings();
  const headers = new Headers(options.headers || {});
  if (options.body && !(options.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  if (config.apiKey) headers.set("X-API-Key", config.apiKey);
  const response = await fetch(`${config.apiBase}${path}`, {...options, headers});
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json")
    ? await response.json()
    : {detail: await response.text()};
  if (!response.ok) {
    throw new Error(payload.detail || payload.message || `本地服务请求失败（${response.status}）`);
  }
  return payload;
}

export async function updateSettings(value) {
  const next = {};
  if (typeof value.apiBase === "string") next.apiBase = value.apiBase;
  if (typeof value.apiKey === "string") next.apiKey = value.apiKey;
  await chrome.storage.local.set(next);
  return settings();
}
