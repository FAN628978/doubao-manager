(() => {
  if (globalThis.__DOUBAO_TASK_PANEL_MOUNTED__) return;
  globalThis.__DOUBAO_TASK_PANEL_MOUNTED__ = true;

  function mount() {
    if (!document.documentElement || document.getElementById("doubao-manager-extension-root")) return;
    const host = document.createElement("div");
    host.id = "doubao-manager-extension-root";
    document.documentElement.appendChild(host);
    const panel = globalThis.DoubaoTaskPanel.createPanel(host);
    panel.renderWaiting("该网页尚未绑定分镜任务");
    chrome.runtime.sendMessage({type: "settings.get"}, () => void chrome.runtime.lastError);
  }

  if (document.documentElement) mount();
  else document.addEventListener("DOMContentLoaded", mount, {once: true});
})();
