const form = document.getElementById("settings-form");
const apiBase = document.getElementById("api-base");
const apiKey = document.getElementById("api-key");
const feedback = document.getElementById("feedback");

chrome.storage.local.get(["apiBase", "apiKey"]).then(value => {
  apiBase.value = value.apiBase || "http://127.0.0.1:8765";
  apiKey.value = value.apiKey || "";
});

form.addEventListener("submit", async event => {
  event.preventDefault();
  const base = apiBase.value.trim().replace(/\/$/, "");
  if (!/^http:\/\/(127\.0\.0\.1|localhost)(:\d+)?$/.test(base)) {
    feedback.textContent = "服务地址必须是本机 HTTP 地址";
    feedback.className = "error";
    return;
  }
  await chrome.storage.local.set({apiBase: base, apiKey: apiKey.value.trim()});
  feedback.textContent = "已保存";
  feedback.className = "ok";
});
