import {localApi, updateSettings} from "./api.js";

const taskActions = {
  dispatch: jobId => localApi(`/api/v1/videos/generations/${encodeURIComponent(jobId)}/dispatch`, {method: "POST"}),
  prepare: jobId => localApi(`/api/v1/videos/generations/${encodeURIComponent(jobId)}/prepare-manual`, {method: "POST"}),
  send: jobId => localApi(`/api/v1/videos/generations/${encodeURIComponent(jobId)}/send?confirm_send=true`, {method: "POST"}),
  confirm: (jobId, message) => {
    if (!message.presetId) throw new Error("请选择确认语预设");
    return localApi(`/api/v1/videos/generations/${encodeURIComponent(jobId)}/send-confirmation?confirm_send=true`, {
      method: "POST", body: JSON.stringify({preset_id: message.presetId})
    });
  },
  download: jobId => localApi(`/api/v1/videos/generations/${encodeURIComponent(jobId)}/recall?force_redownload=true`, {method: "POST"}),
  push: jobId => localApi(`/api/v1/videos/generations/${encodeURIComponent(jobId)}/push-result`, {method: "POST"}),
  takeover: jobId => localApi(`/api/v1/videos/generations/${encodeURIComponent(jobId)}/takeover`, {method: "POST"}),
  resume_takeover: jobId => localApi(`/api/v1/videos/generations/${encodeURIComponent(jobId)}/resume-takeover`, {method: "POST"}),
  restart_auto: jobId => localApi(`/api/v1/videos/generations/${encodeURIComponent(jobId)}/restart-auto`, {method: "POST"}),
  next_task: jobId => localApi(`/api/v1/videos/generations/${encodeURIComponent(jobId)}/dispatch-next`, {method: "POST"})
};

const handlers = {
  async "settings.get"() {
    const value = await chrome.storage.local.get(["apiBase", "apiKey"]);
    return {apiBase: value.apiBase || "http://127.0.0.1:8765", hasApiKey: Boolean(value.apiKey)};
  },
  async "settings.update"(message) {
    const value = await updateSettings(message.value || {});
    return {apiBase: value.apiBase, hasApiKey: Boolean(value.apiKey)};
  },
  async "settings.open"() {
    await chrome.runtime.openOptionsPage();
    return {opened: true};
  },
  async "task.resolve"(message) {
    return localApi(`/api/v1/extension/tasks/resolve?binding_token=${encodeURIComponent(message.bindingToken || "")}`);
  },
  async "task.action"(message) {
    const action = taskActions[message.action];
    const jobId = String(message.jobId || "").trim();
    if (!action || !jobId) throw new Error("未知任务操作");
    return action(jobId, message);
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
