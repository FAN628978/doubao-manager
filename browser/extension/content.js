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
    panel.setActionHandler(async (action, presetId) => {
      const current = document.querySelector('meta[name="doubao-manager-task-binding"]')?.content || "";
      if (!current || !bindingToken) return;
      const resolved = await send({type: "task.resolve", bindingToken});
      const task = resolved?.data;
      const definition = [
        ...(task?.actions || []), ...(task?.manual_controls || [])
      ].find(item => item.key === action);
      if (!resolved?.ok || !task || !definition?.enabled) {
        panel.feedback(resolved?.error || definition?.reason || "当前操作不可执行", true);
        await sync();
        return;
      }
      if (definition.confirm && !confirm(definition.confirm)) return;
      panel.setBusy(action, true);
      panel.feedback(`正在执行：${definition.label}…`);
      try {
        const response = await send({
          type: "task.action", action, jobId: task.job_id, presetId
        });
        if (!response?.ok) throw new Error(response?.error || "操作失败");
        panel.feedback(action === "download"
          ? "下载任务已启动；完成后会自动归档，最后一个任务完成时将关闭账号窗口。"
          : `${definition.label}操作成功`);
      } catch (error) {
        panel.feedback(error.message || String(error), true);
      } finally {
        panel.setBusy(action, false);
        await sync();
      }
    });
    new MutationObserver(sync).observe(document.documentElement, {childList: true, subtree: true});
    sync();
    setInterval(sync, 3000);
  }

  if (document.documentElement) mount();
  else document.addEventListener("DOMContentLoaded", mount, {once: true});
})();
