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
    let actionHandler = null;
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
    chrome.storage.local.get(["panelClosed", "panelCollapsed"]).then(value => {
      root.classList.toggle("is-closed", Boolean(value.panelClosed));
      root.classList.toggle("is-collapsed", Boolean(value.panelCollapsed));
      collapse.textContent = value.panelCollapsed ? "+" : "−";
    });
    root.querySelector('[data-action="close"]').addEventListener("click", () => {
      root.classList.add("is-closed"); chrome.storage.local.set({panelClosed: true});
    });
    root.querySelector(".db-panel-opener").addEventListener("click", () => {
      root.classList.remove("is-closed"); chrome.storage.local.set({panelClosed: false});
    });
    collapse.addEventListener("click", () => {
      root.classList.toggle("is-collapsed");
      collapse.textContent = root.classList.contains("is-collapsed") ? "+" : "−";
      chrome.storage.local.set({panelCollapsed: root.classList.contains("is-collapsed")});
    });
    root.addEventListener("click", event => {
      const button = event.target.closest("[data-task-action]");
      if (!button || button.disabled || !actionHandler) return;
      const preset = root.querySelector('[data-role="confirmation-preset"]');
      actionHandler(button.dataset.taskAction, preset?.value || "", button);
    });
    return {
      root,
      setActionHandler(handler) { actionHandler = handler; },
      setBusy(action, busy) {
        root.querySelectorAll("[data-task-action]").forEach(button => {
          button.disabled = busy || button.dataset.baseDisabled === "true";
          button.classList.toggle("is-loading", busy && button.dataset.taskAction === action);
        });
      },
      renderWaiting(message) {
        root.classList.remove("is-loading");
        root.querySelector('[data-role="status"]').textContent = "未绑定任务";
        root.querySelector('[data-role="content"]').innerHTML = `<div class="db-panel-empty">${escapeHtml(message)}</div>`;
      },
      renderTask(task) {
        root.classList.remove("is-loading");
        root.querySelector('[data-role="status"]').textContent = stateLabels[task.status] || task.status;
        const mode = task.submit_mode === "manual" ? "人工发送" : "自动执行";
        const error = task.error_message || task.error_code;
        const presets = (task.confirmation_presets || []).map(preset =>
          `<option value="${escapeHtml(preset.id)}">${escapeHtml(preset.name)} · ${escapeHtml(preset.text)}</option>`
        ).join("");
        const actions = (task.actions || []).map((action, index) => `
          <button type="button" data-task-action="${escapeHtml(action.key)}" data-base-disabled="${!action.enabled}"
            ${action.enabled ? "" : "disabled"} title="${escapeHtml(action.enabled ? action.confirm : action.reason)}">
            <i>${index + 1}</i><span>${escapeHtml(action.label)}</span>
          </button>`).join("");
        const controls = (task.manual_controls || []).map(control => `
          <button type="button" class="db-manual-control" data-task-action="${escapeHtml(control.key)}"
            data-base-disabled="${!control.enabled}" ${control.enabled ? "" : "disabled"}
            title="${escapeHtml(control.enabled ? control.confirm : control.reason)}">${escapeHtml(control.label)}</button>`).join("");
        const events = (task.events || []).slice(-5).reverse().map(event => `
          <li><time>${escapeHtml(formatTime(event.timestamp))}</time><span>${escapeHtml(event.message)}</span></li>`).join("");
        root.querySelector('[data-role="content"]').innerHTML = `
          <div class="db-task-title"><b>${escapeHtml(task.task_name || task.shot_id || task.job_id)}</b><span>${escapeHtml(mode)}</span></div>
          <dl class="db-task-meta">
            <div><dt>账号</dt><dd>${escapeHtml(task.account_name || task.account_id || "待分配")}</dd></div>
            <div><dt>项目</dt><dd>${escapeHtml(task.project_id || "—")}</dd></div>
            <div><dt>集数</dt><dd>${escapeHtml(task.episode_id || "—")}</dd></div>
            <div><dt>分镜</dt><dd>${escapeHtml(task.shot_id || "—")}</dd></div>
            <div class="wide"><dt>任务 ID</dt><dd title="${escapeHtml(task.job_id)}">${escapeHtml(task.job_id)}</dd></div>
            <div><dt>状态</dt><dd>${escapeHtml(stateLabels[task.status] || task.status)}</dd></div>
            <div><dt>最近操作</dt><dd>${escapeHtml(formatTime(task.last_action_at))}</dd></div>
          </dl>
          ${error ? `<div class="db-task-error"><b>当前异常</b><span>${escapeHtml(error)}</span></div>` : ""}
          ${controls ? `<section class="db-manual-controls ${task.takeover?.active ? "is-active" : ""}">
            <b>${task.takeover?.active ? "人工接管中" : "人工与恢复"}</b><div>${controls}</div></section>` : ""}
          <section class="db-task-actions" data-role="actions">
            ${actions}
            <label class="db-confirm-preset"><span>确认语预设</span><select data-role="confirmation-preset">${presets}</select></label>
          </section>
          <details class="db-task-events" ${error ? "open" : ""}><summary>最近操作记录</summary><ul>${events || "<li><span>暂无操作记录</span></li>"}</ul></details>`;
        this.feedback(task.last_action || "任务状态已同步");
      },
      feedback(message, failed = false) {
        const footer = root.querySelector('[data-role="feedback"]');
        footer.textContent = message;
        footer.classList.toggle("is-error", failed);
      }
    };
  }

  function formatTime(value) {
    if (!value) return "—";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("zh-CN", {hour12: false});
  }

  globalThis.DoubaoTaskPanel = {createPanel, escapeHtml, stateLabels, formatTime};
})();
