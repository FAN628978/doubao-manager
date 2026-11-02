(() => {
  const stateLabels = {
    queued: "排队中", opening: "打开页面", configuring: "配置参数", uploading: "上传素材",
    submitting: "提交中", confirming: "发送确认语", generating: "生成中",
    downloading: "下载视频", awaiting_manual_send: "待人工发送",
    needs_review: "需人工处理", downloaded: "已归档", failed: "失败"
  };

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, char => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;"
    })[char]);
  }

  function createPanel(host) {
    const shadow = host.attachShadow({mode: "open"});
    const style = document.createElement("link");
    style.rel = "stylesheet";
    style.href = chrome.runtime.getURL("panel.css");
    shadow.appendChild(style);
    const root = document.createElement("div");
    root.className = "db-task-panel is-loading";
    root.innerHTML = `
      <button class="db-panel-opener" type="button" title="打开任务面板">任务</button>
      <section class="db-panel-card" aria-label="豆包分镜任务控制面板">
        <header><div><b>分镜任务</b><span data-role="status">正在识别当前任务…</span></div>
          <nav><button data-action="collapse" title="折叠">−</button><button data-action="close" title="关闭">×</button></nav>
        </header>
        <main data-role="content"><div class="db-panel-empty">正在连接本地管理服务…</div></main>
        <footer data-role="feedback">扩展不会影响后台自动任务</footer>
      </section>`;
    shadow.appendChild(root);
    const collapse = root.querySelector('[data-action="collapse"]');
    root.querySelector('[data-action="close"]').addEventListener("click", () => root.classList.add("is-closed"));
    root.querySelector(".db-panel-opener").addEventListener("click", () => root.classList.remove("is-closed"));
    collapse.addEventListener("click", () => {
      root.classList.toggle("is-collapsed");
      collapse.textContent = root.classList.contains("is-collapsed") ? "+" : "−";
    });
    return {
      root,
      renderWaiting(message) {
        root.classList.remove("is-loading");
        root.querySelector('[data-role="status"]').textContent = "未绑定任务";
        root.querySelector('[data-role="content"]').innerHTML = `<div class="db-panel-empty">${escapeHtml(message)}</div>`;
      },
      renderTask(task) {
        root.classList.remove("is-loading");
        root.querySelector('[data-role="status"]').textContent = stateLabels[task.status] || task.status;
        root.querySelector('[data-role="content"]').innerHTML = `<div class="db-panel-empty">已绑定任务<br><b>${escapeHtml(task.task_name || task.job_id)}</b></div>`;
      },
      feedback(message, failed = false) {
        const footer = root.querySelector('[data-role="feedback"]');
        footer.textContent = message;
        footer.classList.toggle("is-error", failed);
      }
    };
  }

  globalThis.DoubaoTaskPanel = {createPanel, escapeHtml, stateLabels};
})();
