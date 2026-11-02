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

    let bindingToken = "";
    let resolving = false;
    const send = message => new Promise(resolve => {
      chrome.runtime.sendMessage(message, response => resolve(
        chrome.runtime.lastError ? {ok: false, error: chrome.runtime.lastError.message} : response
      ));
    });
    const sync = async () => {
      const marker = document.querySelector('meta[name="doubao-manager-task-binding"]');
      const token = String(marker?.content || "").trim();
      if (!token || resolving) return;
      bindingToken = token;
      resolving = true;
      try {
        const response = await send({type: "task.resolve", bindingToken});
        if (!response?.ok) throw new Error(response?.error || "无法读取任务状态");
        panel.renderTask(response.data);
      } catch (error) {
        panel.renderWaiting(error.message || String(error));
        panel.feedback("任务状态同步失败", true);
      } finally {
        resolving = false;
      }
    };
    new MutationObserver(sync).observe(document.documentElement, {childList: true, subtree: true});
    sync();
    setInterval(sync, 3000);
  }

  if (document.documentElement) mount();
  else document.addEventListener("DOMContentLoaded", mount, {once: true});
})();
