const state = { accounts: [], jobs: [], stats: {}, submitMode: 'manual', processingLimit: 0, jobSection: 'processing', jobSubgroup: '', selectedJobIds: new Set(), lastJobCheckIndex: -1, bulkJobAction: null, bulkProgress: null, confirmationPresets: [], confirmJobId: null, confirmJobIds: [], confirmPresetReturn: false, editingPresetId: null, uploadJobId: null, activeView: 'overview' };
  state.autoDispatchEnabled=false;
  state.autoDispatchIntervalSeconds=5;
  state.acceptingTasks=true;
  state.settingAcceptingTasks=true;
  state.settingAutoDispatch=false;
  state.settingAutoExecution=false;
  state.memberVideoModel='Seedance 2.0';
  state.nonMemberVideoModel='Seedance 2.0 Mini';
  state.ignoreQuotaCheck=false;
  state.settingIgnoreQuota=false;
  state.claudeRecoveryEnabled=false;
  state.settingClaudeRecovery=false;
  state.autoSwitchOnException=false;
  state.settingAutoSwitchOnException=false;
  state.accountPage=1;
  state.accountPageSize=20;
  state.accountQuery='';
  const viewNames = {overview:'工作概览',accounts:'账号管理',jobs:'分镜任务',integration:'API 接入'};
  const statusLabels = {queued:['排队中','yellow'],ready:['待人工提交','blue'],opening:['打开页面','blue'],configuring:['配置参数','blue'],uploading:['上传素材','blue'],downloading:['下载视频','blue'],submitting:['提交中','blue'],confirming:['发送确认语','blue'],generating:['生成中','blue'],awaiting_manual_send:['待人工发送','yellow'],needs_review:['需人工处理','red'],downloaded:['已归档','green'],failed:['失败','red']};
  const $ = (id) => document.getElementById(id);
  const buttonFeedback = new WeakMap();
  let pendingButtonInteraction = null;
  let interactionSequence = 0;
  let pendingClickTimer;
  function clearPendingButtonInteraction(){
    pendingButtonInteraction=null;
    clearTimeout(pendingClickTimer);
  }
  function rememberButton(button){
    if(!button)return;
    pendingButtonInteraction={button,token:++interactionSequence};
    clearTimeout(pendingClickTimer);
    const token=pendingButtonInteraction.token;
    pendingClickTimer=setTimeout(()=>{
      if(pendingButtonInteraction?.token===token)clearPendingButtonInteraction();
    },1200);
  }
  function rememberButtonInteraction(event){
    const button=event.target instanceof Element?event.target.closest('button'):null;
    if(!button){clearPendingButtonInteraction();return;}
    rememberButton(button);
  }
  function rememberSubmitInteraction(event){
    const button=event.submitter instanceof HTMLButtonElement?event.submitter:null;
    if(!button)return;
    if(pendingButtonInteraction?.button===button)return;
    rememberButton(button);
  }
  document.addEventListener('click',rememberButtonInteraction,true);
  document.addEventListener('submit',rememberSubmitInteraction,true);
  document.addEventListener('visibilitychange',()=>{if(document.hidden)clearPendingButtonInteraction()});
  window.addEventListener('focus',clearPendingButtonInteraction);
  function setButtonLoading(button,feedback){
    feedback.phase='loading';
    button.classList.remove('is-success');
    button.classList.add('is-loading');
    button.dataset.feedbackMode=feedback.native?'native':'replace';
    button.setAttribute('aria-busy','true');
    if(!feedback.native){
      button.disabled=true;
      button.innerHTML='<span class="button-spinner" aria-hidden="true"></span><span>处理中…</span>';
    }
  }
  function beginButtonFeedback(interaction){
    if(!interaction?.button?.isConnected)return null;
    const button=interaction.button;
    let feedback=buttonFeedback.get(button);
    if(feedback&&feedback.token!==interaction.token){
      restoreButtonFeedback(button,feedback);
      feedback=null;
    }
    const sameInteraction=feedback&&feedback.token===interaction.token&&feedback.captured&&feedback.phase!=='success';
    if(!sameInteraction){
      feedback={
        token:interaction.token,
        count:0,
        native:button.disabled,
        failed:false,
        captured:true,
        phase:'idle',
        idleTimer:null,
        successTimer:null,
        originalHtml:button.innerHTML,
        originalDisabled:button.disabled,
        originalAriaBusy:button.getAttribute('aria-busy'),
      };
      buttonFeedback.set(button,feedback);
    }
    clearTimeout(feedback.idleTimer);
    feedback.count++;
    if(feedback.count===1)setButtonLoading(button,feedback);
    return{button,feedback};
  }
  function clearPendingForFeedback(feedback){
    if(pendingButtonInteraction?.token===feedback.token){
      clearPendingButtonInteraction();
    }
  }
  function restoreButtonFeedback(button,feedback){
    clearTimeout(feedback.idleTimer);
    clearTimeout(feedback.successTimer);
    feedback.count=0;
    feedback.captured=false;
    feedback.phase='idle';
    clearPendingForFeedback(feedback);
    if(!button.isConnected)return;
    button.classList.remove('is-loading','is-success');
    delete button.dataset.feedbackMode;
    if(!feedback.native){
      button.innerHTML=feedback.originalHtml;
      button.disabled=feedback.originalDisabled;
    }
    if(feedback.originalAriaBusy===null)button.removeAttribute('aria-busy');
    else button.setAttribute('aria-busy',feedback.originalAriaBusy);
  }
  function finishButtonFeedback(session){
    const {button,feedback}=session;
    if(feedback.count||feedback.phase!=='loading')return;
    if(feedback.failed||feedback.native||!button.isConnected){
      restoreButtonFeedback(button,feedback);
      return;
    }
    feedback.phase='success';
    clearPendingForFeedback(feedback);
    button.classList.remove('is-loading');
    button.classList.add('is-success');
    button.innerHTML='<span class="button-check" aria-hidden="true">✓</span><span>已完成</span>';
    button.disabled=true;
    button.setAttribute('aria-busy','false');
    feedback.successTimer=setTimeout(()=>restoreButtonFeedback(button,feedback),620);
  }
  function endButtonFeedback(session,ok){
    if(!session)return;
    const {button,feedback}=session;
    feedback.count=Math.max(0,feedback.count-1);
    if(!ok)feedback.failed=true;
    if(feedback.count)return;
    clearTimeout(feedback.idleTimer);
    feedback.idleTimer=setTimeout(()=>finishButtonFeedback(session),140);
  }
  function esc(v){return String(v ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
  function shortDate(v){if(!v)return '—';try{return new Date(v).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});}catch{return v;}}
  function statusPill(s){const item=statusLabels[s]||['未知','gray'];return `<span class="pill ${item[1]}">${item[0]}</span>`;}
  function quotaDuration(seconds){const total=Math.max(0,Math.round(Number(seconds)||0)),h=Math.floor(total/3600),m=Math.floor(total%3600/60),s=total%60;if(h)return `${h}小时${m?`${m}分`:''}${s?`${s}秒`:''}`;if(m)return `${m}分${s?`${s}秒`:''}`;return `${s}秒`;}
  function quotaCell(a){const q=a.quota||{};const rem=q.remaining_seconds??0,used=q.consumed_seconds??0,lim=q.daily_limit_seconds??30,res=q.reserved_seconds??0;return `<div class="quota-cell"><div class="quota-remaining"><span>剩余</span><strong>${rem}s</strong><small>/ ${lim}s</small></div><div class="cell-sub">已用 ${used}s${res?` · 排队 ${res}s`:''}</div></div>`;}
  let toastTimer;
  function toast(message,isError=false){const el=$('toast');el.textContent=message;el.classList.toggle('error',isError);el.setAttribute('role','status');el.setAttribute('aria-live',isError?'assertive':'polite');el.classList.add('show');clearTimeout(toastTimer);toastTimer=setTimeout(()=>el.classList.remove('show'),3600);}
  async function api(url, options={}){
    const opts={...options,headers:{...(options.body instanceof FormData?{}:{'Content-Type':'application/json'}),...(options.headers||{})}};
    const feedback=beginButtonFeedback(pendingButtonInteraction);
    try{
      const res=await fetch(url,opts);
      let data;try{data=await res.json()}catch{data={}}
      if(!res.ok)throw new Error(data.detail||`请求失败 (${res.status})`);
      endButtonFeedback(feedback,true);
      return data;
    }catch(error){
      endButtonFeedback(feedback,false);
      throw error;
    }
  }
  function switchView(name){state.activeView=name;document.querySelector('.page').classList.toggle('jobs-wide',name==='jobs');document.querySelectorAll('.view').forEach(v=>v.classList.add('hidden'));$('view-'+name).classList.remove('hidden');document.querySelectorAll('.nav-btn').forEach(b=>b.classList.toggle('active',b.dataset.view===name));$('crumbName').textContent=viewNames[name]||name;renderCurrent();}
  document.querySelectorAll('.nav-btn').forEach(b=>b.addEventListener('click',()=>switchView(b.dataset.view)));
  function openModal(id){if(id==='jobModal')fillAccountSelect();if(id==='webModal'){$('webAccount').innerHTML=accountOptions('','自动分配（进入处理前）');const dispatch=state.autoDispatchEnabled?'自动进入调度':'先进入待处理',execution=state.submitMode==='auto'?'自动执行':'准备后等待人工发送';$('webSubmitModeHint').textContent=`当前设置：${dispatch}，${execution}。`;syncWebMode();}if(id==='accountModal')prefillAccountName();$(id).classList.add('show');}
  function closeModal(id){$(id).classList.remove('show');}
  document.querySelectorAll('.modal-backdrop').forEach(el=>el.addEventListener('click',e=>{if(e.target===el){if(el.id==='confirmationPresetsModal')closeConfirmationPresets();else el.classList.remove('show')}}));
  let refreshing=null;
  async function loadState(){const [accounts,jobs,stats,settings,acceptanceSettings,dispatchSettings,dispatchIntervalSettings,modelSettings,limitSettings,quotaSettings,claudeSettings,autoSwitchSettings]=await Promise.all([api('/api/accounts'),api('/api/jobs'),api('/api/stats'),api('/api/settings/video-submit-mode'),api('/api/settings/task-acceptance'),api('/api/settings/auto-dispatch'),api('/api/settings/auto-dispatch-interval'),api('/api/settings/video-models'),api('/api/settings/processing-limit'),api('/api/settings/ignore-quota-check'),api('/api/settings/claude-recovery'),api('/api/settings/auto-switch-on-exception')]);state.accounts=accounts;state.jobs=jobs;state.stats=stats;state.submitMode=settings.submit_mode||'manual';state.acceptingTasks=acceptanceSettings.accepting_tasks!==false;state.autoDispatchEnabled=Boolean(dispatchSettings.auto_dispatch_enabled);state.autoDispatchIntervalSeconds=Number(dispatchIntervalSettings.auto_dispatch_interval_seconds)||5;state.memberVideoModel=modelSettings.member_model||'Seedance 2.0';state.nonMemberVideoModel=modelSettings.non_member_model||'Seedance 2.0 Mini';state.processingLimit=Number(limitSettings.processing_limit)||0;state.ignoreQuotaCheck=Boolean(quotaSettings.ignore_quota_check);state.claudeRecoveryEnabled=Boolean(claudeSettings.claude_recovery_enabled);state.autoSwitchOnException=Boolean(autoSwitchSettings.auto_switch_on_exception);const existing=new Set(jobs.map(j=>j.id));state.selectedJobIds=new Set([...state.selectedJobIds].filter(id=>existing.has(id)));}
  // 定时刷新与手动刷新共用一次请求：上一次没结束就复用它，避免定时器把请求越堆越多。
  // 静默刷新失败时不再完全吞掉：页面顶部给出提示，恢复后自动消失，避免界面停在旧数据上。
  function setRefreshStatus(failed,message){
    const el=$('refreshStatus');
    if(!el)return;
    el.classList.toggle('show',Boolean(failed));
    el.classList.toggle('error',Boolean(failed));
    if(failed)el.textContent=`数据刷新失败：${message||'无法连接本地服务'}（界面可能不是最新，点击“↻ 刷新”重试）`;
  }
  // pendingManualRefresh 记录“正在进行的静默请求之后还要再跑一次”，
  // 保证用户点“↻ 刷新”时拿到的一定是新数据，而不是复用一个进行中的静默请求。
  let pendingManualRefresh=null;
  function refreshAll(options={}){const {silent=false}=options;
    if(refreshing){
      if(silent)return refreshing;
      if(!pendingManualRefresh){
        pendingManualRefresh=refreshing.catch(()=>{}).then(()=>{pendingManualRefresh=null;return refreshAll({silent:false})});
      }
      return pendingManualRefresh;
    }
    // 无论自动还是手动刷新都走增量更新：整表重绘会打断正在播放的视频、丢失滚动位置。
    refreshing=(async()=>{try{await loadState();renderCurrent({keepMedia:true});$('statAccounts').textContent=state.stats.accounts_total;$('statQueued').textContent=state.stats.pending??state.stats.queued;$('statProcessing').textContent=state.stats.processing??state.stats.generating;$('statAttention').textContent=state.stats.attention??0;$('statDownloaded').textContent=state.stats.archived??state.stats.downloaded;state.lastRefreshError='';setRefreshStatus(false);}catch(e){state.lastRefreshError=e.message||String(e);setRefreshStatus(true,state.lastRefreshError);if(!silent)toast(e.message,true)}finally{refreshing=null}})();return refreshing;}
  // 用户正在操作时暂停自动刷新，避免打断。只拦真正会被重绘破坏的操作：
  // 弹窗、正在播放的视频、正在编辑正文。单纯聚焦搜索框不应冻结整个列表。
  function autoRefreshPaused(){
    if(document.hidden)return true;
    if(document.querySelector('.modal-backdrop.show'))return true;
    const active=document.activeElement;
    if(active&&active.isContentEditable)return true;
    // 弹窗外的输入框（如任务搜索）只在用户正在输入时短暂暂停，聚焦本身不暂停。
    if(active&&['INPUT','TEXTAREA','SELECT'].includes(active.tagName)&&active.dataset.freezeRefresh==='1')return true;
    const video=[...document.querySelectorAll('#jobsTable video')].find(item=>!item.paused);
    if(video)return true;
    return false;
  }
  function renderIgnoreQuotaSettingToggle(){
    const toggle=$('ignoreQuotaSettingToggle'),status=$('ignoreQuotaSettingState');
    if(!toggle||!status)return;
    const active=state.settingIgnoreQuota;
    toggle.checked=active;
    status.textContent=active?'已开启 · 自动选择最大剩余账号':'已关闭 · 执行本地额度判断';
  }
  function setIgnoreQuotaSetting(active){state.settingIgnoreQuota=Boolean(active);renderIgnoreQuotaSettingToggle()}
  function renderClaudeRecoverySettingToggle(){const toggle=$('claudeRecoverySettingToggle'),status=$('claudeRecoverySettingState');if(!toggle||!status)return;const active=state.settingClaudeRecovery;toggle.checked=active;status.textContent=active?'已开启 · 异常任务由 Claude 自动恢复':'已关闭 · 异常任务转人工处理'}
  function setClaudeRecoverySetting(active){state.settingClaudeRecovery=Boolean(active);renderClaudeRecoverySettingToggle()}
  function renderAutoSwitchOnExceptionSettingToggle(){const toggle=$('autoSwitchOnExceptionSettingToggle'),status=$('autoSwitchOnExceptionSettingState');if(!toggle||!status)return;const active=state.settingAutoSwitchOnException;toggle.checked=active;status.textContent=active?'已开启 · 首次异常自动换号执行':'已关闭 · 异常任务不自动换号'}
  function setAutoSwitchOnExceptionSetting(active){state.settingAutoSwitchOnException=Boolean(active);renderAutoSwitchOnExceptionSettingToggle()}
  function renderAutomationSettingToggles(){const acceptanceToggle=$('taskAcceptanceSettingToggle'),acceptanceState=$('taskAcceptanceSettingState'),acceptanceFlow=$('acceptanceFlowState'),dispatchToggle=$('autoDispatchSettingToggle'),dispatchState=$('autoDispatchSettingState'),executionToggle=$('autoExecutionSettingToggle'),executionState=$('autoExecutionSettingState'),dispatchFlow=$('dispatchFlowState'),executionFlow=$('executionFlowState');if(acceptanceToggle){acceptanceToggle.checked=state.settingAcceptingTasks;acceptanceState.textContent=state.settingAcceptingTasks?'接受中 · 新任务可以正常提交':'已停止 · 拒绝接收新任务'}if(acceptanceFlow){acceptanceFlow.textContent=state.settingAcceptingTasks?'接受新任务':'停止接收任务';acceptanceFlow.classList.toggle('flow-start',state.settingAcceptingTasks);acceptanceFlow.classList.toggle('flow-stop',!state.settingAcceptingTasks)}if(dispatchToggle){dispatchToggle.checked=state.settingAutoDispatch;dispatchState.textContent=state.settingAutoDispatch?'已开启 · 新任务自动进入调度':'已关闭 · 新任务先进入待处理'}if(executionToggle){executionToggle.checked=state.settingAutoExecution;executionState.textContent=state.settingAutoExecution?'已开启 · 脚本自动发送并完成流程':'已关闭 · 准备页面后等待人工发送'}if(dispatchFlow)dispatchFlow.textContent=state.settingAutoDispatch?'自动进入调度':'等待手动放行';if(executionFlow)executionFlow.textContent=state.settingAutoExecution?'自动执行完整流程':'准备后人工发送'}
  function setTaskAcceptanceSetting(active){state.settingAcceptingTasks=Boolean(active);renderAutomationSettingToggles()}
  function setAutoDispatchSetting(active){state.settingAutoDispatch=Boolean(active);renderAutomationSettingToggles()}
  function setAutoExecutionSetting(active){state.settingAutoExecution=Boolean(active);renderAutomationSettingToggles()}
  function openSubmitModeSettings(){state.settingAcceptingTasks=state.acceptingTasks;state.settingAutoDispatch=state.autoDispatchEnabled;state.settingAutoExecution=state.submitMode==='auto';state.settingIgnoreQuota=state.ignoreQuotaCheck;state.settingClaudeRecovery=state.claudeRecoveryEnabled;state.settingAutoSwitchOnException=state.autoSwitchOnException;$('memberModelSetting').value=state.memberVideoModel;$('nonMemberModelSetting').value=state.nonMemberVideoModel;$('processingLimitSetting').value=String(state.processingLimit);$('autoDispatchIntervalSetting').value=String(state.autoDispatchIntervalSeconds);renderAutomationSettingToggles();renderIgnoreQuotaSettingToggle();renderClaudeRecoverySettingToggle();renderAutoSwitchOnExceptionSettingToggle();openModal('submitModeModal')}
  $('submitModeForm').addEventListener('submit',async e=>{e.preventDefault();const acceptingTasks=Boolean(state.settingAcceptingTasks),autoDispatch=Boolean(state.settingAutoDispatch),dispatchInterval=Math.min(3600,Math.max(1,Math.trunc(Number($('autoDispatchIntervalSetting').value)||5))),mode=state.settingAutoExecution?'auto':'manual',memberModel=$('memberModelSetting').value,nonMemberModel=$('nonMemberModelSetting').value,limit=Math.max(0,Math.trunc(Number($('processingLimitSetting').value)||0)),ignoreQuota=Boolean(state.settingIgnoreQuota),claudeRecovery=Boolean(state.settingClaudeRecovery),autoSwitchOnException=Boolean(state.settingAutoSwitchOnException);try{const [acceptanceResult,dispatchResult,dispatchIntervalResult,modeResult,modelResult,limitResult,quotaResult,claudeResult,autoSwitchResult]=await Promise.all([api('/api/settings/task-acceptance',{method:'PUT',body:JSON.stringify({accepting_tasks:acceptingTasks})}),api('/api/settings/auto-dispatch',{method:'PUT',body:JSON.stringify({auto_dispatch_enabled:autoDispatch})}),api('/api/settings/auto-dispatch-interval',{method:'PUT',body:JSON.stringify({auto_dispatch_interval_seconds:dispatchInterval})}),api('/api/settings/video-submit-mode',{method:'PUT',body:JSON.stringify({submit_mode:mode})}),api('/api/settings/video-models',{method:'PUT',body:JSON.stringify({member_model:memberModel,non_member_model:nonMemberModel})}),api('/api/settings/processing-limit',{method:'PUT',body:JSON.stringify({processing_limit:limit})}),api('/api/settings/ignore-quota-check',{method:'PUT',body:JSON.stringify({ignore_quota_check:ignoreQuota})}),api('/api/settings/claude-recovery',{method:'PUT',body:JSON.stringify({claude_recovery_enabled:claudeRecovery})}),api('/api/settings/auto-switch-on-exception',{method:'PUT',body:JSON.stringify({auto_switch_on_exception:autoSwitchOnException})})]);state.acceptingTasks=acceptanceResult.accepting_tasks!==false;state.autoDispatchEnabled=Boolean(dispatchResult.auto_dispatch_enabled);state.autoDispatchIntervalSeconds=dispatchIntervalResult.auto_dispatch_interval_seconds;state.submitMode=modeResult.submit_mode;state.memberVideoModel=modelResult.member_model;state.nonMemberVideoModel=modelResult.non_member_model;state.processingLimit=limitResult.processing_limit;state.ignoreQuotaCheck=quotaResult.ignore_quota_check;state.claudeRecoveryEnabled=Boolean(claudeResult.claude_recovery_enabled);state.autoSwitchOnException=Boolean(autoSwitchResult.auto_switch_on_exception);closeModal('submitModeModal');toast(`设置已保存：${state.acceptingTasks?'接受任务':'停止接收'}；自动调度${state.autoDispatchEnabled?'已开启':'已关闭'}（${state.autoDispatchIntervalSeconds} 秒），自动执行${state.submitMode==='auto'?'已开启':'已关闭'}，异常自动换号${state.autoSwitchOnException?'已开启':'已关闭'}`)}catch(err){toast(err.message,true)}});
  function renderCurrent(options={}){renderOverview();renderAccounts();renderJobs(options);}
  function jobAccountOrder(accountId){const account=state.accounts.find(item=>item.id===accountId);return account&&Number(account.sequence)?String(Number(account.sequence)).padStart(2,'0'):'—';}
  function renderOverview(){const jobs=state.jobs.slice(0,5);$('overviewJobs').innerHTML=jobs.length?`<div class="mini-list">${jobs.map(j=>{const label=[j.project_id,j.episode_id,j.shot_id].filter(Boolean).join(' / ')||j.id;return `<div class="mini-row"><div class="avatar">${esc(jobAccountOrder(j.account_id))}</div><div class="mini-info"><strong>${esc(label)}</strong><span>${esc((j.prompt||'').slice(0,65))}${(j.prompt||'').length>65?'…':''}</span></div>${statusPill(j.status)}</div>`}).join('')}</div>`:`<div class="empty"><div class="empty-icon">▤</div>还没有任务。创建一个分镜任务开始使用。</div>`;}
  function changeAccountPage(delta){state.accountPage+=Number(delta)||0;renderAccounts()}
  function renderAccounts(){const q=($('accountSearch')?.value||'').trim().toLowerCase();if(q!==state.accountQuery){state.accountQuery=q;state.accountPage=1}const list=state.accounts.filter(a=>`${a.name} ${a.note} ${a.id}`.toLowerCase().includes(q)),pageSize=state.accountPageSize,totalPages=Math.max(1,Math.ceil(list.length/pageSize));state.accountPage=Math.min(Math.max(1,state.accountPage),totalPages);const start=(state.accountPage-1)*pageSize,pageList=list.slice(start,start+pageSize);if($('accountCountLabel'))$('accountCountLabel').textContent=`${state.accounts.length} 个档案 · 显示 ${list.length} 个${list.length>pageSize?` · 第 ${state.accountPage}/${totalPages} 页`:''}`;if($('accountQuotaTotal'))$('accountQuotaTotal').textContent=quotaDuration(state.accounts.reduce((sum,a)=>sum+(Number(a.quota?.remaining_seconds)||0),0));$('accountsTable').innerHTML=pageList.length?pageList.map(a=>`<tr><td><span class="pill gray">${String(Number(a.sequence)||0).padStart(2,'0')}</span></td><td><div class="account-cell"><div class="account-symbol">${esc((a.name||'DB').slice(0,2))}</div><div class="account-main"><div class="cell-title">${esc(a.name)}</div><div class="cell-sub">${esc(a.note||a.id)}</div></div></div></td><td>${a.is_member?'<span class="pill green">会员</span>':'<span class="pill gray">普通</span>'}${a.paused?' <span class="pill yellow">已暂停</span>':''}<div class="cell-sub">优先级 ${Number(a.priority)||0}</div></td><td>${a.browser_open?'<span class="pill green">● 窗口已打开</span>':'<span class="pill gray">未打开</span>'}</td><td>${quotaCell(a)}</td><td>${esc(shortDate(a.last_opened_at))}</td><td><div class="row-actions">${a.browser_open?`<button class="btn small" onclick="closeAccount('${esc(a.id)}')">关闭窗口</button>`:`<button class="btn small primary" onclick="openAccount('${esc(a.id)}')">打开豆包</button>`}<button class="btn small ${a.paused?'primary':''}" onclick="toggleAccountPaused('${esc(a.id)}',${Boolean(a.paused)})">${a.paused?'恢复使用':'暂停使用'}</button><button class="btn small ghost" onclick="openAccountSettings('${esc(a.id)}')">设置</button><button class="btn small ghost danger" onclick="deleteAccount('${esc(a.id)}','${esc(a.name)}')">删除</button></div></td></tr>`).join(''):`<tr><td colspan="7"><div class="empty"><div class="empty-icon">◎</div>没有匹配的账号档案。点击“添加账号档案”创建。</div></td></tr>`;const pager=$('accountPagination');if(pager){pager.classList.toggle('hidden',list.length<=pageSize);pager.innerHTML=`<span>第 ${state.accountPage} / ${totalPages} 页 · 本页 ${pageList.length} 个</span><div><button class="btn small" onclick="changeAccountPage(-1)" ${state.accountPage<=1?'disabled':''}>← 上一页</button><button class="btn small" onclick="changeAccountPage(1)" ${state.accountPage>=totalPages?'disabled':''}>下一页 →</button></div>`}}
  function accountOptions(selected,emptyLabel='暂不指定'){return `<option value="">${esc(emptyLabel)}</option>`+state.accounts.map(a=>`<option value="${esc(a.id)}" ${a.id===selected?'selected':''} ${a.paused&&a.id!==selected?'disabled':''}>${esc(a.name)}${a.paused?' · 已暂停':''}${a.is_member?' · 会员':''} · 优先级${Number(a.priority)||0}${a.browser_open?' · 窗口打开':''} · 剩${a.quota?.remaining_seconds??0}s</option>`).join('');}
  function fillAccountSelect(){$('jobAccount').innerHTML=accountOptions('');}
  const statusHints = {queued:'等待本机开始；进入处理前才分配账号',ready:'等待人工提交',opening:'正在打开豆包页面',configuring:'正在配置参数',uploading:'正在上传素材',submitting:'正在提交',generating:'确认语已发送，等待豆包生成',confirming:'豆包要求确认，正在发送确认语',awaiting_manual_send:'页面已填好，可以执行“确认发送”',downloading:'已检测到视频，正在下载归档',needs_review:'需要人工打开任务页面检查',downloaded:'视频已下载；确认无误后点“归档”',failed:'任务已停止，可重新准备'};
  const modeNames = {text2video:'文生视频',image2video:'首帧图生',first_last_frame:'首尾帧',reference_images:'参考图'};
  function statusHint(s){return statusHints[s]||'—';}
  const recoveryToolLabels = {restart_browser:'重启账号浏览器',switch_page:'切换任务会话页',refresh_page:'刷新任务页面',inspect_dom:'检查页面 DOM',dismiss_popup:'处理干扰弹窗',locate_task_page:'定位任务页面',verify_task_status:'核对任务状态',check_assets:'检查任务素材',recall_existing_result:'召回已有结果',download_video:'下载绑定视频',retry_h3_push:'重推 H3',restart_auto:'恢复自动任务',regenerate:'创建重试任务',release_account:'释放账号占用'};
  function recoveryActive(j){return Boolean(j.recovery&&j.recovery.active)}
  function recoveryHint(j){const r=j.recovery||{},label=recoveryToolLabels[r.action]||r.action||'';if(r.active)return `${r.detail||'Claude 正在处理异常'}${label&&!(r.detail||'').includes(label)?`（${label}）`:''}`;if(r.state==='needs_human'&&(r.manual_reason||r.detail))return `Claude 自动恢复未完成：${r.manual_reason||r.detail}`;if(r.state==='failed'&&r.detail)return `Claude 恢复失败：${r.detail}`;return ''}
  function recoveryBadge(j){const r=j.recovery||{};if(r.active)return `<span class="badge claude"><span class="claude-dot"></span>Claude 处理中</span>`;if(r.state==='needs_human')return '<span class="badge claude-manual">Claude 已转人工</span>';if(r.state==='failed'||r.state==='cancelled')return '<span class="badge claude-error">Claude 已停止</span>';return ''}
  function recoveryPill(j){const r=j.recovery||{};if(r.active)return '<span class="pill claude"><span class="claude-dot"></span>Claude 处理中</span>';if(r.state==='needs_human')return '<span class="pill red">Claude 转人工</span>';return ''}
  function jobStatusHint(j){const live=recoveryHint(j);if(live)return live;if(j.error_code==='USER_CANCELED')return '任务已取消，可安全删除或重新执行';if(j.status==='needs_review'&&j.error_code==='VERIFICATION_REQUIRED')return '需要人工验证；完成后点击“换号自动执行”';if(j.status==='needs_review'&&j.error_code==='LOGIN_REQUIRED')return '需要人工登录；完成后点击“换号自动执行”';if(j.status==='needs_review'&&j.error_code==='SUBMISSION_UNCONFIRMED')return '提交结果未确认；请先核对豆包记录，确认未生成后点击“重新生成（新任务）”';return statusHint(j.status)}
  function jobStep(j){const s=j.status;if(!j.is_web_generation)return 0;if(s==='queued')return 1;if(['opening','configuring','uploading'].includes(s))return 2;if(['awaiting_manual_send','submitting','generating','confirming','downloading'].includes(s))return 3;if(s==='downloaded')return 4;if(s==='needs_review'||s==='failed')return 3;return 1;}
  function stepTrack(j){const step=jobStep(j),manual=j.submit_mode==='manual';const labels=manual?['建单','准备页面','网页发送','关联归档']:['建单','准备页面','自动提交','关联归档'];const blocked=(j.status==='needs_review'||j.status==='failed')&&!recoveryActive(j);const dots=labels.map((label,i)=>{const n=i+1;let cls='';if(j.status==='downloaded')cls='done';else if(n<step)cls='done';else if(n===step)cls=blocked?'blocked':'current';return `<span class="step-dot ${cls}" title="${label}"></span>`;}).join('');return `<div class="progress-top"><span class="progress-status">${statusPill(j.status)}${recoveryPill(j)}</span><span class="step-count">${step}/4 步</span></div><div class="step-track">${dots}</div><div class="progress-hint"><strong>${recoveryActive(j)?'Claude 自主恢复':labels[Math.max(0,step-1)]||labels[0]}</strong> · ${jobStatusHint(j)}</div>`;}
  function fixedJobActions(j){const isWeb=j.is_web_generation,hasPage=Boolean(j.has_task_page||j.web_page_url),pending=j.status==='queued'||j.status==='ready',takeover=Boolean(j.operator_takeover),recovering=recoveryActive(j),busyHint='Claude 正在处理该任务，请等待恢复结束';const canPrepare=!isWeb||['queued','awaiting_manual_send','confirming','generating','needs_review','failed'].includes(j.status);
    // 待处理任务进入调度后，仍按建单时保存的自动/人工执行方式处理。
    // 已放行但还在等名额的任务在库里仍是 queued，靠 dispatched_at 把它和“没放行”分开。
    const dispatched=pending&&Boolean(j.dispatched_at);
    const prepareFn=isWeb?(pending?`dispatchJob('${esc(j.id)}')`:`prepareManualJob('${esc(j.id)}')`):`prepareJob('${esc(j.id)}')`;
    const prepareLabel=isWeb?(pending?(dispatched?'⏳ 已放行 · 排队中':j.submit_mode==='manual'?'▶ 进入调度 · 人工发送':'▶ 进入调度'):'1 提交素材'):'1 人工准备';
    const prepareTitle=recovering&&!takeover?busyHint:dispatched&&!takeover?'已交给调度器，正在等处理名额或可用账号，无需重复点击。':'';const openPageFn=`openJobPage('${esc(j.id)}')`;const closePageFn=`closeJobPage('${esc(j.id)}')`;const sendFn=`sendPreparedJob('${esc(j.id)}')`;const confirmFn=`sendJobConfirm('${esc(j.id)}')`;const downloadFn=isWeb?`linkJobResult('${esc(j.id)}')`:(j.video_path?`window.open('/api/jobs/${esc(j.id)}/video','_blank')`:'');const pushFn=`pushJobResult('${esc(j.id)}',this)`;const hint=takeover?'人工接管中，可直接尝试任一流程操作；接口仍会校验所需页面、视频和推送配置。':recovering?busyHint:isWeb?'自动提交和人工任务均可使用这些按钮人工准备、发送、确认、下载和推送。':'普通任务不支持网页发送、确认、下载和回推操作。';const nextIndex=isWeb?({queued:0,awaiting_manual_send:1,confirming:2,generating:3,needs_review:3,failed:3,downloaded:4}[j.status]??-1):-1;const canSend=isWeb&&j.status==='awaiting_manual_send'&&hasPage;const canConfirm=isWeb&&['awaiting_manual_send','confirming','needs_review'].includes(j.status)&&hasPage;const canDownload=isWeb&&['downloaded','needs_review','failed','awaiting_manual_send','confirming','generating'].includes(j.status)&&hasPage;const actions=[{label:prepareLabel,fn:prepareFn,disabled:!takeover&&(recovering||!canPrepare||dispatched),title:prepareTitle},{label:'2 确认发送',fn:sendFn,disabled:!takeover&&(recovering||!canSend),title:recovering&&!takeover?busyHint:''},{label:'3 发送确认语',fn:confirmFn,disabled:!takeover&&(recovering||!canConfirm),title:recovering&&!takeover?busyHint:''},{label:'4 下载视频',fn:downloadFn,disabled:!takeover&&(recovering||(isWeb?!canDownload:!j.video_path)),title:recovering&&!takeover?busyHint:''},{label:j.result_pushed_at?'再次推送服务器':'推送服务器',fn:pushFn,disabled:!takeover&&(recovering||!isWeb||!j.video_path||!j.can_push_result),title:recovering&&!takeover?busyHint:''},{label:'打开网页',fn:openPageFn,disabled:!isWeb||!hasPage},...(isWeb&&hasPage?[{label:'关闭网页',fn:closePageFn,disabled:recovering,title:recovering?busyHint:''}]:[]),...(isWeb&&pending?[{label:'准备页面（人工发送）',fn:`prepareManualJob('${esc(j.id)}')`,disabled:recovering,title:recovering?busyHint:''}]:[])].map((action,i)=>({...action,cls:i===nextIndex&&!recovering?'primary':''}));return {hint,recovering,actions,tripleSecondary:isWeb&&hasPage};}
  function jobSearchTerms(value){return String(value||'').toLowerCase().split(/[\s/／]+/).filter(Boolean)}
  function highlightJobSearchMatches(root,terms){if(!terms.length)return;root.querySelectorAll('.job-title').forEach(node=>{const text=node.textContent||'',lower=text.toLowerCase(),matched=Array(text.length).fill(false);terms.forEach(term=>{let index=0;while((index=lower.indexOf(term,index))>=0){for(let i=index;i<index+term.length;i++)matched[i]=true;index+=term.length}});if(!matched.some(Boolean))return;const fragment=document.createDocumentFragment();for(let start=0;start<text.length;){const highlighted=matched[start];let end=start+1;while(end<text.length&&matched[end]===highlighted)end++;const part=document.createElement(highlighted?'mark':'span');if(highlighted)part.className='job-search-match';part.textContent=text.slice(start,end);fragment.appendChild(part);start=end}node.replaceChildren(fragment)})}
  function filteredJobs(){const terms=jobSearchTerms($('jobSearch')?.value);return state.jobs.filter(j=>{if(jobSectionKey(j)!==state.jobSection||(state.jobSubgroup&&jobSubgroupKey(j)!==state.jobSubgroup))return false;const searchText=`${j.project_id||''} ${j.episode_id||''} ${j.shot_id||''}`.toLowerCase();return terms.every(term=>searchText.includes(term))});}
  function selectedJobs(){const byId=new Map(state.jobs.map(j=>[j.id,j]));return [...state.selectedJobIds].map(id=>byId.get(id)).filter(Boolean);}
  function bulkApplies(action,job){if(action==='delete')return true;if(action==='archive')return job.status==='downloaded'&&!job.archived;const web=Boolean(job.is_web_generation),hasPage=Boolean(job.has_task_page||job.web_page_url),restartable=['queued','awaiting_manual_send','confirming','generating','needs_review','failed'];if(action==='cancel')return web&&!['downloaded','failed'].includes(job.status);if(action==='prepare')return web&&['queued','ready'].includes(job.status)&&!job.dispatched_at;if(action==='auto')return web&&restartable.includes(job.status);if(action==='send')return web&&job.status==='awaiting_manual_send'&&hasPage;if(action==='confirm')return web&&['awaiting_manual_send','confirming','needs_review'].includes(job.status)&&hasPage;if(action==='download')return web&&['downloaded','needs_review','failed','awaiting_manual_send','confirming','generating'].includes(job.status)&&hasPage;if(action==='push')return web&&Boolean(job.video_path)&&Boolean(job.can_push_result)&&!job.result_pushed_at;return false}
  function bulkActionJobs(action){const jobs=selectedJobs();return action==='delete'?jobs:jobs.filter(job=>bulkApplies(action,job))}
  function syncJobBulkControls(list=filteredJobs()){const existing=new Set(state.jobs.map(j=>j.id));[...state.selectedJobIds].forEach(id=>{if(!existing.has(id))state.selectedJobIds.delete(id)});const count=state.selectedJobIds.size,busy=Boolean(state.bulkJobAction),progress=state.bulkProgress||{done:0,total:0},bar=$('jobBulkBar'),visibleIds=list.map(j=>j.id),selectedVisible=visibleIds.filter(id=>state.selectedJobIds.has(id)).length,hidden=Math.max(0,count-selectedVisible);bar.classList.toggle('active',count>0);bar.classList.toggle('busy',busy);$('jobSelectedCount').textContent=busy?(progress.total?`正在处理 ${progress.done}/${progress.total} 项…`:'正在处理…'):`已选 ${count} 项`;$('jobSelectedCount').classList.toggle('hidden',count===0&&!busy);$('jobSelectedTotal').textContent=busy?'':count>0?`当前筛选 ${list.length} 项`:'';$('jobHiddenSelected').textContent=hidden>0?`其中 ${hidden} 项不在当前筛选`:'';$('jobHiddenSelected').classList.toggle('hidden',hidden===0||busy);$('clearSelectedJobs').classList.toggle('hidden',count===0||busy);$('jobBulkHint').classList.toggle('hidden',count>0);document.querySelectorAll('[data-bulk-action]').forEach(button=>{const action=button.dataset.bulkAction,n=action==='delete'?count:bulkActionJobs(action).length,badge=button.querySelector('.bulk-n');if(badge)badge.textContent=n;button.disabled=busy||n===0;button.title=busy?'批量处理进行中':n===0?'选中的任务中没有适用项':`对 ${n} 个适用任务执行`});const head=$('jobSelectAllHead');if(head){head.checked=visibleIds.length>0&&selectedVisible===visibleIds.length;head.indeterminate=selectedVisible>0&&selectedVisible<visibleIds.length;head.disabled=busy||visibleIds.length===0;head.title=visibleIds.length?`全选当前筛选的 ${visibleIds.length} 项`:'当前没有任务'};const prog=$('jobBulkProgress'),showProgress=busy&&progress.total>0;prog.classList.toggle('hidden',!showProgress);if(showProgress)$('jobBulkProgressBar').style.width=`${Math.round(progress.done/progress.total*100)}%`}
  function paintJobSelection(){document.querySelectorAll('#jobsTable tr[data-job-id]').forEach(row=>{const on=state.selectedJobIds.has(row.dataset.jobId);row.classList.toggle('job-selected',on);const input=row.querySelector('.job-select-input');if(input&&input.checked!==on)input.checked=on})}
  // 顶部四个队列。原“待人工”并入“正在处理”之下，用子分组区分正常与异常。
  const jobGroupDefs=[
    {key:'queue',label:'待处理',hint:'等待开始执行；进入处理前才分配账号',statuses:new Set(['queued','ready'])},
    {key:'processing',label:'正在处理',hint:'机器正在执行；等待人工的任务也在这里',
     statuses:new Set(['opening','configuring','uploading','submitting','confirming','generating','downloading','awaiting_manual_send','needs_review']),
     subgroups:[
       {key:'normal',label:'正常',hint:'机器正在执行',statuses:new Set(['opening','configuring','uploading','submitting','confirming','generating','downloading'])},
       {key:'abnormal',label:'异常待人工',hint:'需要你确认或复查',statuses:new Set(['awaiting_manual_send','needs_review'])},
     ]},
    {key:'finished',label:'处理完',hint:'视频已下载；确认后手动归档',statuses:new Set(['downloaded','failed'])},
    {key:'archived',label:'已归档',hint:'你已确认归档的任务',statuses:new Set(),match:job=>Boolean(job.archived)},
  ];
  function jobSectionKey(job){if(job.archived)return 'archived';const group=jobGroupDefs.find(item=>item.statuses.has(job.status));return group?.key||'processing'}
  // 正在处理内部的子分组：等待人工算“异常待人工”，其余执行阶段算“正常”。
  function jobSubgroupKey(job){if(job.archived)return '';const group=jobGroupDefs.find(item=>item.key===jobSectionKey(job));const found=group?.subgroups?.find(sub=>sub.statuses.has(job.status));return found?.key||''}
  // 切换顶部队列时清空子分组（回到该队列的“全部”视图）。
  function setJobSection(key){if(!jobGroupDefs.some(group=>group.key===key))return;state.jobSection=key;state.jobSubgroup='';state.lastJobCheckIndex=-1;renderJobs()}
  function setJobSubgroup(key){
    const group=jobGroupDefs.find(item=>item.key===state.jobSection);
    if(!group?.subgroups)return;
    // 点同一个子分组等于取消筛选，回到“全部”。
    state.jobSubgroup=state.jobSubgroup===key?'':key;
    state.lastJobCheckIndex=-1;
    renderJobs();
  }
  function syncJobSectionTabs(){
    document.querySelectorAll('[data-job-section]').forEach(button=>{
      const key=button.dataset.jobSection,count=state.jobs.filter(job=>jobSectionKey(job)===key).length;
      button.classList.toggle('active',key===state.jobSection);
      const badge=button.querySelector('b');if(badge)badge.textContent=count;
    });
    const group=jobGroupDefs.find(item=>item.key===state.jobSection);
    const bar=$('jobSubgroupBar');
    if(!bar)return;
    if(!group?.subgroups){bar.classList.add('hidden');bar.innerHTML='';return;}
    const total=state.jobs.filter(job=>jobSectionKey(job)===state.jobSection).length;
    const parts=[`<button class="job-subgroup-tab ${state.jobSubgroup?'':'active'}" onclick="setJobSubgroup('')">全部 <b>${total}</b></button>`];
    group.subgroups.forEach(sub=>{
      const count=state.jobs.filter(job=>jobSectionKey(job)===state.jobSection&&jobSubgroupKey(job)===sub.key).length;
      const abnormal=sub.key==='abnormal'&&count>0?' alert':'';
      parts.push(`<button class="job-subgroup-tab${abnormal} ${state.jobSubgroup===sub.key?'active':''}" data-job-subgroup="${sub.key}" onclick="setJobSubgroup('${sub.key}')" title="${esc(sub.hint)}">${esc(sub.label)} <b>${count}</b></button>`);
    });
    bar.innerHTML=parts.join('');
    bar.classList.remove('hidden');
  }
  function orderedVisibleJobs(){return filteredJobs()}
  function jobCheckboxClick(event,jobId,input){const ids=orderedVisibleJobs().map(j=>j.id),idx=ids.indexOf(jobId),checked=input.checked;if(event.shiftKey&&state.lastJobCheckIndex>=0&&idx>=0){const from=Math.min(state.lastJobCheckIndex,idx),to=Math.max(state.lastJobCheckIndex,idx);for(let i=from;i<=to;i++){if(checked)state.selectedJobIds.add(ids[i]);else state.selectedJobIds.delete(ids[i])}}else{if(checked)state.selectedJobIds.add(jobId);else state.selectedJobIds.delete(jobId)}state.lastJobCheckIndex=idx;paintJobSelection();syncJobBulkControls()}
  function toggleAllJobs(checked){const visible=new Set(filteredJobs().map(job=>job.id));for(const id of visible){if(checked)state.selectedJobIds.add(id);else state.selectedJobIds.delete(id)}state.lastJobCheckIndex=-1;paintJobSelection();syncJobBulkControls()}
  function clearJobSelection(){state.selectedJobIds.clear();state.lastJobCheckIndex=-1;paintJobSelection();syncJobBulkControls()}
  async function deleteSelectedJobs(){const ids=[...state.selectedJobIds];if(!ids.length)return;if(!confirm(`永久删除选中的 ${ids.length} 个分镜任务？\n\n任务记录和归档视频将直接删除，不会放入垃圾桶且无法恢复；进行中的任务不会被删除。`))return;try{const result=await api('/api/jobs/bulk-delete',{method:'POST',body:JSON.stringify({job_ids:ids})});state.selectedJobIds.clear();await refreshAll();toast(`已永久删除 ${result.deleted} 个任务${result.deleted_files?`和 ${result.deleted_files} 个归档视频`:''}。`)}catch(e){toast(e.message,true)}}
  async function deleteJob(jobId){const job=state.jobs.find(item=>item.id===jobId),name=job?.task_name||[job?.project_id,job?.episode_id,job?.shot_id].filter(Boolean).join(' / ')||jobId;if(!confirm(`永久删除任务“${name}”？\n\n任务记录和归档视频将直接删除，不会放入垃圾桶且无法恢复；正在执行的任务不能删除。`))return;try{const result=await api('/api/jobs/bulk-delete',{method:'POST',body:JSON.stringify({job_ids:[jobId]})});state.selectedJobIds.delete(jobId);await refreshAll();toast(`任务已永久删除${result.deleted_files?'，归档视频也已删除':''}。`)}catch(e){toast(e.message,true)}}
  async function runSelectedJobAction(action){const jobs=bulkActionJobs(action),skipped=state.selectedJobIds.size-jobs.length,labels={auto:'换号自动执行',prepare:'进入调度',send:'确认发送',download:'下载视频',push:'推送服务器',archive:'归档',cancel:'取消任务'},label=labels[action]||'操作';if(!jobs.length){toast(`选中的任务中没有可执行“${label}”的任务`,true);return}state.bulkJobAction=action;state.bulkProgress={done:0,total:jobs.length};syncJobBulkControls();let succeeded=0;const failed=[];const runOne=async job=>{try{const id=encodeURIComponent(job.id);if(action==='archive')await api(`/api/jobs/${id}/archive`,{method:'POST'});else{if(!job.is_web_generation)throw Error('不是网页生成任务');if(action==='auto')await api(`/api/v1/videos/generations/${id}/restart-auto`,{method:'POST'});else if(action==='prepare')await api(`/api/v1/videos/generations/${id}/dispatch`,{method:'POST'});else if(action==='send')await api(`/api/v1/videos/generations/${id}/send?confirm_send=true`,{method:'POST'});else if(action==='download')await api(`/api/v1/videos/generations/${id}/recall?force_redownload=true`,{method:'POST'});else if(action==='push')await api(`/api/v1/videos/generations/${id}/push-result`,{method:'POST'});else if(action==='cancel')await api(`/api/v1/videos/generations/${id}/cancel?confirm_cancel=true`,{method:'POST'});else throw Error('未知批量操作');}succeeded++;}catch(e){failed.push({job,message:e.message});}state.bulkProgress.done++;syncJobBulkControls()};try{if(action==='auto')await Promise.all(jobs.map(runOne));else for(const job of jobs)await runOne(job);}finally{state.bulkJobAction=null;state.bulkProgress=null}if(action==='archive'&&succeeded)state.selectedJobIds.clear();syncJobBulkControls();await refreshAll();const first=failed[0],detail=first?`；首个失败：${first.job.task_name||first.job.shot_id||first.job.id}（${first.message}）`:'',skipText=skipped>0?`，跳过 ${skipped} 个不适用任务`:'';toast(`批量${label}完成：成功 ${succeeded} 个，失败 ${failed.length} 个${skipText}${detail}`,succeeded===0&&failed.length>0);}
  function webJobAccountControl(j,a){const pending=j.status==='queued'||j.status==='ready';if(pending)return `<select class="select-mini web-account-select" data-freeze-refresh="1" onchange="assignQueuedWebAccount('${esc(j.id)}',this.value)">${accountOptions(j.account_id,'自动分配（进入处理前）')}</select><div class="cell-sub">${a?`已选定 ${esc(a.name)}；开始执行时预占额度`:'未指定账号；开始执行时自动选择'}</div>`;return `<div class="job-account"><div class="account-mini ${a?'':'empty'}">${esc(a?(a.name||'DB').slice(0,2):'—')}</div><div style="min-width:0"><div class="cell-title">${esc(a?a.name:'未关联账号')}</div><div class="quota-line">${a?`剩余 <b>${a.quota?.remaining_seconds??'?'}s</b>${a.browser_open?' · 窗口已开':''}${j.archived?' · 已归档':''}`:'请先关联账号'}</div></div></div>`;}
  let jobVideoObserver=null;
  function loadLazyJobVideo(video){if(video.hasAttribute('src')||!video.dataset.src)return;const resumeAt=Number(video.dataset.resumeAt)||0;if(resumeAt>0)video.addEventListener('loadedmetadata',()=>{if(resumeAt<video.duration)video.currentTime=resumeAt},{once:true});video.src=video.dataset.src;video.load()}
  function releaseLazyJobVideo(video){if(!video.hasAttribute('src')||!video.paused)return;if(Number.isFinite(video.currentTime)&&video.currentTime>0)video.dataset.resumeAt=String(video.currentTime);video.removeAttribute('src');video.load()}
  function bindLazyJobVideos(root){const videos=[...root.querySelectorAll('.job-video-preview[data-src]')];if(!('IntersectionObserver' in window)){videos.forEach(loadLazyJobVideo);return}if(!jobVideoObserver)jobVideoObserver=new IntersectionObserver(entries=>entries.forEach(entry=>{const video=entry.target;video.dataset.nearViewport=entry.isIntersecting?'1':'0';if(entry.isIntersecting)loadLazyJobVideo(video);else releaseLazyJobVideo(video)}),{rootMargin:'600px 0px'});videos.forEach(video=>{if(video.dataset.lazyObserved==='1')return;video.dataset.lazyObserved='1';video.addEventListener('pause',()=>{if(video.dataset.nearViewport==='0')releaseLazyJobVideo(video)});jobVideoObserver.observe(video)})}
  function unbindLazyJobVideos(root){if(!jobVideoObserver)return;root.querySelectorAll('.job-video-preview[data-src]').forEach(video=>jobVideoObserver.unobserve(video))}
  function renderJobRow(j){const a=state.accounts.find(x=>x.id===j.account_id);const info=[j.project_id,j.episode_id,j.shot_id].filter(Boolean).join(' / ')||j.id;const title=j.task_name||info;const prompt=(j.prompt||'').replace(/\s+/g,' ').trim();const controls=fixedJobActions(j);const isWeb=j.is_web_generation,manual=j.submit_mode==='manual',selected=state.selectedJobIds.has(j.id);const meta=[];if(isWeb)meta.push(`<span class="badge ${manual?'manual':'auto'}">${manual?'人工发送':'自动提交'}</span>`);else meta.push('<span class="badge">手工任务</span>');if(isWeb&&j.duration)meta.push(`<span class="badge">${esc(String(j.duration))} 秒</span>`);if(isWeb&&j.ratio)meta.push(`<span class="badge">${esc(j.ratio)}</span>`);if(isWeb&&modeNames[j.mode])meta.push(`<span class="badge">${modeNames[j.mode]}</span>`);if(isWeb&&j.model)meta.push(`<span class="badge model">${esc(j.model)}</span>`);if(recoveryActive(j)||['needs_human','failed','cancelled'].includes((j.recovery||{}).state))meta.push(recoveryBadge(j));if(isWeb&&j.recall_state==='recalling')meta.push(`<span class="badge">${j.status==='downloaded'?'重新下载中':'等待原结果'}</span>`);const account=isWeb?webJobAccountControl(j,a):`<select class="select-mini" onchange="assignAccount('${esc(j.id)}',this.value)">${accountOptions(j.account_id)}</select><div class="cell-sub">${a?esc(a.id):'尚未关联账号'}</div>`;const video=j.video_path?`<video class="job-video-preview" controls preload="metadata" playsinline data-src="/api/jobs/${encodeURIComponent(j.id)}/video"></video><div class="cell-sub" title="${esc(j.video_path)}">${esc(String(j.video_path).split(/[\\/]/).pop())}</div>`:`<span class="pill gray">未归档</span><div class="cell-sub">${isWeb?(manual?'等待人工检查并下载':'等待自动归档；异常时人工处理'):'生成后导入视频'}</div>`;const progress=isWeb?stepTrack(j):`${statusPill(j.status)}<div class="progress-hint">${statusHint(j.status)}</div>`;const buttons=controls.actions.map(action=>`<button class="btn small ${action.cls}" ${action.disabled?'disabled':''} onclick="${action.fn}" title="${esc(action.title||controls.hint)}">${action.label}</button>`).join('');const rowClass=controls.tripleSecondary?' has-page-actions':'';const archiveActions=(j.status==='downloaded'&&!j.archived)?`<button class="text-link archive-action" onclick="archiveJob('${esc(j.id)}')">✔ 归档</button>`:(j.archived?`<button class="text-link" onclick="unarchiveJob('${esc(j.id)}')">取消归档</button>`:'');const cancelAction=isWeb&&!['downloaded','failed'].includes(j.status)?`<button class="text-link danger" onclick="cancelJob('${esc(j.id)}')">取消任务</button>`:'';const utility=`<div class="next-utility">${archiveActions}${isWeb?`<button class="text-link" onclick="copyJob('${esc(j.id)}')">复制为新任务</button><button class="btn small primary utility-action" onclick="autoRunJob('${esc(j.id)}')">换号自动执行</button>`:''}${cancelAction}<button class="text-link danger" onclick="deleteJob('${esc(j.id)}')">删除任务</button></div>`;return `<tr data-job-id="${esc(j.id)}" class="${selected?'job-selected':''}"><td class="select-col"><label class="job-row-check" title="选择 ${esc(title)}"><input class="job-select-input" type="checkbox" aria-label="选择任务 ${esc(title)}" ${selected?'checked':''} onclick="jobCheckboxClick(event,'${esc(j.id)}',this)"></label></td><td><div class="job-main"><div class="job-order">${esc(jobAccountOrder(j.account_id))}</div><div style="min-width:0"><div class="job-title-row"><div class="job-title">${esc(title)}</div></div><div class="job-prompt" title="${esc(prompt)}">${esc(prompt.slice(0,120))}${prompt.length>120?'…':''}</div><div class="job-meta">${meta.join('')}</div><div class="cell-sub">创建时间：${esc(shortDate(j.created_at))} · ID: ${esc(j.id)}</div></div></div></td><td><div class="job-progress">${progress}</div></td><td>${account}</td><td>${video}</td><td><div class="next-action"><div class="next-row${rowClass}">${buttons}</div>${utility}${isWeb?'':`<div class="next-hint">${esc(controls.hint)}</div>`}</div></td></tr>`;}
  // 行内容指纹：只用来判断“这一行需不需要重建”，不参与渲染。
  // 行签名决定“这一行要不要重绘”。凡是会出现在行里的数据都必须进签名，
  // 否则数据变了但签名没变，静默刷新会跳过这一行，界面看起来像没刷新。
  function jobRowSignature(j){return [j.id,j.status,j.archived,j.archived_at,j.account_id,j.video_path,j.submit_mode,j.task_name,j.quota_state,j.has_task_page,j.web_page_url,j.session_url,j.session_id,j.result_card_index,j.recall_state,j.can_push_result,j.result_pushed_at,j.result_push_error,j.operator_takeover,j.error_code,j.error_message,j.updated_at,j.manual_confirm_sent,j.model,j.duration,j.ratio,j.mode,j.prompt,JSON.stringify(j.recovery||{})].join('|');}
  function renderJobs(options={}){const all=state.jobs,list=filteredJobs(),q=($('jobSearch')?.value||'').trim(),section=jobGroupDefs.find(group=>group.key===state.jobSection);syncJobSectionTabs();const table=$('jobsTable');
    // 连选锚点只在列表内容真正变化时失效；静默刷新不应打断正在进行的选择。
    const visibleIds=list.map(j=>j.id).join(',');
    if(state.renderedJobIds!==visibleIds)state.lastJobCheckIndex=-1;
    state.renderedJobIds=visibleIds;
    if(options.keepMedia&&updateJobsInPlace(list)){syncJobBulkControls(list);return;}
    unbindLazyJobVideos(table);table.innerHTML=list.length?list.map(j=>renderJobRow(j)).join(''):`<tr><td colspan="6"><div class="job-empty"><div class="empty-icon">▤</div>${q?'当前分类没有匹配的任务，请修改搜索条件。':all.length?`${section?.label||'当前分类'}暂无任务。`:'还没有分镜任务。点击右上角「＋ 新建任务」开始。'}</div></td></tr>`;
    highlightJobSearchMatches(table,jobSearchTerms(q));
    state.jobRowSignatures=new Map(list.map(j=>[j.id,jobRowSignature(j)]));bindLazyJobVideos(table);syncJobBulkControls(list);}
  // 静默刷新：只替换内容变化的行，正在播放的视频行原地保留。
  // 行序按 id 对齐：任务增删或排序变化时也能增量更新，不必整表重绘（避免闪烁与视频中断）。
  function updateJobsInPlace(list){
    const table=$('jobsTable');
    // 列表变空时交给整表渲染，才能正确显示空状态。
    if(!list.length)return false;
    const rows=[...table.querySelectorAll('tr[data-job-id]')];const byId=new Map(rows.map(row=>[row.dataset.jobId,row]));const previous=state.jobRowSignatures||new Map();const playing=[...table.querySelectorAll('.job-video-preview')].find(video=>!video.paused);const playingRow=playing?(playing.closest('tr')?.dataset.jobId||''):'';let changed=false;
    list.forEach((j)=>{const signature=jobRowSignature(j);const row=byId.get(j.id);
      if(!row){changed=true;const template=document.createElement('tbody');template.innerHTML=renderJobRow(j);table.appendChild(template.firstElementChild);return;}
      if(previous.get(j.id)!==signature&&j.id!==playingRow){changed=true;unbindLazyJobVideos(row);const template=document.createElement('tbody');template.innerHTML=renderJobRow(j);row.replaceWith(template.firstElementChild);}
    });
    // 列表里已不存在的行要移除，否则切分类或删除后仍会残留。
    rows.forEach(row=>{if(!list.some(j=>j.id===row.dataset.jobId)){unbindLazyJobVideos(row);row.remove();changed=true;}});
    if(changed){
      // 重新按最新顺序摆放，避免增删后行序错乱。
      const order=new Map(list.map((j,index)=>[j.id,index]));
      [...table.querySelectorAll('tr[data-job-id]')]
        .sort((a,b)=>(order.get(a.dataset.jobId)??0)-(order.get(b.dataset.jobId)??0))
        .forEach(row=>table.appendChild(row));
    }
    // 选中态与批量栏无论如何都要跟最新状态对齐。
    highlightJobSearchMatches(table,jobSearchTerms($('jobSearch')?.value));
    state.jobRowSignatures=new Map(list.map(j=>[j.id,jobRowSignature(j)]));bindLazyJobVideos(table);paintJobSelection();return true;}
  function openJobActions(jobId){const j=state.jobs.find(x=>x.id===jobId);if(!j)return;const a=state.accounts.find(x=>x.id===j.account_id),hasPage=Boolean(j.has_task_page||j.web_page_url);const actions=[];const add=(icon,label,desc,fn,danger=false)=>actions.push({icon,label,desc,fn,danger});if(j.is_web_generation){if(j.status==='queued')add('▶','发送到豆包','只准备素材与提示词，等待人工确认发送',`prepareManualJob('${esc(j.id)}')`);if(['awaiting_manual_send','needs_review','failed'].includes(j.status))add('⇩','关联下载','复用准备页面，下载并归档已生成的视频',`linkJobResult('${esc(j.id)}')`);if(['awaiting_manual_send','needs_review','failed'].includes(j.status))add('↻','重新发送','沿用原任务重新准备页面，旧页面与旧结果作废',`prepareManualJob('${esc(j.id)}')`);if(hasPage)add('↗','打开网页','打开并激活该任务绑定的网页标签页',`openJobPage('${esc(j.id)}')`);if(hasPage)add('▣','关闭网页','仅关闭该任务绑定的网页；同账号其他任务网页不受影响',`closeJobPage('${esc(j.id)}')`);if(['awaiting_manual_send','confirming','needs_review'].includes(j.status))add('✎','发送确认语','向当前豆包会话发送预设确认语（可能再次触发生成）',`sendJobConfirm('${esc(j.id)}')`);if(j.submit_mode==='manual'&&['queued','awaiting_manual_send','needs_review','failed'].includes(j.status))add('×','判定失败','H3/外部调度会收到失败并停止等待',`markJobFailed('${esc(j.id)}')`,true);if(['needs_review','failed'].includes(j.status))add('⇧','复查归档 / 导入视频','人工核对后上传本地 MP4 归档',`openUpload('${esc(j.id)}')`);if(['needs_review','failed'].includes(j.status)&&j.submit_mode!=='manual')add('⧉','重新生成（新任务）','复制原任务并换账号重新排队',`retryJob('${esc(j.id)}')`,true);add('≡','查看日志','查看该任务的状态事件',`showWebEvents('${esc(j.id)}')`);if(j.video_path)add('▷','查看视频','在新标签页打开已归档视频',`window.open('/api/jobs/${esc(j.id)}/video','_blank')`);if(!actions.length)add('i','暂无可用操作','该任务正在执行，请等待状态更新',`toast('请等待当前步骤结束')`);}else{if(j.status==='queued')add('⚡','转为网页任务','用所选账号在豆包网页生成',`webGenerateJob('${esc(j.id)}')`);add('✎','人工准备','打开浏览器并复制提示词',`prepareJob('${esc(j.id)}')`);add('⇧','导入视频','上传本地视频并归档到该分镜',`openUpload('${esc(j.id)}')`);if(j.video_path)add('▷','查看视频','在新标签页打开已归档视频',`window.open('/api/jobs/${esc(j.id)}/video','_blank')`);}add('×','删除任务','永久删除任务记录和归档视频',`deleteJob('${esc(j.id)}')`,true);const statusSelect=j.is_web_generation?'':`<div class="action-section">修改任务状态</div><div class="field" style="margin-bottom:0"><select onchange="updateStatus('${esc(j.id)}',this.value);closeModal('jobActionsModal')">${Object.entries(statusLabels).map(([k,v])=>`<option value="${k}" ${j.status===k?'selected':''}>${v[0]}</option>`).join('')}</select></div>`;const html=`<div class="action-section">当前任务</div><div class="mini-row" style="margin-bottom:12px"><div class="avatar">${esc(jobAccountOrder(j.account_id))}</div><div class="mini-info"><strong>${esc(j.task_name||[j.project_id,j.episode_id,j.shot_id].filter(Boolean).join(' / ')||j.id)}</strong><span>${esc(recoveryHint(j)||statusHint(j.status))}${a?` · ${esc(a.name)}`:''}</span></div>${statusPill(j.status)}</div><div class="action-section">可用操作</div><div class="action-list">${actions.map(x=>`<button class="action-item ${x.danger?'danger':''}" onclick="closeModal('jobActionsModal');${x.fn}"><span class="action-icon">${x.icon}</span><span class="action-copy"><strong>${x.label}</strong><span>${x.desc}</span></span></button>`).join('')}</div>${statusSelect}`;$('jobActionsBody').innerHTML=html;openModal('jobActionsModal');}
  async function createAccount(e){e.preventDefault();try{const created=await api('/api/accounts',{method:'POST',body:JSON.stringify({name:$('accountName').value.trim(),note:$('accountNote').value.trim(),is_member:$('accountMember').checked,priority:Number($('accountPriority').value)||0})});closeModal('accountModal');$('accountForm').reset();$('accountPriority').value='0';await refreshAll();switchView('accounts');toast(`账号档案“${created.name}”已创建。打开浏览器后请手动登录。`)}catch(err){toast(err.message,true)}}
  async function createAccountDirect(button){button.disabled=true;let created;try{created=await api('/api/accounts',{method:'POST',body:JSON.stringify({name:'',note:'',is_member:false,priority:0})});await api(`/api/accounts/${encodeURIComponent(created.id)}/open`,{method:'POST'});await refreshAll();switchView('accounts');toast(`账号档案“${created.name}”已创建并打开，请在浏览器中登录豆包。`)}catch(err){if(created)await refreshAll();toast(created?`账号档案“${created.name}”已创建，但网页打开失败：${err.message}`:err.message,true)}finally{button.disabled=false}}
  async function createJob(e){e.preventDefault();try{await api('/api/jobs',{method:'POST',body:JSON.stringify({project_id:$('projectId').value.trim(),episode_id:$('episodeId').value.trim(),shot_id:$('shotId').value.trim(),account_id:$('jobAccount').value||null,prompt:$('jobPrompt').value.trim()})});closeModal('jobModal');$('jobForm').reset();await refreshAll();toast('分镜任务已创建。')}catch(err){toast(err.message,true)}}
  function syncWebMode(){const m=$('webMode').value;$('webFirstBlock').style.display=(m==='image2video'||m==='first_last_frame')?'':'none';$('webLastBlock').style.display=m==='first_last_frame'?'':'none';$('webRefsBlock').style.display=m==='reference_images'?'':'none';}
  async function postAsset(f){const fd=new FormData();fd.append('file',f);const o=await api('/api/v1/videos/assets',{method:'POST',body:fd});return o.asset_id;}
  $('webForm').addEventListener('submit',async e=>{
    e.preventDefault(); const btn=e.submitter;btn.disabled=true;btn.textContent='上传素材中…';
    try{if(!state.acceptingTasks)throw Error('当前已停止接受新任务，请先在任务设置中开启“接受任务”');const mode=$('webMode').value,first=$('webFirst').files[0],last=$('webLast').files[0],refs=[...$('webRefs').files];
      if(mode==='image2video'&&!first)throw Error('请上传首帧图片');
      if(mode==='first_last_frame'&&(!first||!last))throw Error('请上传首帧和尾帧');
      if(mode==='reference_images'&&(!refs.length||refs.length>9))throw Error('请选择 1 到 9 张参考图');
      const payload={account_id:$('webAccount').value,mode,ratio:$('webRatio').value,duration:Number($('webDuration').value),
        project_id:$('webProject').value.trim(),episode_id:$('webShot').value.split('/')[0]||'',
        shot_id:$('webShot').value.split('/')[1]||'',prompt:$('webPrompt').value.trim()};
      if($('webKey').value.trim())payload.idempotency_key=$('webKey').value.trim();
      if(mode==='image2video'||mode==='first_last_frame')payload.first_frame_asset_id=await postAsset(first);
      if(mode==='first_last_frame')payload.last_frame_asset_id=await postAsset(last);
      if(mode==='reference_images'){payload.reference_asset_ids=[];for(const f of refs)payload.reference_asset_ids.push(await postAsset(f));}
      const manual=state.submitMode==='manual',autoDispatch=state.autoDispatchEnabled;
      if(!confirm(`将创建任务：${autoDispatch?'自动进入调度':'先进入「待处理」'}，${manual?'页面准备后等待人工发送':'由脚本自动执行完整流程'}。确认创建吗？`))return;
      btn.textContent='创建并执行…';const result=await api('/api/v1/videos/generations',{method:'POST',body:JSON.stringify(payload)});
      closeModal('webModal');await refreshAll();
      toast(autoDispatch?(manual?'任务已进入调度，页面准备完成后请人工发送。':'网页生成任务已创建：'+result.id):'任务已创建并进入「待处理」。');
    }catch(err){toast(err.message,true)}finally{btn.disabled=false;btn.textContent='创建任务并执行';}
  });
  $('accountForm').addEventListener('submit',createAccount);$('jobForm').addEventListener('submit',createJob);
  async function openAccount(id){try{await api(`/api/accounts/${id}/open`,{method:'POST'});await refreshAll();toast('浏览器已打开，请在新窗口中确认豆包登录状态。')}catch(e){toast(e.message,true)}}
  async function closeAccount(id){try{await api(`/api/accounts/${id}/close`,{method:'POST'});await refreshAll();toast('已关闭浏览器窗口。')}catch(e){toast(e.message,true)}}
  async function toggleAccountPaused(id,isPaused){try{await api(`/api/accounts/${encodeURIComponent(id)}/${isPaused?'resume':'pause'}`,{method:'POST'});await refreshAll();toast(isPaused?'账号已恢复使用。':'账号已暂停使用。')}catch(e){toast(e.message,true)}}
  async function safeCleanup(){
    if(!confirm('执行安全清理？\n\n将清理：\n· 已关闭账号的浏览器可再生缓存\n· data/trash 回收区\n· 验收报告临时文件\n\n不会删除账号登录态、任务数据库、网页素材或已归档视频。正在打开的浏览器会自动跳过。'))return;
    const btn=$('safeCleanupBtn');btn.disabled=true;btn.textContent='正在清理…';
    try{
      const r=await api('/api/cleanup/safe',{method:'POST'});
      const browser=r.areas?.browser_cache||{},trash=r.areas?.trash||{},reports=r.areas?.acceptance_reports||{};
      const parts=[];
      if(browser.cleaned_accounts)parts.push(`浏览器缓存 ${Number(browser.freed_bytes||0)/1048576<0.01?'<0.01':(Number(browser.freed_bytes||0)/1048576).toFixed(2)} MB / ${browser.cleaned_accounts} 个账号`);
      if(trash.freed_bytes)parts.push(`回收区 ${(Number(trash.freed_bytes||0)/1048576).toFixed(2)} MB`);
      if(reports.freed_bytes)parts.push(`验收临时文件 ${(Number(reports.freed_bytes||0)/1048576).toFixed(2)} MB`);
      if(!parts.length)parts.push('没有发现可清理内容');
      const skipped=(r.skipped?.length||0)+['trash','acceptance_reports'].reduce((n,k)=>n+(r.areas?.[k]?.skipped?.length||0),0);
      toast(`安全清理完成：${parts.join('；')}${skipped?`；跳过 ${skipped} 项`:''}。`);
    }catch(e){toast(e.message,true)}
    finally{btn.disabled=false;btn.textContent='安全清理'}
  }
  function openAccountSettings(id){const a=state.accounts.find(x=>x.id===id);if(!a)return;$('accountSettingsId').value=a.id;$('accountSettingsMember').checked=Boolean(a.is_member);$('accountSettingsPriority').value=Number(a.priority)||0;openModal('accountSettingsModal');}
  $('accountSettingsForm').addEventListener('submit',async e=>{e.preventDefault();const id=$('accountSettingsId').value;try{await api(`/api/accounts/${encodeURIComponent(id)}`,{method:'PATCH',body:JSON.stringify({is_member:$('accountSettingsMember').checked,priority:Number($('accountSettingsPriority').value)||0})});closeModal('accountSettingsModal');await refreshAll();toast('账号设置已保存。')}catch(err){toast(err.message,true)}});
  async function prefillAccountName(){const input=$('accountName');if(!input||input.value.trim())return;try{const suggested=await api('/api/accounts/suggested-name');if(!input.value.trim())input.value=suggested.name||'';}catch{/* 名称可留空，由服务端自动命名 */}}
  async function deleteAccount(id){const account=state.accounts.find(a=>a.id===id);const name=account?account.name:id;if(!confirm(`删除账号档案“${name}”？\n\n· 浏览器登录资料将移动到 data/trash/，可稍后手动彻底删除；\n· 分镜任务与已归档视频保留，只解除账号关联。\n\n请先关闭该账号的浏览器窗口。`))return;try{const r=await api(`/api/accounts/${id}`,{method:'DELETE'});await refreshAll();toast(r.moved_to?`账号已删除，登录资料已移动到 ${r.moved_to}。`:'账号已删除。')}catch(e){toast(e.message,true)}}
  async function assignAccount(jobId,accountId){try{await api(`/api/jobs/${jobId}`,{method:'PATCH',body:JSON.stringify({account_id:accountId||null})});await refreshAll();toast('账号关联已更新。')}catch(e){toast(e.message,true)}}
  async function updateStatus(jobId,status){try{await api(`/api/jobs/${jobId}`,{method:'PATCH',body:JSON.stringify({status})});await refreshAll();toast('任务状态已更新。')}catch(e){toast(e.message,true)}}
  async function webGenerateJob(jobId){
    const job=state.jobs.find(j=>j.id===jobId);
    if(!job?.account_id){toast('请先给分镜任务选择已登录的账号。',true);return;}
    if(!confirm('确认使用选定账号在豆包网页提交此分镜？该操作可能消耗账号的生成额度。'))return;
    try{await api(`/api/v1/videos/generations/from-job/${encodeURIComponent(jobId)}`,{method:'POST'});
      await refreshAll();toast('已进入网页生成队列，状态会自动更新。');}
    catch(e){toast(e.message,true)}
  }
  async function prepareJob(jobId){try{const data=await api(`/api/jobs/${jobId}/prepare`,{method:'POST'});let copied=false;try{await navigator.clipboard.writeText(data.prompt);copied=true}catch{const temp=document.createElement('textarea');temp.value=data.prompt;temp.style.position='fixed';temp.style.opacity='0';document.body.appendChild(temp);temp.select();try{copied=document.execCommand('copy')}catch{}temp.remove()}await refreshAll();toast(copied?'已打开关联浏览器并复制提示词。请在豆包页面人工粘贴、核对并提交。':'已打开关联浏览器。复制提示词时遇到权限限制，请手动复制任务提示词。');}catch(e){toast(e.message,true)}}
  function openUpload(jobId){state.uploadJobId=jobId;const job=state.jobs.find(j=>j.id===jobId);$('uploadTargetLabel').textContent=`目标任务：${job?[job.project_id,job.episode_id,job.shot_id].filter(Boolean).join(' / '):jobId}`;$('videoFile').value='';$('manualConfirmed').checked=false;$('manualConfirmWrap').style.display=job?.is_web_generation?'block':'none';openModal('uploadModal')}
  async function showWebEvents(jobId){try{const ev=await api(`/api/v1/videos/generations/${encodeURIComponent(jobId)}/events`);const lines=ev.slice(-12).map(e=>`${e.timestamp||''}  [${e.status}] ${e.message}`).join('\n');alert(lines||'暂无日志');}catch(e){toast(e.message,true)}}
  async function openJobPage(jobId){try{const r=await api(`/api/v1/videos/generations/${encodeURIComponent(jobId)}/open-page`,{method:'POST'});await refreshAll();toast(r.message||'已打开该任务绑定的网页。')}catch(e){await refreshAll();toast(e.message,true)}}
  async function closeJobPage(jobId){try{const r=await api(`/api/v1/videos/generations/${encodeURIComponent(jobId)}/close-page`,{method:'POST'});await refreshAll();toast(r.closed?'已关闭该任务绑定的网页。':'该任务网页已经关闭。')}catch(e){await refreshAll();toast(e.message,true)}}
  async function assignQueuedWebAccount(jobId,accountId){try{await api(`/api/jobs/${encodeURIComponent(jobId)}/account`,{method:'POST',body:JSON.stringify({account_id:accountId||null})});await refreshAll();toast(accountId?'已为待处理任务选定账号。':'已恢复为进入处理前自动分配账号。')}catch(e){await refreshAll();toast(e.message,true)}}
  async function dispatchJob(jobId){const job=state.jobs.find(item=>item.id===jobId);try{await api(`/api/v1/videos/generations/${encodeURIComponent(jobId)}/dispatch`,{method:'POST'});await refreshAll();toast(job?.submit_mode==='manual'?'已进入调度；页面准备完成后等待人工发送。':'已进入调度，任务会自动执行，状态会自动更新。');}catch(e){toast(e.message,true)}}
  async function prepareManualJob(jobId){try{await api(`/api/v1/videos/generations/${encodeURIComponent(jobId)}/prepare-manual`,{method:'POST'});await refreshAll();toast('正在准备素材与提示词；完成后可打开任务网页并人工确认发送。');}catch(e){toast(e.message,true)}}
  async function sendPreparedJob(jobId){try{await api(`/api/v1/videos/generations/${encodeURIComponent(jobId)}/send?confirm_send=true`,{method:'POST'});await refreshAll();toast('任务内容已发送。请继续执行“3 发送确认语”。');}catch(e){await refreshAll();toast(e.message,true)}}
  async function startJob(jobId){const job=state.jobs.find(j=>j.id===jobId);const manual=job?.submit_mode==='manual';try{await api(`/api/v1/videos/generations/${encodeURIComponent(jobId)}/start`,{method:'POST'});await refreshAll();toast(manual?'正在准备豆包网页；完成后请执行“2 确认发送”。':'任务已启动，状态会自动更新。');}catch(e){toast(e.message,true)}}
  async function copyJob(jobId){if(!confirm('将按服务器最初提交的参数复制为新任务，包括提示词、模式、素材、提交方式和服务器回推信息。系统会重新选择额度足够的空闲账号（优先避开原账号）。新任务只排队，不会自动执行；原任务保持不变。确认复制？'))return;try{const r=await api(`/api/v1/videos/generations/${encodeURIComponent(jobId)}/copy?confirm_copy=true`,{method:'POST'});const account=state.accounts.find(a=>a.id===r.account_id);await refreshAll();toast(`已复制为新任务 ${r.id}，已选择账号 ${account?(account.name||account.id):(r.account_id||'未指定')}，请手动点击执行。`);}catch(e){toast(e.message,true)}}
  async function autoRunJob(jobId){const job=state.jobs.find(item=>item.id===jobId);if(recoveryActive(job)){toast('Claude 正在处理该任务，请等待恢复结束。',true);return;}try{await api(`/api/v1/videos/generations/${encodeURIComponent(jobId)}/restart-auto`,{method:'POST'});await refreshAll();toast('已关闭原账号窗口并按原任务参数换号自动执行，正在匹配可用账号。');}catch(e){await refreshAll();toast(e.message,true)}}
  async function resendJob(jobId){const job=state.jobs.find(item=>item.id===jobId);try{await api(`/api/v1/videos/generations/${encodeURIComponent(jobId)}/resend?confirm_resend=true`,{method:'POST'});await refreshAll();toast(job?.submit_mode==='auto'?'自动任务已重新开始，将重新准备并自动提交。':'正在重新准备素材；完成后可用“打开网页”定位到新页面。');}catch(e){toast(e.message,true)}}
  async function markJobFailed(jobId){if(!confirm('将把该任务结束为失败：H3/外部调度会收到失败状态并停止等待。\n\n如果视频其实已经生成，请先点“检查结果并下载”。结束后仍可重新准备页面，但 H3 需要手动召回才会恢复。确认结束为失败？'))return;try{await api(`/api/v1/videos/generations/${encodeURIComponent(jobId)}/mark-failed?confirm_failed=true`,{method:'POST'});await refreshAll();toast('任务已结束为失败，H3 将显示失败。');}catch(e){toast(e.message,true)}}
  async function cancelJob(jobId){const job=state.jobs.find(item=>item.id===jobId),name=job?.task_name||jobId;if(!confirm(`取消任务“${name}”？\n\n系统会停止后台执行并释放账号、额度和处理名额；H3 提交的任务会同步取消。`))return;try{const result=await api(`/api/v1/videos/generations/${encodeURIComponent(jobId)}/cancel?confirm_cancel=true`,{method:'POST'});await refreshAll();const configured=Boolean(result.web?.result_push_url),synced=result.web?.delivery_state==='delivered';toast(synced?'任务已取消，H3 已同步取消。':configured?'本地任务已取消，但 H3 同步失败；请检查 SSH 3013 端口后重试。':'任务已取消，现在可以删除。',configured&&!synced)}catch(e){toast(e.message,true)}}
  async function cancelSelectedJobs(){const jobs=bulkActionJobs('cancel');if(!jobs.length){toast('选中的任务中没有可取消的任务',true);return}if(!confirm(`取消选中的 ${jobs.length} 个任务？\n\n系统会停止后台执行并释放账号、额度和处理名额；取消后可删除任务记录。`))return;await runSelectedJobAction('cancel')}
  async function linkJobResult(jobId){try{await api(`/api/v1/videos/generations/${encodeURIComponent(jobId)}/recall?force_redownload=true`,{method:'POST'});toast('已开始从任务页面下载视频；完成前请保持豆包窗口打开。');await refreshAll()}catch(e){toast(e.message,true)}}
  async function archiveJob(jobId){try{await api(`/api/jobs/${encodeURIComponent(jobId)}/archive`,{method:'POST'});await refreshAll();toast('任务已归档。')}catch(e){toast(e.message,true)}}
  async function unarchiveJob(jobId){try{await api(`/api/jobs/${encodeURIComponent(jobId)}/archive`,{method:'DELETE'});await refreshAll();toast('任务已取消归档，回到处理完。')}catch(e){toast(e.message,true)}}
  async function archiveSelectedJobs(){const jobs=bulkActionJobs('archive');if(!jobs.length){toast('选中的任务中没有可归档的处理完任务',true);return}if(!confirm(`把选中的 ${jobs.length} 个任务移入“已归档”？\n\n归档是你手动确认的动作，归档后任务会从“处理完”移到“已归档”队列，随时可以取消归档。`))return;await runSelectedJobAction('archive')}
  async function pushJobResult(jobId,button){const oldText=button?.textContent||'推送服务器';if(button){button.disabled=true;button.textContent='正在推送…'}toast('正在推送视频到服务器，请稍候…');try{await api(`/api/v1/videos/generations/${encodeURIComponent(jobId)}/push-result`,{method:'POST'});await refreshAll();toast('视频已推送服务器，H3 分镜任务已完成。')}catch(e){await refreshAll();toast(e.message,true)}finally{if(button&&button.isConnected){button.disabled=false;button.textContent=oldText}}}
  async function recoverJobVideo(jobId){if(!confirm('将使用原任务会话和绑定的视频卡片召回结果。系统不会重新提交生成。'))return;try{await api(`/api/v1/videos/generations/${encodeURIComponent(jobId)}/recall`,{method:'POST'});toast('已开始召回；生成完成后会自动下载到归档区。');await refreshAll()}catch(e){toast(e.message,true)}}
  async function retryJob(jobId){if(!confirm('将复制原任务的提示词、模式和素材，创建一个新的任务 ID 并加入生成队列。原任务仍保留在异常列表；系统会优先选择其他空闲且额度足够的账号。确认复制为新任务？'))return;try{const r=await api(`/api/v1/videos/generations/${encodeURIComponent(jobId)}/retry?confirm_retry=true`,{method:'POST'});toast(`已复制为新任务 ${r.id}，选择账号：${r.account_id}`);await refreshAll()}catch(e){toast(e.message,true)}}
  async function loadConfirmationPresets(){state.confirmationPresets=await api('/api/v1/videos/confirmation-presets');return state.confirmationPresets;}
  function renderConfirmationPresets(){const target=$('confirmationPresetsList');$('confirmationPresetCount').textContent=`${state.confirmationPresets.length} 个预设`;target.innerHTML=state.confirmationPresets.length?state.confirmationPresets.map(p=>`<div class="preset-manage-item ${state.editingPresetId===p.id?'active':''}"><button type="button" class="preset-manage-main" onclick="editConfirmationPreset('${esc(p.id)}')"><strong>${esc(p.name)}</strong><span title="${esc(p.text)}">${esc(p.text)}</span></button><div class="preset-manage-actions">${p.id==='preset_default'?'<span class="pill green">默认</span>':''}<button type="button" class="btn small" onclick="editConfirmationPreset('${esc(p.id)}')">编辑</button>${p.id==='preset_default'?'':`<button type="button" class="btn small ghost danger" onclick="deleteConfirmationPreset('${esc(p.id)}')">删除</button>`}</div></div>`).join(''):'<div class="empty">暂无预设，请在右侧创建。</div>';}
  function resetConfirmationPresetForm(){state.editingPresetId=null;$('confirmationPresetId').value='';$('confirmationPresetName').value='';$('confirmationPresetText').value='';$('confirmationPresetFormTitle').textContent='新增预设';$('confirmationPresetFormHint').textContent='保存后即可在任务中选择发送';$('confirmationPresetSave').textContent='添加预设';$('confirmationPresetCancelEdit').textContent='清空';renderConfirmationPresets();$('confirmationPresetName').focus();}
  function editConfirmationPreset(id){const p=state.confirmationPresets.find(x=>x.id===id);if(!p)return;state.editingPresetId=p.id;$('confirmationPresetId').value=p.id;$('confirmationPresetName').value=p.name;$('confirmationPresetText').value=p.text;$('confirmationPresetFormTitle').textContent=`编辑“${p.name}”`;$('confirmationPresetFormHint').textContent='修改后会立即用于后续发送';$('confirmationPresetSave').textContent='保存修改';$('confirmationPresetCancelEdit').textContent='取消编辑';renderConfirmationPresets();$('confirmationPresetName').focus();}
  async function openConfirmationPresets(returnToSend=false){try{state.confirmPresetReturn=returnToSend;if(returnToSend)closeModal('confirmationSendModal');await loadConfirmationPresets();resetConfirmationPresetForm();$('confirmationPresetDone').textContent=returnToSend?'完成并返回发送':'完成';openModal('confirmationPresetsModal')}catch(e){toast(e.message,true)}}
  function closeConfirmationPresets(){const shouldReturn=state.confirmPresetReturn,jobIds=[...state.confirmJobIds];state.confirmPresetReturn=false;closeModal('confirmationPresetsModal');if(shouldReturn&&jobIds.length)openJobConfirmation(jobIds);}
  async function deleteConfirmationPreset(id){const p=state.confirmationPresets.find(x=>x.id===id);if(!confirm(`删除确认语预设“${p?.name||id}”？`))return;try{await api(`/api/v1/videos/confirmation-presets/${encodeURIComponent(id)}`,{method:'DELETE'});if(localStorage.getItem('doubao-confirmation-preset')===id)localStorage.removeItem('doubao-confirmation-preset');await loadConfirmationPresets();resetConfirmationPresetForm();toast('确认语预设已删除。')}catch(e){toast(e.message,true)}}
  function syncConfirmationPreview(){const p=state.confirmationPresets.find(x=>x.id===$('confirmationPresetSelect').value);$('confirmationPreview').value=p?.text||'';$('confirmationPreviewMeta').textContent=p?`${p.name} · ${p.text.length} 字`:'请选择预设';}
  function renderConfirmationChoices(){const target=$('confirmationPresetChoices');let selected=$('confirmationPresetSelect').value;if(!state.confirmationPresets.some(p=>p.id===selected)){const remembered=localStorage.getItem('doubao-confirmation-preset');selected=state.confirmationPresets.find(p=>p.id===remembered)?.id||state.confirmationPresets.find(p=>p.id==='preset_default')?.id||state.confirmationPresets[0]?.id||'';$('confirmationPresetSelect').value=selected;}target.innerHTML=state.confirmationPresets.map(p=>`<button type="button" class="preset-choice ${p.id===selected?'active':''}" onclick="selectConfirmationPreset('${esc(p.id)}')"><strong>${esc(p.name)}<i class="preset-check">${p.id===selected?'✓':''}</i></strong><span>${esc(p.text)}</span></button>`).join('');syncConfirmationPreview();}
  function selectConfirmationPreset(id){$('confirmationPresetSelect').value=id;localStorage.setItem('doubao-confirmation-preset',id);renderConfirmationChoices();}
  async function openJobConfirmation(jobIds){try{const ids=[...new Set(jobIds)].filter(id=>state.jobs.some(job=>job.id===id));if(!ids.length)throw Error('请先选择任务');state.confirmJobIds=ids;state.confirmJobId=ids[0];await loadConfirmationPresets();if(!state.confirmationPresets.length)throw Error('请先添加确认语预设');const jobs=ids.map(id=>state.jobs.find(job=>job.id===id)).filter(Boolean);if(jobs.length===1){const job=jobs[0],title=job.task_name||[job.project_id,job.episode_id,job.shot_id].filter(Boolean).join(' / ')||job.id;$('confirmationSendTarget').innerHTML=`<div class="send-target-icon">${esc(jobAccountOrder(job.account_id))}</div><div><strong>${esc(title)}</strong><span>${esc(statusHint(job.status))}</span></div>`;}else{const names=jobs.slice(0,3).map(job=>job.task_name||job.shot_id||job.id);$('confirmationSendTarget').innerHTML=`<div class="send-target-icon">${jobs.length}</div><div><strong>向 ${jobs.length} 个所选任务发送确认语</strong><span>${esc(names.join('、'))}${jobs.length>3?' 等':''}</span></div>`;}$('confirmationPresetSelect').value='';renderConfirmationChoices();openModal('confirmationSendModal')}catch(e){toast(e.message,true)}}
  async function sendJobConfirm(jobId){await openJobConfirmation([jobId]);}
  async function sendSelectedJobConfirm(){const jobs=bulkActionJobs('confirm');if(!jobs.length){toast('选中的任务中没有可发送确认语的网页任务',true);return}await openJobConfirmation(jobs.map(job=>job.id));}
  $('confirmationPresetForm').addEventListener('submit',async e=>{e.preventDefault();const id=$('confirmationPresetId').value;const body=JSON.stringify({name:$('confirmationPresetName').value.trim(),text:$('confirmationPresetText').value.trim()});const submit=e.submitter;let saved=false;submit.disabled=true;submit.textContent='保存中…';try{await api(id?`/api/v1/videos/confirmation-presets/${encodeURIComponent(id)}`:'/api/v1/videos/confirmation-presets',{method:id?'PUT':'POST',body});await loadConfirmationPresets();resetConfirmationPresetForm();saved=true;toast(id?'确认语预设已更新。':'确认语预设已添加。')}catch(err){toast(err.message,true)}finally{submit.disabled=false;if(!saved)submit.textContent=id?'保存修改':'添加预设'}});
  $('confirmationSendForm').addEventListener('submit',async e=>{e.preventDefault();const preset=state.confirmationPresets.find(x=>x.id===$('confirmationPresetSelect').value),ids=[...state.confirmJobIds];if(!preset||!ids.length)return;const submit=e.submitter;submit.disabled=true;state.bulkJobAction='confirmation';syncJobBulkControls();let succeeded=0;const failed=[];try{for(let i=0;i<ids.length;i++){submit.textContent=ids.length>1?`发送中 ${i+1}/${ids.length}…`:'发送中…';try{await api(`/api/v1/videos/generations/${encodeURIComponent(ids[i])}/send-confirmation?confirm_send=true`,{method:'POST',body:JSON.stringify({preset_id:preset.id})});succeeded++;}catch(err){failed.push({id:ids[i],message:err.message});}}if(ids.length===1&&failed.length)throw Error(failed[0].message);closeModal('confirmationSendModal');state.confirmJobIds=[];const first=failed[0],job=first&&state.jobs.find(x=>x.id===first.id),detail=first?`；首个失败：${job?.task_name||job?.shot_id||first.id}（${first.message}）`:'';toast(ids.length===1?`确认语“${preset.name}”已发送。`:`批量发送确认语完成：成功 ${succeeded} 个，失败 ${failed.length} 个${detail}`,succeeded===0&&failed.length>0);await refreshAll()}catch(err){toast(err.message,true)}finally{state.bulkJobAction=null;syncJobBulkControls();submit.disabled=false;submit.textContent='发送确认语'}});
  $('uploadForm').addEventListener('submit',async e=>{e.preventDefault();const file=$('videoFile').files[0];if(!file||!state.uploadJobId)return;const form=new FormData();form.append('file',file);const submit=e.submitter;submit.disabled=true;submit.textContent='正在上传…';try{const job=state.jobs.find(j=>j.id===state.uploadJobId);let url=`/api/jobs/${state.uploadJobId}/upload`;if(job?.is_web_generation){if(!$('manualConfirmed').checked)throw Error('请先在豆包网页核对对应视频，并勾选人工核对声明');form.append('confirm_review','true');url=`/api/v1/videos/generations/${state.uploadJobId}/manual-result`;}await api(url,{method:'POST',body:form});closeModal('uploadModal');await refreshAll();toast('视频已核对并归档到对应分镜。')}catch(err){toast(err.message,true)}finally{submit.disabled=false;submit.textContent='上传并归档'}});
  async function copyApiExample(){try{await navigator.clipboard.writeText($('apiExample').textContent);toast('接口示例已复制。')}catch{toast('复制失败，请手动选择代码复制。',true)}}
  document.addEventListener('keydown',e=>{if(e.key==='Escape'&&state.selectedJobIds.size&&!document.querySelector('.modal-backdrop.show'))clearJobSelection()});
  function openJobTools(jobId){openJobActions(jobId);const names={'发送到豆包':'提交素材','开始执行':'开始自动生成','关联下载':'检查结果并下载','重新发送':'提交素材','打开网页':'打开网页','关闭网页':'关闭网页','判定失败':'结束为失败','复查归档 / 导入视频':'导入本地视频','重新生成（新任务）':'复制为新任务','查看日志':'处理记录','查看视频':'播放视频','转为网页任务':'转为网页生成','人工准备':'打开并复制提示词','导入视频':'导入本地视频'};document.querySelectorAll('#jobActionsBody .action-item strong').forEach(el=>{el.textContent=names[el.textContent]||el.textContent;});document.querySelectorAll('#jobActionsBody .action-copy>span').forEach(el=>{el.textContent=el.textContent.replaceAll('重新发送','重新准备页面').replaceAll('关联下载','检查结果并下载').replaceAll('判定失败','结束为失败');});}
  refreshAll();
  // 自动刷新：用户正在操作、页面不可见、或上一个请求还没回来时跳过，避免打断操作和堆积请求。
  const AUTO_REFRESH_MS=12000;
  setInterval(()=>{if(autoRefreshPaused())return;refreshAll({silent:true})},AUTO_REFRESH_MS);
  // 切回标签页时立即补一次，不用等下一个周期。
  document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible')refreshAll({silent:true})});
  // 窗口重新获得焦点（比如从豆包窗口切回来）也补一次。
  window.addEventListener('focus',()=>{if(!autoRefreshPaused())refreshAll({silent:true})});
