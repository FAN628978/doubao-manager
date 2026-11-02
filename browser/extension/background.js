import {localApi, updateSettings} from "./api.js";

const handlers = {
  async "settings.get"() {
    const value = await chrome.storage.local.get(["apiBase", "apiKey"]);
    return {apiBase: value.apiBase || "http://127.0.0.1:8765", hasApiKey: Boolean(value.apiKey)};
  },
  async "settings.update"(message) {
    const value = await updateSettings(message.value || {});
    return {apiBase: value.apiBase, hasApiKey: Boolean(value.apiKey)};
  },
  async "task.resolve"(message) {
    return localApi(`/api/v1/extension/tasks/resolve?binding_token=${encodeURIComponent(message.bindingToken || "")}`);
  }
};

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  const handler = handlers[message?.type];
  if (!handler) return false;
  Promise.resolve(handler(message))
    .then(data => sendResponse({ok: true, data}))
    .catch(error => sendResponse({ok: false, error: error.message || String(error)}));
  return true;
});
