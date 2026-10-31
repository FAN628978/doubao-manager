<template>
  <div class="studio" v-if="drama">
    <header class="studio-topbar">
      <div class="studio-topbar-main">
        <button
          type="button"
          class="back-btn topbar-back"
          :title="t('episode.topbar.back')"
          :aria-label="t('episode.topbar.back')"
          @click="backToProject"
        >
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round">
            <line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/>
          </svg>
          {{ t('episode.topbar.back') }}
        </button>
        <div class="studio-identity">
          <h1 class="studio-title">{{ drama.title }}</h1>
          <!-- 自定义剧集名优先：此时集号退化为 EP nn 徽标（与列表卡片 EP 块同形），名字与列表一致；
               未改名时沿用「第 N 集」文案，不出现两条等价信息 -->
          <span class="studio-episode-chip">{{ episodeChipLabel }}</span>
          <span v-if="episodeCustomTitle" class="studio-episode-title" :title="episodeCustomTitle">{{ episodeCustomTitle }}</span>
          <div class="studio-meta-row">
            <span class="studio-meta-pill">{{ currentSubStageLabel }}</span>
            <span class="studio-meta-pill is-progress">{{ pipelineProgress }}/{{ pipelineTotal }}</span>
            <span class="studio-meta-inline">{{ t('episode.topbar.meta', { roles: chars.length, shots: sbs.length }) }}</span>
          </div>
        </div>

        <!-- 体验账号专属：紧跟标题排一行的反馈与合作联系方式 -->
        <TrialContactBadge placement="topbar" />
      </div>

      <div class="studio-topbar-side">
        <div class="studio-model-picks">
          <span class="studio-meta-pill" :title="claudeRuntimeTitle">Agent · Claude CLI</span>
          <ModelSelect
            v-if="imageModelOptions.length"
            v-model="imageModel"
            :label="t('common.serviceType.image')"
            :options="imageModelOptions"
            :default-label="t('episode.model.defaultWith', { model: imageModelOptions[0].model })"
            :show-config="imageModelMultiCfg"
          />
          <ModelSelect
            v-if="videoModelOptions.length"
            v-model="videoModel"
            :label="t('common.serviceType.video')"
            :options="videoModelOptions"
            :default-label="t('episode.model.defaultWith', { model: videoModelOptions[0].model })"
            :show-config="videoModelMultiCfg"
          />
          <ModelSelect
            v-if="!isDoubaoLocalVideo"
            v-model="episodeResolution"
            :label="t('episode.topbar.resolution')"
            :options="resolutionOptions"
            hide-default
          />
        </div>
        <div class="studio-actions">
          <button class="btn btn-icon tour-help-btn" :title="t('tour.helpTitle')" @click="startTour('episode', EPISODE_TOUR, t)">
            <CircleHelp :size="14" :stroke-width="1.8" />
          </button>
          <button class="btn" :disabled="dataRefreshing" @click="manualRefresh">
            <svg :class="{ 'animate-spin': dataRefreshing }" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>
            {{ dataRefreshing ? t('episode.sidebar.refreshing') : t('common.refresh') }}
          </button>
          <button class="btn task-drawer-trigger" @click="openTaskDrawer">
            <ListTodo :size="12" />
            {{ t('episode.topbar.tasks') }}
            <span v-if="genTaskActiveCount" class="task-drawer-badge">{{ genTaskActiveCount }}</span>
          </button>
        </div>
      </div>
    </header>

    <div class="studio-body">
    <!-- ========== LEFT SIDEBAR ========== -->
    <aside class="sidebar" :class="{ collapsed: sidebarCollapsed }">
      <nav class="pipeline">
        <div
          v-for="section in sidebarSections"
          :key="section.id"
          :class="['pipe-section', 'is-' + sectionState(section.id)]"
        >
          <div class="pipe-section-label">
            <span v-if="sectionState(section.id) !== 'none'" class="pipe-section-state">
              <svg v-if="sectionState(section.id) === 'done'" width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><polyline points="20 6 9 17 4 12"/></svg>
              <span v-else-if="sectionState(section.id) === 'active'" class="pipe-section-pulse" />
              <span v-else class="pipe-section-dot" />
            </span>
            <span>{{ section.label }}</span>
            <span v-if="sectionState(section.id) === 'active'" class="pipe-section-tag">{{ t('episode.sidebar.inProgress') }}</span>
          </div>
          <button
            v-for="item in section.items"
            :key="item.key"
            :class="['pipe-item pipe-item-sub', {
              active: activeSubStepKey === item.key,
              done: sectionState(section.id) === 'done',
              doing: sectionState(section.id) === 'active',
            }]"
            :title="sidebarCollapsed ? item.label : undefined"
            @click="goSubStep(item.key)"
          >
            <span class="pipe-icon" :class="sectionState(section.id) === 'done' ? 'icon-done' : activeSubStepKey === item.key ? 'icon-active' : ''">
              <!-- 收起态：始终显示步骤图标，进行中用右上角小脉冲点表达 -->
              <template v-if="sidebarCollapsed">
                <component :is="item.icon" :size="12" />
                <span v-if="sectionState(section.id) === 'active'" class="pipe-mini-pulse" />
              </template>
              <svg v-else-if="sectionState(section.id) === 'done'" width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><polyline points="20 6 9 17 4 12"/></svg>
              <span v-else-if="sectionState(section.id) === 'active'" class="pipe-item-pulse" />
              <component v-else :is="item.icon" :size="11" />
            </span>
            <span class="pipe-copy">
              <span class="pipe-label">{{ item.label }}</span>
              <span v-if="item.desc" class="pipe-sub">{{ item.desc }}</span>
            </span>
          </button>
        </div>
      </nav>

      <!-- Bottom: 收起/展开 + Stage marquee + Refresh -->
      <div class="sidebar-bottom">
        <button
          type="button"
          class="sidebar-toggle"
          :title="t(sidebarCollapsed ? 'episode.sidebar.expand' : 'episode.sidebar.collapse')"
          @click="toggleSidebar"
        >
          <svg class="sidebar-toggle-icon" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 18 9 12 15 6"/></svg>
          <span v-if="!sidebarCollapsed">{{ t('episode.sidebar.collapse') }}</span>
        </button>
        <!-- 步骤跑马灯：五段主流程进度，当前段流动光效，点击段可跳转 -->
        <div class="sidebar-progress">
          <div class="sidebar-progress-head">
            <span class="sidebar-progress-title">{{ currentStageLabel }}</span>
            <span class="sidebar-progress-count">{{ currentMainIdx + 1 }}/{{ mainProgressSteps.length }}</span>
          </div>
          <div class="sidebar-progress-track">
            <button
              v-for="(s, i) in mainProgressSteps"
              :key="s.id"
              type="button"
              :class="['sidebar-progress-seg', { done: i < currentMainIdx || mainStageDone(s.id), current: i === currentMainIdx }]"
              :title="s.label"
              @click="goMainStage(s.id)"
            ><span class="sidebar-progress-seg-fill" /></button>
          </div>
          <div class="sidebar-progress-labels">
            <span
              v-for="(s, i) in mainProgressSteps"
              :key="s.id"
              :class="{ on: i === currentMainIdx, done: i < currentMainIdx || mainStageDone(s.id) }"
            >{{ s.label }}</span>
          </div>
        </div>
        <button class="refresh-btn" :disabled="dataRefreshing" @click="manualRefresh">
          <svg :class="{ 'animate-spin': dataRefreshing }" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>
          {{ dataRefreshing ? t('episode.sidebar.refreshing') : t('episode.sidebar.refreshData') }}
        </button>
      </div>
    </aside>

    <!-- ========== MAIN CONTENT ========== -->
    <main class="main">
      <!-- ===== SCRIPT PANEL ===== -->
      <div v-if="panel === 'script'" class="content-panel">
        <!-- Step 0: Raw Content -->
        <div v-if="scriptStep === 0" class="step-editor">
          <div class="step-toolbar">
            <div class="toolbar-left">
              <div class="step-indicator">
                <span class="step-num">01</span>
                <span class="step-name">{{ t('episode.script.raw') }}</span>
              </div>
            </div>
            <div class="toolbar-right">
              <span v-if="rawLen" class="char-count">{{ t('episode.script.charCount', { n: rawLen }) }}</span>
              <button class="btn btn-sm" @click="saveRawWithToast">
                <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>
                {{ t('common.save') }}
              </button>
            </div>
          </div>
          <NovelChapterSourcePicker
            :drama-id="dramaId"
            :episode-number="episodeNumber"
            :current-content="localRaw"
            :current-start="episode?.source_start_chapter || episode?.sourceStartChapter"
            :current-end="episode?.source_end_chapter || episode?.sourceEndChapter"
            @imported="onNovelSourceImported"
          />
          <textarea
            class="fill-textarea"
            v-model="localRaw"
            :placeholder="t('episode.script.rawPlaceholder')"
          />
        </div>

        <!-- Step 1: AI rewrite and script confirmation -->
        <div v-else-if="scriptStep === 1" class="step-editor">
          <div class="step-toolbar">
            <div class="toolbar-left">
              <div class="step-indicator">
                <span class="step-num">02</span>
                <span class="step-name">{{ t('episode.script.rewrite') }}</span>
              </div>
            </div>
            <div class="toolbar-right">
              <button class="btn btn-sm ctx-toggle" :title="t('episode.script.ctxToggle')" @click="scriptCtxOpen = !scriptCtxOpen"><SlidersHorizontal :size="11" />{{ t('episode.script.ctxToggle') }}</button>
              <span v-if="scriptLen" class="char-count">{{ t('episode.script.charCount', { n: scriptLen }) }}</span>
              <span v-if="scriptDirty" class="script-dirty-dot" :title="t('episode.script.unsaved')"><i />{{ t('episode.script.unsaved') }}</span>
              <button
                v-if="scriptContent"
                :class="['btn btn-sm', scriptDirty && 'btn-primary']"
                :disabled="!scriptDirty || scriptSaving"
                @click="saveScrManual"
              >
                <Loader2 v-if="scriptSaving" :size="11" class="animate-spin" />
                <svg v-else width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>
                {{ t('common.save') }}
              </button>
              <button
                v-if="localScript.trim()"
                class="btn btn-sm"
                :title="t('episode.script.exportTxtHint')"
                @click="exportScriptTxt"
              >
                <Download :size="11" />
                {{ t('episode.script.exportTxt') }}
              </button>
              <button v-if="!scriptContent && rawContent" class="btn btn-sm btn-primary" @click="skipRewrite">
                <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M5 12h14"/><path d="M13 18l6-6-6-6"/></svg>
                {{ t('episode.script.skipRewrite') }}
              </button>
              <button v-if="scriptContent" class="btn btn-sm" @click="doRewrite" :disabled="rn">
                <Loader2 v-if="rn && rt === 'script_rewriter'" :size="11" class="animate-spin" />
                <svg v-else width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4"/><polyline points="10 17 15 12 10 7"/><line x1="15" y1="12" x2="3" y2="12"/></svg>
                {{ t('episode.script.rewriteAgain') }}
              </button>
            </div>
          </div>

          <div class="step-script-work">
            <div class="step-script-main">
              <div v-if="!scriptContent && !rn" class="step-empty">
                <div class="empty-visual">
                  <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round">
                    <path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4"/><polyline points="10 17 15 12 10 7"/><line x1="15" y1="12" x2="3" y2="12"/>
                  </svg>
                </div>
                <div class="empty-title">{{ t('episode.script.emptyTitle') }}</div>
                <div class="empty-desc">{{ t('episode.script.emptyDesc') }}</div>
                <div class="step-empty-actions">
                  <button class="btn btn-primary" @click="doRewrite">
                    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>
                    {{ t('episode.script.startRewrite') }}
                  </button>
                  <button class="btn" @click="skipRewrite">
                    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M5 12h14"/><path d="M13 18l6-6-6-6"/></svg>
                    {{ t('episode.script.skipRewrite') }}
                  </button>
                </div>
              </div>
              <div v-else-if="rn && rt === 'script_rewriter'" class="step-loading">
                <Loader2 :size="24" class="animate-spin" style="color:var(--accent)" />
                <div class="loading-text">{{ t('episode.script.rewriting') }}</div>
              </div>
              <template v-else>
                <div class="step-script-body">
                  <!-- Agent 修改待确认：剧本文本内直接高亮增删；否则显示带行号的编辑框 -->
                  <PromptDiffView
                    v-if="scriptAgentCandidate"
                    :segments="scriptAgentCandidate.segments"
                    class="fill-textarea"
                  />
                  <LineNumberTextarea
                    v-else
                    v-model="localScript"
                    :placeholder="t('episode.script.scriptPlaceholder')"
                    style="--ln-font-size: .84375rem; --ln-line-height: 1.9; --ln-pad-y: 26px; --ln-pad-x: 28px"
                  />
                </div>
                <PromptAgentEditor
                  class="step-script-agent"
                  :drama-id="dramaId"
                  :episode-id="epId"
                  target-type="script"
                  :target-id="epId"
                  :current-prompt="scriptContent || ''"
                  :model="chatModelOverride()"
                  :config-id="chatConfigId()"
                  @candidate="scriptAgentCandidate = $event"
                  @cleared="scriptAgentCandidate = null"
                  @applied="onScriptAgentApplied"
                />
              </template>
            </div>
            <!-- 改写右侧栏：本集要求 + 文档状态，参考逐集改编工作台的上下文检查器 -->
            <aside class="script-ctx" :class="{ open: scriptCtxOpen }">
              <div class="script-ctx-head">
                <div><small>{{ t('episode.script.ctxEpisode', { n: episodeNumber }) }}</small><strong>{{ t('episode.script.ctxTitle') }}</strong></div>
                <button type="button" class="btn btn-sm script-ctx-close" :title="t('episode.script.ctxClose')" @click="scriptCtxOpen = false"><X :size="12" /></button>
              </div>
              <div class="script-ctx-scroll">
                <section class="script-ctx-section">
                  <header><span>{{ t('episode.script.ctxRequire') }}</span></header>
                  <label><span>{{ t('episode.script.ctxAgentRequirements') }} <small>{{ t('episode.script.ctxAgentRequirementsOptional') }}</small></span><textarea v-model="rewriteForm.requirements" rows="5" maxlength="3000" :placeholder="t('episode.script.ctxAgentRequirementsPlaceholder')" /></label>
                </section>
                <section class="script-ctx-section">
                  <header><span>{{ t('episode.script.ctxDoc') }}</span></header>
                  <div class="script-ctx-row"><span>{{ t('episode.script.raw') }}</span><strong class="mono">{{ rawLen ? t('episode.script.charCount', { n: rawLen }) : '—' }}</strong></div>
                  <div class="script-ctx-row"><span>{{ t('episode.script.ctxScript') }}</span><strong class="mono">{{ scriptLen ? t('episode.script.charCount', { n: scriptLen }) : '—' }}<em v-if="scriptDirty" class="ctx-dirty">{{ t('episode.script.unsaved') }}</em></strong></div>
                  <div class="script-ctx-row"><span>{{ t('episode.script.ctxChapters') }}</span><strong class="mono">{{ sourceChapterRange || '—' }}</strong></div>
                </section>
              </div>
              <div class="script-ctx-actions">
                <button v-if="rn && rt === 'script_rewriter'" class="btn btn-sm script-ctx-btn" disabled><Loader2 :size="12" class="animate-spin" />{{ t('episode.script.rewriting') }}</button>
                <button v-else-if="!scriptContent" class="btn btn-sm btn-primary script-ctx-btn" :disabled="!rawContent" @click="doRewrite"><Sparkles :size="12" />{{ t('episode.script.startRewrite') }}</button>
                <button v-else class="btn btn-sm script-ctx-btn" :disabled="rn" @click="doRewrite"><RotateCcw :size="12" />{{ t('episode.script.rewriteAgain') }}</button>
              </div>
            </aside>
          </div>
        </div>
      </div>

      <!-- ===== PRODUCTION PANEL ===== -->
      <div v-else-if="panel === 'production'" class="content-panel">
        <!-- Guard: current production step prerequisites -->
        <div v-if="productionBlockMessage" class="step-empty" style="flex:1">
          <div class="empty-visual">
            <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"><polygon points="23 7 16 12 23 17 23 7"/><rect x="1" y="5" width="15" height="14" rx="2" ry="2"/></svg>
          </div>
          <div class="empty-title">{{ t('episode.prod.notReady') }}</div>
          <div class="empty-desc">{{ productionBlockMessage }}</div>
          <button class="btn btn-primary" @click="goProductionBlockTarget">{{ productionBlockActionLabel }}</button>
        </div>

        <template v-else>
          <!-- 制作子步骤导航（资产/分镜拆分/视频生成）由左侧栏承担，顶部不再重复展示 -->
          <!-- Sub: Assets -->
          <div v-if="prodTab === 'assets'" class="prod-content">
            <div class="prod-section-bar">
              <span class="dim" style="font-size:.75rem">{{ t('episode.prod.assets') }}</span>
              <span class="tag mono">{{ t('episode.prod.readyCount', { ready: assetReadyCount, total: assetTotalCount }) }}</span>
              <div class="ml-auto flex gap-1 asset-bar-actions">
                <button
                  v-for="et in EXTRACT_TARGETS"
                  :key="et.key"
                  class="btn btn-sm asset-btn-extract"
                  :disabled="isExtracting(et.key)"
                  @click="doExtract(et.key)"
                >
                  <Loader2 v-if="isExtracting(et.key)" :size="11" class="animate-spin" />
                  <svg v-else width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
                  {{ (et.key === 'characters' ? chars.length : et.key === 'scenes' ? scenes.length : propItems.length) ? t('episode.prod.reextract', { type: et.label }) : t('episode.prod.extract', { type: et.label }) }}
                </button>
                <span class="asset-bar-divider" />
                <button class="btn btn-sm asset-btn-batch" @click="batchCharImages">
                  <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg>
                  {{ t('episode.prod.batchChar') }}
                </button>
                <button class="btn btn-sm asset-btn-batch" @click="batchSceneImages">
                  <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg>
                  {{ t('episode.prod.batchScene') }}
                </button>
                <button class="btn btn-sm asset-btn-batch" @click="batchPropImages">
                  <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg>
                  {{ t('episode.prod.batchProp') }}
                </button>
                <span class="asset-bar-divider" />
                <button class="btn btn-sm asset-btn-import" :title="t('episode.import.desc')" @click="openAssetImport">
                  <FolderDown :size="11" />
                  {{ t('episode.import.btn') }}
                </button>
                <button
                  class="btn btn-sm asset-btn-import"
                  :disabled="exportingAssets || (!chars.length && !scenes.length && !propItems.length)"
                  :title="t('episode.prod.exportJsonDesc')"
                  @click="openAssetExport"
                >
                  <Loader2 v-if="exportingAssets" :size="11" class="animate-spin" />
                  <Download v-else :size="11" />
                  {{ t('episode.prod.exportJson') }}
                </button>
              </div>
            </div>
            <div v-if="extractingTargets.length && !chars.length && !scenes.length && !propItems.length" class="step-loading">
              <Loader2 :size="24" class="animate-spin" style="color:var(--accent)" />
              <div class="loading-text">{{ t('episode.prod.extractingTypes', { types: extractingLabels }) }}</div>
            </div>
            <div v-else-if="!chars.length && !scenes.length && !propItems.length" class="step-empty asset-empty-state">
              <div class="empty-visual">
                <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
              </div>
              <div class="empty-title">{{ t('episode.prod.emptyTitle') }}</div>
              <div class="empty-desc">{{ t('episode.prod.emptyDesc') }}</div>
              <button class="btn btn-primary" :disabled="!!extractingTargets.length" @click="doExtractAll">
                <Loader2 v-if="extractingTargets.length" :size="13" class="animate-spin" />
                <svg v-else width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>
                {{ extractingTargets.length ? t('episode.prod.extractingTypesDots', { types: extractingLabels }) : t('episode.prod.startExtract') }}
              </button>
            </div>
            <template v-else>
            <div class="style-prefix-row">
              <span class="style-prefix-label">{{ t('episode.asset.stylePrefixLabel') }}</span>
              <input
                class="input style-prefix-input"
                :value="effectiveStylePrefix('character')"
                :placeholder="presetStylePrefix('character') || t('episode.asset.stylePrefixEmptyPlaceholder')"
                :title="t('episode.asset.stylePrefixCharHint')"
                @change="saveStylePrefix('character', $event.target.value)"
              />
              <BaseSelect
                class="style-prefix-preset-select"
                :model-value="''"
                :options="stylePresetOptions"
                :placeholder="t('episode.asset.stylePrefixImportPlaceholder')"
                searchable
                @update:model-value="importStylePreset('character', $event)"
              />
              <span class="style-prefix-hint">{{ t('episode.asset.stylePrefixCharHint') }}</span>
            </div>
            <div class="asset-section-title">
              {{ t('common.role') }}
              <button class="asset-add-btn" @click="openAssetCreate('character')"><Plus :size="11" /> {{ t('common.add') }}</button>
            </div>
            <template v-if="visualChars.length">
            <div class="character-asset-grid">
              <article
                v-for="c in visualChars"
                :key="c.id"
                class="card character-asset-card"
                :class="{ 'is-asset-drop-active': isAssetDropActive('character', c.id) }"
                tabindex="0"
                role="button"
                @click="openAssetDetail('character', c)"
                @keydown.enter.prevent="openAssetDetail('character', c)"
                @keydown.space.prevent="openAssetDetail('character', c)"
                @dragenter.prevent.stop="activateAssetImageDrop($event, 'character', c.id)"
                @dragover.prevent.stop="activateAssetImageDrop($event, 'character', c.id)"
                @dragleave.stop="deactivateAssetImageDrop($event, 'character', c.id)"
                @drop.prevent.stop="dropAssetImage($event, 'character', c.id)"
              >
                <span v-if="isAssetDropActive('character', c.id)" class="asset-drop-overlay">{{ t('episode.upload.dropImage') }}</span>
                <button class="asset-del-btn" :title="t('episode.asset.delChar')" @click.stop="askDeleteAsset('character', c)"><X :size="11" /></button>
                <div class="character-asset-main">
                  <div class="character-asset-overview"><div class="character-portrait">
                      <img
                        v-if="c.image_url || c.imageUrl"
                        :src="thumbOf(assetImageSrc(c))"
                        class="previewable-image"
                        loading="lazy"
                        @error="thumbFallback($event, assetImageSrc(c))"
                        @click.stop="openImageViewer(assetImageSrc(c), t('episode.asset.charImageTitle', { name: c.name }))"
                      />
                      <div v-else class="character-portrait-empty">
                        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>
                      </div>
                      <span class="asset-cover-badge" :class="isPendingCharImage(c.id) ? 'is-pending' : (isAssetImageFailed('character', c.id) ? 'is-failed' : ((c.image_url || c.imageUrl) ? 'is-ready' : ''))">
                        {{ isPendingCharImage(c.id) ? t('episode.asset.portraitPending') : (isAssetImageFailed('character', c.id) ? t('episode.status.failed') : ((c.image_url || c.imageUrl) ? t('episode.asset.portraitReady') : t('episode.asset.portraitTodo'))) }}
                      </span>
                    </div>

                    <div class="character-asset-head">
                      <div class="character-title-block">
                        <div class="character-name-row">
                          <strong class="character-name">{{ c.name }}</strong>
                          <span class="tag">{{ c.beauty_positioning || c.beautyPositioning || c.role || t('common.role') }}</span>
                        </div>
                        <div class="character-visual-summary" :title="characterVisualSummary(c)">
                          <span v-if="c.aliases">{{ t('episode.asset.aliasesLabel') }}{{ c.aliases }}</span>
                          <span>{{ characterDescriptionValue(c) }}</span>
                        </div>
                      </div>
                      <button class="btn btn-sm character-gen-btn" :disabled="isPendingCharImage(c.id)" @click.stop="genCharImg(c.id)">
                        <Loader2 v-if="isPendingCharImage(c.id)" :size="11" class="animate-spin" />
                        {{ (c.image_url || c.imageUrl) ? t('episode.asset.regen') : (isPendingCharImage(c.id) ? t('episode.asset.generating') : t('episode.asset.generate')) }}
                      </button>
                      <button class="btn btn-sm" :title="t('episode.asset.uploadCharImage')" :disabled="isUploadingAsset('character', c.id)" @click.stop="uploadAssetImage('character', c.id)">
                        <Loader2 v-if="isUploadingAsset('character', c.id)" :size="11" class="animate-spin" />
                        <svg v-else width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
                        {{ t('episode.asset.upload') }}
                      </button>
                    </div>
                    <div class="character-audio-toolbar" @click.stop @keydown.stop>
                      <div class="character-audio-label">
                        <span class="character-audio-icon">♫</span>
                        <span>角色音频</span>
                        <span v-if="c.audio_url || c.audioUrl" class="character-audio-bound">已绑定</span>
                        <span v-else class="character-audio-unbound">未绑定</span>
                      </div>
                      <div v-if="c.audio_url || c.audioUrl" class="character-audio-controls">
                        <button class="btn btn-sm btn-icon character-audio-play" title="播放/暂停角色音频" aria-label="播放/暂停角色音频" @click.stop="toggleAssetAudio(c.id)">
                          <svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M8 5.2v13.6a1 1 0 0 0 1.52.86l10.2-6.8a1 1 0 0 0 0-1.66L9.52 4.34A1 1 0 0 0 8 5.2Z" /></svg>
                        </button>
                        <audio :id="`character-audio-${c.id}`" class="character-audio-player" :src="assetAudioSrc(c)" controls preload="none" aria-label="播放角色音频" />
                        <button class="btn btn-sm" :disabled="isUploadingAsset('character-audio', c.id)" title="更换角色音频" @click.stop="uploadAssetAudio(c.id)">
                          <Loader2 v-if="isUploadingAsset('character-audio', c.id)" :size="11" class="animate-spin" />
                          <span v-else>更换</span>
                        </button>
                        <button class="btn btn-sm character-audio-clear" :disabled="isUploadingAsset('character-audio', c.id)" title="取消角色音频绑定" @click.stop="clearAssetAudio(c.id)">
                          <span>取消</span>
                        </button>
                      </div>
                      <button v-else class="btn btn-sm" :disabled="isUploadingAsset('character-audio', c.id)" title="上传角色音频" @click.stop="uploadAssetAudio(c.id)">
                        <Loader2 v-if="isUploadingAsset('character-audio', c.id)" :size="11" class="animate-spin" />
                        <span v-else>绑定音频</span>
                      </button>
                    </div>
                    <!-- 服装/造型状态一览（底图之外）；增删与生图在详情弹窗内 -->
                    <div v-if="(c.states || []).length" class="character-costume-strip" @click.stop="openAssetDetail('character', c)">
                      <span class="costume-chip is-base">{{ t('episode.asset.costumeBase') }}</span>
                      <span v-for="st in c.states" :key="st.id" :class="['costume-chip', { 'is-ready': st.image_url || st.imageUrl }]">{{ st.name }}</span>
                    </div>
                  </div>
                  <div class="asset-final-prompt" :title="c.final_prompt || c.finalPrompt || ''">
                    <span class="afp-label">{{ t('episode.asset.finalPromptTurnaround') }}</span>
                    <span v-if="assetPromptStale(c)" class="afp-stale">{{ t('episode.asset.promptStale') }}</span>
                    <span :class="['afp-text', !(c.final_prompt || c.finalPrompt) && 'dim']">{{ c.final_prompt || c.finalPrompt || t('episode.asset.finalPromptAutoTurnaround') }}</span>
                  </div>
                </div>
              </article>
            </div>
            </template>

            <div class="style-prefix-row">
              <span class="style-prefix-label">{{ t('episode.asset.stylePrefixLabel') }}</span>
              <input
                class="input style-prefix-input"
                :value="effectiveStylePrefix('scene')"
                :placeholder="presetStylePrefix('scene') || t('episode.asset.stylePrefixEmptyPlaceholder')"
                :title="t('episode.asset.stylePrefixSceneHint')"
                @change="saveStylePrefix('scene', $event.target.value)"
              />
              <BaseSelect
                class="style-prefix-preset-select"
                :model-value="''"
                :options="stylePresetOptions"
                :placeholder="t('episode.asset.stylePrefixImportPlaceholder')"
                searchable
                @update:model-value="importStylePreset('scene', $event)"
              />
              <span class="style-prefix-hint">{{ t('episode.asset.stylePrefixSceneHint') }}</span>
            </div>
            <div class="asset-section-title">
              {{ t('common.scene') }}
              <button class="asset-add-btn" @click="openAssetCreate('scene')"><Plus :size="11" /> {{ t('common.add') }}</button>
            </div>
            <template v-if="scenes.length">
            <div class="asset-grid">
              <div
                v-for="s in scenes"
                :key="s.id"
                class="card asset-card asset-click-card"
                :class="{ 'is-asset-drop-active': isAssetDropActive('scene', s.id) }"
                tabindex="0"
                role="button"
                @click="openAssetDetail('scene', s)"
                @keydown.enter.prevent="openAssetDetail('scene', s)"
                @keydown.space.prevent="openAssetDetail('scene', s)"
                @dragenter.prevent.stop="activateAssetImageDrop($event, 'scene', s.id)"
                @dragover.prevent.stop="activateAssetImageDrop($event, 'scene', s.id)"
                @dragleave.stop="deactivateAssetImageDrop($event, 'scene', s.id)"
                @drop.prevent.stop="dropAssetImage($event, 'scene', s.id)"
              >
                <span v-if="isAssetDropActive('scene', s.id)" class="asset-drop-overlay">{{ t('episode.upload.dropImage') }}</span>
                <button class="asset-del-btn" :title="t('episode.asset.delScene')" @click.stop="askDeleteAsset('scene', s)"><X :size="11" /></button>
                <div class="asset-cover wide">
                  <img
                    v-if="s.image_url || s.imageUrl"
                    :src="thumbOf(assetImageSrc(s))"
                    class="previewable-image"
                    loading="lazy"
                    @error="thumbFallback($event, assetImageSrc(s))"
                    @click.stop="openImageViewer(assetImageSrc(s), t('episode.asset.sceneImageTitle', { name: s.location }))"
                  />
                  <div v-else class="asset-cover-empty">
                    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"><path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"/><circle cx="12" cy="10" r="3"/></svg>
                  </div>
                  <span class="asset-cover-badge" :class="isPendingSceneImage(s.id) ? 'is-pending' : (isAssetImageFailed('scene', s.id, s) ? 'is-failed' : ((s.image_url || s.imageUrl) ? 'is-ready' : ''))">{{ isPendingSceneImage(s.id) ? t('episode.asset.generating') : (isAssetImageFailed('scene', s.id, s) ? t('episode.status.failed') : ((s.image_url || s.imageUrl) ? t('episode.asset.ready') : t('episode.asset.todo'))) }}</span>
                </div>
                <div class="asset-body">
                  <div class="asset-name" :title="s.location">{{ s.location }}</div>
                  <div v-if="s.aliases" class="asset-meta dim" :title="s.aliases">{{ t('episode.asset.aliasesLabel') }}{{ s.aliases }}</div>
                  <div class="asset-meta asset-desc dim" :title="sceneDescriptionValue(s)">{{ sceneDescriptionValue(s) }}</div>
                  <div v-if="sceneLightingValue(s)" class="asset-meta asset-light dim" :title="sceneLightingValue(s)">{{ t('episode.asset.lighting') }}{{ sceneLightingValue(s) }}</div>
                  <div class="asset-meta asset-final" :class="{ dim: !(s.final_prompt || s.finalPrompt) }" :title="s.final_prompt || s.finalPrompt || ''">
                    <span class="afp-label">{{ t('episode.asset.finalPromptFixed') }}</span>
                    <span v-if="assetPromptStale(s)" class="afp-stale">{{ t('episode.asset.promptStale') }}</span>
                    {{ s.final_prompt || s.finalPrompt || t('episode.asset.finalPromptAutoFixed') }}
                  </div>
                </div>
                <div class="asset-foot">
                  <span :class="['dot', (s.image_url || s.imageUrl) && 'ok', isPendingSceneImage(s.id) && 'pending']" />
                  <button class="btn btn-sm ml-auto" :title="t('episode.asset.uploadSceneImage')" :disabled="isUploadingAsset('scene', s.id)" @click.stop="uploadAssetImage('scene', s.id)">
                    <Loader2 v-if="isUploadingAsset('scene', s.id)" :size="11" class="animate-spin" />
                    <svg v-else width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
                    {{ t('episode.asset.upload') }}
                  </button>
                  <button class="btn btn-sm" :disabled="isPendingSceneImage(s.id)" @click.stop="genSceneImg(s.id)">
                    <Loader2 v-if="isPendingSceneImage(s.id)" :size="11" class="animate-spin" />
                    {{ (s.image_url || s.imageUrl) ? t('episode.asset.regen') : (isPendingSceneImage(s.id) ? t('episode.asset.generating') : t('episode.asset.generate')) }}
                  </button>
                </div>
              </div>
            </div>
            </template>

            <div class="asset-section-title">
              {{ t('common.prop') }}
              <button class="asset-add-btn" @click="openAssetCreate('prop')"><Plus :size="11" /> {{ t('common.add') }}</button>
            </div>
            <div v-if="propItems.length" class="asset-grid">
              <div
                v-for="p in propItems"
                :key="p.id"
                class="card asset-card asset-click-card prop-card"
                :class="{ 'is-asset-drop-active': isAssetDropActive('prop', p.id) }"
                tabindex="0"
                role="button"
                @click="openAssetDetail('prop', p)"
                @keydown.enter.prevent="openAssetDetail('prop', p)"
                @keydown.space.prevent="openAssetDetail('prop', p)"
                @dragenter.prevent.stop="activateAssetImageDrop($event, 'prop', p.id)"
                @dragover.prevent.stop="activateAssetImageDrop($event, 'prop', p.id)"
                @dragleave.stop="deactivateAssetImageDrop($event, 'prop', p.id)"
                @drop.prevent.stop="dropAssetImage($event, 'prop', p.id)"
              >
                <span v-if="isAssetDropActive('prop', p.id)" class="asset-drop-overlay">{{ t('episode.upload.dropImage') }}</span>
                <button class="asset-del-btn" :title="t('episode.asset.delProp')" @click.stop="askDeleteAsset('prop', p)"><X :size="11" /></button>
                <div class="asset-cover wide">
                  <img
                    v-if="p.image_url || p.imageUrl"
                    :src="thumbOf(assetImageSrc(p))"
                    class="previewable-image"
                    loading="lazy"
                    @error="thumbFallback($event, assetImageSrc(p))"
                    @click.stop="openImageViewer(assetImageSrc(p), t('episode.asset.propImageTitle', { name: p.name }))"
                  />
                  <div v-else class="asset-cover-empty">
                    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><polyline points="3.27 6.96 12 12.01 20.73 6.96"/><line x1="12" y1="22.08" x2="12" y2="12"/></svg>
                  </div>
                  <span class="asset-cover-badge" :class="(p.image_url || p.imageUrl) ? 'is-ready' : (isPendingPropImage(p.id) ? 'is-pending' : '')">{{ (p.image_url || p.imageUrl) ? t('episode.asset.ready') : (isPendingPropImage(p.id) ? t('episode.asset.generating') : t('episode.asset.todo')) }}</span>
                </div>
                <div class="asset-body">
                  <div class="prop-name-row">
                    <span class="asset-name" :title="p.name">{{ p.name }}</span>
                    <span class="tag">{{ p.type || t('common.prop') }}</span>
                  </div>
                  <div class="asset-meta asset-desc dim" :title="p.description || ''">{{ p.description || t('episode.asset.noDescription') }}</div>
                  <div class="asset-meta asset-final" :class="{ dim: !(p.final_prompt || p.finalPrompt) }" :title="p.final_prompt || p.finalPrompt || ''">
                    <span class="afp-label">{{ t('episode.asset.finalPromptWhiteBg') }}</span>
                    <span v-if="assetPromptStale(p)" class="afp-stale">{{ t('episode.asset.promptStale') }}</span>
                    {{ p.final_prompt || p.finalPrompt || t('episode.asset.finalPromptAutoWhiteBg') }}
                  </div>
                </div>
                <div class="asset-foot">
                  <span :class="['dot', (p.image_url || p.imageUrl) && 'ok', isPendingPropImage(p.id) && 'pending']" />
                  <button class="btn btn-sm ml-auto" :title="t('episode.asset.uploadPropImage')" :disabled="isUploadingAsset('prop', p.id)" @click.stop="uploadAssetImage('prop', p.id)">
                    <Loader2 v-if="isUploadingAsset('prop', p.id)" :size="11" class="animate-spin" />
                    <svg v-else width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
                    {{ t('episode.asset.upload') }}
                  </button>
                  <button class="btn btn-sm" :disabled="isPendingPropImage(p.id)" @click.stop="genPropImg(p.id)">
                    <Loader2 v-if="isPendingPropImage(p.id)" :size="11" class="animate-spin" />
                    {{ (p.image_url || p.imageUrl) ? t('episode.asset.regen') : (isPendingPropImage(p.id) ? t('episode.asset.generating') : t('episode.asset.generate')) }}
                  </button>
                </div>
              </div>
            </div>
            <div v-else class="asset-props-empty">{{ t('episode.asset.propsEmpty') }}</div>
            </template>
          </div>

          <!-- Sub: Storyboard Breakdown（独立展示拆分结果） -->
          <div v-if="prodTab === 'breakdown'" class="prod-content breakdown-content">
            <div class="prod-section-bar">
              <div class="breakdown-bar-copy">
                <span class="breakdown-bar-title">{{ t('episode.breakdown.title') }}</span>
                <span class="dim">{{ t('episode.breakdown.subtitle') }}</span>
              </div>
              <span class="tag mono">{{ t('episode.sb.segmentStat', { n: sbs.length, dur: totalDuration }) }}</span>
              <div class="breakdown-agent-switch">
                <button
                  v-for="a in BREAKDOWN_AGENTS"
                  :key="a.type"
                  type="button"
                  class="btn btn-sm"
                  :class="{ 'is-on': breakdownAgent === a.type }"
                  :disabled="breakdownRunning"
                  :title="t(a.hint)"
                  @click="setBreakdownAgent(a.type)"
                >{{ t(a.label) }}</button>
              </div>
              <div class="ml-auto flex gap-1">
                <button
                  type="button"
                  class="btn btn-sm btn-primary one-click-storyboard-video-btn"
                  :disabled="oneClickStoryboardVideo.running || rn || storyboardReviewBusy || episodeVideoPipeline.running || videoPromptBatch.running || claudeSelectedUnavailable"
                  :title="t('episode.breakdown.oneClickHint')"
                  @click="openOneClickStoryboardVideoConfirm"
                >
                  <Loader2 v-if="oneClickStoryboardVideo.running" :size="11" class="animate-spin" />
                  <Clapperboard v-else :size="12" />
                  {{ oneClickStoryboardVideoLabel }}
                </button>
                <button
                  v-if="v8ReviewAvailable"
                  class="btn btn-sm"
                  :disabled="AUTOFIX_PAUSED || rn || storyboardReviewBusy || claudeSelectedUnavailable"
                  :title="AUTOFIX_PAUSED ? t('episode.breakdown.autofixPaused') : ''"
                  @click="autofixV8Storyboards"
                >
                  <Loader2 v-if="storyboardReview.running" :size="11" class="animate-spin" />
                  <Wrench v-else :size="12" />
                  {{ storyboardReview.running ? t('episode.breakdown.autofixRunning') : t('episode.breakdown.autofix') }}
                </button>
                <button class="btn btn-sm" :disabled="rn || storyboardReviewBusy || claudeSelectedUnavailable" @click="doBreakdown">
                  <Loader2 v-if="breakdownRunning" :size="11" class="animate-spin" />
                  <ListTodo v-else :size="12" />
                  {{ sbs.length ? t('episode.breakdown.regenerate') : t('episode.breakdown.generate') }}
                </button>
              </div>
            </div>

            <div v-if="breakdownRunning" class="breakdown-progress" role="status" aria-live="polite">
              <div class="breakdown-progress-head">
                <span class="breakdown-progress-icon"><Loader2 :size="16" class="animate-spin" /></span>
                <div>
                  <strong>{{ t('episode.breakdown.running') }}</strong>
                  <p>{{ breakdownProgress.queueStatus === 'queued'
                    ? t('episode.breakdown.claudeQueued', { position: breakdownProgress.queuePosition })
                    : breakdownProgress.savedCount
                    ? t('episode.breakdown.progressSaving', { count: breakdownProgress.savedCount, duration: breakdownProgress.savedDuration })
                    : t('episode.breakdown.progressAnalyzing') }}</p>
                </div>
                <span class="breakdown-progress-time mono">{{ t('episode.breakdown.progressElapsed', { time: formatBreakdownElapsed(breakdownProgress.elapsedSeconds) }) }}</span>
              </div>
              <div
                class="breakdown-progress-track"
                role="progressbar"
                :aria-label="t('episode.breakdown.running')"
              ><span /></div>
              <div class="breakdown-progress-foot">
                <span>{{ t('episode.breakdown.progressHint') }}</span>
                <span v-if="breakdownProgress.savedCount" class="breakdown-progress-count mono">
                  {{ t('episode.breakdown.progressSaved', { count: breakdownProgress.savedCount }) }}
                </span>
              </div>
            </div>
            <div v-if="storyboardReview.running" class="breakdown-progress storyboard-review-progress" role="status" aria-live="polite">
              <div class="breakdown-progress-head">
                <span class="breakdown-progress-icon"><Loader2 :size="16" class="animate-spin" /></span>
                <div>
                  <strong>{{ t('episode.breakdown.autofixRunning') }}</strong>
                  <p>{{ storyboardReviewProgress.queueStatus === 'queued'
                    ? t('episode.breakdown.claudeQueued', { position: storyboardReviewProgress.queuePosition })
                    : storyboardReviewProgressText }}</p>
                </div>
                <span class="breakdown-progress-time mono">{{ t('episode.breakdown.progressElapsed', { time: formatBreakdownElapsed(storyboardReviewProgress.elapsedSeconds) }) }}</span>
              </div>
              <div
                class="breakdown-progress-track"
                role="progressbar"
                :aria-valuenow="storyboardReviewProgressPercent"
                aria-valuemin="0"
                aria-valuemax="100"
                :aria-label="t('episode.breakdown.autofixRunning')"
              ><span :style="{ transform: `scaleX(${storyboardReviewProgressPercent / 100})` }" /></div>
              <div class="breakdown-progress-foot">
                <span>{{ t('episode.breakdown.autofixProgressHint') }}</span>
                <span v-if="storyboardReviewProgress.lastRange" class="breakdown-progress-count mono">
                  {{ t('episode.breakdown.autofixProgressRange', { range: storyboardReviewProgress.lastRange }) }}
                </span>
              </div>
            </div>
            <div v-if="!breakdownRunning && !storyboardReview.running && latestStoryboardFailed" class="breakdown-run-failure" role="alert">
              <AlertTriangle :size="17" />
              <div>
                <strong>{{ t('episode.breakdown.recentFailed') }}</strong>
                <p>{{ latestStoryboardFailureReason }}</p>
                <span>{{ t('episode.breakdown.recentFailedHint') }}</span>
              </div>
            </div>
            <div v-else-if="lastBreakdownRunInfo" class="breakdown-run-duration mono">
              {{ t('episode.breakdown.lastRun', { time: lastBreakdownRunInfo.time, duration: formatBreakdownElapsed(lastBreakdownRunInfo.seconds) }) }}
            </div>
            <div v-if="storyboardReview.error" class="storyboard-review-error" role="alert">
              <AlertTriangle :size="16" />
              <span>{{ storyboardReview.error }}</span>
            </div>
            <div v-if="storyboardReview.notice" class="storyboard-review-notice" role="status">
              <Wrench :size="15" />
              <span>{{ storyboardReview.notice }}</span>
            </div>
            <div v-if="!breakdownRunning && !sbs.length" class="step-empty breakdown-empty-state">
              <div class="empty-visual"><ListTodo :size="32" /></div>
              <div class="empty-title">{{ t('episode.breakdown.emptyTitle') }}</div>
              <div class="empty-desc">{{ t('episode.breakdown.emptyDesc') }}</div>
              <button class="btn btn-primary" :disabled="rn" @click="doBreakdown">
                <ListTodo :size="13" />
                {{ t('episode.breakdown.generate') }}
              </button>
            </div>
            <template v-if="sbs.length && breakdownResultsVisible">
              <div class="breakdown-metrics">
                <div class="breakdown-metric">
                  <span>{{ t('episode.breakdown.totalShots') }}</span>
                  <strong>{{ sbs.length }}</strong>
                </div>
                <div class="breakdown-metric">
                  <span>{{ t('episode.breakdown.totalDuration') }}</span>
                  <strong>{{ totalDuration }}s</strong>
                </div>
                <div class="breakdown-metric">
                  <span>{{ t('episode.breakdown.sceneBound') }}</span>
                  <strong>{{ breakdownSceneBoundCount }}/{{ sbs.length }}</strong>
                </div>
                <div class="breakdown-metric">
                  <span>{{ t('episode.breakdown.promptReady') }}</span>
                  <strong>{{ breakdownPromptReadyCount }}/{{ sbs.length }}</strong>
                </div>
              </div>

              <div class="breakdown-table-wrap">
                <table class="breakdown-table">
                  <thead>
                    <tr>
                      <th>{{ t('episode.breakdown.colNumber') }}</th>
                      <th>{{ t('episode.breakdown.colTitle') }}</th>
                      <th>{{ t('episode.breakdown.colDuration') }}</th>
                      <th>{{ t('episode.breakdown.colScene') }}</th>
                      <th>{{ t('episode.breakdown.colCharacters') }}</th>
                      <th>{{ t('episode.breakdown.colProps') }}</th>
                      <th class="is-wide">{{ t('episode.breakdown.colDescription') }}</th>
                      <th>{{ t('episode.breakdown.colAtmosphere') }}</th>
                      <th>{{ t('episode.breakdown.colPrompt') }}</th>
                      <th class="is-action"></th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr
                      v-for="(sb, index) in sbs"
                      :key="sb.id"
                      :tabindex="breakdownRunning ? -1 : 0"
                      :role="breakdownRunning ? undefined : 'button'"
                      :aria-disabled="breakdownRunning ? 'true' : undefined"
                      @click="openBreakdownStoryboard(sb)"
                      @keydown.enter.prevent="openBreakdownStoryboard(sb)"
                    >
                      <td><span class="breakdown-shot-number">#{{ String(sb.storyboard_number || sb.storyboardNumber || index + 1).padStart(2, '0') }}</span></td>
                      <td><strong class="breakdown-shot-title">{{ storyboardDisplayTitle(sb, index) }}</strong></td>
                      <td><span class="breakdown-duration">{{ Number(sb.duration || 0) }}s</span></td>
                      <td>
                        <span :class="['breakdown-cell-chip', getStoryboardScene(sb) && 'is-ready']">
                          {{ getSceneName(sb) || t('episode.breakdown.unbound') }}
                        </span>
                      </td>
                      <td>
                        <div v-if="getStoryboardCharacters(sb).length" class="breakdown-chip-list">
                          <span v-for="char in getStoryboardCharacters(sb)" :key="char.id" class="breakdown-cell-chip is-person">{{ char.name }}</span>
                        </div>
                        <span v-else class="breakdown-empty-cell">{{ t('episode.breakdown.none') }}</span>
                      </td>
                      <td>
                        <div v-if="getStoryboardProps(sb).length" class="breakdown-chip-list">
                          <span v-for="prop in getStoryboardProps(sb)" :key="prop.id" class="breakdown-cell-chip">{{ prop.name }}</span>
                        </div>
                        <span v-else class="breakdown-empty-cell">{{ t('episode.breakdown.none') }}</span>
                      </td>
                      <td><p class="breakdown-description" :title="sb.description || ''">{{ sb.description || t('episode.breakdown.none') }}</p></td>
                      <td><p class="breakdown-atmosphere" :title="sb.atmosphere || ''">{{ sb.atmosphere || t('episode.breakdown.none') }}</p></td>
                      <td>
                        <span :class="['breakdown-prompt-state', storyboardPromptReady(sb) && 'is-ready']">
                          <i class="dot" :class="storyboardPromptReady(sb) && 'ok'" />
                          {{ storyboardPromptReady(sb) ? t('episode.breakdown.statusReady') : t('episode.breakdown.statusPending') }}
                        </span>
                      </td>
                      <td class="is-action">
                        <button class="btn btn-sm" :disabled="breakdownRunning" @click.stop="openBreakdownStoryboard(sb)">{{ t('episode.breakdown.viewEdit') }}</button>
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </template>
          </div>

          <!-- Sub: Video Production -->
          <div v-if="prodTab === 'videos'" class="prod-content">
            <div class="style-prefix-row">
              <span class="style-prefix-label">{{ t('episode.asset.stylePrefixLabel') }}</span>
              <input
                class="input style-prefix-input"
                :value="effectiveStylePrefix('video')"
                :placeholder="presetStylePrefix('video') || t('episode.asset.stylePrefixEmptyPlaceholder')"
                :title="t('episode.asset.stylePrefixVideoHint')"
                @change="saveStylePrefix('video', $event.target.value)"
              />
              <BaseSelect
                class="style-prefix-preset-select"
                :model-value="''"
                :options="stylePresetOptions"
                :placeholder="t('episode.asset.stylePrefixImportPlaceholder')"
                searchable
                @update:model-value="importStylePreset('video', $event)"
              />
              <span class="style-prefix-hint">{{ t('episode.asset.stylePrefixVideoHint') }}</span>
            </div>
            <div class="prod-section-bar">
              <span class="dim" style="font-size:.75rem">{{ t('episode.prod.videos') }}</span>
              <span class="tag mono">{{ t('episode.sb.segmentStat', { n: sbs.length, dur: totalDuration }) }}</span>
              <span class="tag mono" :title="t('episode.vid.aspectRatio')">{{ dramaAspectRatio }}</span>
              <span class="tag" :title="t('episode.sb.promptSkillOfficialHint')">{{ t('episode.sb.promptSkillOfficial') }}</span>
              <div class="ml-auto flex gap-1">
                <button
                  class="btn btn-sm btn-primary"
                  :title="t('episode.vid.fullPipelineHint')"
                  :disabled="episodeVideoPipeline.running || videoPromptBatch.running || !sbs.length"
                  @click="completeEpisodeVideos"
                >
                  <Loader2 v-if="episodeVideoPipeline.running" :size="11" class="animate-spin" />
                  <Clapperboard v-else :size="11" />
                  {{ episodeVideoPipelineButtonLabel }}
                </button>
                <button class="btn btn-sm" :disabled="episodeVideoPipeline.running || videoPromptBatch.running || !sbs.length" @click="batchVideoPrompts">
                  <Loader2 v-if="videoPromptBatch.running" :size="11" class="animate-spin" />
                  <svg v-else width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>
                  {{ videoPromptBatch.running
                    ? (videoPromptBatch.queueStatus === 'queued'
                      ? t('episode.breakdown.claudeQueued', { position: videoPromptBatch.queuePosition })
                      : t('episode.sb.promptProgress', { done: videoPromptBatch.completed, total: videoPromptBatch.total }))
                    : (videoSelectMode && selectedVideoSbIds.length ? t('episode.sb.promptSelected', { n: selectedVideoSbIds.length }) : t('episode.sb.batchPrompts')) }}
                </button>
                <button v-if="videoTaskFailedCount" class="btn btn-sm video-retry-failed" @click="retryFailedVideos">
                  <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>
                  {{ t('episode.vid.retryFailed', { n: videoTaskFailedCount }) }}
                </button>
                <button class="btn btn-sm" :class="{ 'is-on': videoSelectMode }" @click="toggleVideoSelectMode">
                  {{ videoSelectMode ? t('episode.vid.selectDone', { n: selectedVideoSbIds.length }) : t('episode.vid.select') }}
                </button>
                <button class="btn btn-sm" :disabled="episodeVideoPipeline.running || !sbs.length" @click="batchVideos">
                  <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polygon points="23 7 16 12 23 17 23 7"/><rect x="1" y="5" width="15" height="14" rx="2" ry="2"/></svg>
                  {{ videoSelectMode && selectedVideoSbIds.length ? t('episode.vid.batchSelected', { n: selectedVideoSbIds.length }) : t('episode.vid.batchVideos') }}
                </button>
              </div>
            </div>
            <div v-if="videoStatusSyncInterrupted" class="video-sync-warning">
              {{ t('episode.vid.syncInterrupted') }}
            </div>
            <div v-if="!sbs.length" class="step-empty video-task-empty-state">
              <div class="empty-visual">
                <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"><rect x="2" y="2" width="20" height="20" rx="2.5"/><line x1="7" y1="8" x2="7" y2="16"/><line x1="10" y1="8" x2="10" y2="16"/><line x1="13" y1="8" x2="13" y2="16"/></svg>
              </div>
              <div class="empty-title">{{ t('episode.sb.emptyTitle') }}</div>
              <div class="empty-desc">{{ t('episode.sb.emptyDesc') }}</div>
              <div class="locked-config-banner">{{ t('episode.vid.lockedModel') }}{{ effectiveVideoModelLabel }}</div>
              <button class="btn btn-primary" @click="prodTab = 'breakdown'">
                <ListTodo :size="13" />
                {{ t('episode.breakdown.goBreakdown') }}
              </button>
            </div>
            <div v-else class="video-task-workbench has-player" :style="{ '--vleft': videoLeftW + 'px', '--vright': videoRightW + 'px' }">
              <section class="video-task-list">
                <div class="video-task-head">
                <div>
                  <div class="video-task-title">{{ t('episode.vid.listTitle') }}</div>
                  <div class="video-task-meta">{{ videoListFilter ? t('episode.vid.listMetaFiltered', { n: videoTaskRows.length, total: allVideoTaskRows.length }) : t('episode.vid.listMeta', { n: videoTaskRows.length }) }}</div>
                </div>
                <div class="video-task-metrics">
                  <button type="button" class="video-task-metric is-pending" :class="{ on: videoListFilter === 'pending' }" @click="toggleVideoFilter('pending')">{{ t('episode.vid.metricPending', { n: pendingVideoIds.length }) }}</button>
                  <button type="button" class="video-task-metric is-done" :class="{ on: videoListFilter === 'done' }" @click="toggleVideoFilter('done')">{{ t('episode.vid.metricDone', { n: videoTaskDoneCount }) }}</button>
                  <button type="button" class="video-task-metric is-failed" :class="{ on: videoListFilter === 'failed' }" @click="toggleVideoFilter('failed')">{{ t('episode.vid.metricFailed', { n: videoTaskFailedCount }) }}</button>
                </div>
                </div>
                <div v-if="videoSelectMode" class="shot-quick-actions video-quick-actions">
                  <button class="shot-quick-btn" @click="toggleSelectAllVideos">{{ t('episode.sb.selectAll') }}</button>
                  <button class="shot-quick-btn" @click="selectMissingVideos">{{ t('episode.vid.selectMissing') }}</button>
                  <button class="shot-quick-btn" @click="selectedVideoSbIds = []">{{ t('episode.sb.clear') }}</button>
                </div>
                <div class="video-task-table">
                <template v-for="task in videoTaskRows" :key="task.id">
                <div
                  :class="['video-task-row', 'is-' + videoTaskState(task.storyboard), { active: !videoSelectMode && selectedSb?.id === task.storyboard.id, 'is-selected': videoSelectMode && isVideoSbSelected(task.id) }]"
                  role="button"
                  tabindex="0"
                  @click="onVideoTaskRowClick(task.storyboard)"
                  @keydown.enter.prevent="onVideoTaskRowClick(task.storyboard)"
                  @keydown.space.prevent="onVideoTaskRowClick(task.storyboard)"
                >
                  <div class="video-task-preview">
                    <span
                      v-if="videoSelectMode"
                      class="shot-check video-task-check"
                      :class="{ on: isVideoSbSelected(task.id) }"
                    >
                      <svg v-if="isVideoSbSelected(task.id)" width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
                    </span>
                    <video
                      v-if="hasVid(task.storyboard)"
                      :src="'/' + getVideoUrl(task.storyboard)"
                      :poster="posterOf('/' + getVideoUrl(task.storyboard)) || undefined"
                      preload="none"
                      playsinline
                      muted
                      tabindex="-1"
                    />
                    <div v-else class="video-task-empty">
                      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"><polygon points="23 7 16 12 23 17 23 7"/><rect x="1" y="5" width="15" height="14" rx="2" ry="2"/></svg>
                    </div>
                    <span class="video-task-index">#{{ String(task.index + 1).padStart(2, '0') }}</span>
                  </div>
                  <div class="video-task-main">
                    <div class="video-task-line">
                      <strong class="video-task-name">{{ task.title }}</strong>
                    </div>
                    <div class="video-task-meta-line">
                      <span :class="['video-task-state', 'is-' + videoTaskStatusTone(task.storyboard)]">
                        <i :class="['dot', 'is-' + videoTaskStatusTone(task.storyboard)]" />{{ videoTaskStatusLabel(task.storyboard) }}
                      </span>
                      <span class="video-task-sep">·</span>
                      <span>{{ task.duration }}s</span>
                      <template v-if="task.meta">
                        <span class="video-task-sep">·</span>
                        <span class="video-task-loc truncate">{{ task.meta }}</span>
                      </template>
                    </div>
                    <div v-if="task.error || task.warning" :class="['video-task-error', task.warning && 'is-warning']" :title="task.error || task.warning">
                      {{ task.warning || mapError(task.error) }}
                      <div v-if="videoModerationHint(task.error)" class="video-task-error-hint">{{ videoModerationHint(task.error) }}</div>
                    </div>
                  </div>
                  <button
                    class="btn btn-icon btn-sm video-task-action"
                    :title="videoTaskActionLabel(task.storyboard)"
                    :disabled="videoTaskState(task.storyboard) === 'pending'"
                    @click.stop="genVid(task.storyboard)"
                  >
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polygon points="23 7 16 12 23 17 23 7"/><rect x="1" y="5" width="15" height="14" rx="2" ry="2"/></svg>
                  </button>
                </div>
                </template>
                </div>
              </section>

              <div v-if="selectedSb" class="video-task-side">
              <div class="video-main-col">
              <div class="video-main-scroll">
                <div class="video-main-grid">
                  <section class="video-inspector-section">
                    <span class="video-inspector-label">{{ t('episode.sb.descSection') }}</span>
                    <label class="field">
                      <textarea :value="selectedSb.description || ''" class="textarea" rows="7" @blur="updateField(selectedSb, 'description', $event.target.value)" :placeholder="t('episode.sb.descPlaceholder')" />
                    </label>
                    <label class="field">
                      <span class="field-label">{{ t('episode.sb.atmosphere') }}</span>
                      <textarea :value="selectedSb.atmosphere || ''" class="textarea" rows="2" @blur="updateField(selectedSb, 'atmosphere', $event.target.value)" :placeholder="t('episode.sb.atmospherePlaceholder')" />
                    </label>
                  </section>

                  <section class="video-inspector-section">
                    <div class="video-inspector-prompt-head">
                      <span class="video-inspector-label">{{ t('episode.ref.title') }}</span>
                      <span class="tag mono">{{ t('episode.ref.boundCount', { bound: refBindableAssets.filter(a => a.bound).length, total: refBindableAssets.length }) }}</span>
                    </div>
                    <div class="storyboard-ref-list is-embedded">
                      <template v-for="g in REF_KINDS" :key="g.kind">
                        <div v-if="refBindableAssets.filter(a => a.kind === g.kind).length" class="storyboard-ref-group">
                          <div class="storyboard-ref-group-label">{{ g.label }}</div>
                          <div
                            v-for="asset in refBindableAssets.filter(a => a.kind === g.kind)"
                            :key="asset.key"
                            :class="['storyboard-ref-item', { bound: asset.bound }]"
                            :title="asset.bound ? t('episode.ref.clickRemove') : t('episode.ref.clickAdd')"
                            @click="toggleShotBind(selectedSb, asset)"
                          >
                            <button
                              type="button"
                              class="storyboard-ref-thumb"
                              :disabled="!asset.ready"
                              @click.stop="asset.ready && openImageViewer(assetImageSrc({ imageUrl: asset.imageUrl }), `${asset.name} ${asset.typeLabel}`)"
                            >
                              <img v-if="asset.ready" :src="thumbOf(assetImageSrc({ imageUrl: asset.imageUrl }))" class="previewable-image" loading="lazy" @error="thumbFallback($event, assetImageSrc({ imageUrl: asset.imageUrl }))" />
                              <span v-else>{{ asset.kind === 'scene' ? t('episode.ref.shortScene') : asset.kind === 'prop' ? t('episode.ref.shortProp') : t('episode.ref.shortChar') }}</span>
                            </button>
                            <div class="storyboard-ref-main">
                              <span class="storyboard-ref-name">{{ asset.name }}</span>
                              <span class="storyboard-ref-meta">{{ asset.typeLabel }} · {{ asset.meta }}</span>
                              <span :class="['storyboard-ref-state', asset.bound && asset.ready ? 'is-ready' : '']">
                                {{ asset.bound ? (asset.ready ? t('episode.ref.usable') : t('episode.ref.notReady')) : t('episode.ref.unbound') }}
                              </span>
                              <button v-if="asset.bound && !asset.ready" type="button" class="storyboard-ref-goto" @click.stop="prodTab = 'assets'">{{ t('episode.ref.gotoGenerate') }}</button>
                              <!-- 已绑定角色且有带图服装状态时：选择本镜使用的造型（仅影响提交时的参考图） -->
                              <select
                                v-if="asset.kind === 'character' && asset.bound && asset.states && asset.states.length"
                                class="input storyboard-ref-costume-select"
                                :value="asset.selectedStateId == null ? '' : String(asset.selectedStateId)"
                                @click.stop
                                @change="setShotCharacterState(selectedSb, asset.id, $event.target.value === '' ? null : Number($event.target.value))"
                              >
                                <option value="">{{ t('episode.ref.costumeBase') }}</option>
                                <option v-for="st in asset.states" :key="st.id" :value="String(st.id)">{{ st.name }}</option>
                              </select>
                            </div>
                          </div>
                        </div>
                      </template>
                      <div v-if="!refBindableAssets.length" class="storyboard-ref-empty">{{ t('episode.ref.empty') }}</div>
                    </div>
                  </section>
                </div>

                  <section class="video-inspector-section">
                    <div class="video-inspector-prompt-head">
                      <span class="video-inspector-label video-inspector-label-hero">{{ t('episode.sb.videoPromptSection') }}</span>
                      <div class="video-inspector-prompt-actions">
                        <button
                          type="button"
                          class="btn btn-sm"
                          :disabled="videoPromptGeneratingIds.includes(selectedSb?.id) || videoPromptBatch.running"
                          @click="genVideoPrompt(selectedSb)"
                        >
                          <Loader2 v-if="videoPromptGeneratingIds.includes(selectedSb?.id)" :size="11" class="animate-spin" />
                          {{ t('episode.sb.videoPromptGenerate') }}
                        </button>
                      </div>
                    </div>
                    <!-- Agent 修改待确认：原提示词框内直接高亮增删；否则显示普通编辑框 -->
                    <PromptDiffView
                      v-if="videoAgentCandidate"
                      :segments="videoAgentCandidate.segments"
                      class="textarea video-inspector-prompt"
                    />
                    <MentionTextarea
                      v-else
                      :model-value="selectedSb.video_prompt || selectedSb.videoPrompt || ''"
                      :options="mentionOptions"
                      :rows="14"
                      input-class="textarea video-inspector-prompt"
                      :placeholder="t('episode.inspector.videoPromptPlaceholder')"
                      @commit="v => updateField(selectedSb, 'video_prompt', v)"
                    />
                    <PromptAgentEditor
                      v-if="selectedSb"
                      :drama-id="dramaId"
                      :episode-id="epId"
                      target-type="storyboard"
                      :target-id="selectedSb.id"
                      :current-prompt="selectedSb.video_prompt || selectedSb.videoPrompt || ''"
                      :model="chatModelOverride()"
                      :config-id="chatConfigId()"
                      @candidate="videoAgentCandidate = $event"
                      @cleared="videoAgentCandidate = null"
                      @applied="onStoryboardAgentApplied"
                    />
                  </section>
              </div>
              </div>

              <aside class="video-task-inspector">
            <aside class="video-task-player">
              <div class="video-player-head">
                <div class="video-player-head-info">
                  <div class="video-player-title">{{ t('episode.vid.playerTitle', { n: String(selectedVideoTaskNumber).padStart(2, '0') }) }}</div>
                  <span :class="['video-task-status', 'is-' + videoTaskStatusTone(selectedSb)]">
                    <span :class="['dot', 'is-' + videoTaskStatusTone(selectedSb)]" />
                    {{ videoTaskStatusLabel(selectedSb) }}
                  </span>
                  <span v-if="selectedSb.duration" class="video-player-sub">{{ selectedSb.duration }}s</span>
                </div>
                <button
                  v-if="previewVideoUrl"
                  class="btn btn-sm btn-primary"
                  @click="setAsMainVideo()"
                >
                  {{ t('episode.vid.setMain') }}
                </button>
                <a
                  v-if="previewVideoUrl || hasVid(selectedSb)"
                  :href="'/' + (previewVideoUrl || getVideoUrl(selectedSb))"
                  download
                  class="btn btn-sm"
                >
                  <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
                  {{ t('common.download') }}
                </a>
              </div>
              <div class="video-player-stage">
                <video
                  v-if="previewVideoUrl || hasVid(selectedSb)"
                  :key="previewVideoUrl || getVideoUrl(selectedSb)"
                  :src="'/' + (previewVideoUrl || getVideoUrl(selectedSb))"
                  :poster="posterOf('/' + (previewVideoUrl || getVideoUrl(selectedSb))) || undefined"
                  controls
                  preload="metadata"
                  playsinline
                  class="video-player-video"
                />
                <div v-else class="video-player-empty">
                  <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"><polygon points="23 7 16 12 23 17 23 7"/><rect x="1" y="5" width="15" height="14" rx="2" ry="2"/></svg>
                  <div class="video-player-empty-copy">
                    <div class="video-player-empty-title">{{ videoTaskState(selectedSb) === 'pending' ? t('episode.vid.emptyGenerating') : t('episode.vid.emptyNoVideo') }}</div>
                    <div class="video-player-empty-desc">{{ videoTaskState(selectedSb) === 'pending' ? t('episode.vid.emptyGeneratingDesc') : t('episode.vid.emptyNoVideoDesc') }}</div>
                  </div>
                  <button
                    v-if="videoTaskState(selectedSb) !== 'pending'"
                    class="btn btn-primary btn-sm video-player-empty-action"
                    @click="genVid(selectedSb)"
                  >
                    {{ t('episode.vid.generateVideo') }}
                  </button>
                </div>
              </div>
            </aside>

            <div v-if="sbVideoHistory.length" class="video-player-history">
              <div class="video-player-history-head">
                <span>{{ t('episode.vid.history') }}</span>
                <span class="video-player-history-count">{{ sbVideoHistory.length }}</span>
                <span class="video-player-history-hint">{{ t('episode.vid.historyItemHint') }}</span>
              </div>
              <div class="video-player-history-list">
                <div
                  v-for="h in sbVideoHistory"
                  :key="h.id"
                  :class="['video-history-item', { current: isCurrentVideo(h), viewing: !!previewVideoUrl && previewVideoUrl === taskVideoPath(h) }]"
                  :title="t('episode.vid.historyItemHint')"
                  role="button"
                  tabindex="0"
                  @click="previewHistoryVideo(h)"
                  @dblclick.prevent="setAsMainVideo(h)"
                  @keydown.enter.prevent="previewHistoryVideo(h)"
                >
                  <video :src="'/' + taskVideoPath(h)" :poster="posterOf('/' + taskVideoPath(h)) || undefined" preload="none" muted playsinline tabindex="-1" />
                  <span class="video-history-time">{{ formatHistoryTime(taskCreatedAt(h)) }}</span>
                  <button
                    v-if="taskVideoSeed(h) !== null"
                    type="button"
                    class="video-history-seed"
                    :class="{ same: taskVideoSeed(h) === storyboardVideoSeed(selectedSb) }"
                    :title="t('episode.vid.historySeedTitle', { seed: taskVideoSeed(h) })"
                    @click.stop="applyHistorySeed(h)"
                    @dblclick.stop
                  >seed {{ taskVideoSeed(h) }}</button>
                  <span v-if="isCurrentVideo(h)" class="video-history-badge">{{ t('episode.vid.current') }}</span>
                  <button v-else type="button" class="video-history-del" :title="t('episode.vid.deleteRecord')" @click.stop="askDeleteHistoryVideo(h)" @dblclick.stop>×</button>
                </div>
              </div>
            </div>
                <div class="video-inspector-body">
                  <section class="video-inspector-section">
                    <div class="video-inspector-prompt-head">
                      <span class="video-inspector-label">{{ t('episode.inspector.boundRefs') }}</span>
                      <span class="tag mono">{{ boundRefAssets.length }}</span>
                    </div>
                    <div v-if="boundRefAssets.length" class="video-bound-refs">
                      <button
                        v-for="asset in boundRefAssets"
                        :key="asset.key"
                        type="button"
                        class="video-bound-ref"
                        :disabled="!asset.ready"
                        :title="`${asset.name} · ${asset.typeLabel}`"
                        @click="asset.ready && openImageViewer(assetImageSrc({ imageUrl: asset.imageUrl }), `${asset.name} ${asset.typeLabel}`)"
                      >
                        <img v-if="asset.ready" :src="thumbOf(assetImageSrc({ imageUrl: asset.imageUrl }))" :alt="asset.name" loading="lazy" @error="thumbFallback($event, assetImageSrc({ imageUrl: asset.imageUrl }))" />
                        <span v-else class="video-bound-ref-empty">{{ asset.kind === 'scene' ? t('episode.ref.shortScene') : asset.kind === 'prop' ? t('episode.ref.shortProp') : t('episode.ref.shortChar') }}</span>
                        <small>{{ asset.name }}</small>
                      </button>
                    </div>
                    <div v-else class="video-bound-refs-empty">{{ t('episode.inspector.noBoundRefs') }}</div>
                  </section>
                </div>

                <!-- 分镜时长 + 生成操作常驻底部：不随检查器内容滚动 -->
                <div class="video-inspector-footer">
                  <section class="video-inspector-section video-params-card">
                    <div class="video-param-row">
                      <span class="video-param-name">{{ t('episode.inspector.duration') }}</span>
                      <span class="video-param-control">
                        <input
                          :value="selectedSb.duration || 10"
                          type="number"
                          min="2"
                          max="15"
                          class="input video-duration-input"
                          @change="onVideoDurationChange"
                        />
                        <span class="video-param-unit">{{ t('episode.inspector.durationUnit') }}</span>
                      </span>
                    </div>
                    <div class="video-param-row video-seed-row">
                      <span class="video-param-name">{{ t('episode.inspector.seed') }}</span>
                      <span class="video-param-control">
                        <code class="video-seed-value">{{ storyboardVideoSeed(selectedSb) ?? t('episode.inspector.seedPending') }}</code>
                        <button
                          type="button"
                          class="btn btn-sm btn-icon video-seed-randomize"
                          :title="t('episode.inspector.randomizeSeed')"
                          :aria-label="t('episode.inspector.randomizeSeed')"
                          :disabled="videoTaskState(selectedSb) === 'pending'"
                          @click="randomizeVideoSeed(selectedSb)"
                        >
                          <Shuffle :size="12" />
                        </button>
                      </span>
                    </div>
                    <div class="video-param-hint">{{ t('episode.inspector.durationHint') }}</div>
                    <div class="video-param-hint">{{ t('episode.inspector.seedHint') }}</div>
                  </section>
                  <div class="video-inspector-effective">
                    {{ t('episode.inspector.effective', { model: effectiveVideoModelLabel || t('episode.vid.defaultModel'), res: episodeResolutionShort, dur: effectiveVideoDuration }) }}
                  </div>
                  <button
                    class="btn btn-primary video-inspector-action"
                    :disabled="videoTaskState(selectedSb) === 'pending'"
                    @click="genVid(selectedSb)"
                  >
                    {{ videoTaskActionLabel(selectedSb) }}
                  </button>
                </div>
              </aside>
              </div>
              <div
                class="video-col-divider is-left"
                role="separator"
                aria-orientation="vertical"
                @pointerdown="startVideoColDrag('left', $event)"
                @dblclick="videoLeftW = VIDEO_COL_DEFAULTS.left"
              ></div>
              <div
                class="video-col-divider is-right"
                role="separator"
                aria-orientation="vertical"
                @pointerdown="startVideoColDrag('right', $event)"
                @dblclick="videoRightW = VIDEO_COL_DEFAULTS.right"
              ></div>
            </div>
          </div>

          <!-- Production Navigator -->
        </template>
      </div>

      <!-- ===== EXPORT PANEL ===== -->
      <div v-else class="content-panel">
        <div v-if="!sbs.length" class="step-empty" style="flex:1">
          <div class="empty-visual">
            <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
          </div>
          <div class="empty-title">{{ t('episode.prod.notReady') }}</div>
          <div class="empty-desc">{{ t('episode.export.notReadyDesc') }}</div>
          <button class="btn btn-primary" @click="panel = 'script'">{{ t('episode.export.gotoScript') }}</button>
        </div>
        <div v-else class="export-split">
          <div class="export-main">
            <!-- 上方:成片列表 -->
            <div class="export-section">
              <div class="export-section-head">
                <span class="export-section-title">{{ t('episode.export.filmList') }}</span>
                <span class="dim" style="font-size:.6875rem">{{ t('episode.export.countN', { n: exportMerges.length }) }}</span>
                <button
                  :class="['btn btn-sm ml-auto export-done-btn', { on: exportDone }]"
                  :title="t('episode.export.markDoneTitle')"
                  @click="toggleExportDone"
                >
                  <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
                  {{ exportDone ? t('episode.export.markedDone') : t('episode.export.markDone') }}
                </button>
                <button class="btn btn-sm" @click="loadExportMerges">
                  <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>
                  {{ t('common.refresh') }}
                </button>
              </div>
              <div v-if="exportMerges.length" class="export-merge-strip">
                <div
                  v-for="m in exportMerges"
                  :key="m.id"
                  :class="['merge-card', m.status === 'completed' && m.merged_url && 'playable']"
                  :role="m.status === 'completed' && m.merged_url ? 'button' : undefined"
                  :tabindex="m.status === 'completed' && m.merged_url ? 0 : undefined"
                  @click="m.status === 'completed' && m.merged_url && (activeMerge = m)"
                  @keydown.enter.prevent="m.status === 'completed' && m.merged_url && (activeMerge = m)"
                >
                  <div class="merge-card-thumb">
                    <video
                      v-if="m.status === 'completed' && m.merged_url"
                      :src="'/' + m.merged_url"
                      :poster="posterOf('/' + m.merged_url) || undefined"
                      preload="none"
                      muted
                      playsinline
                      tabindex="-1"
                    />
                    <div v-else :class="['merge-card-pending', m.status === 'failed' && 'is-failed']" :title="m.status === 'failed' ? m.error_msg : null">
                      {{ m.status === 'failed' ? mapError(m.error_msg, { fallback: 'episode.export.mergeFailed' }) : t('episode.export.merging') }}
                    </div>
                    <span v-if="m.status === 'completed' && m.merged_url" class="merge-card-play">
                      <svg width="22" height="22" viewBox="0 0 24 24" fill="currentColor" stroke="none"><polygon points="6 3 20 12 6 21 6 3"/></svg>
                    </span>
                  </div>
                  <div class="merge-card-meta">
                    <span class="mono">{{ formatHistoryTime(m.created_at) }}</span>
                    <span v-if="m.duration">· {{ m.duration }}s</span>
                    <a
                      v-if="m.status === 'completed' && m.merged_url"
                      :href="'/' + m.merged_url"
                      download
                      class="btn btn-sm"
                      @click.stop
                    >
                      <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
                      {{ t('common.download') }}
                    </a>
                    <button
                      v-if="m.status !== 'processing' && m.status !== 'pending'"
                      type="button"
                      class="btn btn-sm btn-ghost"
                      :title="t('episode.export.deleteFilm')"
                      @click.stop="askDeleteMerge(m)"
                    >
                      <Trash2 :size="11" />
                    </button>
                  </div>
                </div>
              </div>
              <div v-else class="export-merge-empty">{{ t('episode.export.empty') }}</div>
            </div>

            <!-- 下方:镜头素材(可勾选) -->
            <div class="export-section export-section-grow">
              <div class="export-section-head">
                <span class="export-section-title">{{ t('episode.export.shotAssets') }}</span>
                <span class="dim" style="font-size:.6875rem">{{ t('episode.export.shotStat', { done: shotVidCount, total: sbs.length, selected: exportSelectedReadyIds.length }) }}</span>
                <div class="ml-auto flex gap-1">
                  <button class="btn btn-sm" :disabled="!exportReadyIds.length" @click="toggleSelectAllExport">
                    {{ exportSelectedReadyIds.length === exportReadyIds.length && exportReadyIds.length ? t('episode.export.clearSelection') : t('episode.export.selectAllReady') }}
                  </button>
                  <button
                    class="btn btn-sm"
                    :disabled="!exportSelectedReadyIds.length || batchDownloading"
                    @click="batchDownloadVideos"
                  >
                    <Loader2 v-if="batchDownloading" :size="11" class="animate-spin" />
                    <svg v-else width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
                    {{ t('episode.export.batchDownload') }}
                  </button>
                  <button
                    class="btn btn-sm btn-primary"
                    :disabled="!exportSelectedReadyIds.length"
                    @click="doMerge(exportSelectedReadyIds)"
                  >
                    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/></svg>
                    {{ t('episode.export.mergeSelected', { n: exportSelectedReadyIds.length }) }}
                  </button>
                </div>
              </div>
              <div class="export-grid">
                <div
                  v-for="(sb, i) in sbs"
                  :key="sb.id"
                  :class="['exp-card', { selected: isExportSelected(sb.id), playable: hasVid(sb) }]"
                  :role="hasVid(sb) ? 'button' : undefined"
                  :tabindex="hasVid(sb) ? 0 : undefined"
                  @click="toggleExportSelect(sb)"
                  @keydown.enter.prevent="toggleExportSelect(sb)"
                >
                  <div class="exp-thumb">
                    <video
                      v-if="hasVid(sb)"
                      :src="'/' + getVideoUrl(sb)"
                      :poster="posterOf('/' + getVideoUrl(sb)) || undefined"
                      preload="none"
                      muted
                      playsinline
                      tabindex="-1"
                    />
                    <div v-else class="exp-thumb-empty">
                      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"><polygon points="23 7 16 12 23 17 23 7"/><rect x="1" y="5" width="15" height="14" rx="2" ry="2"/></svg>
                    </div>
                    <span class="exp-thumb-index">#{{ String(i+1).padStart(2,'0') }}</span>
                    <span v-if="sb.duration" class="exp-thumb-duration">{{ sb.duration }}s</span>
                    <span
                      v-if="hasVid(sb)"
                      class="exp-play"
                      :title="t('episode.export.previewShot')"
                      @click.stop="previewShot = sb"
                    >
                      <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor" stroke="none"><polygon points="6 3 20 12 6 21 6 3"/></svg>
                    </span>
                    <span v-if="hasVid(sb)" :class="['exp-check', isExportSelected(sb.id) && 'on']">
                      <svg v-if="isExportSelected(sb.id)" width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
                    </span>
                  </div>
                  <div class="exp-row-line">
                    <span class="truncate" style="flex:1;font-size:.6875rem">{{ sb.description || sb.title || '—' }}</span>
                    <span :class="['dot', hasVid(sb) && 'ok']" />
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- ===== TASK DRAWER ===== -->
      <div v-if="taskDrawer" class="task-drawer-overlay" @click.self="closeTaskDrawer">
        <aside class="task-drawer" role="dialog" aria-modal="true" :aria-label="t('episode.tasks.title')">
          <header class="task-drawer-head">
            <div>
              <div class="video-task-title">{{ t('episode.tasks.title') }}</div>
              <div class="video-task-meta">{{ t('episode.tasks.meta', { n: genTaskRows.length }) }}</div>
            </div>
            <div class="task-drawer-head-actions">
              <button class="btn btn-sm" @click="loadGenTasks">
                <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>
                {{ t('common.refresh') }}
              </button>
              <button class="btn btn-ghost btn-icon" @click="closeTaskDrawer"><X :size="14" /></button>
            </div>
          </header>
          <div class="video-task-metrics task-drawer-metrics">
            <span class="video-task-metric is-pending">{{ t('episode.vid.metricPending', { n: genTaskActiveCount }) }}</span>
            <span class="video-task-metric is-done">{{ t('episode.vid.metricDone', { n: genTaskDoneCount }) }}</span>
            <span class="video-task-metric is-failed">{{ t('episode.vid.metricFailed', { n: genTaskFailedCount }) }}</span>
            <span v-if="genTaskCanceledCount" class="video-task-metric is-canceled">{{ t('episode.tasks.canceledCount', { n: genTaskCanceledCount }) }}</span>
          </div>
          <div v-if="!genTaskRows.length" class="step-empty task-drawer-empty">
            <div class="empty-visual">
              <ListTodo :size="32" />
            </div>
            <div class="empty-title">{{ t('episode.tasks.emptyTitle') }}</div>
            <div class="empty-desc">{{ t('episode.tasks.emptyDesc') }}</div>
          </div>
          <div v-else class="video-task-table task-drawer-body">
            <div
              v-for="row in genTaskRows"
              :key="row.key"
              :class="['video-task-row', 'gen-task-row', 'is-' + genTaskStateClass(row.status)]"
            >
              <div class="video-task-preview">
                <video
                  v-if="row.previewUrl && (row.kind === 'video' || row.kind === 'merge')"
                  :src="genTaskPreviewSrc(row.previewUrl)"
                  :poster="posterOf(genTaskPreviewSrc(row.previewUrl)) || undefined"
                  controls
                  preload="none"
                  playsinline
                />
                <img
                  v-else-if="row.previewUrl"
                  :src="thumbOf(genTaskPreviewSrc(row.previewUrl))"
                  :alt="row.targetLabel"
                  loading="lazy"
                  @error="thumbFallback($event, genTaskPreviewSrc(row.previewUrl))"
                  @click="openImageViewer(genTaskPreviewSrc(row.previewUrl), row.targetLabel)"
                />
                <div v-else class="video-task-empty">
                  <Loader2 v-if="row.status === 'processing'" :size="18" class="animate-spin" />
                  <Clock3 v-else-if="row.status === 'pending'" :size="18" />
                  <svg v-else width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>
                </div>
                <span class="video-task-index">{{ genTaskKindLabel(row.kind) }}</span>
              </div>
              <div class="video-task-main">
                <div class="video-task-line">
                  <strong class="video-task-name truncate">{{ row.targetLabel }}</strong>
                </div>
                <div class="video-task-meta-line">
                  <span class="video-task-loc truncate">{{ row.provider }}{{ row.model ? ' · ' + row.model : '' }}</span>
                  <template v-if="genTaskDuration(row)">
                    <span class="video-task-sep">·</span>
                    <span>{{ t('episode.tasks.duration', { dur: genTaskDuration(row) }) }}</span>
                  </template>
                  <span class="video-task-sep">·</span>
                  <span>#{{ row.id }}</span>
                </div>
                <div v-if="row.errorMsg" :class="['video-task-error', (row.status === 'pending' || row.status === 'processing') && 'is-warning']" :title="row.errorMsg">
                  {{ row.status === 'pending' || row.status === 'processing' ? row.errorMsg : mapError(row.errorMsg) }}
                  <div v-if="row.status === 'failed' && row.kind === 'video' && videoModerationHint(row.errorMsg)" class="video-task-error-hint">{{ videoModerationHint(row.errorMsg) }}</div>
                </div>
              </div>
              <div class="gen-task-actions">
                <span :class="['video-task-status', 'is-' + genTaskStateClass(row.status)]">
                  <span :class="['dot', 'is-' + genTaskStateClass(row.status)]" />
                  {{ genTaskStatusLabel(row) }}
                </span>
                <button
                  v-if="canRecallDoubaoVideo(row)"
                  type="button"
                  class="btn btn-sm gen-task-recall"
                  :disabled="recallingTaskIds.includes(row.id)"
                  @click="recallDoubaoVideo(row)"
                >
                  <RotateCcw :size="10" :class="{ 'animate-spin': recallingTaskIds.includes(row.id) }" />
                  {{ recallingTaskIds.includes(row.id) ? t('episode.tasks.recalling') : t('episode.tasks.recallAction') }}
                </button>
                <button
                  v-if="canCancelVideoTask(row)"
                  type="button"
                  class="btn btn-sm gen-task-cancel"
                  @click="cancelVideoTask(row)"
                >
                  <Square :size="10" fill="currentColor" />
                  {{ t('episode.tasks.cancelAction') }}
                </button>
              </div>
            </div>
          </div>
        </aside>
      </div>

      <div v-if="assetDetail.open && assetDetail.item" class="overlay asset-detail-overlay" @click.self="closeAssetDetail">
        <section
          class="dialog asset-detail-dialog"
          role="dialog"
          aria-modal="true"
          :aria-label="t('episode.asset.detailTitle', { type: assetTypeLabel(assetDetail.type) })"
        >
          <header class="dialog-head asset-detail-head">
            <div class="asset-detail-title-block">
              <span class="asset-detail-kicker">{{ assetTypeLabel(assetDetail.type) }}</span>
              <h2 class="asset-detail-title">{{ assetDetailTitle(assetDetail) }}</h2>
            </div>
            <div class="asset-detail-head-actions">
              <span class="tag" v-if="assetDetail.type === 'character'">{{ assetDetail.item.beauty_positioning || assetDetail.item.beautyPositioning || assetDetail.item.role || t('common.role') }}</span>
              <span class="tag" v-else-if="assetDetail.type === 'prop'">{{ assetDetail.item.type || t('common.prop') }}</span>
              <span class="tag" v-else>{{ assetDetail.item.time || t('episode.asset.noTime') }}</span>
              <button class="btn btn-ghost btn-icon" @click="closeAssetDetail">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
              </button>
            </div>
          </header>

          <div class="dialog-body asset-detail-body">
            <div class="asset-detail-shell">
              <aside
                class="asset-detail-preview-panel"
                :class="{ 'is-asset-drop-active': isAssetDropActive(assetDetail.type, assetDetail.item.id) }"
                @dragenter.prevent.stop="activateAssetImageDrop($event, assetDetail.type, assetDetail.item.id)"
                @dragover.prevent.stop="activateAssetImageDrop($event, assetDetail.type, assetDetail.item.id)"
                @dragleave.stop="deactivateAssetImageDrop($event, assetDetail.type, assetDetail.item.id)"
                @drop.prevent.stop="dropAssetImage($event, assetDetail.type, assetDetail.item.id)"
              >
                <span v-if="isAssetDropActive(assetDetail.type, assetDetail.item.id)" class="asset-drop-overlay">{{ t('episode.upload.dropImage') }}</span>
                <div class="asset-detail-section-title">
                  <span>{{ t('episode.asset.visualPreview') }}</span>
                  <span :class="['asset-detail-state', isAssetImagePending(assetDetail.type, assetDetail.item.id) ? 'is-pending' : (isAssetImageFailed(assetDetail.type, assetDetail.item.id, assetDetail.item) ? 'is-failed' : (assetImageSrc(assetDetail.item) ? 'is-ready' : ''))]">
                    {{ isAssetImagePending(assetDetail.type, assetDetail.item.id) ? t('episode.asset.generating') : (isAssetImageFailed(assetDetail.type, assetDetail.item.id, assetDetail.item) ? t('episode.status.failed') : (assetImageSrc(assetDetail.item) ? t('episode.asset.ready') : t('episode.asset.todo'))) }}
                  </span>
                </div>

                <button
                  type="button"
                  class="asset-detail-media-frame"
                  :disabled="!assetDetailDisplaySrc"
                  @click.stop="openImageViewer(assetDetailDisplaySrc, assetDetailImageTitle(assetDetail))"
                >
                  <img
                    v-if="assetDetailDisplaySrc"
                    :src="thumbOf(assetDetailDisplaySrc)"
                    class="previewable-image"
                    @error="thumbFallback($event, assetDetailDisplaySrc)"
                  />
                  <span v-else class="asset-detail-media-empty">
                    <svg v-if="assetDetail.type === 'character'" width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>
                    <svg v-else-if="assetDetail.type === 'prop'" width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><polyline points="3.27 6.96 12 12.01 20.73 6.96"/><line x1="12" y1="22.08" x2="12" y2="12"/></svg>
                    <svg v-else width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"><path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"/><circle cx="12" cy="10" r="3"/></svg>
                  </span>
                </button>

                <div class="asset-detail-meta-row">
                  <div class="asset-detail-meta-item">
                    <span>{{ t('episode.asset.kindLabel') }}</span>
                    <strong>{{ assetDetail.type === 'character' ? t('episode.asset.charPortrait') : assetDetail.type === 'prop' ? t('common.prop') : t('episode.asset.sceneImage') }}</strong>
                  </div>
                  <div class="asset-detail-meta-item">
                    <span>{{ assetDetail.type === 'character' ? t('episode.asset.roleLabel') : assetDetail.type === 'prop' ? t('episode.asset.propTypeLabel') : t('episode.asset.timeLabel') }}</span>
                    <strong>{{ assetDetail.type === 'character' ? (assetDetail.item.beauty_positioning || assetDetail.item.beautyPositioning || assetDetail.item.role || t('common.role')) : assetDetail.type === 'prop' ? (assetDetail.item.type || t('common.prop')) : (assetDetail.item.time || t('episode.asset.noTime')) }}</strong>
                  </div>
                </div>

                <section class="asset-image-history">
                  <div class="asset-detail-section-title">
                    <span>{{ t('episode.asset.historyCompare') }}</span>
                    <span class="asset-history-count">{{ assetImageHistory.length }}</span>
                  </div>
                  <div v-if="assetHistoryLoading" class="asset-history-empty">
                    <Loader2 :size="14" class="animate-spin" /> {{ t('common.loading') }}
                  </div>
                  <div v-else-if="selectedAssetHistory" class="asset-history-content">
                    <div class="asset-history-compare-grid">
                      <div class="asset-history-compare-item">
                        <span class="asset-history-label">{{ t('episode.asset.currentImage') }}</span>
                        <button
                          type="button"
                          class="asset-history-compare-image"
                          :disabled="!assetImageSrc(assetDetail.item)"
                          @click="openImageViewer(assetImageSrc(assetDetail.item), t('episode.asset.currentImage'))"
                        >
                          <img v-if="assetImageSrc(assetDetail.item)" :src="thumbOf(assetImageSrc(assetDetail.item))" @error="thumbFallback($event, assetImageSrc(assetDetail.item))" />
                          <span v-else>{{ t('episode.asset.noCurrentImage') }}</span>
                        </button>
                      </div>
                      <div class="asset-history-compare-item">
                        <span class="asset-history-label">{{ t('episode.asset.selectedHistory') }}</span>
                        <button
                          type="button"
                          class="asset-history-compare-image"
                          @click="openImageViewer(assetHistorySrc(selectedAssetHistory), t('episode.asset.selectedHistory'))"
                        >
                          <img :src="thumbOf(assetHistorySrc(selectedAssetHistory))" @error="thumbFallback($event, assetHistorySrc(selectedAssetHistory))" />
                        </button>
                      </div>
                    </div>
                    <div class="asset-history-selected-meta">
                      <span>{{ formatHistoryTime(taskCreatedAt(selectedAssetHistory)) }}</span>
                      <span v-if="selectedAssetHistory.model" class="truncate">{{ selectedAssetHistory.model }}</span>
                    </div>
                    <div v-if="assetHistoryPreview" class="asset-history-preview-flag">
                      {{ t('episode.asset.historyPreview') }}
                    </div>
                    <div class="asset-history-actions">
                      <button type="button" class="btn btn-primary btn-sm" @click="setAssetHistoryAsCurrent(selectedAssetHistory)">
                        {{ t('episode.asset.restoreHistory') }}
                      </button>
                      <button v-if="assetHistoryPreview" type="button" class="btn btn-ghost btn-sm" @click="exitAssetHistoryPreview">
                        {{ t('episode.asset.backToCurrent') }}
                      </button>
                      <button
                        type="button"
                        class="btn btn-sm"
                        :disabled="!assetImageSrc(assetDetail.item)"
                        @click="openAssetCompare"
                      >
                        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 3 21 3 21 9"/><polyline points="9 21 3 21 3 15"/><line x1="21" y1="3" x2="14" y2="10"/><line x1="3" y1="21" x2="10" y2="14"/></svg>
                        {{ t('episode.asset.enlargeCompare') }}
                      </button>
                      <button type="button" class="btn btn-danger btn-sm" @click="removeAssetHistory(selectedAssetHistory)">
                        {{ t('common.delete') }}
                      </button>
                    </div>
                    <div class="asset-history-strip">
                      <button
                        v-for="history in assetImageHistory"
                        :key="history.id"
                        type="button"
                        :class="['asset-history-thumb', { selected: history.id === selectedAssetHistoryId }]"
                        :title="formatHistoryTime(taskCreatedAt(history))"
                        @click="selectAssetHistoryImage(history)"
                      >
                        <img :src="thumbOf(assetHistorySrc(history))" @error="thumbFallback($event, assetHistorySrc(history))" />
                      </button>
                    </div>
                  </div>
                  <div v-else class="asset-history-empty">{{ t('episode.asset.noImageHistory') }}</div>
                </section>
              </aside>

              <section class="asset-detail-editor-panel">
                <div class="asset-detail-section-title">
                  <span>{{ t('episode.asset.editInfo') }}</span>
                  <span class="dim">{{ assetDetail.type === 'character' ? t('episode.asset.editHintChar') : assetDetail.type === 'prop' ? t('episode.asset.editHintProp') : t('episode.asset.editHintScene') }}</span>
                </div>

                <div v-if="assetDetail.type !== 'prop'" class="asset-detail-edit-grid">
                  <label class="asset-detail-edit-field">
                    <span>{{ t('episode.asset.nameField') }}</span>
                    <input v-model="assetDetailDraft.name" class="input" :placeholder="assetDetail.type === 'character' ? t('episode.asset.namePlaceholderChar') : t('episode.asset.namePlaceholderScene')" />
                  </label>
                  <label class="asset-detail-edit-field">
                    <span>{{ t('episode.asset.aliasesField') }}</span>
                    <input v-model="assetDetailDraft.aliases" class="input" :placeholder="t('episode.asset.aliasesPlaceholder')" />
                  </label>
                  <label v-if="assetDetail.type === 'character'" class="asset-detail-edit-field">
                    <span>{{ t('episode.asset.beautyPositioningField') }}</span>
                    <input v-model="assetDetailDraft.beautyPositioning" class="input" :placeholder="t('episode.asset.beautyPositioningPlaceholder')" />
                  </label>
                  <label v-if="assetDetail.type === 'character'" class="asset-detail-edit-field">
                    <span>{{ t('episode.asset.characterDescriptionField') }}</span>
                    <textarea
                      v-model="assetDetailDraft.description"
                      class="textarea asset-detail-textarea"
                      rows="7"
                      :placeholder="t('episode.asset.characterDescriptionPlaceholder')"
                    />
                  </label>
                </div>

                <div v-if="assetDetail.type === 'prop'" class="asset-detail-edit-grid asset-detail-edit-grid--prop">
                  <label class="asset-detail-edit-field">
                    <span>{{ t('episode.asset.appearanceOfObject') }}</span>
                    <textarea
                      v-model="assetDetailDraft.description"
                      class="textarea asset-detail-textarea"
                      rows="6"
                      :placeholder="t('episode.asset.appearancePlaceholder')"
                    />
                  </label>
                </div>

                <div v-else :class="['asset-detail-edit-grid', `asset-detail-edit-grid--${assetDetail.type}`]">
                  <label v-if="assetDetail.type === 'character'" class="asset-detail-edit-field">
                    <span>{{ t('episode.asset.appearanceField') }}</span>
                    <textarea
                      v-model="assetDetailDraft.appearance"
                      class="textarea asset-detail-textarea"
                      rows="6"
                      :placeholder="t('episode.asset.appearanceFieldPlaceholder')"
                    />
                  </label>
                  <label v-if="assetDetail.type === 'character'" class="asset-detail-edit-field">
                    <span>{{ t('episode.asset.stylingField') }}</span>
                    <textarea
                      v-model="assetDetailDraft.styling"
                      class="textarea asset-detail-textarea"
                      rows="6"
                      :placeholder="t('episode.asset.stylingFieldPlaceholder')"
                    />
                  </label>
                  <label v-if="assetDetail.type === 'scene'" class="asset-detail-edit-field">
                    <span>{{ t('episode.asset.sceneDescField') }}</span>
                    <textarea
                      v-model="assetDetailDraft.prompt"
                      class="textarea asset-detail-textarea"
                      rows="5"
                      :placeholder="t('episode.asset.sceneDescPlaceholder')"
                    />
                  </label>
                  <label v-if="assetDetail.type === 'scene'" class="asset-detail-edit-field">
                    <span>{{ t('episode.asset.sceneLightField') }}</span>
                    <textarea
                      v-model="assetDetailDraft.lighting"
                      class="textarea asset-detail-textarea"
                      rows="5"
                      :placeholder="t('episode.asset.sceneLightPlaceholder')"
                    />
                  </label>
                </div>

                <!-- 服装/造型状态：底图之外、原文明确换装的造型；定妆图与底图同脸 -->
                <div v-if="assetDetail.type === 'character'" class="asset-costume-panel">
                  <div class="asset-costume-head">
                    <span class="asset-detail-section-title"><span>{{ t('episode.asset.costumePanelTitle') }}</span></span>
                    <button v-if="!costumeCreating" class="btn btn-sm" @click="costumeCreating = true">{{ t('episode.asset.costumeAdd') }}</button>
                  </div>
                  <p class="asset-costume-hint">{{ t('episode.asset.costumePanelHint') }}</p>
                  <div v-for="st in (assetDetail.item.states || [])" :key="st.id" class="asset-costume-row">
                    <div
                      class="asset-costume-thumb"
                      :class="{ 'is-asset-drop-active': isAssetDropActive('costume', st.id) }"
                      @click.stop="(st.image_url || st.imageUrl) && openImageViewer(assetImageSrc(st), `${assetDetail.item.name} · ${st.name}`)"
                      @dragenter.prevent.stop="activateAssetImageDrop($event, 'costume', st.id)"
                      @dragover.prevent.stop="activateAssetImageDrop($event, 'costume', st.id)"
                      @dragleave.stop="deactivateAssetImageDrop($event, 'costume', st.id)"
                      @drop.prevent.stop="dropCostumeImage($event, st)"
                    >
                      <span v-if="isAssetDropActive('costume', st.id)" class="asset-drop-overlay asset-drop-overlay--compact">{{ t('episode.upload.dropImageShort') }}</span>
                      <img v-if="st.image_url || st.imageUrl" :src="thumbOf(assetImageSrc(st))" class="previewable-image" loading="lazy" @error="thumbFallback($event, assetImageSrc(st))" />
                      <span v-else class="asset-costume-thumb-empty">{{ isPendingCostumeImage(assetDetail.item.id, st.id) ? '…' : '＋' }}</span>
                    </div>
                    <div class="asset-costume-main">
                      <input class="input" :value="st.name" :placeholder="t('episode.asset.costumeNamePlaceholder')" @change="saveCostumeField(st, 'name', $event.target.value)" />
                      <textarea class="textarea" rows="2" :value="st.description" :placeholder="t('episode.asset.costumeDescPlaceholder')" @blur="saveCostumeField(st, 'description', $event.target.value)" />
                    </div>
                    <div class="asset-costume-actions">
                      <button class="btn btn-sm" :disabled="isPendingCostumeImage(assetDetail.item.id, st.id)" @click="genCostumeImg(st)">
                        <Loader2 v-if="isPendingCostumeImage(assetDetail.item.id, st.id)" :size="11" class="animate-spin" />
                        {{ (st.image_url || st.imageUrl) ? t('episode.asset.regen') : t('episode.asset.generate') }}
                      </button>
                      <button class="btn btn-sm" :disabled="isUploadingAsset('costume', st.id)" @click="uploadCostumeImg(st)">{{ t('episode.asset.upload') }}</button>
                      <button class="btn btn-sm" @click="deleteCostume(st)">{{ t('common.delete') }}</button>
                    </div>
                  </div>
                  <div v-if="costumeCreating" class="asset-costume-row is-creating">
                    <div class="asset-costume-main">
                      <input v-model="costumeDraft.name" class="input" :placeholder="t('episode.asset.costumeNamePlaceholder')" />
                      <textarea v-model="costumeDraft.description" class="textarea" rows="2" :placeholder="t('episode.asset.costumeDescPlaceholder')" />
                    </div>
                    <div class="asset-costume-actions">
                      <button class="btn btn-sm btn-primary" :disabled="costumeSaving || !costumeDraft.name.trim()" @click="createCostume">{{ t('common.save') }}</button>
                      <button class="btn btn-sm" @click="costumeCreating = false">{{ t('common.cancel') }}</button>
                    </div>
                  </div>
                </div>

              </section>
            </div>

            <section class="asset-detail-prompt-panel">
              <div class="asset-detail-section-title">
                <span>{{ assetDetail.type === 'character' ? t('episode.asset.finalPromptTurnaround') : assetDetail.type === 'scene' ? t('episode.asset.finalPromptFixed') : t('episode.asset.finalPromptWhiteBg') }}</span>
                <div class="asset-detail-prompt-head-actions">
                  <button
                    class="btn btn-sm"
                    :disabled="isGeneratingPrompt(assetDetail.type, assetDetail.item.id) || isAssetImagePending(assetDetail.type, assetDetail.item.id)"
                    @click="genAssetFinalPrompt"
                  >
                    <Loader2 v-if="isGeneratingPrompt(assetDetail.type, assetDetail.item.id)" :size="11" class="animate-spin" />
                    {{ isGeneratingPrompt(assetDetail.type, assetDetail.item.id) ? t('episode.asset.generating') : (assetFinalPrompt ? t('episode.asset.regenPrompt') : t('episode.asset.genPrompt')) }}
                  </button>
                  <span :class="['asset-detail-state', assetFinalPrompt && (assetPromptStale(assetDetail.item) ? 'is-stale' : 'is-ready')]">
                    {{ assetFinalPrompt ? (assetPromptStale(assetDetail.item) ? t('episode.asset.promptStale') : t('episode.asset.ready')) : t('episode.asset.todo') }}
                  </span>
                  <button
                    v-if="assetPromptDraft"
                    class="btn btn-ghost btn-sm asset-detail-copy-btn"
                    @click="copyAssetFinalPrompt"
                  >
                    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
                    {{ t('common.copy') }}
                  </button>
                </div>
              </div>
              <!-- Agent 修改待确认：最终提示词框内直接高亮增删；否则显示普通编辑框 -->
              <PromptDiffView
                v-if="assetAgentCandidate"
                :segments="assetAgentCandidate.segments"
                class="textarea asset-detail-prompt-textarea"
              />
              <textarea
                v-else
                :value="assetPromptDraft"
                @input="onAssetPromptInput"
                class="textarea asset-detail-prompt-textarea"
                rows="5"
                :placeholder="assetDetail.type === 'character'
                  ? t('episode.asset.promptPlaceholderChar')
                  : assetDetail.type === 'scene'
                    ? t('episode.asset.promptPlaceholderScene')
                    : t('episode.asset.promptPlaceholderProp')"
              />
              <p class="asset-detail-prompt-hint">
                {{ assetDetail.type === 'character'
                  ? t('episode.asset.promptHintChar')
                  : assetDetail.type === 'scene'
                    ? t('episode.asset.promptHintScene')
                    : t('episode.asset.promptHintProp') }}
              </p>
              <PromptAgentEditor
                :drama-id="dramaId"
                :episode-id="epId"
                :target-type="assetDetail.type"
                :target-id="assetDetail.item.id"
                :current-prompt="assetFinalPrompt"
                :model="chatModelOverride()"
                :config-id="chatConfigId()"
                @candidate="assetAgentCandidate = $event"
                @cleared="assetAgentCandidate = null"
                @applied="onAssetAgentApplied"
              />
            </section>
          </div>

          <footer class="dialog-foot asset-detail-foot">
            <div class="asset-detail-secondary-actions">
              <button class="btn btn-danger" @click="askDeleteAsset(assetDetail.type, assetDetail.item)">{{ t('common.delete') }}</button>
              <button class="btn" @click="closeAssetDetail">{{ t('common.close') }}</button>
            </div>
            <div class="asset-detail-primary-actions">
              <button
                class="btn"
                :disabled="isUploadingAsset(assetDetail.type, assetDetail.item.id)"
                @click="uploadAssetImage(assetDetail.type, assetDetail.item.id)"
              >
                <Loader2 v-if="isUploadingAsset(assetDetail.type, assetDetail.item.id)" :size="11" class="animate-spin" />
                <svg v-else width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
                {{ t('episode.asset.uploadImage') }}
              </button>
              <button
                v-if="assetDetail.type === 'character'"
                class="btn"
                :disabled="isPendingCharImage(assetDetail.item.id)"
                @click="genCharImg(assetDetail.item.id)"
              >
                {{ assetImageSrc(assetDetail.item) ? t('episode.asset.regenPortrait') : (isPendingCharImage(assetDetail.item.id) ? t('episode.asset.generating') : t('episode.asset.genPortrait')) }}
              </button>
              <button
                v-else-if="assetDetail.type === 'scene'"
                class="btn"
                :disabled="isPendingSceneImage(assetDetail.item.id)"
                @click="genSceneImg(assetDetail.item.id)"
              >
                {{ assetImageSrc(assetDetail.item) ? t('episode.asset.regenScene') : (isPendingSceneImage(assetDetail.item.id) ? t('episode.asset.generating') : t('episode.asset.genScene')) }}
              </button>
              <button
                v-else-if="assetDetail.type === 'prop'"
                class="btn"
                :disabled="isPendingPropImage(assetDetail.item.id)"
                @click="genPropImg(assetDetail.item.id)"
              >
                {{ assetImageSrc(assetDetail.item) ? t('episode.asset.regenProp') : (isPendingPropImage(assetDetail.item.id) ? t('episode.asset.generating') : t('episode.asset.genProp')) }}
              </button>
              <button class="btn btn-primary" :disabled="savingAssetDetail" @click="saveAssetDetail">
                <Loader2 v-if="savingAssetDetail" :size="12" class="animate-spin" />
                {{ t('episode.asset.saveChanges') }}
              </button>
            </div>
          </footer>
        </section>
      </div>

      <div v-if="assetCompareOpen && selectedAssetHistory" class="overlay asset-compare-overlay" @click.self="closeAssetCompare">
        <section class="dialog asset-compare-dialog" role="dialog" aria-modal="true" :aria-label="t('episode.asset.enlargedCompareTitle')">
          <header class="image-viewer-head">
            <div class="image-viewer-title">{{ t('episode.asset.enlargedCompareTitle') }}</div>
            <button class="btn btn-ghost btn-icon" @click="closeAssetCompare">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
            </button>
          </header>
          <div class="asset-compare-body">
            <figure class="asset-compare-figure">
              <figcaption>
                <strong>{{ t('episode.asset.currentImage') }}</strong>
              </figcaption>
              <div class="asset-compare-stage">
                <img :src="assetImageSrc(assetDetail.item)" :alt="t('episode.asset.currentImage')" />
              </div>
            </figure>
            <figure class="asset-compare-figure">
              <figcaption>
                <strong>{{ t('episode.asset.selectedHistory') }}</strong>
                <span>{{ formatHistoryTime(taskCreatedAt(selectedAssetHistory)) }}</span>
              </figcaption>
              <div class="asset-compare-stage">
                <img :src="assetHistorySrc(selectedAssetHistory)" :alt="t('episode.asset.selectedHistory')" />
              </div>
            </figure>
          </div>
        </section>
      </div>

      <div v-if="imageViewer.open && imageViewer.src" class="overlay image-viewer-overlay" @click.self="closeImageViewer">
        <div class="dialog image-viewer-dialog">
          <div class="image-viewer-head">
            <div class="image-viewer-title">{{ imageViewer.title || t('episode.viewer.imagePreview') }}</div>
            <button class="btn btn-ghost btn-icon" @click="closeImageViewer">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
            </button>
          </div>
          <div class="image-viewer-body">
            <img :src="imageViewer.src" :alt="imageViewer.title || t('episode.viewer.imagePreview')" class="image-viewer-img" />
          </div>
        </div>
      </div>

      <div v-if="previewShot" class="overlay image-viewer-overlay" @click.self="previewShot = null">
        <div class="dialog image-viewer-dialog merge-viewer-dialog">
          <div class="image-viewer-head">
            <div class="image-viewer-title">{{ t('episode.export.shotPreview', { n: shotNumberOf(previewShot) }) }}</div>
            <span v-if="previewShot.duration" class="dim" style="font-size:.6875rem">{{ previewShot.duration }}s</span>
            <a :href="'/' + getVideoUrl(previewShot)" download class="btn btn-sm" style="margin-left:auto">
              <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
              {{ t('common.download') }}
            </a>
            <button class="btn btn-ghost btn-icon" @click="previewShot = null">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
            </button>
          </div>
          <div class="merge-viewer-body">
            <video
              :key="previewShot.id"
              :src="'/' + getVideoUrl(previewShot)"
              controls
              autoplay
              playsinline
              class="merge-viewer-video"
            />
          </div>
        </div>
      </div>

      <div v-if="activeMerge" class="overlay image-viewer-overlay" @click.self="activeMerge = null">
        <div class="dialog image-viewer-dialog merge-viewer-dialog">
          <div class="image-viewer-head">
            <div class="image-viewer-title">{{ t('episode.viewer.filmPreview') }}</div>
            <span class="dim" style="font-size:.6875rem">{{ formatHistoryTime(activeMerge.created_at) }}<template v-if="activeMerge.duration"> · {{ activeMerge.duration }}s</template></span>
            <a :href="'/' + activeMerge.merged_url" download class="btn btn-sm" style="margin-left:auto">
              <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
              {{ t('episode.viewer.downloadFilm') }}
            </a>
            <button class="btn btn-ghost btn-icon" @click="activeMerge = null">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
            </button>
          </div>
          <div class="merge-viewer-body">
            <video
              :key="activeMerge.id"
              :src="'/' + activeMerge.merged_url"
              controls
              autoplay
              playsinline
              class="merge-viewer-video"
            />
          </div>
        </div>
      </div>

      <div v-if="assetCreate.open" class="overlay" @click.self="assetCreate.open = false">
        <div class="dialog asset-create-dialog">
          <header class="dialog-head">
            <h2 class="dialog-title">{{ t('episode.create.title', { type: assetCreateTypeLabel }) }}</h2>
            <button class="btn btn-ghost btn-icon" @click="assetCreate.open = false">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
            </button>
          </header>
          <div class="dialog-body asset-create-body">
            <template v-if="assetCreate.type === 'character'">
              <label class="field"><span class="field-label">{{ t('episode.create.name') }}</span><input v-model="assetCreateDraft.name" class="input" :placeholder="t('episode.create.namePlaceholderChar')" /></label>
              <label class="field"><span class="field-label">{{ t('episode.asset.aliasesField') }}</span><input v-model="assetCreateDraft.aliases" class="input" :placeholder="t('episode.asset.aliasesPlaceholder')" /></label>
              <label class="field"><span class="field-label">{{ t('episode.asset.beautyPositioningField') }}</span><input v-model="assetCreateDraft.beautyPositioning" class="input" :placeholder="t('episode.asset.beautyPositioningPlaceholder')" /></label>
              <label class="field"><span class="field-label">{{ t('episode.asset.characterDescriptionField') }}</span><textarea v-model="assetCreateDraft.description" class="textarea" rows="5" :placeholder="t('episode.asset.characterDescriptionPlaceholder')" /></label>
              <label class="field"><span class="field-label">{{ t('episode.create.roleField') }}</span><input v-model="assetCreateDraft.role" class="input" :placeholder="t('episode.create.rolePlaceholder')" /></label>
              <label class="field"><span class="field-label">{{ t('episode.asset.appearanceField') }}</span><textarea v-model="assetCreateDraft.appearance" class="textarea" rows="3" :placeholder="t('episode.create.appearancePlaceholder')" /></label>
              <label class="field"><span class="field-label">{{ t('episode.asset.stylingField') }}</span><textarea v-model="assetCreateDraft.styling" class="textarea" rows="2" :placeholder="t('episode.create.stylingPlaceholder')" /></label>
            </template>
            <template v-else-if="assetCreate.type === 'scene'">
              <label class="field"><span class="field-label">{{ t('episode.create.location') }}</span><input v-model="assetCreateDraft.location" class="input" :placeholder="t('episode.create.locationPlaceholder')" /></label>
              <label class="field"><span class="field-label">{{ t('episode.asset.aliasesField') }}</span><input v-model="assetCreateDraft.aliases" class="input" :placeholder="t('episode.asset.aliasesPlaceholder')" /></label>
              <label class="field"><span class="field-label">{{ t('episode.create.time') }}</span><input v-model="assetCreateDraft.time" class="input" :placeholder="t('episode.create.timePlaceholder')" /></label>
              <label class="field"><span class="field-label">{{ t('episode.asset.sceneDescField') }}</span><textarea v-model="assetCreateDraft.prompt" class="textarea" rows="3" :placeholder="t('episode.create.sceneDescPlaceholder')" /></label>
              <label class="field"><span class="field-label">{{ t('episode.asset.sceneLightField') }}</span><input v-model="assetCreateDraft.lighting" class="input" :placeholder="t('episode.create.lightPlaceholder')" /></label>
            </template>
            <template v-else>
              <label class="field"><span class="field-label">{{ t('episode.create.name') }}</span><input v-model="assetCreateDraft.name" class="input" :placeholder="t('episode.create.namePlaceholderProp')" /></label>
              <label class="field"><span class="field-label">{{ t('episode.create.typeField') }}</span><input v-model="assetCreateDraft.type" class="input" :placeholder="t('episode.create.typePlaceholder')" /></label>
              <label class="field"><span class="field-label">{{ t('episode.asset.appearanceOfObject') }}</span><textarea v-model="assetCreateDraft.description" class="textarea" rows="3" :placeholder="t('episode.create.appearanceOnlyPlaceholder')" /></label>
            </template>
          </div>
          <footer class="dialog-foot">
            <button class="btn" @click="assetCreate.open = false">{{ t('common.cancel') }}</button>
            <button class="btn btn-primary" :disabled="assetCreate.saving" @click="saveAssetCreate">
              <Loader2 v-if="assetCreate.saving" :size="12" class="animate-spin" />
              {{ t('common.add') }}
            </button>
          </footer>
        </div>
      </div>

      <!-- 导入项目资产：把其他集已创建的资产引用到本集 -->
      <div v-if="assetImport.open" class="overlay" @click.self="assetImport.open = false">
        <div class="dialog asset-import-dialog">
          <header class="dialog-head">
            <h2 class="dialog-title">{{ t('episode.import.title') }}</h2>
            <button class="btn btn-ghost btn-icon" @click="assetImport.open = false">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
            </button>
          </header>
          <div class="dialog-body asset-import-body">
            <div v-if="assetImport.loading" class="asset-import-loading">
              <Loader2 :size="18" class="animate-spin" style="color:var(--accent)" />
            </div>
            <template v-else>
              <div v-if="!assetImportCandidates.length" class="asset-import-empty">{{ t('episode.import.empty') }}</div>
              <section v-for="sec in assetImportSections" :key="sec.kind" v-show="sec.items.length" class="asset-import-section">
                <h3 class="asset-import-section-title">{{ sec.label }}<span class="asset-import-count">{{ sec.items.length }}</span></h3>
                <label v-for="item in sec.items" :key="sec.kind + '-' + item.id" class="asset-import-row">
                  <input type="checkbox" v-model="assetImportSel[sec.kind]" :value="item.id" />
                  <span class="asset-import-thumb">
                    <img v-if="item.img" :src="item.img" loading="lazy" :alt="item.name" @error="$event.target.style.display = 'none'" />
                  </span>
                  <span class="asset-import-name">{{ item.name }}</span>
                  <span v-if="item.sub" class="asset-import-sub dim">{{ item.sub }}</span>
                </label>
              </section>
            </template>
          </div>
          <footer class="dialog-foot">
            <button class="btn" @click="assetImport.open = false">{{ t('common.cancel') }}</button>
            <button class="btn btn-primary" :disabled="assetImport.saving || !assetImportCount" @click="saveAssetImport">
              <Loader2 v-if="assetImport.saving" :size="12" class="animate-spin" />
              {{ assetImportCount ? t('episode.import.submit', { n: assetImportCount }) : t('episode.import.none') }}
            </button>
          </footer>
        </div>
      </div>

      <!-- 自选资产导出：人物、场景、道具均可逐项勾选 -->
      <div v-if="assetExport.open" class="overlay" @click.self="assetExport.open = false">
        <div class="dialog asset-export-dialog">
          <header class="dialog-head">
            <div>
              <h2 class="dialog-title">{{ t('episode.prod.exportSelectTitle') }}</h2>
              <p class="asset-export-hint">{{ t('episode.prod.exportSelectHint') }}</p>
            </div>
            <button class="btn btn-ghost btn-icon" :title="t('common.close')" :aria-label="t('common.close')" @click="assetExport.open = false">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
            </button>
          </header>
          <div class="asset-export-tools">
            <span>{{ t('episode.prod.exportSelectedCount', { n: assetExportCount }) }}</span>
            <div class="asset-export-tool-actions">
              <button type="button" class="btn btn-ghost btn-sm" @click="selectAllAssetExports">{{ t('episode.prod.exportSelectAll') }}</button>
              <button type="button" class="btn btn-ghost btn-sm" :disabled="!assetExportCount" @click="clearAssetExports">{{ t('episode.prod.exportClear') }}</button>
            </div>
          </div>
          <div class="dialog-body asset-export-body">
            <section v-for="sec in assetExportSections" :key="sec.kind" v-show="sec.items.length" class="asset-export-section">
              <div class="asset-export-section-head">
                <h3 class="asset-export-section-title">
                  {{ sec.label }}
                  <span class="asset-import-count">{{ selectedAssetExportCount(sec.kind) }}/{{ sec.items.length }}</span>
                </h3>
                <button type="button" class="asset-export-toggle" @click="toggleAssetExportSection(sec)">
                  {{ isAssetExportSectionSelected(sec) ? t('episode.prod.exportClearType') : t('episode.prod.exportSelectType') }}
                </button>
              </div>
              <div class="asset-export-grid">
                <label
                  v-for="item in sec.items"
                  :key="sec.kind + '-' + item.id"
                  :class="['asset-export-card', { selected: assetExportSel[sec.kind].includes(item.id) }]"
                >
                  <input type="checkbox" v-model="assetExportSel[sec.kind]" :value="item.id" />
                  <span class="asset-export-thumb">
                    <img v-if="item.img" :src="item.img" loading="lazy" :alt="item.name" @error="$event.target.style.display = 'none'" />
                  </span>
                  <span class="asset-export-card-copy">
                    <strong>{{ item.name }}</strong>
                    <span v-if="item.sub">{{ item.sub }}</span>
                  </span>
                </label>
              </div>
            </section>
          </div>
          <footer class="dialog-foot asset-export-footer">
            <button class="btn" @click="assetExport.open = false">{{ t('common.cancel') }}</button>
            <div class="asset-export-submit-actions">
              <button class="btn btn-primary" :disabled="exportingAssets || !assetExportCharacterCount" @click="exportAssetsJson('characters')">
                {{ t('episode.prod.exportCharacters', { n: assetExportCharacterCount }) }}
              </button>
              <button class="btn btn-primary" :disabled="exportingAssets || !assetExportScenePropCount" @click="exportAssetsJson('scene-props')">
                {{ t('episode.prod.exportSceneProps', { n: assetExportScenePropCount }) }}
              </button>
            </div>
          </footer>
        </div>
      </div>

      <div v-if="batchVideoConfirm.open" class="overlay" @click.self="batchVideoConfirm.open = false">
        <div class="dialog batch-video-dialog">
          <header class="dialog-head">
            <h2 class="dialog-title">{{ batchVideoConfirm.mode === 'episode' ? t('episode.vid.fullPipelineConfirmTitle') : t('episode.vid.confirmTitle') }}</h2>
            <button class="btn btn-ghost btn-icon" @click="batchVideoConfirm.open = false">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
            </button>
          </header>
          <div class="dialog-body batch-video-body">
            <div class="batch-video-row"><span>{{ t('episode.vid.confirmShots') }}</span><strong>{{ t('episode.vid.confirmShotsValue', { n: batchVideoConfirm.targets.length }) }}</strong></div>
            <div v-if="batchVideoConfirm.mode === 'episode'" class="batch-video-row"><span>{{ t('episode.vid.fullPipelinePrompts') }}</span><strong>{{ t('episode.vid.fullPipelinePromptsValue', { n: batchVideoConfirm.missingPromptIds.length }) }}</strong></div>
            <div class="batch-video-row"><span>{{ t('episode.vid.confirmTotal') }}</span><strong>{{ t('episode.vid.confirmApprox', { n: batchVideoTotalDuration }) }}</strong></div>
            <div class="batch-video-row"><span>{{ t('episode.vid.confirmModel') }}</span><strong>{{ effectiveVideoModelLabel || t('episode.vid.defaultModel') }}</strong></div>
            <div v-if="!isDoubaoLocalVideo" class="batch-video-row"><span>{{ t('episode.vid.confirmResolution') }}</span><strong>{{ episodeResolutionLabel }}</strong></div>
            <p class="batch-video-note">{{ batchVideoConfirm.mode === 'episode' ? t('episode.vid.fullPipelineConfirmNote') : t('episode.vid.confirmNote') }}</p>
          </div>
          <footer class="dialog-foot">
            <button class="btn" @click="batchVideoConfirm.open = false">{{ t('common.cancel') }}</button>
            <button class="btn btn-primary" @click="confirmBatchVideos">
              {{ batchVideoConfirm.mode === 'episode'
                ? t('episode.vid.fullPipelineConfirmStart', { n: batchVideoConfirm.targets.length })
                : t('episode.vid.confirmStart', { n: batchVideoConfirm.targets.length }) }}
            </button>
          </footer>
        </div>
      </div>

      <ConfirmDialog
        :open="oneClickStoryboardVideo.confirmOpen"
        :title="t('episode.breakdown.oneClickConfirmTitle')"
        :message="oneClickStoryboardVideoConfirmMessage"
        :confirm-text="t('episode.breakdown.oneClickConfirmStart')"
        variant="primary"
        @confirm="startOneClickStoryboardVideo"
        @cancel="oneClickStoryboardVideo.confirmOpen = false"
      />

      <ConfirmDialog
        :open="assetDelete.open"
        :title="t('episode.delete.title', { type: assetDeleteTypeLabel })"
        :message="t('episode.delete.message', { type: assetDeleteTypeLabel, name: assetDeleteName })"
        :loading="assetDelete.loading"
        @confirm="confirmDeleteAsset"
        @cancel="assetDelete.open = false"
      />

      <ConfirmDialog
        :open="mergeDelete.open"
        :title="t('episode.export.deleteFilm')"
        :message="t('episode.export.deleteFilmConfirm', { time: formatHistoryTime(mergeDelete.item?.created_at || '') })"
        :loading="mergeDelete.loading"
        @confirm="confirmDeleteMerge"
        @cancel="mergeDelete.open = false"
      />

      <ConfirmDialog
        :open="videoDelete.open"
        :title="t('episode.vid.deleteRecord')"
        :message="t('episode.vid.deleteRecordConfirm', { time: formatHistoryTime(taskCreatedAt(videoDelete.item)) })"
        :loading="videoDelete.loading"
        @confirm="confirmDeleteHistoryVideo"
        @cancel="videoDelete.open = false"
      />

    </main>
    </div>
  </div>
</template>

<script setup>
import { toast } from 'vue-sonner'
import { useI18n } from 'vue-i18n'
import {
  Users, FileText, FolderKanban, Clapperboard, Download, Loader2,
  Plus, X, ListTodo, CircleHelp, Clock3, Trash2, Square, Shuffle, FolderDown, AlertTriangle, Wrench,
  SlidersHorizontal, Sparkles, RotateCcw,
} from 'lucide-vue-next'
import NovelChapterSourcePicker from '~/components/NovelChapterSourcePicker.vue'
import { api, dramaAPI, episodeAPI, storyboardAPI, storyboardReviewAPI, agentRuntimeAPI, characterAPI, sceneAPI, propAPI, taskAPI, mergeAPI, aiConfigAPI, uploadAPI, stylePresetAPI } from '~/composables/useApi'
import { startTour, stopTour } from '~/composables/useTour'
import { useAgent } from '~/composables/useAgent'
import { toastError, mapError, MODERATION_RE } from '~/composables/useToast'
import { isActiveVideoTask, selectActiveVideoTasksByStoryboard, selectEffectiveVideoTasksByStoryboard, videoTaskStatusById } from '~/utils/videoTasks'

definePageMeta({ layout: 'studio' })

const { t } = useI18n()

const route = useRoute()
const router = useRouter()
const dramaId = Number(route.params.id)
const episodeNumber = Number(route.params.episodeNumber)
async function backToProject() {
  const target = `/drama/${dramaId}`
  stopTour()
  try {
    await router.push(target)
  } catch {
    // 只有客户端路由明确失败时才硬导航，避免慢速异步路由被计时器二次刷新。
    window.location.assign(target)
  }
}
const drama = useDramaDetailCache(dramaId)
const episode = ref(null), chars = ref([]), scenes = ref([]), propItems = ref([]), sbs = ref([]), mergeData = ref(null)
// 工作台面板位置记忆（按剧集隔离）：仅页面刷新(reload)时恢复到上次所在步骤；
// 从列表/详情页点击进入时始终默认「剧本」面板
const PANEL_STORE_KEY = `h3:workbench:panel:${dramaId}:${episodeNumber}`
const isPageReload = (() => {
  try { return performance.getEntriesByType('navigation')[0]?.type === 'reload' } catch { return false }
})()
const storedPanel = (() => {
  if (!isPageReload) return null
  try { return JSON.parse(localStorage.getItem(PANEL_STORE_KEY) || 'null') } catch { return null }
})()
// 首个 refresh 时若已恢复面板位置，跳过按内容自动重置 scriptStep
let panelRestored = !!storedPanel
const panel = ref(['production', 'export'].includes(storedPanel?.panel) ? storedPanel.panel : 'script')
const { running: rn, runningType: rt, run: runAgent } = useAgent()

// 分镜拆解引擎切换：V1=现行分镜拆解，V8=AI影视分镜双流编译器（15秒视频编号 + 逐字台词 + 四字段镜头方案）
const BREAKDOWN_AGENTS = [
  { type: 'storyboard_v8', label: 'episode.breakdown.agentV8', hint: 'episode.breakdown.agentV8Hint' },
  { type: 'storyboard_breaker', label: 'episode.breakdown.agentV1', hint: 'episode.breakdown.agentV1Hint' },
]
const BREAKDOWN_AGENT_STORE_KEY = 'h3-breakdown-agent'
const DEFAULT_BREAKDOWN_AGENT = 'storyboard_v8'
const breakdownAgent = ref((() => {
  try {
    const v = localStorage.getItem(BREAKDOWN_AGENT_STORE_KEY)
    return BREAKDOWN_AGENTS.some(a => a.type === v) ? v : DEFAULT_BREAKDOWN_AGENT
  } catch { return DEFAULT_BREAKDOWN_AGENT }
})())
function setBreakdownAgent(type) {
  breakdownAgent.value = type
  try { localStorage.setItem(BREAKDOWN_AGENT_STORE_KEY, type) } catch { /* ignore */ }
}
const claudeRuntime = reactive({ loading: true, enabled: false, available: false, authenticated: false, version: '', error: '' })
const claudeRuntimeAvailable = computed(() => claudeRuntime.enabled && claudeRuntime.available && claudeRuntime.authenticated)
const claudeSelectedUnavailable = computed(() => !claudeRuntime.loading && !claudeRuntimeAvailable.value)
const claudeRuntimeTitle = computed(() => claudeRuntimeAvailable.value
  ? t('episode.breakdown.claudeAvailable', { version: claudeRuntime.version || 'Claude CLI' })
  : (claudeRuntime.error || t('episode.breakdown.claudeUnavailable')))
async function loadClaudeRuntime(refresh = false) {
  claudeRuntime.loading = true
  try {
    const status = await agentRuntimeAPI.claude(refresh)
    Object.assign(claudeRuntime, status || {}, { loading: false, error: status?.error || '' })
  } catch (error) {
    Object.assign(claudeRuntime, { loading: false, enabled: false, available: false, authenticated: false, error: mapError(error) })
  }
}
function selectedV8Engine() {
  if (!claudeRuntimeAvailable.value) {
    toast.warning(claudeRuntimeTitle.value)
    return null
  }
  return 'claude_cli'
}
const breakdownRunning = computed(() => rt.value === 'storyboard_breaker' || rt.value === 'storyboard_v8')
const breakdownProgress = ref({
  savedCount: 0,
  savedDuration: 0,
  elapsedSeconds: 0,
  lastSavedAt: '',
  localStartedAt: 0,
  queueStatus: '',
  queuePosition: 0,
})
const latestStoryboardRun = ref(null)
// 检查修复暂停使用（临时下线）：恢复时改回 false
const AUTOFIX_PAUSED = true
const storyboardReview = reactive({
  running: false,
  error: '',
  notice: '',
})
const storyboardReviewProgress = reactive({
  phase: 'reading',
  processedCount: 0,
  totalCount: 0,
  candidateCount: 0,
  replacementCount: 0,
  lastRange: '',
  elapsedSeconds: 0,
  localStartedAt: 0,
  queueStatus: '',
  queuePosition: 0,
})
const storyboardReviewProgressText = computed(() => {
  if (storyboardReviewProgress.phase === 'publishing') return t('episode.breakdown.autofixProgressPublishing')
  if (storyboardReviewProgress.phase === 'validating') return t('episode.breakdown.autofixProgressValidating', {
    candidate: storyboardReviewProgress.candidateCount,
  })
  if (storyboardReviewProgress.phase === 'repairing') return t('episode.breakdown.autofixProgressRepairing', {
    processed: storyboardReviewProgress.processedCount,
    total: storyboardReviewProgress.totalCount,
    candidate: storyboardReviewProgress.candidateCount,
  })
  return t('episode.breakdown.autofixProgressReading')
})
const storyboardReviewProgressPercent = computed(() => {
  if (storyboardReviewProgress.phase === 'publishing') return 98
  if (storyboardReviewProgress.phase === 'validating') return 92
  if (storyboardReviewProgress.phase === 'repairing') {
    const total = Math.max(1, storyboardReviewProgress.totalCount)
    return Math.min(88, 10 + Math.round((storyboardReviewProgress.processedCount / total) * 78))
  }
  return 6
})
const storyboardReviewBusy = computed(() => storyboardReview.running)
const v8ReviewAvailable = computed(() =>
  sbs.value.length > 0
  && String(latestStoryboardRun.value?.agent_type || '') === 'storyboard_v8'
  && String(latestStoryboardRun.value?.status || '') === 'completed')
const latestStoryboardFailed = computed(() =>
  ['failed', 'interrupted'].includes(String(latestStoryboardRun.value?.status || '')))
const latestStoryboardFailureReason = computed(() => {
  const reason = String(latestStoryboardRun.value?.error_msg || '').trim()
  if (!reason) return t('episode.breakdown.recentFailedUnknown')
  return reason.length > 260 ? `${reason.slice(0, 260)}…` : reason
})
const breakdownResultsVisible = computed(() => !breakdownRunning.value || breakdownProgress.value.savedCount > 0)
// 最近一次成功拆解的完成时间与耗时（storyboard_runs 的 started_at/completed_at），拆解区静态展示
const lastBreakdownRunInfo = computed(() => {
  const run = latestStoryboardRun.value
  if (String(run?.status || '') !== 'completed') return null
  const started = Date.parse(String(run?.started_at || ''))
  const completed = Date.parse(String(run?.completed_at || ''))
  if (!Number.isFinite(started) || !Number.isFinite(completed) || completed <= started) return null
  return { time: formatHistoryTime(run.completed_at), seconds: Math.round((completed - started) / 1000) }
})
let breakdownProgressTimer = null

function applyLatestStoryboardRun(response) {
  latestStoryboardRun.value = response?.latest_storyboard_run || null
}

function formatBreakdownElapsed(totalSeconds) {
  const seconds = Math.max(0, Number(totalSeconds) || 0)
  const minutes = Math.floor(seconds / 60)
  return `${String(minutes).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`
}

function applyStoryboardRows(rows) {
  sbs.value = Array.isArray(rows) ? rows : []
  selectedVideoSbIds.value = selectedVideoSbIds.value.filter(sid => sbs.value.some(sb => sb.id === sid))
  if (sbs.value.length) {
    const currentSelectedId = selectedSb.value?.id
    selectedSb.value = sbs.value.find(sb => sb.id === currentSelectedId) || sbs.value[0]
  } else {
    selectedSb.value = null
  }
}

function stopBreakdownProgressPolling() {
  if (breakdownProgressTimer) clearTimeout(breakdownProgressTimer)
  breakdownProgressTimer = null
}

async function pollBreakdownProgress() {
  if (!breakdownRunning.value || !epId.value) return
  const previous = breakdownProgress.value
  let next = {
    ...previous,
    elapsedSeconds: previous.localStartedAt
      ? Math.floor((Date.now() - previous.localStartedAt) / 1000)
      : previous.elapsedSeconds,
  }
  try {
    const res = await episodeAPI.agentRunning(epId.value)
    applyLatestStoryboardRun(res)
    const task = (Array.isArray(res?.tasks) ? res.tasks : [])
      .find(item => item?.type === 'storyboard_breaker' || item?.type === 'storyboard_v8')
    if (task) {
      const savedCount = Number(task.saved_storyboard_count || 0)
      const lastSavedAt = String(task.last_saved_at || '')
      const batchChanged = savedCount > 0 && (
        savedCount !== previous.savedCount || lastSavedAt !== previous.lastSavedAt
      )
      let batchLoaded = true
      if (batchChanged) {
        try {
          applyStoryboardRows(await episodeAPI.storyboardDraft(epId.value))
        } catch {
          batchLoaded = false
        }
      }
      next = {
        ...next,
        savedCount: batchLoaded ? savedCount : previous.savedCount,
        savedDuration: batchLoaded ? Number(task.saved_duration || 0) : previous.savedDuration,
        elapsedSeconds: Number(task.elapsed_seconds || 0),
        lastSavedAt: batchLoaded ? lastSavedAt : previous.lastSavedAt,
        queueStatus: String(task.queue_status || ''),
        queuePosition: Number(task.queue_position || 0),
      }
    }
  } catch { /* 下一轮继续同步 */ }
  breakdownProgress.value = next
  if (breakdownRunning.value) breakdownProgressTimer = setTimeout(pollBreakdownProgress, 2000)
}

function startBreakdownProgressPolling() {
  stopBreakdownProgressPolling()
  breakdownProgress.value = {
    savedCount: 0,
    savedDuration: 0,
    elapsedSeconds: 0,
    lastSavedAt: '',
    localStartedAt: Date.now(),
    queueStatus: '',
    queuePosition: 0,
  }
  pollBreakdownProgress()
}

watch(breakdownRunning, async active => {
  if (active) startBreakdownProgressPolling()
  else {
    stopBreakdownProgressPolling()
    // 成功时正式分镜刚完成事务发布；失败时草稿已被删除。两种情况都重新
    // 读取正式表，避免进度预览中的 draft 临时 ID 残留在工作台。
    if (epId.value) {
      try { applyStoryboardRows(await episodeAPI.storyboards(epId.value)) } catch { /* refresh/onDone 会继续兜底 */ }
      try { applyLatestStoryboardRun(await episodeAPI.agentRunning(epId.value)) } catch { /* 下一次刷新继续同步 */ }
    }
  }
})

const localRaw = ref(''), localScript = ref('')
// AI 改写右侧栏（参考逐集改编工作台的上下文检查器）：用户给 Agent 的要求只注入本次改写提示词，不落库；窄屏收进抽屉
const scriptCtxOpen = ref(false)
const rewriteForm = reactive({ requirements: '' })
const sourceChapterRange = computed(() => {
  const s = Number(episode.value?.source_start_chapter || episode.value?.sourceStartChapter)
  const e = Number(episode.value?.source_end_chapter || episode.value?.sourceEndChapter)
  if (!s || !e) return ''
  return s === e ? t('episode.script.ctxChaptersSingle', { s }) : t('episode.script.ctxChaptersRange', { s, e })
})
const dataRefreshing = ref(false)
const rawContent = computed(() => episode.value?.content || '')
const scriptContent = computed(() => episode.value?.script_content || episode.value?.scriptContent || '')
const epId = computed(() => episode.value?.id || 0)
// 顶栏剧集名与剧集列表卡片同源（episodes.title）。只有 title 仍是后端建集时自动写入的
// 「第{本集集号}集」才算未命名（此时集号 chip 已表达同一信息）；其余一律按自定义名展示 ——
// 集号与名字可以脱钩（如 EP3 名为「第2集」），不能拿集号当名字。
const episodeTitle = computed(() => String(episode.value?.title || '').trim())
const episodeCustomTitle = computed(() => {
  const title = episodeTitle.value
  if (!title) return ''
  return title.replace(/\s+/g, '') === `第${episodeNumber}集` ? '' : title
})
const episodeChipLabel = computed(() => episodeCustomTitle.value
  ? `EP ${String(episodeNumber).padStart(2, '0')}`
  : t('episode.topbar.episodeN', { n: episodeNumber }))
const rawLen = computed(() => localRaw.value.replace(/\s/g, '').length || 0)
const scriptLen = computed(() => localScript.value.replace(/\s/g, '').length || 0)

// ===== 拼接导出:镜头选择 + 成片列表 =====
const exportSelectedIds = ref([]) // 勾选的镜头 id
const exportMerges = ref([])      // 成片(拼接记录)列表
let exportSelTouched = false      // 用户手动操作过选择后,不再自动全选

const exportReadyIds = computed(() => sbs.value.filter(s => hasVid(s)).map(s => s.id))
const exportSelectedReadyIds = computed(() => exportSelectedIds.value.filter(id => exportReadyIds.value.includes(id)))

watch(exportReadyIds, (ids) => {
  if (exportSelTouched) {
    exportSelectedIds.value = exportSelectedIds.value.filter(id => ids.includes(id))
  } else {
    exportSelectedIds.value = [...ids]
  }
})

function isExportSelected(id) { return exportSelectedIds.value.includes(id) }
function toggleExportSelect(sb) {
  if (!hasVid(sb)) return
  exportSelTouched = true
  exportSelectedIds.value = isExportSelected(sb.id)
    ? exportSelectedIds.value.filter(x => x !== sb.id)
    : [...exportSelectedIds.value, sb.id]
}
function toggleSelectAllExport() {
  exportSelTouched = true
  exportSelectedIds.value = exportSelectedReadyIds.value.length === exportReadyIds.value.length ? [] : [...exportReadyIds.value]
}

async function loadExportMerges() {
  if (!epId.value) return
  try { exportMerges.value = await mergeAPI.list(epId.value) || [] } catch { /* 静默 */ }
}

// 成片删除：后端永久删除 MP4、海报与数据库记录，列表中即时移除；正在拼接的不允许删
const mergeDelete = ref({ open: false, item: null, loading: false })
function askDeleteMerge(m) { mergeDelete.value = { open: true, item: m, loading: false } }
async function confirmDeleteMerge() {
  const m = mergeDelete.value.item
  if (!m || mergeDelete.value.loading) return
  mergeDelete.value.loading = true
  try {
    await mergeAPI.del(m.id)
    exportMerges.value = exportMerges.value.filter(x => x.id !== m.id)
    if (activeMerge.value?.id === m.id) activeMerge.value = null
    mergeDelete.value = { open: false, item: null, loading: false }
    toast.success(t('episode.export.filmDeleted'))
  } catch (e) {
    mergeDelete.value.loading = false
    toastError(e)
  }
}

const scriptStep = ref(storedPanel ? (storedPanel.scriptStep === 0 ? 0 : 1) : 0)
// 兼容旧版 storyboard 子步骤名称，并恢复到新的独立「拆分分解」步骤。
const storedProdTab = storedPanel?.prodTab === 'storyboard' ? 'breakdown' : storedPanel?.prodTab
const prodTab = ref(['assets', 'breakdown', 'videos'].includes(storedProdTab) ? storedProdTab : 'assets')
// 面板位置变化即持久化
watch([panel, scriptStep, prodTab], ([p, s, pt]) => {
  try { localStorage.setItem(PANEL_STORE_KEY, JSON.stringify({ panel: p, scriptStep: s, prodTab: pt })) } catch { /* 静默 */ }
})
// ===== 视频制作三栏宽度：拖拽调节 + 全局持久化（双击分隔条恢复默认） =====
const VIDEO_COL_STORE_KEY = 'h3:workbench:video-cols'
const VIDEO_COL_DEFAULTS = { left: 236, right: 340 }
const VIDEO_COL_LIMITS = { left: [180, 420], right: [260, 560] }
const storedVideoCols = (() => {
  try {
    const c = JSON.parse(localStorage.getItem(VIDEO_COL_STORE_KEY) || 'null')
    return c && typeof c === 'object' ? c : null
  } catch { return null }
})()
const clampVideoCol = (which, w) => Math.min(VIDEO_COL_LIMITS[which][1], Math.max(VIDEO_COL_LIMITS[which][0], Math.round(w)))
const videoLeftW = ref(clampVideoCol('left', Number(storedVideoCols?.left) || VIDEO_COL_DEFAULTS.left))
const videoRightW = ref(clampVideoCol('right', Number(storedVideoCols?.right) || VIDEO_COL_DEFAULTS.right))
watch([videoLeftW, videoRightW], ([l, r]) => {
  try { localStorage.setItem(VIDEO_COL_STORE_KEY, JSON.stringify({ left: l, right: r })) } catch { /* 静默 */ }
})
function startVideoColDrag(which, e) {
  if (e.button !== 0) return
  e.preventDefault()
  const target = e.currentTarget
  const startX = e.clientX
  const startW = which === 'left' ? videoLeftW.value : videoRightW.value
  const onMove = (ev) => {
    const dx = ev.clientX - startX
    const w = clampVideoCol(which, which === 'left' ? startW + dx : startW - dx)
    if (which === 'left') videoLeftW.value = w
    else videoRightW.value = w
  }
  const onUp = () => {
    target.removeEventListener('pointermove', onMove)
    target.removeEventListener('pointerup', onUp)
    target.removeEventListener('pointercancel', onUp)
    document.body.classList.remove('is-video-col-dragging')
  }
  document.body.classList.add('is-video-col-dragging')
  target.addEventListener('pointermove', onMove)
  target.addEventListener('pointerup', onUp)
  target.addEventListener('pointercancel', onUp)
  target.setPointerCapture?.(e.pointerId)
}
const activeExtractTab = ref('characters')
const prodTabIdx = computed({
  get: () => prodTabDefs.value.findIndex(d => d.id === prodTab.value),
  set: (v) => { prodTab.value = prodTabDefs.value[v]?.id || 'assets' },
})
const imageConfigs = ref([])
const videoConfigs = ref([])
// 生成时可选模型：空串 = 跟随配置默认（models[0]）；选择持久化到 localStorage，刷新页面后保留
const MODEL_STORE_KEYS = { image: 'h3:model:image', video: 'h3:model:video' }
function readStoredModel(key, legacyKey = '') {
  try { return localStorage.getItem(key) || (legacyKey && localStorage.getItem(legacyKey)) || '' } catch { return '' }
}
const imageModel = ref(readStoredModel(MODEL_STORE_KEYS.image))
const videoModel = ref(readStoredModel(MODEL_STORE_KEYS.video))
function persistModel(modelRef, key) {
  watch(modelRef, v => {
    try { v ? localStorage.setItem(key, v) : localStorage.removeItem(key) } catch {}
  })
}
persistModel(imageModel, MODEL_STORE_KEYS.image)
persistModel(videoModel, MODEL_STORE_KEYS.video)
// 左侧菜单栏收起/展开：收起为窄图标栏给内容区让位，持久化到 localStorage
const SIDEBAR_COLLAPSED_KEY = 'h3:sidebar-collapsed'
const sidebarCollapsed = ref((() => {
  try { return localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === '1' } catch { return false }
})())
function toggleSidebar() {
  sidebarCollapsed.value = !sidebarCollapsed.value
  try {
    sidebarCollapsed.value
      ? localStorage.setItem(SIDEBAR_COLLAPSED_KEY, '1')
      : localStorage.removeItem(SIDEBAR_COLLAPSED_KEY)
  } catch { /* 静默 */ }
}
// Agent 文本生成统一使用 Claude CLI 的全局模型配置，不再从 Web AI 配置选择文本模型。
function chatModelOverride() { return undefined }
function chatConfigId() { return undefined }
const pendingCharImageIds = ref([])
const pendingSceneImageIds = ref([])
const pendingPropImageIds = ref([])
// 提交中兜底（同 submittingVideoIds 思路）：点击生成到 sys_task 占位行落库之间，
// loadGenTasks 的全量重建不能把这些 id 从"生成中"里抹掉，否则卡片徽标会闪回"去生成"
const submittingImageKeys = ref([])
function setImageSubmitting(type, id, on) {
  const key = `${type}:${id}`
  submittingImageKeys.value = on
    ? [...new Set([...submittingImageKeys.value, key])]
    : submittingImageKeys.value.filter(k => k !== key)
}
function submittingImageIdsOf(type) {
  const prefix = `${type}:`
  return submittingImageKeys.value.filter(k => k.startsWith(prefix)).map(k => Number(k.slice(prefix.length)))
}
const pendingVideoIds = ref([])
const videoQueueAhead = ref({})
const submittingVideoIds = ref([])
const failedVideoMessages = ref({})
const videoTaskPhases = ref({})
const videoSyncWarnings = ref({})
const videoStatusSyncInterrupted = ref(false)
let generationTaskSyncFailures = 0
let generationTaskSyncBusy = false
// 任务列表面板：顶栏按钮触发的右侧抽屉,按集聚合 sys_task + video_merges
const genTasks = ref([])
const genMerges = ref([])
const taskDrawer = ref(false)
const cancelingTaskIds = ref([])
const recallingTaskIds = ref([])
let genTasksTimer = null

function openTaskDrawer() {
  taskDrawer.value = true
  loadGenTasks()
}
function closeTaskDrawer() {
  taskDrawer.value = false
}
const imageViewer = ref({ open: false, src: '', title: '' })
const activeMerge = ref(null) // 成片大预览弹窗中正在播放的拼接记录
const previewShot = ref(null) // 导出页镜头素材预览弹窗中正在播放的分镜
function shotNumberOf(sb) {
  const i = sbs.value.findIndex(s => s.id === sb?.id)
  return i >= 0 ? i + 1 : 0
}
// 导出步骤完成 = 用户手动标记（episodes.status = 'completed'），不再按最新拼接记录推算
const exportDone = computed(() => episode.value?.status === 'completed')
async function toggleExportDone() {
  if (!epId.value) return
  const status = exportDone.value ? 'active' : 'completed'
  try {
    await episodeAPI.update(epId.value, { status })
    if (episode.value) episode.value.status = status
    toast.success(status === 'completed' ? t('episode.export.markedDoneToast') : t('episode.export.unmarkDoneToast'))
  } catch (e) {
    toastError(e)
  }
}
const assetDetail = ref({ open: false, type: '', item: null })
const assetDetailDraft = ref({ name: '', aliases: '', beautyPositioning: '', appearance: '', styling: '', prompt: '', lighting: '', description: '' })
const assetImageHistory = ref([])
const selectedAssetHistoryId = ref(null)
// 已把描述草稿对齐到其快照的「当前图片生成任务」id：描述/提示词以当前图片为准
const assetCurrentTaskId = ref(null)
// 历史对比预览态：记住进入预览前的草稿，点「设为当前图片」确认后才真正切换
const assetHistoryPreview = ref(null)
const assetHistoryLoading = ref(false)
const assetCompareOpen = ref(false)
// 最终提示词手动编辑：dirty 时才随保存提交，避免无修改保存误清空 Agent 生成的提示词
const assetPromptDraft = ref('')
const assetPromptDirty = ref(false)
const savingAssetDetail = ref(false)

function isPendingCharImage(id) {
  return pendingCharImageIds.value.includes(id)
}

function openImageViewer(src, title = '') {
  if (!src) return
  imageViewer.value = { open: true, src, title }
}

function closeImageViewer() {
  imageViewer.value = { open: false, src: '', title: '' }
}

function openAssetDetail(type, item) {
  if (!item) return
  assetDetail.value = { open: true, type, item }
  assetDetailDraft.value = {
    name: type === 'scene' ? (item.location || '') : (item.name || ''),
    aliases: item.aliases || '',
    beautyPositioning: item.beauty_positioning || item.beautyPositioning || '',
    appearance: item.appearance || '',
    styling: item.styling || '',
    prompt: item.prompt || (type === 'prop' ? '' : item.description) || '',
    lighting: item.lighting || '',
    description: item.description || '',
  }
  assetPromptDraft.value = item.final_prompt || item.finalPrompt || ''
  assetPromptDirty.value = false
  assetCurrentTaskId.value = null
  assetHistoryPreview.value = null
  void loadAssetImageHistory()
}

function closeAssetDetail() {
  assetDetail.value = { open: false, type: '', item: null }
  assetDetailDraft.value = { name: '', aliases: '', beautyPositioning: '', appearance: '', styling: '', prompt: '', lighting: '', description: '' }
  assetPromptDraft.value = ''
  assetPromptDirty.value = false
  assetImageHistory.value = []
  selectedAssetHistoryId.value = null
  assetCurrentTaskId.value = null
  assetHistoryPreview.value = null
  assetCompareOpen.value = false
}

function rawAssetImagePath(item) {
  return item?.image_url || item?.imageUrl || ''
}

function taskImagePath(task) {
  return task?.local_path || task?.localPath || task?.result_url || task?.resultUrl || ''
}

function assetHistorySrc(task) {
  return assetImageSrc({ image_url: taskImagePath(task) })
}

function comparableMediaPath(value) {
  return String(value || '').replace(/^\/+/, '')
}

function isCurrentAssetImage(task) {
  return !!taskImagePath(task)
    && comparableMediaPath(taskImagePath(task)) === comparableMediaPath(rawAssetImagePath(assetDetail.value.item))
}

const selectedAssetHistory = computed(() =>
  assetImageHistory.value.find(task => task.id === selectedAssetHistoryId.value) || null)

// 预览历史图时主图框展示该历史图，未预览时展示当前图
const assetDetailDisplaySrc = computed(() =>
  (assetHistoryPreview.value && selectedAssetHistory.value ? assetHistorySrc(selectedAssetHistory.value) : '')
  || assetImageSrc(assetDetail.value.item))

function openAssetCompare() {
  if (!assetImageSrc(assetDetail.value.item) || !selectedAssetHistory.value) return
  assetCompareOpen.value = true
}

function closeAssetCompare() {
  assetCompareOpen.value = false
}

function assetHistoryQuery(type, id) {
  const targetKey = type === 'character' ? 'character_id' : type === 'scene' ? 'scene_id' : 'prop_id'
  return { type: 'image', [targetKey]: id }
}

async function loadAssetImageHistory() {
  const detail = assetDetail.value
  if (!detail.open || !detail.item?.id) return
  const requestedType = detail.type
  const requestedId = detail.item.id
  assetHistoryLoading.value = true
  try {
    const rows = await taskAPI.list(assetHistoryQuery(requestedType, requestedId))
    if (!assetDetail.value.open || assetDetail.value.type !== requestedType || assetDetail.value.item?.id !== requestedId) return
    const completed = (Array.isArray(rows) ? rows : [])
      .filter(task => task.status === 'completed' && taskImagePath(task))
    // 描述以当前图片为准：找到真正生成当前图的那次任务，变化时把草稿对齐到它的快照
    const currentTask = completed.find(task => isCurrentAssetImage(task)) || null
    if (currentTask && currentTask.id !== assetCurrentTaskId.value) applyTaskSnapshotToDraft(requestedType, currentTask)
    assetCurrentTaskId.value = currentTask?.id ?? null
    assetImageHistory.value = completed
      .filter(task => !isCurrentAssetImage(task))
      .sort((a, b) => taskCreatedAt(b).localeCompare(taskCreatedAt(a)))
    const selectedStillExists = assetImageHistory.value.some(task => task.id === selectedAssetHistoryId.value)
    if (!selectedStillExists) {
      selectedAssetHistoryId.value = assetImageHistory.value[0]?.id ?? null
    }
  } catch {
    assetImageHistory.value = []
    selectedAssetHistoryId.value = null
  } finally {
    assetHistoryLoading.value = false
  }
}

/** 解析图片任务参数；旧任务或异常数据返回空对象 */
function assetTaskParams(task) {
  const raw = task?.params
  if (!raw || typeof raw !== 'string') return {}
  try {
    const parsed = JSON.parse(raw)
    return parsed && typeof parsed === 'object' ? parsed : {}
  } catch {
    return {}
  }
}

/** 解析图片任务发起时记录的资产字段快照；旧任务无快照返回空对象 */
function assetTaskSnapshot(task) {
  const snapshot = assetTaskParams(task).assetSnapshot
  return snapshot && typeof snapshot === 'object' ? snapshot : {}
}

function isManualUploadTask(task) {
  return assetTaskParams(task).source === 'manual_upload'
}

/** 描述/提示词以当前图片为准：把编辑草稿对齐到生成该图的任务快照（无快照的旧任务保持库值） */
function applyTaskSnapshotToDraft(type, task) {
  const snapshot = assetTaskSnapshot(task)
  if (type === 'character') {
    if (typeof snapshot.appearance === 'string') assetDetailDraft.value.appearance = snapshot.appearance
    if (typeof snapshot.styling === 'string') assetDetailDraft.value.styling = snapshot.styling
    // 角色生图回退链 appearance || description：appearance 为空时 description 才是成图依据，一并回滚
    if (typeof snapshot.description === 'string') assetDetailDraft.value.description = snapshot.description
  } else if (type === 'scene') {
    if (typeof snapshot.prompt === 'string') assetDetailDraft.value.prompt = snapshot.prompt
    if (typeof snapshot.lighting === 'string') assetDetailDraft.value.lighting = snapshot.lighting
  } else if (type === 'prop') {
    if (typeof snapshot.description === 'string') assetDetailDraft.value.description = snapshot.description
  }
  const prompt = String(task?.prompt || '').trim()
  if (prompt || isManualUploadTask(task)) {
    assetPromptDraft.value = prompt
    assetPromptDirty.value = false
  }
}

/** 点历史缩略图 = 进入对比预览：主图、描述、提示词展示该图当时的内容，但不落库；
 *  确认无误再点「设为当前图片」整体切换；再次点同一缩略图退出预览 */
function selectAssetHistoryImage(task) {
  if (selectedAssetHistoryId.value === task.id && assetHistoryPreview.value) {
    exitAssetHistoryPreview()
    return
  }
  selectedAssetHistoryId.value = task.id
  enterAssetHistoryPreview(task)
}

function enterAssetHistoryPreview(task) {
  if (!assetHistoryPreview.value) {
    assetHistoryPreview.value = {
      draft: { ...assetDetailDraft.value },
      promptDraft: assetPromptDraft.value,
      promptDirty: assetPromptDirty.value,
      currentTaskId: assetCurrentTaskId.value,
    }
  }
  applyTaskSnapshotToDraft(assetDetail.value.type, task)
}

/** 退出对比预览：把编辑草稿还原到进入预览前的内容（图片未动，不该带上历史描述） */
function exitAssetHistoryPreview() {
  const saved = assetHistoryPreview.value
  if (!saved) return
  assetDetailDraft.value = saved.draft
  assetPromptDraft.value = saved.promptDraft
  assetPromptDirty.value = saved.promptDirty
  assetCurrentTaskId.value = saved.currentTaskId
  assetHistoryPreview.value = null
}

async function setAssetHistoryAsCurrent(task) {
  const detail = assetDetail.value
  const imagePath = taskImagePath(task)
  if (!detail.open || !detail.item?.id || !imagePath || isCurrentAssetImage(task)) return
  // 还原历史图片时连同当时的信息字段与最终提示词一起回滚，保证图文一致
  const snapshot = assetTaskSnapshot(task)
  const historyPrompt = String(task.prompt || '').trim()
  const restoreEmptyPrompt = isManualUploadTask(task)
  try {
    const payload = { image_url: imagePath, local_path: imagePath }
    const infoPatch = {}
    if (detail.type === 'character') {
      if (typeof snapshot.appearance === 'string') { payload.appearance = snapshot.appearance; infoPatch.appearance = snapshot.appearance }
      if (typeof snapshot.styling === 'string') { payload.styling = snapshot.styling; infoPatch.styling = snapshot.styling }
      if (typeof snapshot.description === 'string') { payload.description = snapshot.description; infoPatch.description = snapshot.description }
    } else if (detail.type === 'scene') {
      if (typeof snapshot.prompt === 'string') { payload.prompt = snapshot.prompt; Object.assign(infoPatch, { prompt: snapshot.prompt, description: snapshot.prompt }) }
      if (typeof snapshot.lighting === 'string') { payload.lighting = snapshot.lighting; infoPatch.lighting = snapshot.lighting }
    } else {
      if (typeof snapshot.type === 'string') { payload.type = snapshot.type; infoPatch.type = snapshot.type }
      if (typeof snapshot.description === 'string') { payload.description = snapshot.description; infoPatch.description = snapshot.description }
    }
    if (historyPrompt || restoreEmptyPrompt) payload.final_prompt = historyPrompt
    if (detail.type === 'character') await characterAPI.update(detail.item.id, payload)
    else if (detail.type === 'scene') await sceneAPI.update(detail.item.id, payload)
    else await propAPI.update(detail.item.id, payload)
    const patch = { ...infoPatch, image_url: imagePath, imageUrl: imagePath, local_path: imagePath, localPath: imagePath }
    if (historyPrompt || restoreEmptyPrompt) Object.assign(patch, { final_prompt: historyPrompt, finalPrompt: historyPrompt })
    Object.assign(detail.item, patch)
    const list = detail.type === 'character' ? chars.value : detail.type === 'scene' ? scenes.value : propItems.value
    const target = list.find(item => item.id === detail.item.id)
    if (target) Object.assign(target, patch)
    // 已确认切换：该历史图成为当前图，预览态结束（草稿随即对齐其快照，勿还原旧值）
    assetHistoryPreview.value = null
    // 弹窗草稿对齐该图快照（loadAssetImageHistory 会识别 currentTask 并记录）
    applyTaskSnapshotToDraft(detail.type, task)
    assetCurrentTaskId.value = task.id
    await loadAssetImageHistory()
    toast.success(t('episode.asset.historyRestored'))
  } catch (error) {
    toastError(error, { fallback: 'episode.asset.historyRestoreFailed' })
  }
}

async function removeAssetHistory(task) {
  if (!task?.id || isCurrentAssetImage(task)) return
  try {
    await taskAPI.del(task.id)
    assetImageHistory.value = assetImageHistory.value.filter(item => item.id !== task.id)
    if (selectedAssetHistoryId.value === task.id) {
      exitAssetHistoryPreview()
      selectedAssetHistoryId.value = assetImageHistory.value[0]?.id ?? null
    }
    toast.success(t('episode.asset.historyDeleted'))
  } catch (error) {
    toastError(error, { fallback: 'common.deleteFailed' })
  }
}

// ─── 手动新增资产 ────────────────────────────────────────────
// 类型短显示名渲染时求值（不模块级固化），逻辑判断一律用 kind code
const assetKindLabelMap = computed(() => ({
  character: t('common.role'),
  scene: t('common.scene'),
  prop: t('common.prop'),
}))
function assetKindLabel(type) {
  return assetKindLabelMap.value[type] || t('episode.asset.fallbackType')
}
const assetCreate = ref({ open: false, type: 'character', saving: false })
const assetCreateDraft = ref({})
const assetCreateTypeLabel = computed(() => assetKindLabel(assetCreate.value.type))

function openAssetCreate(type) {
  assetCreateDraft.value = { name: '', aliases: '', beautyPositioning: '', role: '', appearance: '', styling: '', location: '', time: '', prompt: '', lighting: '', type: '', description: '' }
  assetCreate.value = { open: true, type, saving: false }
}

async function saveAssetCreate() {
  const d = assetCreateDraft.value
  const type = assetCreate.value.type
  if (assetCreate.value.saving) return
  if (type === 'scene' ? !d.location?.trim() : !d.name?.trim()) {
    toast.warning(type === 'scene' ? t('episode.create.locationRequired') : t('episode.create.nameRequired'))
    return
  }
  assetCreate.value.saving = true
  try {
    const base = { drama_id: dramaId, episode_id: epId.value }
    if (type === 'character') await characterAPI.create({ ...base, name: d.name, aliases: d.aliases, beauty_positioning: d.beautyPositioning, description: d.description, role: d.role, appearance: d.appearance, styling: d.styling })
    else if (type === 'scene') await sceneAPI.create({ ...base, location: d.location, aliases: d.aliases, time: d.time, prompt: d.prompt, lighting: d.lighting })
    else await propAPI.create({ ...base, name: d.name, type: d.type, description: d.description })
    toast.success(t('episode.create.created', { type: assetCreateTypeLabel.value }))
    assetCreate.value.open = false
    await refresh()
  } catch (e) {
    toastError(e)
  } finally {
    assetCreate.value.saving = false
  }
}

// ─── 导入项目资产（引用其他集创建的资产到本集，只补写关联不复制本体） ───
const assetImport = ref({ open: false, loading: false, saving: false, characters: [], scenes: [], props: [] })
const assetImportSel = ref({ character: [], scene: [], prop: [] })
const assetImportCount = computed(() =>
  assetImportSel.value.character.length + assetImportSel.value.scene.length + assetImportSel.value.prop.length)
const assetImportSections = computed(() => ([
  { kind: 'character', label: t('common.role'), items: assetImport.value.characters.map(a => ({ id: a.id, name: a.name, sub: a.role || '', img: thumbOf(assetImageSrc(a)) })) },
  { kind: 'scene', label: t('common.scene'), items: assetImport.value.scenes.map(a => ({ id: a.id, name: a.location, sub: a.time || '', img: thumbOf(assetImageSrc(a)) })) },
  { kind: 'prop', label: t('common.prop'), items: assetImport.value.props.map(a => ({ id: a.id, name: a.name, sub: a.type || '', img: thumbOf(assetImageSrc(a)) })) },
]))
const assetImportCandidates = computed(() => assetImportSections.value.reduce((n, s) => n + s.items.length, 0))

async function openAssetImport() {
  assetImportSel.value = { character: [], scene: [], prop: [] }
  assetImport.value = { open: true, loading: true, saving: false, characters: [], scenes: [], props: [] }
  try {
    const d = await dramaAPI.get(dramaId)
    const linked = {
      character: new Set(chars.value.map(a => a.id)),
      scene: new Set(scenes.value.map(a => a.id)),
      prop: new Set(propItems.value.map(a => a.id)),
    }
    assetImport.value.characters = (d?.characters || []).filter(a => !a.deleted_at && !linked.character.has(a.id))
    assetImport.value.scenes = (d?.scenes || []).filter(a => !a.deleted_at && !linked.scene.has(a.id))
    assetImport.value.props = (d?.props || []).filter(a => !a.deleted_at && !linked.prop.has(a.id))
  } catch (e) {
    assetImport.value.open = false
    toastError(e)
  } finally {
    assetImport.value.loading = false
  }
}

async function saveAssetImport() {
  if (assetImport.value.saving || !assetImportCount.value) return
  assetImport.value.saving = true
  try {
    const res = await episodeAPI.importAssets(epId.value, {
      character_ids: assetImportSel.value.character,
      scene_ids: assetImportSel.value.scene,
      prop_ids: assetImportSel.value.prop,
    })
    toast.success(t('episode.import.done', { n: res?.imported ?? assetImportCount.value }))
    assetImport.value.open = false
    await refresh()
  } catch (e) {
    toastError(e)
  } finally {
    assetImport.value.saving = false
  }
}

// ===== 资产导出 JSON（人物与场景道具分文件导出，均可逐项选择）=====
const exportingAssets = ref(false)
const assetExport = ref({ open: false })
const assetExportSel = ref({ character: [], scene: [], prop: [] })
const assetExportSections = computed(() => ([
  {
    kind: 'character',
    label: t('common.role'),
    items: chars.value.map(item => ({
      id: item.id,
      name: item.name || t('common.role'),
      sub: item.role || item.beauty_positioning || item.beautyPositioning || '',
      img: thumbOf(assetImageSrc(item)),
    })),
  },
  {
    kind: 'scene',
    label: t('common.scene'),
    items: scenes.value.map(item => ({
      id: item.id,
      name: item.location || t('common.scene'),
      sub: item.time || '',
      img: thumbOf(assetImageSrc(item)),
    })),
  },
  {
    kind: 'prop',
    label: t('common.prop'),
    items: propItems.value.map(item => ({
      id: item.id,
      name: item.name || t('common.prop'),
      sub: item.type || '',
      img: thumbOf(assetImageSrc(item)),
    })),
  },
]))
const assetExportCount = computed(() =>
  assetExportSel.value.character.length + assetExportSel.value.scene.length + assetExportSel.value.prop.length)
const assetExportCharacterCount = computed(() => assetExportSel.value.character.length)
const assetExportScenePropCount = computed(() => assetExportSel.value.scene.length + assetExportSel.value.prop.length)

function selectAllAssetExports() {
  assetExportSel.value = {
    character: chars.value.map(item => item.id),
    scene: scenes.value.map(item => item.id),
    prop: propItems.value.map(item => item.id),
  }
}
function clearAssetExports() {
  assetExportSel.value = { character: [], scene: [], prop: [] }
}
function openAssetExport() {
  selectAllAssetExports()
  assetExport.value.open = true
}
function selectedAssetExportCount(kind) {
  return assetExportSel.value[kind]?.length || 0
}
function isAssetExportSectionSelected(section) {
  if (!section.items.length) return false
  const selected = new Set(assetExportSel.value[section.kind])
  return section.items.every(item => selected.has(item.id))
}
function toggleAssetExportSection(section) {
  assetExportSel.value[section.kind] = isAssetExportSectionSelected(section)
    ? []
    : section.items.map(item => item.id)
}

async function exportAssetsJson(group) {
  if (exportingAssets.value) return
  const selected = {
    character: new Set(assetExportSel.value.character),
    scene: new Set(assetExportSel.value.scene),
    prop: new Set(assetExportSel.value.prop),
  }
  const selectedChars = chars.value.filter(item => selected.character.has(item.id))
  const selectedScenes = scenes.value.filter(item => selected.scene.has(item.id))
  const selectedProps = propItems.value.filter(item => selected.prop.has(item.id))
  const exportCharacters = group === 'characters'
  const selectedCount = exportCharacters
    ? selectedChars.length
    : selectedScenes.length + selectedProps.length
  if (!selectedCount) return
  exportingAssets.value = true
  try {
    const exportBase = {
      kind: 'h3-assets-export',
      version: 1,
      scope: 'selected',
      exported_at: new Date().toISOString(),
      drama: drama.value?.title || '',
      episode: { number: episodeNumber, title: episode.value?.title || '' },
    }
    const characters = selectedChars.map(c => {
      const entry = {
        name: c.name || '',
        aliases: c.aliases || '',
        role: c.role || '',
        personality: c.personality || '',
        appearance: c.appearance || '',
        styling: c.styling || '',
        description: c.description || '',
        final_prompt: c.final_prompt || '',
      }
      // 角色列表接口已随角色附带过滤后的服装/造型状态（c.states）
      const states = (c.states || []).map(s => ({
        name: s.name || '',
        description: s.description || '',
        final_prompt: s.final_prompt || '',
      }))
      if (states.length) entry.states = states
      return entry
    })
    const exportedScenes = selectedScenes.map(s => ({
      location: s.location || '',
      aliases: s.aliases || '',
      time: s.time || '',
      lighting: s.lighting || '',
      prompt: s.prompt || '',
      final_prompt: s.final_prompt || '',
    }))
    const exportedProps = selectedProps.map(p => ({
      name: p.name || '',
      aliases: p.aliases || '',
      type: p.type || '',
      narrative_function: p.narrative_function || '',
      description: p.description || '',
      prompt: p.prompt || '',
      final_prompt: p.final_prompt || '',
    }))

    const safeName = String(drama.value?.title || `drama-${dramaId}`).replace(/[\\/:*?"<>|\s]+/g, '_').slice(0, 40) || 'h3'
    const suffix = t(exportCharacters
      ? 'episode.prod.exportSuffixCharacters'
      : 'episode.prod.exportSuffixSceneProps')
    const payload = exportCharacters
      ? {
          ...exportBase,
          group: 'characters',
          selection: { characters: characters.length, scenes: 0, props: 0 },
          characters,
        }
      : {
          ...exportBase,
          group: 'scenes-and-props',
          selection: { characters: 0, scenes: exportedScenes.length, props: exportedProps.length },
          ...(exportedScenes.length ? { scenes: exportedScenes } : {}),
          ...(exportedProps.length ? { props: exportedProps } : {}),
        }
    const filename = `${safeName}_第${episodeNumber}集_${suffix}.json`
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    document.body.appendChild(a)
    a.click()
    a.remove()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
    toast.success(t('episode.prod.exportedJson', { n: selectedCount, type: suffix }))
  } catch (e) {
    toastError(e)
  } finally {
    exportingAssets.value = false
  }
}

// ─── 删除资产 ────────────────────────────────────────────────
const assetDelete = ref({ open: false, type: '', item: null, loading: false })
const assetDeleteTypeLabel = computed(() => assetKindLabel(assetDelete.value.type))
const assetDeleteName = computed(() => assetDelete.value.item?.name || assetDelete.value.item?.location || '')

function askDeleteAsset(type, item) {
  assetDelete.value = { open: true, type, item, loading: false }
}

async function confirmDeleteAsset() {
  const { type, item } = assetDelete.value
  if (!item || assetDelete.value.loading) return
  assetDelete.value.loading = true
  try {
    if (type === 'character') await characterAPI.del(item.id)
    else if (type === 'scene') await sceneAPI.del(item.id)
    else await propAPI.del(item.id)
    toast.success(t('episode.delete.deleted', { type: assetDeleteTypeLabel.value }))
    assetDelete.value.open = false
    if (assetDetail.value.open && assetDetail.value.type === type && assetDetail.value.item?.id === item.id) closeAssetDetail()
    await refresh()
  } catch (e) {
    toastError(e)
  } finally {
    assetDelete.value.loading = false
  }
}

/** 提示词是否已过期：核心视觉资料在提示词生成后又改过，下次生图会自动按新资料重新生成 */
function assetPromptStale(item) {
  return !!(item && (item.prompt_stale ?? item.promptStale)) && !!(item.final_prompt || item.finalPrompt)
}

function onAssetPromptInput(event) {
  assetPromptDraft.value = event.target.value
  assetPromptDirty.value = true
}

const assetFinalPrompt = computed(() => {
  const item = assetDetail.value?.item
  return item?.final_prompt || item?.finalPrompt || ''
})

/** 把生成好的最终提示词同步到列表项与弹窗项（新生成即最新，清除过期标记） */
function applyFinalPrompt(type, id, fp) {
  const patch = { final_prompt: fp, finalPrompt: fp, prompt_stale: 0, promptStale: 0 }
  const list = type === 'character' ? chars.value : type === 'scene' ? scenes.value : propItems.value
  const target = list.find(x => x.id === id)
  if (target) Object.assign(target, patch)
  if (assetDetail.value.open && assetDetail.value.type === type && assetDetail.value.item?.id === id) {
    Object.assign(assetDetail.value.item, patch)
    assetPromptDraft.value = fp
    assetPromptDirty.value = false
  }
}

// Agent 修改的待确认候选（携带 diff 片段）：存在时提示词框切换为高亮预览
const videoAgentCandidate = ref(null)
const assetAgentCandidate = ref(null)
const scriptAgentCandidate = ref(null)

function onScriptAgentApplied(prompt) {
  localScript.value = prompt
  if (episode.value) episode.value.script_content = prompt
  toast.success(t('common.saved'))
}

function onAssetAgentApplied(prompt, applied = {}) {
  const type = assetDetail.value.type
  const id = assetDetail.value.item?.id
  if (!type || !id) return
  applyFinalPrompt(type, id, prompt)
  if (type === 'character' && applied.appearance && applied.styling) {
    const characterPatch = { appearance: applied.appearance, styling: applied.styling }
    Object.assign(assetDetail.value.item, characterPatch)
    const character = chars.value.find(item => item.id === id)
    if (character) Object.assign(character, characterPatch)
    assetDetailDraft.value.appearance = applied.appearance
    assetDetailDraft.value.styling = applied.styling
  } else if (type === 'scene' && applied.scene_description && applied.scene_lighting) {
    const scenePatch = {
      prompt: applied.scene_description,
      description: applied.scene_description,
      lighting: applied.scene_lighting,
    }
    Object.assign(assetDetail.value.item, scenePatch)
    const scene = scenes.value.find(item => item.id === id)
    if (scene) Object.assign(scene, scenePatch)
    assetDetailDraft.value.prompt = applied.scene_description
    assetDetailDraft.value.lighting = applied.scene_lighting
  } else if (type === 'prop' && applied.prop_type && applied.prop_description) {
    const propPatch = { type: applied.prop_type, description: applied.prop_description }
    Object.assign(assetDetail.value.item, propPatch)
    const prop = propItems.value.find(item => item.id === id)
    if (prop) Object.assign(prop, propPatch)
    assetDetailDraft.value.description = applied.prop_description
  }
  assetPromptDraft.value = prompt
  assetPromptDirty.value = false
  toast.success(t('common.saved'))
  // 提示词/造型改了但图还是旧的：提醒重新生图，避免后续视频参考图与文本打架
  if (rawAssetImagePath(assetDetail.value.item)) toast.warning(t('episode.asset.textImageOutOfSync'))
}

function onStoryboardAgentApplied(prompt) {
  if (!selectedSb.value) return
  selectedSb.value.video_prompt = prompt
  selectedSb.value.videoPrompt = prompt
  toast.success(t('common.saved'))
}

const generatingPromptKeys = ref([])

function isGeneratingPrompt(type, id) {
  return generatingPromptKeys.value.includes(`${type}:${id}`)
}

/** 该资产图片是否在外层「生成」流程中（含提示词阶段与生图阶段） */
function isAssetImagePending(type, id) {
  return type === 'character' ? isPendingCharImage(id) : type === 'scene' ? isPendingSceneImage(id) : isPendingPropImage(id)
}

/** 最近一次持久化图片任务失败时显示失败状态；按钮保持可用，直接点击即可重试。 */
function isAssetImageFailed(type, id, item = null) {
  if (isAssetImagePending(type, id)) return false
  const idField = type === 'character' ? 'character_id' : type === 'scene' ? 'scene_id' : 'prop_id'
  const latest = genTasks.value.find(task => task.type === 'image' && Number(task[idField]) === Number(id))
  return latest?.status === 'failed' || (type === 'scene' && item?.status === 'failed')
}

/**
 * 生成最终提示词（弹窗按钮与外层两段式生图共用同一 key 状态，避免重复触发）
 * force=true 时忽略已有提示词强制重新生成
 * 返回最终提示词；生成失败由接口抛错，Agent 返回空时返回 ''
 */
async function ensureAssetPrompt(type, id, force = false) {
  const key = `${type}:${id}`
  if (generatingPromptKeys.value.includes(key)) return ''
  generatingPromptKeys.value.push(key)
  try {
    const res = type === 'character'
      ? await characterAPI.generatePrompt(id, epId.value, force, chatModelOverride(), chatConfigId())
      : type === 'scene'
        ? await sceneAPI.generatePrompt(id, epId.value, force, chatModelOverride(), chatConfigId())
        : await propAPI.generatePrompt(id, epId.value, force, chatModelOverride(), chatConfigId())
    const fp = res?.final_prompt || res?.finalPrompt || ''
    if (fp) applyFinalPrompt(type, id, fp)
    return fp
  } finally {
    generatingPromptKeys.value = generatingPromptKeys.value.filter(k => k !== key)
  }
}

/** 弹窗内生成最终提示词（不生图）；已有最终提示词时重新生成（force） */
async function genAssetFinalPrompt() {
  const detail = assetDetail.value
  if (!detail.open || !detail.item?.id) return
  // 先持久化当前编辑草稿，避免 Agent 仍读到修改前的描述。
  if (assetDetailHasChanges()) {
    const saved = await saveAssetDetail({ silent: true })
    if (!saved) return
  }
  const force = !!assetFinalPrompt.value
  try {
    const fp = await ensureAssetPrompt(detail.type, detail.item.id, force)
    if (!fp) throw new Error(t('episode.asset.promptGenFailedRetry'))
    assetPromptDraft.value = fp
    assetPromptDirty.value = false
    toast.success(force ? t('episode.asset.promptRegenerated') : t('episode.asset.promptGenerated'))
  } catch (e) {
    toastError(e, { fallback: 'episode.asset.promptGenFailed' })
  }
}

async function copyAssetFinalPrompt() {
  const text = assetPromptDraft.value || assetFinalPrompt.value
  if (!text) return
  try {
    await navigator.clipboard.writeText(text)
    toast.success(t('episode.asset.promptCopied'))
  } catch {
    toast.error(t('episode.asset.copyFailed'))
  }
}

function assetDetailHasChanges() {
  const detail = assetDetail.value
  const item = detail.item
  if (!detail.open || !item) return false
  if (detail.type === 'character') return assetDetailDraft.value.name !== (item.name || '')
    || assetDetailDraft.value.aliases !== (item.aliases || '')
    || assetDetailDraft.value.beautyPositioning !== (item.beauty_positioning || item.beautyPositioning || '')
    || assetDetailDraft.value.description !== (item.description || '')
    || assetDetailDraft.value.appearance !== (item.appearance || '')
    || assetDetailDraft.value.styling !== (item.styling || '')
    || assetPromptDirty.value
  if (detail.type === 'scene') return assetDetailDraft.value.name !== (item.location || '')
    || assetDetailDraft.value.aliases !== (item.aliases || '')
    || assetDetailDraft.value.prompt !== (item.prompt || '')
    || assetDetailDraft.value.lighting !== (item.lighting || '')
    || assetPromptDirty.value
  return assetDetailDraft.value.description !== (item.description || '') || assetPromptDirty.value
}

async function saveAssetDetail(options = {}) {
  const silent = options?.silent === true
  const detail = assetDetail.value
  if (!detail.open || !detail.item?.id) return
  const item = detail.item
  // 只提交真正修改过的字段：无修改保存不应触发后端的提示词失效置空
  const payload = {}
  let infoChanged = false
  if (detail.type === 'character') {
    if (assetDetailDraft.value.name !== (item.name || '')) payload.name = assetDetailDraft.value.name.trim()
    if (assetDetailDraft.value.aliases !== (item.aliases || '')) payload.aliases = assetDetailDraft.value.aliases
    if (assetDetailDraft.value.beautyPositioning !== (item.beauty_positioning || item.beautyPositioning || '')) payload.beauty_positioning = assetDetailDraft.value.beautyPositioning
    if (assetDetailDraft.value.description !== (item.description || '')) payload.description = assetDetailDraft.value.description
    if (assetDetailDraft.value.appearance !== (item.appearance || '')) payload.appearance = assetDetailDraft.value.appearance
    if (assetDetailDraft.value.styling !== (item.styling || '')) payload.styling = assetDetailDraft.value.styling
  } else if (detail.type === 'scene') {
    if (assetDetailDraft.value.name !== (item.location || '')) payload.location = assetDetailDraft.value.name.trim()
    if (assetDetailDraft.value.aliases !== (item.aliases || '')) payload.aliases = assetDetailDraft.value.aliases
    if (assetDetailDraft.value.prompt !== (item.prompt || '')) payload.prompt = assetDetailDraft.value.prompt
    if (assetDetailDraft.value.lighting !== (item.lighting || '')) payload.lighting = assetDetailDraft.value.lighting
  } else {
    if (assetDetailDraft.value.description !== (item.description || '')) payload.description = assetDetailDraft.value.description
  }
  infoChanged = Object.keys(payload).length > 0
  // 手动编辑过最终提示词才提交；空串视为清空
  if (assetPromptDirty.value) payload.final_prompt = assetPromptDraft.value.trim() || ''
  if (!infoChanged && !assetPromptDirty.value) {
    if (!silent) toast.info(t('episode.asset.noChanges'))
    return false
  }
  savingAssetDetail.value = true
  try {
    if (detail.type === 'character') await characterAPI.update(item.id, payload)
    else if (detail.type === 'scene') await sceneAPI.update(item.id, payload)
    else await propAPI.update(item.id, payload)
    // 本地同步：手动编辑的提示词以草稿为准；仅信息字段变更时提示词保留但已被后端标记过期（生图时自动重新生成）
    const { final_prompt, ...infoPatch } = payload
    if (payload.beauty_positioning !== undefined) infoPatch.beautyPositioning = payload.beauty_positioning
    const oldPrompt = item.final_prompt || item.finalPrompt || null
    const promptValue = assetPromptDirty.value ? (payload.final_prompt || null) : oldPrompt
    // 与后端 markPromptStaleOnEdit 同规则：显式改提示词→最新；仅信息字段变化且已有提示词→过期
    const promptStale = (assetPromptDirty.value && promptValue !== oldPrompt)
      ? 0
      : (infoChanged && promptValue ? 1 : Number(item.prompt_stale ?? item.promptStale ?? 0))
    Object.assign(item, infoPatch, { final_prompt: promptValue, finalPrompt: promptValue, prompt_stale: promptStale, promptStale })
    const list = detail.type === 'character' ? chars.value : detail.type === 'scene' ? scenes.value : propItems.value
    const target = list.find(x => x.id === item.id)
    if (target) Object.assign(target, infoPatch, { final_prompt: promptValue, finalPrompt: promptValue, prompt_stale: promptStale, promptStale })
    const promptChanged = assetPromptDirty.value
    if (promptChanged) assetPromptDraft.value = payload.final_prompt || ''
    assetPromptDirty.value = false
    // 预览中保存 = 把当前所见内容存为新基准：结束预览态但保留草稿（还原会丢弃刚保存的值）
    assetHistoryPreview.value = null
    if (!silent) toast.success(t('episode.asset.saved'))
    // 描述/提示词改了但图片没变：提醒重新生图，保证图、文、视频三者同源
    if (!silent) {
      if (rawAssetImagePath(item) && (infoChanged || promptChanged)) toast.warning(t('episode.asset.textImageOutOfSync'))
    }
    return true
  } catch (e) {
    toastError(e, { fallback: 'episode.asset.saveFailed' })
    return false
  } finally {
    savingAssetDetail.value = false
  }
}

function assetImageSrc(item) {
  const raw = item?.image_url || item?.imageUrl || ''
  if (!raw) return ''
  if (/^https?:\/\//i.test(raw) || raw.startsWith('/')) return raw
  return `/${raw}`
}

function assetAudioSrc(item) {
  const raw = item?.audio_url || item?.audioUrl || ''
  if (!raw) return ''
  if (/^(https?:\/\/|data:)/i.test(raw) || raw.startsWith('/')) return raw
  return `/${raw}`
}

function toggleAssetAudio(id) {
  const audio = document.getElementById(`character-audio-${id}`)
  if (!(audio instanceof HTMLAudioElement)) return
  if (audio.paused) {
    audio.play().catch(() => toast.error('音频播放失败，请检查音频文件'))
  } else {
    audio.pause()
  }
}

function assetDetailTitle(detail) {
  if (!detail?.item) return ''
  if (detail.type === 'character') return detail.item.name || t('episode.asset.unnamedChar')
  if (detail.type === 'prop') return detail.item.name || t('episode.asset.unnamedProp')
  return detail.item.location || t('episode.asset.unnamedScene')
}

/** 资产详情弹窗 kicker / aria-label 用长标签 */
function assetTypeLabel(type) {
  return { character: t('episode.asset.typeChar'), scene: t('episode.asset.typeScene'), prop: t('episode.asset.typeProp') }[type] || t('episode.asset.fallbackType')
}

/** 资产详情预览图标题（角色形象/场景图/道具图） */
function assetDetailImageTitle(detail) {
  if (!detail?.item) return ''
  const kindLabel = detail.type === 'character' ? t('episode.asset.charPortrait') : detail.type === 'scene' ? t('episode.asset.sceneImage') : t('common.prop')
  return `${assetDetailTitle(detail)} ${kindLabel}`
}

function characterAppearanceValue(char) {
  return char?.appearance || t('episode.asset.appearanceTodo')
}

function characterStylingValue(char) {
  return char?.styling || t('episode.asset.stylingTodo')
}

function characterDescriptionValue(char) {
  return char?.description || [char?.appearance, char?.styling].filter(Boolean).join(' · ') || t('episode.asset.characterDescriptionTodo')
}

function characterVisualSummary(char) {
  return [char?.beauty_positioning || char?.beautyPositioning || '', char?.aliases ? `${t('episode.asset.aliasesLabel')}${char.aliases}` : '', characterDescriptionValue(char)].filter(Boolean).join(' · ')
}

function sceneDescriptionValue(scene) {
  return scene?.prompt || scene?.description || t('episode.asset.sceneDescTodo')
}

function sceneLightingValue(scene) {
  return scene?.lighting || t('episode.asset.sceneLightTodo')
}

function handleImageViewerKeydown(event) {
  if (event.key !== 'Escape') return
  if (imageViewer.value.open) closeImageViewer()
  else if (assetCompareOpen.value) closeAssetCompare()
  else if (assetDetail.value.open) closeAssetDetail()
  else if (taskDrawer.value) closeTaskDrawer()
}

onMounted(() => {
  window.addEventListener('keydown', handleImageViewerKeydown)
})

onBeforeUnmount(() => {
  window.removeEventListener('keydown', handleImageViewerKeydown)
  stopGenTasksPolling()
  stopBreakdownProgressPolling()
  stopEpisodeVideoPipelineWatcher()
})

function isPendingSceneImage(id) {
  return pendingSceneImageIds.value.includes(id)
}

function isPendingVideo(id) {
  return pendingVideoIds.value.includes(id)
}

function videoFailMessage(id) {
  return failedVideoMessages.value[id] || ''
}

function videoSyncWarning(id) {
  return videoSyncWarnings.value[id] || ''
}

// 内容审核类失败（真人/敏感内容，如火山的 OutputVideoSensitiveContentDetected）：
// 各厂商审核尺度不同，给出切换模型重试的引导
function videoModerationHint(msg) {
  return MODERATION_RE.test(String(msg || '')) ? t('episode.vid.moderationHint') : ''
}

function videoTaskState(sb) {
  // 生成中优先于已有视频：重生成期间主视频虽在，也应显示“生成中”。
  if (isPendingVideo(sb?.id)) return 'pending'
  if (hasVid(sb)) return 'done'
  if (videoFailMessage(sb?.id)) return 'failed'
  return 'ready'
}

function videoTaskStatusLabel(sb) {
  const state = videoTaskState(sb)
  if (state === 'done') return t('episode.status.done')
  if (state === 'pending') {
    if (videoTaskPhases.value[sb?.id] === 'canceling') return t('episode.tasks.canceling')
    if (videoTaskPhases.value[sb?.id] !== 'pending') return t('episode.status.generating')
    const ahead = videoQueueAhead.value[sb?.id]
    return Number.isInteger(ahead) && ahead > 0 ? t('episode.status.queueAhead', { n: ahead }) : t('episode.status.queued')
  }
  if (state === 'failed') return t('episode.status.failed')
  return t('episode.status.todo')
}

function videoTaskStatusTone(sb) {
  const state = videoTaskState(sb)
  if (state !== 'pending') return state
  const phase = videoTaskPhases.value[sb?.id]
  if (phase === 'canceling') return 'canceling'
  return phase === 'pending' ? 'queued' : 'processing'
}

function videoTaskActionLabel(sb) {
  const state = videoTaskState(sb)
  if (state === 'done') return t('episode.asset.regen')
  if (state === 'pending') return t('episode.asset.generating')
  return t('episode.asset.generate')
}

const allVideoTaskRows = computed(() => sbs.value.map((sb, index) => {
  const duration = Number(sb.duration || 5)
  const referenceCount = getShotReferenceImages(sb).length
  const sceneName = getSceneName(sb)
  return {
    id: sb.id,
    index,
    storyboard: sb,
    title: sb.description || t('episode.vid.shotN', { n: String(index + 1).padStart(2, '0') }),
    meta: sceneName,
    duration: Number.isFinite(duration) ? duration : 5,
    referenceCount,
    state: videoTaskState(sb),
    // 只有当前处于失败状态才显示错误,避免重试成功的分镜残留历史错误信息
    error: videoTaskState(sb) === 'failed' ? videoFailMessage(sb.id) : '',
    warning: videoTaskState(sb) === 'pending' ? videoSyncWarning(sb.id) : '',
  }
}))
// 列表筛选：点击顶部统计徽章过滤（再点一次取消）
const videoListFilter = ref('')
const videoTaskRows = computed(() => videoListFilter.value
  ? allVideoTaskRows.value.filter(task => task.state === videoListFilter.value)
  : allVideoTaskRows.value)
const videoTaskDoneCount = computed(() => allVideoTaskRows.value.filter(task => task.state === 'done').length)
const videoTaskFailedCount = computed(() => allVideoTaskRows.value.filter(task => task.state === 'failed').length)
function toggleVideoFilter(state) {
  videoListFilter.value = videoListFilter.value === state ? '' : state
}

// ===== 批量视频：选择模式 + 生成前确认（视频生成成本高，避免误触全量触发） =====
const videoSelectMode = ref(false)
const selectedVideoSbIds = ref([])
const batchVideoConfirm = ref({ open: false, targets: [], mode: 'videos', missingPromptIds: [] })
const episodeVideoPipeline = ref({ running: false, promptTotal: 0, promptCompleted: 0, videoTotal: 0, videoCompleted: 0 })
let episodeVideoPipelineRunToken = 0
let episodeVideoPipelineDisposed = false
function episodeVideoPipelineRunActive(runToken) {
  return !episodeVideoPipelineDisposed && runToken === episodeVideoPipelineRunToken
}
function stopEpisodeVideoPipelineWatcher() {
  episodeVideoPipelineDisposed = true
  episodeVideoPipelineRunToken += 1
}
function episodeVideoPipelineStorageKey() { return `h3:episode-video-pipeline:${epId.value}` }
function saveEpisodeVideoPipelineIntent(targetIds) {
  if (!import.meta.client) return
  localStorage.setItem(episodeVideoPipelineStorageKey(), JSON.stringify({ targetIds, startedAt: Date.now() }))
}
function loadEpisodeVideoPipelineIntent() {
  if (!import.meta.client) return []
  try {
    const value = JSON.parse(localStorage.getItem(episodeVideoPipelineStorageKey()) || '{}')
    return Array.isArray(value.targetIds) ? value.targetIds.map(Number).filter(Number.isInteger) : []
  } catch {
    return []
  }
}
function clearEpisodeVideoPipelineIntent() {
  if (import.meta.client) localStorage.removeItem(episodeVideoPipelineStorageKey())
}
const episodeVideoPipelineButtonLabel = computed(() => {
  if (!episodeVideoPipeline.value.running) return t('episode.vid.fullPipeline')
  return t('episode.vid.fullPipelineProgress', {
    promptDone: episodeVideoPipeline.value.promptCompleted,
    promptTotal: episodeVideoPipeline.value.promptTotal,
    videoDone: episodeVideoPipeline.value.videoCompleted,
    videoTotal: episodeVideoPipeline.value.videoTotal,
  })
})

function isVideoSbSelected(id) { return selectedVideoSbIds.value.includes(id) }
function toggleVideoSbSelect(id) {
  selectedVideoSbIds.value = isVideoSbSelected(id)
    ? selectedVideoSbIds.value.filter(item => item !== id)
    : [...selectedVideoSbIds.value, id]
}
function toggleVideoSelectMode() {
  videoSelectMode.value = !videoSelectMode.value
  if (!videoSelectMode.value) selectedVideoSbIds.value = []
}
function onVideoTaskRowClick(sb) {
  if (videoSelectMode.value) toggleVideoSbSelect(sb.id)
  else selectedSb.value = sb
}

// 旁白角色识别：关键词匹配，兼容存量多语言数据（提取产物中的旁白角色不参与画面生成）
function isNarratorCharacter(char) {
  const text = `${char?.name || ''} ${char?.role || ''}`.toLowerCase()
  return ['旁白', '画外音', 'narrator', 'ナレーター', 'ナレーション', '내레이션', '해설'].some(k => text.includes(k))
}

const visualChars = computed(() => chars.value.filter(c => !isNarratorCharacter(c)))
const lockedVideoConfigId = computed(() => episode.value?.video_config_id || episode.value?.videoConfigId || null)
// 集视频分辨率：顶栏直接修改（持久化 episodes.resolution，生成任务按此值锁定）。
// 内部统一存 480p/720p/1080p 三档，界面按当前选中的视频模型显示厂商原生档位
// （Seedance 480p/720p、MiniMax 768P/2K、Wan 3.0 480P/720P/1080P），适配器再映射为官方枚举
const RESOLUTION_TIERS = {
  volcengine: ['480p', '720p'],
  minimax: ['720p', '1080p'],
  aliyun: ['480p', '720p', '1080p'],
}
const RESOLUTION_DISPLAY = {
  volcengine: { '480p': '480p', '720p': '720p', '1080p': '720p' },
  minimax: { '480p': '768P', '720p': '768P', '1080p': '2K' },
  aliyun: { '480p': '480P', '720p': '720P', '1080p': '1080P' },
}
const resolutionProvider = computed(() => RESOLUTION_TIERS[selectedVideoConfig.value?.provider] ? selectedVideoConfig.value.provider : 'volcengine')
const resolutionOptions = computed(() => RESOLUTION_TIERS[resolutionProvider.value].map(key => ({
  key,
  model: `${RESOLUTION_DISPLAY[resolutionProvider.value][key]} · ${t(`episode.resolution.${key === '480p' ? 'smooth' : key === '720p' ? 'hd' : 'uhd'}`)}`,
})))
const episodeResolution = computed({
  get: () => {
    const v = episode.value?.resolution
    return resolutionOptions.value.some(o => o.key === v) ? v : '720p'
  },
  set: (val) => { void changeEpisodeResolution(val) },
})
async function changeEpisodeResolution(val) {
  if (!episode.value || val === episodeResolution.value) return
  const prev = episode.value.resolution
  episode.value.resolution = val
  const label = resolutionOptions.value.find(o => o.key === val)?.model || val
  try {
    await episodeAPI.update(epId.value, { resolution: val })
    toast.success(t('episode.vid.resolutionSwitched', { label }))
  } catch (e) {
    episode.value.resolution = prev
    toastError(e)
  }
}
// 画面比例在创建项目时固定，视频生成统一使用
const dramaAspectRatio = computed(() => drama.value?.aspect_ratio || drama.value?.aspectRatio || '16:9')

/* ===== 项目风格前缀（资产由提示词 Agent 写入；视频在提交生成任务时直接拼接） ===== */
const stylePresets = ref([])
const STYLE_PREFIX_COLUMN = { character: 'character_style_prefix', scene: 'scene_style_prefix', video: 'video_style_prefix' }
const STYLE_PRESET_NONE = '__none__'
const stylePresetOptions = computed(() => [
  { label: t('common.noStyle'), value: STYLE_PRESET_NONE },
  ...stylePresets.value.map(p => ({ label: p.name, value: p.id })),
])
function stylePrefixFromPreset(preset, target) {
  return preset?.[`${target}_prompt`] || preset?.prompt || ''
}
function presetStylePrefix(target) {
  const row = stylePresets.value.find(p => p.value === (drama.value?.style || ''))
  return stylePrefixFromPreset(row, target)
}
function effectiveStylePrefix(target) {
  const v = drama.value?.[STYLE_PREFIX_COLUMN[target]]
  return v != null ? v : presetStylePrefix(target)
}
async function saveStylePrefix(target, value, { notify = true } = {}) {
  const raw = String(value ?? '')
  // 与预设一致时存 null（恢复跟随）；清空存 ''（有意不加前缀）；否则存自定义值
  const next = raw === presetStylePrefix(target) ? null : raw
  const current = drama.value?.[STYLE_PREFIX_COLUMN[target]] ?? null
  if (current === next) return true
  try {
    const updated = await dramaAPI.update(dramaId, { [STYLE_PREFIX_COLUMN[target]]: next })
    Object.assign(drama.value, { [STYLE_PREFIX_COLUMN[target]]: updated?.[STYLE_PREFIX_COLUMN[target]] ?? next })
    if (notify) toast.success(t('episode.asset.stylePrefixSaved'))
    return true
  } catch (e) {
    toastError(e)
    return false
  }
}
async function importStylePreset(target, presetId) {
  if (presetId === STYLE_PRESET_NONE) {
    const saved = await saveStylePrefix(target, '', { notify: false })
    if (saved) toast.success(t('episode.asset.stylePrefixImported', { name: t('common.noStyle') }))
    return
  }
  const preset = stylePresets.value.find(p => String(p.id) === String(presetId))
  if (!preset) return
  const prefix = stylePrefixFromPreset(preset, target)
  if (!prefix) return
  const saved = await saveStylePrefix(target, prefix, { notify: false })
  if (saved) toast.success(t('episode.asset.stylePrefixImported', { name: preset.name }))
}

// 生成可选模型列表：配置中的模型数组（首位为配置默认）；API 可能返回数组或 JSON 字符串
function configModels(cfg) {
  const raw = cfg?.model
  if (!raw) return []
  if (Array.isArray(raw)) return raw.filter(Boolean)
  try { const m = JSON.parse(raw); return Array.isArray(m) ? m.filter(Boolean) : [m].filter(Boolean) } catch { return [raw].filter(Boolean) }
}
// 汇总该类型全部启用配置的模型（按配置+模型去重，按优先级排序），选中模型时连同所属配置一起调用。
// 配置 ID 必须进入复合键：同一厂商的多个服务实例可能暴露同名模型（如两张 GPU 上的 MiniMax H3）。
function collectModelOptions(cfgs) {
  const seen = new Set()
  const out = []
  const sorted = [...cfgs].filter(c => c.is_active && c.service_enabled !== false).sort((a, b) => (b.priority || 0) - (a.priority || 0))
  for (const c of sorted) {
    for (const m of configModels(c)) {
      const key = `${c.id}/${c.provider}/${m}`
      if (seen.has(key)) continue
      seen.add(key)
      out.push({ key, model: m, provider: c.provider, configId: c.id, configName: c.name || c.provider })
    }
  }
  return out
}
// 复合键 → 裸模型名（后端适配器按厂商校验模型名，不能带配置 ID/provider 前缀）。
// 同时兼容旧版持久化的 provider/model 键；模型名自身包含斜杠时保持完整。
function bareModelName(key) {
  if (!key) return ''
  const parts = key.split('/')
  if (/^\d+$/.test(parts[0]) && parts.length >= 3) return parts.slice(2).join('/')
  return parts.length >= 2 ? parts.slice(1).join('/') : key
}
function ownerConfigId(options, key) {
  return key ? (options.find(o => o.key === key)?.configId || undefined) : undefined
}
function hasMultiConfigs(options) {
  return new Set(options.map(o => o.configId)).size > 1
}
const imageModelOptions = computed(() => collectModelOptions(imageConfigs.value))
const videoModelOptions = computed(() => collectModelOptions(videoConfigs.value))
const selectedVideoModelOption = computed(() =>
  videoModelOptions.value.find(option => option.key === videoModel.value))
const selectedVideoConfig = computed(() => {
  const selected = selectedVideoModelOption.value
  if (selected) return videoConfigs.value.find(config => config.id === selected.configId)
  const locked = videoConfigs.value.find(config => config.id === lockedVideoConfigId.value && config.is_active && config.service_enabled !== false)
  if (locked) return locked
  return [...videoConfigs.value]
    .filter(config => config.is_active && config.service_enabled !== false)
    .sort((a, b) => (b.priority || 0) - (a.priority || 0))[0]
})
const isWan3Video = computed(() => selectedVideoConfig.value?.provider === 'aliyun'
  || bareModelName(videoModel.value).startsWith('wan3.0-video'))
const isDoubaoLocalVideo = computed(() => selectedVideoConfig.value?.provider === 'doubao_local')

// 参考图上限（Wan 3.0 官方 10 张，其他模型 9 张），绑定素材收集与 @名字 映射统一读取
const refImageLimit = computed(() => isWan3Video.value ? 10 : 9)

// 本次生成的生效配置（模型/分辨率/时长），用于右侧小结与批量确认弹窗
const effectiveVideoModelLabel = computed(() => {
  // 只接受当前配置列表中的复合键；旧缓存里的裸 H3 模型不能覆盖豆包配置。
  if (selectedVideoModelOption.value) return selectedVideoModelOption.value.model
  return configModels(selectedVideoConfig.value)[0] || ''
})
const episodeResolutionLabel = computed(() =>
  resolutionOptions.value.find(o => o.key === episodeResolution.value)?.model || episodeResolution.value)
// 短档位标签（480p / 768P / 2K 等厂商原生档位），用于底部生效配置小结
const episodeResolutionShort = computed(() =>
  isDoubaoLocalVideo.value ? '自动' : (RESOLUTION_DISPLAY[resolutionProvider.value][episodeResolution.value] || episodeResolution.value))
const effectiveVideoDuration = computed(() => Number(selectedSb.value?.duration || 10))
const batchVideoTotalDuration = computed(() =>
  batchVideoConfirm.value.targets.reduce((sum, sb) => sum + (Number(sb.duration) || 5), 0))

function openBatchVideoConfirm(pool) {
  const targets = pool.filter(s => !isPendingVideo(s.id))
  if (!targets.length) { toast.info(t('episode.vid.noneToGenerate')); return }
  batchVideoConfirm.value = { open: true, targets, mode: 'videos', missingPromptIds: [] }
}
function completeEpisodeVideos() {
  if (episodeVideoPipeline.value.running || videoPromptBatch.value.running) return
  const targets = sbs.value.filter(sb => !hasVid(sb) && !isPendingVideo(sb.id))
  if (!targets.length) {
    toast.info(pendingVideoIds.value.length ? t('episode.vid.noneToGenerate') : t('episode.vid.allDone'))
    return
  }
  batchVideoConfirm.value = {
    open: true,
    targets,
    mode: 'episode',
    missingPromptIds: targets.filter(sb => !storyboardPromptReady(sb)).map(sb => sb.id),
  }
}
function batchVideos() {
  // 选择模式且有勾选 → 仅所选（允许重出已完成镜头）；否则全部未完成（待生成+失败）
  const useSelection = videoSelectMode.value && selectedVideoSbIds.value.length
  const pool = useSelection
    ? sbs.value.filter(s => selectedVideoSbIds.value.includes(s.id))
    : sbs.value.filter(s => !hasVid(s))
  openBatchVideoConfirm(pool)
}
function retryFailedVideos() {
  openBatchVideoConfirm(sbs.value.filter(s => videoTaskState(s) === 'failed'))
}
function confirmBatchVideos() {
  const plan = {
    mode: batchVideoConfirm.value.mode,
    targetIds: batchVideoConfirm.value.targets.map(sb => sb.id),
  }
  const targets = [...batchVideoConfirm.value.targets]
  batchVideoConfirm.value = { open: false, targets: [], mode: 'videos', missingPromptIds: [] }
  if (!targets.length) return
  if (plan.mode === 'episode') {
    void runEpisodeVideoPipeline(plan.targetIds)
    return
  }
  const ids = targets.map(s => s.id)
  targets.forEach(sb => genVid(sb, { silent: true }))
  toast.success(t('episode.vid.batchStarted', { n: ids.length }))
  if (videoSelectMode.value) toggleVideoSelectMode()
}

// 配置变化后校验持久化的模型是否仍存在（配置被删/模型被移除时回退默认，避免把失效模型传给后端）
function pruneStaleModel(modelRef, optionsRef) {
  watch(optionsRef, opts => {
    if (!modelRef.value || !opts.length) return
    if (opts.some(o => o.key === modelRef.value)) return
    // 旧版本地存储只有裸模型名：能对上则升级为复合键，对不上回退默认
    const legacyModel = bareModelName(modelRef.value)
    const legacy = opts.filter(o => o.model === legacyModel)
    modelRef.value = legacy.length ? legacy[0].key : ''
  }, { immediate: true })
}
pruneStaleModel(imageModel, imageModelOptions)
pruneStaleModel(videoModel, videoModelOptions)
const imageModelMultiCfg = computed(() => hasMultiConfigs(imageModelOptions.value))
const videoModelMultiCfg = computed(() => hasMultiConfigs(videoModelOptions.value))

// Production step helpers
// ========== 任务列表面板 ==========
async function loadGenTasks() {
  if (!epId.value) return false
  try {
    const data = await taskAPI.listByEpisode(epId.value)
    genTasks.value = data?.tasks || []
    genMerges.value = data?.merges || []

    // 图片任务也从持久化 sys_task 恢复：刷新页面后角色/场景/道具卡片仍显示真实生成状态。
    // 并集提交中的 id：generate-image 请求在途（后端占位行尚未落库/可见）时不清掉本地"生成中"。
    const activeImageTasks = genTasks.value.filter(task => task.type === 'image' && (task.status === 'pending' || task.status === 'processing'))
    pendingCharImageIds.value = [...new Set([...activeImageTasks.map(task => task.character_id).filter(Boolean), ...submittingImageIdsOf('character')])]
    pendingSceneImageIds.value = [...new Set([...activeImageTasks.map(task => task.scene_id).filter(Boolean), ...submittingImageIdsOf('scene')])]
    pendingPropImageIds.value = [...new Set([...activeImageTasks.map(task => task.prop_id).filter(Boolean), ...submittingImageIdsOf('prop')])]

    // 生成中/失败状态只存在内存里,页面刷新后丢失;从 sys_task 记录按分镜恢复,
    // 否则已失败的镜头刷新后会退化成"待生成"
    // 活动任务必须优先于终态历史。重复任务清理时，创建时间更晚的 canceled 记录
    // 不能遮住同一分镜仍在 pending/processing 的真实队列任务。
    const effectiveBySb = selectEffectiveVideoTasksByStoryboard(genTasks.value)
    // pending/failed 全量重建而非与现有值并集——否则刷新恢复的"生成中"在任务失败后
    // 永不消退(videoTaskState 中 pending 优先于 failed,重试按钮还被禁用)
    const pending = new Set()
    const failed = {}
    const phases = {}
    const queueAhead = {}
    const warnings = {}
    for (const [sbId, task] of effectiveBySb) {
      if (isActiveVideoTask(task)) {
        // 分镜已有主视频也照常跟踪“生成中”：重生成完成前继续展示旧视频。
        pending.add(sbId)
        phases[sbId] = task.status
        if (task.status === 'pending' && task.queue_ahead != null && Number.isFinite(Number(task.queue_ahead))) {
          queueAhead[sbId] = Math.max(0, Number(task.queue_ahead))
        }
        if (task.error_msg) warnings[sbId] = task.error_msg
      } else if (task.status === 'failed') {
        // 分镜已有视频（失败后重试成功或重生成失败仍保留旧片）时不再报历史错误。
        if (!hasVid(sbs.value.find(s => s.id === sbId))) failed[sbId] = task.error_msg || t('episode.status.failed')
      }
    }
    // 批量生成时其他请求可能仍在提交；只有真正处于提交中的镜头允许暂时没有任务记录。
    for (const id of pendingVideoIds.value) {
      if (isActiveVideoTask(effectiveBySb.get(id))) continue
      if (submittingVideoIds.value.includes(id)) pending.add(id)
      else if (!effectiveBySb.has(id)) failed[id] = t('episode.vid.taskMissing')
    }
    for (const id of Object.keys(failedVideoMessages.value)) {
      if (!effectiveBySb.has(Number(id))) failed[id] = failedVideoMessages.value[id]
    }
    pendingVideoIds.value = [...pending]
    failedVideoMessages.value = failed
    videoTaskPhases.value = phases
    videoQueueAhead.value = queueAhead
    videoSyncWarnings.value = warnings
    generationTaskSyncFailures = 0
    videoStatusSyncInterrupted.value = false
    return true
  } catch {
    // 状态接口暂时不可用时保留后台任务，不误判失败、不开放重复生成。
    if (pendingVideoIds.value.length && ++generationTaskSyncFailures >= 3) videoStatusSyncInterrupted.value = true
    return false
  }
}

function activeImageTaskIds() {
  return new Set(genTasks.value
    .filter(task => task.type === 'image' && (task.status === 'pending' || task.status === 'processing'))
    .map(task => task.id))
}

async function syncGenerationTaskProgress() {
  if (generationTaskSyncBusy) return
  generationTaskSyncBusy = true
  // 保存本轮实际跟踪的活动任务 ID。任务结束后按这个 ID 判定成败，不能再按
  // 分镜“最新创建记录”判断，否则更晚的 canceled 重复记录会吞掉完成通知。
  const beforeVideoTasks = selectActiveVideoTasksByStoryboard(genTasks.value)
  const beforeVideos = new Set(beforeVideoTasks.keys())
  const beforeImages = activeImageTaskIds()
  try {
    if (!await loadGenTasks()) return
    const afterImages = activeImageTaskIds()
    const afterVideoTasks = selectActiveVideoTasksByStoryboard(genTasks.value)
    const endedImages = [...beforeImages].filter(id => !afterImages.has(id))
    const endedVideos = [...beforeVideos].filter(id => !afterVideoTasks.has(id))
    if (!endedImages.length && !endedVideos.length) return

    // 图片完成后不只刷新任务行，也刷新角色/场景/道具卡片及打开中的资产详情。
    // refresh 内会再次拉取任务列表，确保任务终态与资产回写来自同一轮最新数据。
    if (endedImages.length) {
      await refresh()
    } else {
      const fresh = await episodeAPI.storyboards(epId.value)
      sbs.value = fresh || []
      selectedVideoSbIds.value = selectedVideoSbIds.value.filter(id => sbs.value.some(sb => sb.id === id))
      if (sbs.value.length) {
        const selectedId = selectedSb.value?.id
        selectedSb.value = sbs.value.find(sb => sb.id === selectedId) || sbs.value[0]
      } else selectedSb.value = null
    }
    // 成败按本轮实际跟踪的任务判定：重生成期间旧视频仍存在，hasVid 会误报。
    const endedStatuses = endedVideos.map(id => videoTaskStatusById(genTasks.value, beforeVideoTasks.get(id)?.id))
    if (endedStatuses.includes('completed')) toast.success(t('episode.vid.genDone'))
    else if (endedStatuses.includes('failed')) toast.error(t('episode.vid.genFailed'))
    // 失败时主视频不变，watch 不触发；任务结束后统一刷新历史列表。
    if (endedVideos.includes(selectedSb.value?.id)) loadSbVideoHistory()
  } catch { /* 下一轮或手动刷新会继续同步 */ }
  finally { generationTaskSyncBusy = false }
}

function stopGenTasksPolling() {
  if (genTasksTimer) { clearInterval(genTasksTimer); genTasksTimer = null }
}

const genTaskActiveCount = computed(() =>
  genTasks.value.filter(t => t.status === 'pending' || t.status === 'processing').length +
  genMerges.value.filter(m => m.status === 'processing' || m.status === 'pending').length
)
const genTaskDoneCount = computed(() =>
  genTasks.value.filter(t => t.status === 'completed').length +
  genMerges.value.filter(m => m.status === 'completed').length
)
const genTaskFailedCount = computed(() =>
  genTasks.value.filter(t => t.status === 'failed').length +
  genMerges.value.filter(m => m.status === 'failed').length
)
const genTaskCanceledCount = computed(() => genTasks.value.filter(t => t.status === 'canceled').length)

function genTaskTargetLabel(task) {
  if (task.storyboard_id) {
    const sb = sbs.value.find(x => x.id === task.storyboard_id)
    return t('episode.tasks.sbN', { n: sb?.storyboard_number ?? sb?.storyboardNumber ?? task.storyboard_id })
  }
  if (task.character_id) {
    const c = chars.value.find(x => x.id === task.character_id)
    return `${t('common.role')} · ${c?.name || task.character_id}`
  }
  if (task.scene_id) {
    const s = scenes.value.find(x => x.id === task.scene_id)
    return `${t('common.scene')} · ${s?.location || task.scene_id}`
  }
  if (task.prop_id) {
    const p = propItems.value.find(x => x.id === task.prop_id)
    return `${t('common.prop')} · ${p?.name || task.prop_id}`
  }
  return t('episode.tasks.generic')
}

// 统一行结构：image / video / merge 三类合并按时间倒序
const genTaskRows = computed(() => {
  const taskRows = genTasks.value.map(t => ({
    key: `task-${t.id}`,
    kind: t.type, // image | video
    id: t.id,
    targetLabel: genTaskTargetLabel(t),
    provider: t.provider || '',
    model: t.model || '',
    status: cancelingTaskIds.value.includes(t.id) ? 'canceling' : (t.status || 'processing'),
    queueAhead: t.queue_ahead == null ? null : Math.max(0, Number(t.queue_ahead)),
    taskId: t.task_id || '',
    errorMsg: t.error_msg || '',
    previewUrl: t.local_path || t.result_url || '',
    prompt: t.prompt || '',
    createdAt: t.created_at || '',
    completedAt: t.completed_at || '',
  }))
  const mergeRows = genMerges.value.map(m => ({
    key: `merge-${m.id}`,
    kind: 'merge',
    id: m.id,
    targetLabel: t('episode.tasks.fullMerge'),
    provider: m.provider || 'ffmpeg',
    model: m.model || '',
    status: m.status || 'pending',
    errorMsg: m.error_msg || '',
    previewUrl: m.merged_url || '',
    prompt: '',
    createdAt: m.created_at || '',
    completedAt: m.completed_at || '',
  }))
  return [...taskRows, ...mergeRows].sort((a, b) => (b.createdAt || '').localeCompare(a.createdAt || ''))
})

function genTaskKindLabel(kind) {
  return kind === 'image' ? t('common.serviceType.image') : kind === 'video' ? t('common.serviceType.video') : t('episode.tasks.mergeKind')
}

function genTaskStatusLabel(row) {
  const status = row.status
  if (status === 'completed') return t('episode.status.done')
  if (status === 'failed') return t('episode.status.failed')
  if (status === 'canceled') return t('episode.status.canceled')
  if (status === 'canceling') return t('episode.tasks.canceling')
  if (status === 'pending') {
    return row.kind === 'video' && Number.isFinite(row.queueAhead) && row.queueAhead > 0
      ? t('episode.status.queueAhead', { n: row.queueAhead })
      : t('episode.status.queued')
  }
  return t('episode.status.generating')
}

// 排队、实际生成与取消中使用独立视觉状态，避免活动任务全部显示成同一种蓝色。
function genTaskStateClass(status) {
  if (status === 'completed') return 'done'
  if (status === 'failed') return 'failed'
  if (status === 'canceled') return 'canceled'
  if (status === 'canceling') return 'canceling'
  if (status === 'pending') return 'queued'
  return 'processing'
}

function canCancelVideoTask(row) {
  return row.kind === 'video'
    && String(row.provider || '').toLowerCase() === 'minimax'
    && !cancelingTaskIds.value.includes(row.id)
    && (row.status === 'pending' || row.status === 'processing')
}

function canRecallDoubaoVideo(row) {
  return row.kind === 'video'
    && String(row.provider || '').toLowerCase() === 'doubao_local'
    && row.status === 'failed'
    && !!row.taskId
}

async function recallDoubaoVideo(row) {
  if (!canRecallDoubaoVideo(row) || recallingTaskIds.value.includes(row.id)) return
  if (!window.confirm(t('episode.tasks.recallConfirm'))) return
  recallingTaskIds.value = [...recallingTaskIds.value, row.id]
  try {
    const recalled = await taskAPI.recallVideo(row.id)
    await loadGenTasks()
    if (recalled?.status === 'completed') await refresh()
    toast.success(recalled?.status === 'completed' ? t('episode.tasks.recallDone') : t('episode.tasks.recallResumed'))
  } catch (err) {
    toastError(err)
  } finally {
    recallingTaskIds.value = recallingTaskIds.value.filter(id => id !== row.id)
  }
}

async function cancelVideoTask(row) {
  if (!canCancelVideoTask(row)) return
  const task = genTasks.value.find(item => item.id === row.id)
  const previousStatus = task?.status || row.status
  // 点击后立即进入取消中，隐藏取消按钮并阻止重复请求。
  cancelingTaskIds.value = [...cancelingTaskIds.value, row.id]
  if (task) task.status = 'canceling'
  try {
    await taskAPI.cancel(row.id)
    await loadGenTasks()
    toast.success(t('episode.tasks.cancelSuccess'))
  } catch (err) {
    if (task) task.status = previousStatus
    toastError(err)
  } finally {
    cancelingTaskIds.value = cancelingTaskIds.value.filter(id => id !== row.id)
  }
}

// local_path 为站内相对路径补 '/',远端 result_url 原样使用
function genTaskPreviewSrc(url) {
  if (!url) return ''
  return /^https?:\/\//.test(url) ? url : '/' + url
}

function genTaskDuration(row) {
  if (!row.createdAt || !row.completedAt) return ''
  const ms = new Date(row.completedAt).getTime() - new Date(row.createdAt).getTime()
  if (!Number.isFinite(ms) || ms < 0) return ''
  return ms >= 60000 ? `${Math.floor(ms / 60000)}m${Math.round((ms % 60000) / 1000)}s` : `${Math.round(ms / 1000)}s`
}

// 所有生成任务无论抽屉是否打开都持续同步；图片请求在提示词阶段也会先创建占位任务，
// submittingImageKeys 让前端从点击提交起就开始发现并刷新该任务。
watch([genTaskActiveCount, () => pendingVideoIds.value.length, () => submittingImageKeys.value.length], ([active, pendingVideos, submittingImages]) => {
  stopGenTasksPolling()
  if (active <= 0 && pendingVideos <= 0 && submittingImages <= 0) return
  void syncGenerationTaskProgress()
  genTasksTimer = setInterval(syncGenerationTaskProgress, 4000)
})

const productionBlockMessage = computed(() => {
  if (!scriptContent.value) return t('episode.prod.scriptFirst')
  return ''
})
const productionBlockActionLabel = computed(() => {
  if (!scriptContent.value) return t('episode.export.gotoScript')
  return t('episode.prod.goBack')
})
function goProductionBlockTarget() {
  if (!scriptContent.value) {
    panel.value = 'script'
    scriptStep.value = rawContent.value ? 1 : 0
    return
  }
  panel.value = 'production'
  prodTab.value = 'assets'
}
const stepLabels = computed(() => [t('episode.script.raw'), t('episode.script.rewrite')])

const charImgCount = computed(() => visualChars.value.filter(c => c.image_url || c.imageUrl).length)
const sceneImgCount = computed(() => scenes.value.filter(s => s.image_url || s.imageUrl).length)
const propImgCount = computed(() => propItems.value.filter(p => p.image_url || p.imageUrl).length)
const shotVidCount = computed(() => sbs.value.filter(s => s.video_url || s.videoUrl).length)
const visualCharTotal = computed(() => visualChars.value.length)
const pendingCharacterImageCount = computed(() => Math.max(visualCharTotal.value - charImgCount.value, 0))
const pendingSceneImageCount = computed(() => Math.max(scenes.value.length - sceneImgCount.value, 0))
const pendingAssetImageCount = computed(() => pendingCharacterImageCount.value + pendingSceneImageCount.value)
const assetTotalCount = computed(() => visualCharTotal.value + scenes.value.length + propItems.value.length)
const assetReadyCount = computed(() => charImgCount.value + sceneImgCount.value + propImgCount.value)

const prodTabDefs = computed(() => [
  { id: 'assets', label: t('episode.prod.assets'), icon: FolderKanban, badge: assetTotalCount.value ? `${assetReadyCount.value}/${assetTotalCount.value}` : '' },
  { id: 'breakdown', label: t('episode.prod.breakdown'), icon: ListTodo, badge: sbs.value.length ? String(sbs.value.length) : '' },
  { id: 'videos', label: t('episode.prod.videos'), icon: Clapperboard, badge: sbs.value.length ? `${shotVidCount.value}/${sbs.value.length}` : '' },
])

const mainStageDefs = computed(() => ([
  { id: 'script', label: t('episode.stage.script'), desc: t('episode.stage.scriptDesc'), icon: FileText },
  { id: 'assets', label: t('episode.prod.assets'), desc: t('episode.stage.assetsDesc'), icon: FolderKanban },
  { id: 'breakdown', label: t('episode.prod.breakdown'), desc: t('episode.stage.breakdownDesc'), icon: ListTodo },
  { id: 'videos', label: t('episode.stage.videos'), desc: t('episode.stage.videosDesc'), icon: Clapperboard },
  { id: 'export', label: t('episode.stage.export'), desc: t('episode.stage.exportDesc'), icon: Download },
]))

const sidebarSections = computed(() => ([
  {
    id: 'script',
    label: t('episode.stage.script'),
    items: [
      { key: 'script:raw', label: t('episode.script.raw'), desc: '', icon: FileText },
      { key: 'script:rewrite', label: t('episode.script.rewrite'), desc: '', icon: FileText },
    ],
  },
  {
    id: 'production',
    label: t('episode.stage.production'),
    items: [
      { key: 'prod:assets', label: t('episode.prod.assets'), desc: '', icon: Users },
      { key: 'prod:breakdown', label: t('episode.prod.breakdown'), desc: '', icon: ListTodo },
      { key: 'prod:videos', label: t('episode.prod.videos'), desc: '', icon: Clapperboard },
    ],
  },
  {
    id: 'export',
    label: t('episode.stage.export'),
    items: [
      { key: 'export:merge', label: t('episode.stage.mergeExport'), desc: '', icon: Download },
    ],
  },
]))

// 大环节状态:pending(未开始)/ active(进行中)/ done(已完成)/ none(不显示状态,导出用)
// 进行中 = 环节内有任意进度但未全部完成,或当前正处于该环节
function sectionState(sectionId) {
  if (sectionId === 'export') return 'none'
  const done = sectionId === 'script'
    ? mainStageDone('script')
    : mainStageDone('assets') && mainStageDone('breakdown') && mainStageDone('videos')
  if (done) return 'done'

  const hasProgress = sectionId === 'script'
    ? !!(rawContent.value || scriptContent.value)
    : !!(chars.value.length || scenes.value.length || propItems.value.length || sbs.value.length || shotVidCount.value)
  const isCurrent = sectionId === 'script'
    ? panel.value === 'script'
    : panel.value === 'production'
  return (hasProgress || isCurrent) ? 'active' : 'pending'
}

const activeMainStage = computed(() => {
  if (panel.value === 'export') return 'export'
  if (panel.value === 'production') {
    return prodTab.value
  }
  return 'script'
})

function mainStageDone(stageId) {
  if (stageId === 'script') return !!scriptContent.value
  if (stageId === 'assets') return assetTotalCount.value > 0 && assetReadyCount.value === assetTotalCount.value
  if (stageId === 'breakdown') return sbs.value.length > 0
  if (stageId === 'videos') {
    return !!sbs.value.length && shotVidCount.value === sbs.value.length
  }
  if (stageId === 'export') return exportDone.value
  return false
}

function goMainStage(stageId) {
  if (stageId === 'script') {
    panel.value = 'script'
    scriptStep.value = Math.min(scriptStep.value, 1)
    return
  }
  if (stageId === 'assets') {
    panel.value = 'production'
    prodTab.value = 'assets'
    return
  }
  if (stageId === 'breakdown') {
    panel.value = 'production'
    prodTab.value = 'breakdown'
    return
  }
  if (stageId === 'videos') {
    panel.value = 'production'
    prodTab.value = 'videos'
    return
  }
  panel.value = 'export'
}

const activeSubStepKey = computed(() => {
  if (panel.value === 'script') {
    if (scriptStep.value === 0) return 'script:raw'
    return 'script:rewrite'
  }
  if (panel.value === 'production') return `prod:${prodTab.value}`
  return 'export:merge'
})

// 步骤跑马灯：剧本 → 资产制作 → 拆分分解 → 视频制作 → 导出
const mainProgressSteps = computed(() => [
  { id: 'script', label: t('episode.stage.script') },
  { id: 'assets', label: t('episode.prod.assets') },
  { id: 'breakdown', label: t('episode.prod.breakdown') },
  { id: 'videos', label: t('episode.stage.videos') },
  { id: 'export', label: t('episode.stage.export') },
])
const currentMainIdx = computed(() => {
  const i = mainProgressSteps.value.findIndex(s => s.id === activeMainStage.value)
  return i < 0 ? 0 : i
})

function goSubStep(key) {
  if (key.startsWith('script:')) {
    panel.value = 'script'
    const stepMap = {
      'script:raw': 0,
      'script:rewrite': 1,
    }
    scriptStep.value = stepMap[key] ?? 0
    return
  }
  if (key.startsWith('prod:')) {
    panel.value = 'production'
    prodTab.value = key.replace('prod:', '')
    return
  }
  panel.value = 'export'
}

const pipelineTotal = 2
const pipelineProgress = computed(() =>
  ['script', 'production'].filter(id => sectionState(id) === 'done').length
)

const currentStageLabel = computed(() => {
  if (panel.value === 'script') return t('episode.stage.scriptStage', { step: stepLabels.value[scriptStep.value] })
  if (panel.value === 'production') return t('episode.stage.prodStage', { step: prodTabDefs.value[prodTabIdx.value]?.label || t('episode.stage.production') })
  return exportDone.value ? t('episode.stage.exportDone') : t('episode.stage.exportWaiting')
})

const currentMainStageLabel = computed(() => {
  const current = mainStageDefs.value.find(stage => stage.id === activeMainStage.value)
  return current?.label || t('episode.stage.workbench')
})

const currentSubStageLabel = computed(() => currentStageLabel.value)

const totalDuration = computed(() => sbs.value.reduce((s, sb) => s + (sb.duration || 10), 0))
const breakdownSceneBoundCount = computed(() => sbs.value.filter(sb => !!(sb.scene_id || sb.sceneId)).length)
const breakdownPromptReadyCount = computed(() => sbs.value.filter(storyboardPromptReady).length)

function storyboardPromptReady(sb) {
  return !!String(sb?.video_prompt || sb?.videoPrompt || '').trim()
}

function storyboardDisplayTitle(sb, index = 0) {
  const explicit = String(sb?.title || '').trim()
  if (explicit) return explicit
  const description = String(sb?.description || '').trim()
  if (description) {
    const first = description.split(/[。！？\n]/)[0].trim()
    if (first) return first.length > 18 ? `${first.slice(0, 18)}…` : first
  }
  return t('episode.vid.shotN', { n: String(index + 1).padStart(2, '0') })
}

function openBreakdownStoryboard(sb) {
  // 运行中的列表来自隔离草稿，使用的是临时 ID；发布前只能预览进度，
  // 不能把临时 ID 带进正式分镜编辑/视频生成链路。
  if (breakdownRunning.value) return
  selectedSb.value = sb
  prodTab.value = 'videos'
}

const selectedSb = ref(null)
const selectedVideoTaskNumber = computed(() => {
  const index = videoTaskRows.value.findIndex(task => String(task.id) === String(selectedSb.value?.id))
  return index >= 0 ? index + 1 : 0
})

function updateField(sb, field, value) {
  const current = sb[field] ?? sb[toCamel(field)]
  if (current === value) return
  sb[field] = value
  const camelField = toCamel(field)
  if (camelField !== field) sb[camelField] = value
  storyboardAPI.update(sb.id, { [field]: value }).catch(e => toastError(e))
}

function toCamel(field) {
  return field.replace(/_([a-z])/g, (_, c) => c.toUpperCase())
}

function getStoryboardCharacterIds(sb) {
  return sb?.character_ids || sb?.characterIds || []
}

function getStoryboardCharacters(sb) {
  const ids = getStoryboardCharacterIds(sb)
  return visualChars.value.filter(char => ids.includes(char.id))
}

// ─── 逐镜服装状态（storyboard_characters.state_id；null = 底图）───────────
function getShotCharacterStateId(sb, charId) {
  const map = sb?.character_state_ids || sb?.characterStateIds || {}
  const raw = map[String(charId)]
  return raw == null || raw === '' ? null : Number(raw)
}
// 本镜该角色的参考图：选中且状态图已生成时取状态图，否则回落脚本底图；
// 参考图数组与 @图片N 索引映射必须共用本函数，否则文字会挂错参考图。
function charStateImageForShot(sb, char) {
  const base = char?.image_url || char?.imageUrl || ''
  const stateId = getShotCharacterStateId(sb, char?.id)
  if (!stateId) return base
  const st = (char?.states || []).find(s => s.id === stateId)
  const url = st ? (st.image_url || st.imageUrl || '') : ''
  return url || base
}
function setShotCharacterState(sb, charId, stateId) {
  const next = { ...(sb.character_state_ids || sb.characterStateIds || {}) }
  // 切回底图必须显式置 null 提交：删键会被后端 diff 同步当作「未提及」跳过，
  // 旧选择残留在库里，刷新后又「复活」成之前的造型。
  next[String(charId)] = stateId ?? null
  sb.character_state_ids = next
  sb.characterStateIds = next
  storyboardAPI.update(sb.id, { character_state_ids: next }).catch(e => toastError(e))
}

function getStoryboardScene(sb) {
  const sceneId = sb?.scene_id || sb?.sceneId
  if (!sceneId) return null
  return scenes.value.find(s => s.id === sceneId) || null
}

function isStoryboardCharacterSelected(sb, charId) {
  return getStoryboardCharacterIds(sb).includes(charId)
}

function toggleStoryboardCharacter(sb, charId) {
  const currentIds = getStoryboardCharacterIds(sb)
  const nextIds = currentIds.includes(charId)
    ? currentIds.filter(id => id !== charId)
    : [...currentIds, charId]
  updateField(sb, 'character_ids', nextIds)
}

function getStoryboardPropIds(sb) {
  return sb?.prop_ids || sb?.propIds || []
}

function getStoryboardProps(sb) {
  const ids = getStoryboardPropIds(sb)
  return propItems.value.filter(p => ids.includes(p.id))
}

function isStoryboardPropSelected(sb, propId) {
  return getStoryboardPropIds(sb).includes(propId)
}

function toggleStoryboardProp(sb, propId) {
  const currentIds = getStoryboardPropIds(sb)
  const nextIds = currentIds.includes(propId)
    ? currentIds.filter(id => id !== propId)
    : [...currentIds, propId]
  updateField(sb, 'prop_ids', nextIds)
}

function getSceneName(sb) {
  const scene = getStoryboardScene(sb)
  if (!scene) return ''
  return `${scene.location} · ${scene.time || t('episode.asset.noTime')}`
}

const sceneOptions = computed(() => [
  { label: t('episode.sb.unboundScene'), value: '' },
  ...scenes.value.map(s => ({ label: `${s.location} · ${s.time || t('episode.asset.noTime')}`, value: s.id })),
])


function sceneShotCount(sceneId) {
  return sbs.value.filter(sb => String(sb?.scene_id || sb?.sceneId || '') === String(sceneId)).length
}

watch(rawContent, v => { localRaw.value = v }, { immediate: true })
watch(scriptContent, v => { localScript.value = v }, { immediate: true })

// 全量加载太重：本集数据改走单集接口，四类列表 + 拼接/任务状态全部并行；
// 项目详情（工作台仅用画幅比例一项）只在首次加载或手动刷新时请求。
// options.drama=true 强制重拉项目详情（手动刷新）。
async function refresh(options = {}) {
  let loaded = false
  const wantDrama = options.drama === true || !drama.value
  const fail = e => ({ __error: e })
  // 第一波：定位本集 + 项目详情（仅在需要时）。路由只有 drama+集号，
  // episode 未加载过时 epId 为 0，必须按集号解析出本集。
  const [epRes, dramaRes] = await Promise.all([
    epId.value ? episodeAPI.get(epId.value).catch(fail) : episodeAPI.byNumber(dramaId, episodeNumber).catch(fail),
    wantDrama ? dramaAPI.get(dramaId).catch(fail) : Promise.resolve(undefined),
  ])
  if (!stylePresets.value.length) stylePresetAPI.list().then(res => { stylePresets.value = res || [] }).catch(() => {})
  if (wantDrama) {
    if (dramaRes?.__error) { toastError(dramaRes.__error); return false }
    drama.value = dramaRes
  }
  if (epRes?.__error) { toastError(epRes.__error); return false }
  episode.value = epRes
  const id = epRes.id
  // 第二波：四类列表 + 拼接状态并行（生成任务/导出记录内部已吞异常）
  const [charList, sceneList, propList, sbRes, mergeStatus] = await Promise.all([
    episodeAPI.characters(id).catch(() => []),
    episodeAPI.scenes(id).catch(() => []),
    episodeAPI.props(id).catch(() => []),
    episodeAPI.storyboards(id).catch(fail),
    mergeAPI.status(id).catch(() => null),
    loadGenTasks(),
    loadExportMerges(),
  ])
  chars.value = charList
  scenes.value = sceneList
  propItems.value = propList
  if (assetDetail.value.open && assetDetail.value.item?.id) {
    const list = assetDetail.value.type === 'character' ? chars.value : assetDetail.value.type === 'scene' ? scenes.value : propItems.value
    const freshAsset = list.find(item => item.id === assetDetail.value.item.id)
    if (freshAsset) assetDetail.value = { ...assetDetail.value, item: freshAsset }
  }
  if (mergeStatus) mergeData.value = mergeStatus
  if (sbRes?.__error) { toastError(sbRes.__error); return loaded }
  applyStoryboardRows(sbRes)

  const epHasContent = !!(episode.value?.content)
  const epHasScript = !!(episode.value?.script_content || episode.value?.scriptContent)

  if (panelRestored) {
    // 已恢复到上次所在步骤，跳过自动重置（仅首次加载生效）
    panelRestored = false
  } else if (epHasScript || epHasContent) scriptStep.value = 1
  else scriptStep.value = 0
  loaded = true
  return loaded
}

async function manualRefresh() {
  if (dataRefreshing.value) return
  dataRefreshing.value = true
  try {
    const loaded = await refresh({ drama: true })
    await loadConfigs()
    await syncExtractStatus()
    if (loaded) toast.success(t('episode.sidebar.refreshDone'))
  } finally {
    dataRefreshing.value = false
  }
}

async function saveRaw() {
  const nextRaw = localRaw.value
  await episodeAPI.update(epId.value, { content: nextRaw })
  episode.value.content = nextRaw
}
async function saveRawWithToast() {
  try {
    await saveRaw()
    toast.success(t('episode.script.saved'))
  } catch (e) {
    toastError(e)
  }
}
function onNovelSourceImported(payload) {
  localRaw.value = payload.text
  episode.value.content = payload.text
  episode.value.source_start_chapter = payload.start_chapter
  episode.value.source_end_chapter = payload.end_chapter
  toast.success(`已导入小说第 ${payload.start_chapter}–${payload.end_chapter} 章`)
}

function exportScriptTxt() {
  const content = String(localScript.value || '')
  if (!content.trim()) return
  const safeName = String(drama.value?.title || `drama-${dramaId}`)
    .replace(/[\\/:*?"<>|\s]+/g, '_')
    .slice(0, 40) || 'h3'
  const filename = `${safeName}_第${episodeNumber}集_剧本.txt`
  // BOM 让 Windows 记事本能稳定识别 UTF-8 中文。
  const blob = new Blob([`\uFEFF${content}`], { type: 'text/plain;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
  toast.success(t('episode.script.exportedTxt'))
}

// 手动编辑剧本的显式保存：有未保存修改时按钮高亮，保存成功后恢复置灰
const scriptSaving = ref(false)
const scriptDirty = computed(() => !!scriptContent.value && localScript.value !== scriptContent.value)
async function saveScrManual() {
  if (!scriptDirty.value || scriptSaving.value) return
  scriptSaving.value = true
  try {
    await episodeAPI.update(epId.value, { script_content: localScript.value })
    episode.value.script_content = localScript.value
    toast.success(t('episode.script.saved'))
  } catch (e) { toastError(e) } finally { scriptSaving.value = false }
}
// 发给 Agent 的 message 是功能性提示词而非 UI 文案：固定中文
async function doRewrite() {
  const raw = (localRaw.value || rawContent.value || '').trim()
  if (!raw) {
    toast.warning(t('episode.script.rawRequired'))
    return
  }
  const executionEngine = selectedV8Engine()
  if (!executionEngine) return
  try {
    await saveRaw()
    await runAgent('script_rewriter', rewriteMessage(), dramaId, epId.value, refresh, chatModelOverride(), chatConfigId(), executionEngine)
  } catch (e) {
    toastError(e)
  }
}
// 右侧栏的用户要求拼进改写提示词：功能性提示词固定中文，不走 i18n
function rewriteMessage() {
  const requirements = rewriteForm.requirements.trim()
  if (!requirements) {
    return '请读取本集原始内容，按照当前生效的剧本规范改写为完整的格式化剧本并保存。'
  }
  return [
    '请读取本集原始内容，改写为完整的格式化剧本并保存。',
    '以下内容来自 AI 改写右侧栏，是本次任务的显式约束；请逐项落实。若与默认创作偏好冲突，以右侧栏要求为准，但不得违反剧本格式硬性要求：',
    `- 用户给 Agent 的要求：${requirements}`,
  ].join('\n')
}
async function skipRewrite() {
  const raw = (localRaw.value || rawContent.value || '').trim()
  if (!raw) {
    toast.warning(t('episode.script.rawRequired'))
    return
  }
  try {
    await episodeAPI.update(epId.value, { script_content: raw })
    localScript.value = raw
    episode.value.script_content = raw
    toast.success(t('episode.script.skipDone'))
    panel.value = 'production'
    prodTab.value = 'assets'
  } catch (e) {
    toastError(e)
  }
}
// 资产提取：按类型独立的异步任务（后端任务表驱动），三类可并行；前端轮询状态直到完成
// label 渲染时求值（不能模块级常量固化），key 为逻辑值
const EXTRACT_TARGETS = computed(() => [
  { key: 'characters', label: t('common.role') },
  { key: 'scenes', label: t('common.scene') },
  { key: 'props', label: t('common.prop') },
])
const extractingTargets = ref([])
const extractingLabels = computed(() => EXTRACT_TARGETS.value.filter(x => extractingTargets.value.includes(x.key)).map(x => x.label).join(t('common.listJoin')))
function isExtracting(target) { return extractingTargets.value.includes(target) }

async function doExtract(target) {
  if (isExtracting(target) || !epId.value) return
  const executionEngine = selectedV8Engine()
  if (!executionEngine) return
  extractingTargets.value.push(target)
  try {
    // 先等待用户当前编辑的原文/剧本落库，防止提取请求超车读到旧内容。
    if (localScript.value !== scriptContent.value) {
      await episodeAPI.update(epId.value, { script_content: localScript.value })
      episode.value.script_content = localScript.value
    }
    await episodeAPI.extract(epId.value, target, chatModelOverride(), chatConfigId(), executionEngine)
    pollExtractStatus(target)
  } catch (e) {
    extractingTargets.value = extractingTargets.value.filter(x => x !== target)
    toastError(e)
  }
}
function doExtractAll() { EXTRACT_TARGETS.value.forEach(x => doExtract(x.key)) }

function pollExtractStatus(target, attempts = 150) {
  const label = EXTRACT_TARGETS.value.find(x => x.key === target)?.label || target
  const tick = async (left) => {
    try {
      const st = await episodeAPI.extractStatus(epId.value)
      const task = st?.[target]
      if (task && task.status !== 'running') {
        extractingTargets.value = extractingTargets.value.filter(x => x !== target)
        if (task.status === 'done') {
          toast.success(t('episode.extract.done', { type: label }))
          await refresh()
        } else {
          toastError(task.error, { fallback: 'episode.extract.failed' })
        }
        return
      }
    } catch {}
    if (left > 0) setTimeout(() => tick(left - 1), 2500)
    else extractingTargets.value = extractingTargets.value.filter(x => x !== target)
  }
  setTimeout(() => tick(attempts), 2500)
}

/** 页面加载后恢复仍在运行的提取任务状态（刷新页面不丢进度展示） */
async function syncExtractStatus() {
  if (!epId.value) return
  try {
    const st = await episodeAPI.extractStatus(epId.value)
    for (const x of EXTRACT_TARGETS.value) {
      if (st?.[x.key]?.status === 'running' && !isExtracting(x.key)) {
        extractingTargets.value.push(x.key)
        pollExtractStatus(x.key)
      }
    }
  } catch {}
  try {
    const vp = await episodeAPI.videoPromptsStatus(epId.value)
    if (vp?.status === 'running' && !videoPromptBatch.value.running) {
      videoPromptBatch.value = {
        running: true,
        total: vp.total || 0,
        completed: (vp.completed || 0) + (vp.failed || 0),
        queueStatus: String(vp.queue_status || ''),
        queuePosition: Number(vp.queue_position || 0),
      }
      // 整集流水线刷新恢复时由专用 waiter 接管，避免普通轮询抢先消费完成状态却不继续提交视频。
      if (!loadEpisodeVideoPipelineIntent().length) pollVideoPromptBatch()
    }
  } catch {}
}

// ─── 批量视频提示词：后端异步逐分镜生成，前端轮询进度 ──────────
const videoPromptBatch = ref({ running: false, total: 0, completed: 0, queueStatus: '', queuePosition: 0 })
// 单个视频提示词生成：按分镜 ID 跟踪，允许不同分镜并行生成（不走全局 rn 锁）
const videoPromptGeneratingIds = ref([])
// 视频制作页多选快捷操作：全选 / 仅选未生成视频（勾选集与批量视频共用 selectedVideoSbIds）
function toggleSelectAllVideos() {
  selectedVideoSbIds.value = selectedVideoSbIds.value.length === sbs.value.length ? [] : sbs.value.map(sb => sb.id)
}
function selectMissingVideos() {
  selectedVideoSbIds.value = sbs.value.filter(sb => !hasVid(sb)).map(sb => sb.id)
}

async function batchVideoPrompts() {
  if (videoPromptBatch.value.running || !epId.value) return
  if (!sbs.value.length) { toast.warning(t('episode.sb.breakFirst')); return }
  // 选择模式下有勾选 → 仅补齐所选；否则全量补齐缺失
  const ids = (videoSelectMode.value && selectedVideoSbIds.value.length) ? [...selectedVideoSbIds.value] : undefined
  try {
    const res = await episodeAPI.generateVideoPrompts(
      epId.value,
      chatModelOverride(),
      chatConfigId(),
      ids,
      effectiveVideoModelLabel.value || undefined,
      selectedVideoConfig.value?.id,
    )
    if (!res?.total) {
      if (res?.already_running) {
        videoPromptBatch.value = { running: true, total: 0, completed: 0, queueStatus: '', queuePosition: 0 }
        pollVideoPromptBatch()
      } else toast.info(ids ? t('episode.sb.selectedMissing') : t('episode.sb.allHavePrompts'))
      return
    }
    videoPromptBatch.value = { running: true, total: res.total, completed: 0, queueStatus: '', queuePosition: 0 }
    toast.info(t('episode.sb.batchStarted', { n: res.total }))
    pollVideoPromptBatch()
  } catch (e) {
    toastError(e)
  }
}

function pollVideoPromptBatch(attempts = 240) {
  const tick = async (left) => {
    try {
      const st = await episodeAPI.videoPromptsStatus(epId.value)
      if (st && st.status !== 'running') {
        videoPromptBatch.value = { running: false, total: 0, completed: 0, queueStatus: '', queuePosition: 0 }
        await refresh()
        if (st.status === 'done') {
          if (st.failed) {
            const failedIds = Array.isArray(st.failed_storyboard_ids) ? st.failed_storyboard_ids.join('、') : ''
            toast.warning(failedIds
              ? t('episode.sb.batchDoneFailedDetail', { n: st.failed, ids: failedIds })
              : t('episode.sb.batchDoneFailed', { n: st.failed }))
          } else toast.success(t('episode.sb.batchDone'))
        } else {
          toastError(st.error, { fallback: 'episode.sb.batchFailed' })
        }
        return
      }
      if (st) {
        const prev = videoPromptBatch.value.completed
        const processed = (st.completed || 0) + (st.failed || 0)
        videoPromptBatch.value = {
          running: true,
          total: st.total || 0,
          completed: processed,
          queueStatus: String(st.queue_status || ''),
          queuePosition: Number(st.queue_position || 0),
        }
        if (processed !== prev) await refresh() // 每处理一条刷新，提示词逐步出现并避免失败项让进度看似卡住
      }
    } catch {}
    if (left > 0) setTimeout(() => tick(left - 1), 2500)
    else videoPromptBatch.value = { running: false, total: 0, completed: 0, queueStatus: '', queuePosition: 0 }
  }
  setTimeout(() => tick(attempts), 2500)
}

async function submitReadyEpisodeVideos(targetIds, attemptedIds, runToken) {
  if (!episodeVideoPipelineRunActive(runToken)) return { submitted: 0, failed: 0, stopped: true }
  const targetSet = new Set(targetIds)
  const ready = sbs.value.filter(sb =>
    targetSet.has(sb.id)
    && storyboardPromptReady(sb)
    && !hasVid(sb)
    && !isPendingVideo(sb.id)
    && !attemptedIds.has(sb.id))
  if (!ready.length) return { submitted: 0, failed: 0, stopped: false }

  // 请求发出前先占位，防止刷新或下一轮轮询重复提交同一分镜。
  ready.forEach(sb => attemptedIds.add(sb.id))
  const results = await Promise.all(ready.map(async sb => await genVid(sb, { silent: true })))
  const submitted = results.filter(Boolean).length
  // 提交即完成不代表出片，视频进度统一由 waitForEpisodeVideoGeneration 按任务终态计数。
  return { submitted, failed: ready.length - submitted, stopped: !episodeVideoPipelineRunActive(runToken) }
}

async function waitForEpisodeVideoPrompts(targetIds, attemptedIds, submission, runToken, attempts = 1440) {
  let previousProcessed = -1
  for (let i = 0; i < attempts; i++) {
    await sleep(2500)
    if (!episodeVideoPipelineRunActive(runToken)) return null
    let st
    try {
      st = await episodeAPI.videoPromptsStatus(epId.value)
    } catch {
      continue
    }
    if (!st) continue
    const processed = (st.completed || 0) + (st.failed || 0)
    videoPromptBatch.value = {
      running: st.status === 'running',
      total: st.total || episodeVideoPipeline.value.promptTotal,
      completed: processed,
      queueStatus: String(st.queue_status || ''),
      queuePosition: Number(st.queue_position || 0),
    }
    episodeVideoPipeline.value.promptCompleted = processed
    episodeVideoPipeline.value.promptTotal = st.total || episodeVideoPipeline.value.promptTotal
    if (processed !== previousProcessed) {
      previousProcessed = processed
      await refresh()
      if (!episodeVideoPipelineRunActive(runToken)) return null
      const result = await submitReadyEpisodeVideos(targetIds, attemptedIds, runToken)
      submission.submitted += result.submitted
      submission.failed += result.failed
    }
    if (st.status === 'running') continue
    videoPromptBatch.value = { running: false, total: 0, completed: 0, queueStatus: '', queuePosition: 0 }
    await refresh()
    if (!episodeVideoPipelineRunActive(runToken)) return null
    const result = await submitReadyEpisodeVideos(targetIds, attemptedIds, runToken)
    submission.submitted += result.submitted
    submission.failed += result.failed
    if (st.status === 'error') throw new Error(st.error || t('episode.sb.batchFailed'))
    return st
  }
  videoPromptBatch.value = { running: false, total: 0, completed: 0, queueStatus: '', queuePosition: 0 }
  throw new Error(t('episode.vid.fullPipelinePromptTimeout'))
}

/**
 * 整集视频生成等待：提交完成后继续轮询 sys_task，直到这批镜头的视频任务全部终态。
 * 进度按"真实出片/终态"计数而非提交数——并发槽有限时提交是一瞬间的事，
 * 排队生成才是大头，界面必须反映后者。返回 { done, failed }，页面失效返回 null。
 */
async function waitForEpisodeVideoGeneration(targetIds, runToken, attempts = 2160) {
  const targetSet = new Set(targetIds)
  for (let i = 0; i < attempts; i++) {
    let tasks = []
    try {
      const data = await taskAPI.listByEpisode(epId.value)
      tasks = data?.tasks || []
    } catch {
      // 任务接口瞬时失败不能误判"全部结束"，保留进度等下一轮
      if (!episodeVideoPipelineRunActive(runToken)) return null
      await sleep(5000)
      continue
    }
    if (!episodeVideoPipelineRunActive(runToken)) return null
    try {
      const fresh = await episodeAPI.storyboards(epId.value)
      if (fresh) {
        sbs.value = fresh
        selectedVideoSbIds.value = selectedVideoSbIds.value.filter(id => sbs.value.some(sb => sb.id === id))
        const selectedId = selectedSb.value?.id
        selectedSb.value = sbs.value.find(sb => sb.id === selectedId) || sbs.value[0] || null
      }
    } catch {}
    if (!episodeVideoPipelineRunActive(runToken)) return null
    const effective = selectEffectiveVideoTasksByStoryboard(tasks)
    let active = 0
    let done = 0
    let failed = 0
    for (const id of targetSet) {
      const task = effective.get(id)
      if (isActiveVideoTask(task)) { active++; continue }
      const sb = sbs.value.find(s => s.id === id)
      // 无视频且任务缺失/失败/取消 → 终态未成功；缺提示词从未提交的镜头同样计入，保证等待能收敛
      if (hasVid(sb) || task?.status === 'completed') done++
      else failed++
    }
    episodeVideoPipeline.value.videoCompleted = Math.max(episodeVideoPipeline.value.videoCompleted, done)
    if (!active) return { done, failed }
    await sleep(5000)
  }
  throw new Error(t('episode.vid.fullPipelineVideoTimeout'))
}

async function runEpisodeVideoPipeline(targetIds) {
  if (episodeVideoPipeline.value.running || !targetIds.length) return
  episodeVideoPipelineDisposed = false
  const runToken = ++episodeVideoPipelineRunToken
  saveEpisodeVideoPipelineIntent(targetIds)
  episodeVideoPipeline.value = {
    running: true,
    promptTotal: 0,
    promptCompleted: 0,
    videoTotal: targetIds.length,
    videoCompleted: 0,
  }
  const attemptedIds = new Set()
  const submission = { submitted: 0, failed: 0 }
  try {
    await refresh()
    if (!episodeVideoPipelineRunActive(runToken)) return
    const targetSet = new Set(targetIds)
    const missingPromptIds = sbs.value
      .filter(sb => targetSet.has(sb.id) && !storyboardPromptReady(sb))
      .map(sb => sb.id)
    episodeVideoPipeline.value.promptTotal = missingPromptIds.length
    // 视频进度基数 = 已经出片的镜头；后续按任务终态递增，刷新后也能恢复同一口径
    episodeVideoPipeline.value.videoCompleted = sbs.value.filter(sb => targetSet.has(sb.id) && hasVid(sb)).length

    // 已有提示词的镜头无需等待批任务，点击确认后立即启动视频生成。
    const initialResult = await submitReadyEpisodeVideos(targetIds, attemptedIds, runToken)
    submission.submitted += initialResult.submitted
    submission.failed += initialResult.failed

    if (missingPromptIds.length) {
      const res = await episodeAPI.generateVideoPrompts(
        epId.value,
        chatModelOverride(),
        chatConfigId(),
        missingPromptIds,
        effectiveVideoModelLabel.value || undefined,
        selectedVideoConfig.value?.id,
      )
      if (res?.total || res?.already_running) {
        videoPromptBatch.value = {
          running: true,
          total: res.total || missingPromptIds.length,
          completed: 0,
          queueStatus: '',
          queuePosition: 0,
        }
        const status = await waitForEpisodeVideoPrompts(targetIds, attemptedIds, submission, runToken)
        if (!status) return
        if (status?.failed) {
          toast.warning(t('episode.vid.fullPipelinePromptFailed', { n: status.failed }))
        }
      } else {
        await refresh()
        if (!episodeVideoPipelineRunActive(runToken)) return
        const result = await submitReadyEpisodeVideos(targetIds, attemptedIds, runToken)
        submission.submitted += result.submitted
        submission.failed += result.failed
      }
    }

    // 最终再排空一次，覆盖批任务恰好结束时刚写入的最后一条提示词。
    await refresh()
    if (!episodeVideoPipelineRunActive(runToken)) return
    const finalResult = await submitReadyEpisodeVideos(targetIds, attemptedIds, runToken)
    submission.submitted += finalResult.submitted
    submission.failed += finalResult.failed
    const missingAfterGeneration = sbs.value.filter(sb => targetSet.has(sb.id) && !storyboardPromptReady(sb))
    if (missingAfterGeneration.length) {
      toast.warning(t('episode.vid.fullPipelinePromptBlocked', { n: missingAfterGeneration.length }))
    }
    if (submission.failed) {
      toast.warning(t('episode.vid.fullPipelineSubmitPartial', {
        done: submission.submitted,
        total: submission.submitted + submission.failed,
      }))
    } else if (submission.submitted) {
      toast.success(t('episode.vid.fullPipelineSubmitted', { n: submission.submitted }))
    } else {
      toast.info(t('episode.vid.allDone'))
    }
    if (videoSelectMode.value) toggleVideoSelectMode()

    // 提交完成不等于流程结束：并发槽有限时排队才是大头，
    // 继续等待这批镜头的视频任务全部终态，按钮期间显示真实出片进度。
    const pendingVideoWork = sbs.value.some(sb => targetSet.has(sb.id) && !hasVid(sb))
    if (pendingVideoWork) {
      const outcome = await waitForEpisodeVideoGeneration(targetIds, runToken)
      if (!outcome) return
      episodeVideoPipeline.value.videoCompleted = outcome.done
      if (outcome.failed) {
        toast.warning(t('episode.vid.fullPipelineVideoPartial', {
          done: outcome.done,
          total: targetIds.length,
          failed: outcome.failed,
        }))
      } else {
        toast.success(t('episode.vid.fullPipelineVideoDone', { n: outcome.done }))
      }
    }
  } catch (e) {
    if (episodeVideoPipelineRunActive(runToken)) toastError(e, { fallback: 'episode.vid.fullPipelineFailed' })
  } finally {
    // 旧页面实例卸载后保留恢复意图，且不能清掉新实例正在使用的共享意图。
    if (episodeVideoPipelineRunActive(runToken)) {
      clearEpisodeVideoPipelineIntent()
      episodeVideoPipeline.value = { running: false, promptTotal: 0, promptCompleted: 0, videoTotal: 0, videoCompleted: 0 }
    }
  }
}

function resumeEpisodeVideoPipeline() {
  const targetIds = loadEpisodeVideoPipelineIntent()
  if (targetIds.length && !episodeVideoPipeline.value.running) void runEpisodeVideoPipeline(targetIds)
}
let breakdownUsesTwoStage = false
// onDoneOverride：一键成片等衔接流程注入自定义完成回调；
// 模板 @click 直调会把事件对象传进来，用 typeof 过滤后回退默认回调。
function doBreakdown(onDoneOverride) {
  const doneHandler = typeof onDoneOverride === 'function' ? onDoneOverride : onBreakdownDone
  storyboardReview.error = ''
  storyboardReview.notice = ''
  const charList = chars.value.length
    ? chars.value.map(c => `${c.name}(ID:${c.id})`).join('、')
    : '（当前集还没有角色）'
  const sceneList = scenes.value.length
    ? scenes.value.map(s => `${s.location} · ${s.time || '未设时间'}(ID:${s.id})`).join('、')
    : '（当前集还没有场景）'
  const propList = propItems.value.length
    ? propItems.value.map(p => `${p.name}(ID:${p.id})`).join('、')
    : '（当前集还没有道具）'
  const videoProvider = String(selectedVideoConfig.value?.provider || '').toLowerCase()
  const isH3Video = videoProvider === 'minimax'
    || (!videoProvider && /^minimax-h3/i.test(effectiveVideoModelLabel.value || ''))
  const isDoubaoVideo = ['volcengine', 'doubao', 'doubao_local'].includes(videoProvider)
    || (!videoProvider && /seedance|doubao/i.test(effectiveVideoModelLabel.value || ''))
  breakdownUsesTwoStage = isH3Video
  const modelLabel = isH3Video
    ? `MiniMax H3 (${effectiveVideoModelLabel.value || 'minimax_h3_720p'})`
    : effectiveVideoModelLabel.value
  const isV8 = breakdownAgent.value === 'storyboard_v8'
  // V1 拆解同样支持双引擎：沿用同一个引擎选择器（缺省跟随 Claude runtime 就绪状态）
  const executionEngine = selectedV8Engine()
  if (!executionEngine) return
  const generationInstruction = isV8
    ? 'V8 拆解引擎（AI影视分镜双流编译器）：一个视频编号 = 一行分镜，总时长不超过 15 秒、不足如实填写，逐镜时长之和必须与 duration 精确相等（每行保存前把逐镜秒数重新加一遍，和值即 duration，禁止另估）；shot_number = 视频编号从 1 连续递增；每批最多保存 3 行，第一批必须 replace_existing: true 初始化本次隔离草稿，后续批从工具返回的 expected_next_number 连续追加；全部视频编号内容写入 save_storyboards 参数，回复正文不要复述分镜全文；本阶段只拆结构并保存，不传 video_prompt、image_prompt或 bgm_prompt（由后续视频提示词任务生成）。视觉风格直接继承 read_storyboard_context 返回的项目 style_preset，不在 description 中写【全局视觉】、主风格或重复风格词；无字幕、无水印、无无关文字、禁止背景音乐由系统级规则统一约束。description 依次写【场景与连续状态】【光线】【出场人物】，【出场人物】只写状态名、本镜服装阶段名（如「穿着：暴富睡衣」）与动态，有参考图的角色禁止展开五官、发型、服装款式、颜色材质等外观词；逐镜写「【镜头01】1.5s | 景别，角度，运镜（动机/落点）」后接构图（开镜静态）/画面进程/表演与互动/声音与台词四行；台词逐字保留原文并在每句前完整写「配音指令：角色名|固定声线类型|表演状态|声音执行参数」，台词行使用「角色名说：「原文」」格式。全部保存完成后直接收尾，系统在运行结束时自动发布草稿，无需再调用校验或反复复查。'
    : isH3Video
      ? `本次视频模型：${modelLabel}。本次采用两阶段生成：当前阶段只拆分并保存分镜结构，填写 description、atmosphere、duration、scene_id、character_ids、prop_ids 等字段，不要生成 video_prompt。保存完全部分镜后停止；等待用户手动点击“生成视频提示词”，再由后续任务逐条生成 MiniMax H3 视频提示词。`
      : isDoubaoVideo
        ? `本次视频模型：${modelLabel}。当前阶段只拆分并保存分镜结构，填写 description、atmosphere、duration、scene_id、character_ids、prop_ids 等字段，不要生成 video_prompt。拆分完成后由豆包视频提示词 Agent 按“参考图N是：名称 + description 原文”格式逐条生成。`
      : `本次视频模型：${modelLabel}。请在拆分时为每个分镜段落同时生成 video_prompt，并按该模型的特性与时长限制编写。`
  return runAgent(breakdownAgent.value, `请基于当前集剧本拆分并保存完整分镜。
${generationInstruction}

当前集已有角色：${charList}
当前集已有场景：${sceneList}
当前集已有道具：${propList}

绑定要求：
- 每个镜头必须根据剧本内容，从上述当前集已有角色中选出出场的角色绑定 character_ids（ID 必须来自上述列表；有角色出场就必须绑定，不要遗漏）
- 每个镜头尽量匹配上述已有场景填写 scene_id（ID 必须来自上述列表），不要凭空创造新场景
- 每个镜头出现关键道具（被使用、交接、特写或在画面中明显可见）时，从上述当前集已有道具中绑定 prop_ids（ID 必须来自上述列表）；没有道具出现可传空数组
- 只有纯环境空镜头才可以不绑定角色`, dramaId, epId.value, doneHandler, chatModelOverride(), chatConfigId(), executionEngine)
}

// ─── 一键拆分分镜并生成视频 ────────────────────────────────────────────────
// 严格复用现有两段能力：先走现有拆分分镜流程（doBreakdown），拆分成功后把新分镜
// 直接交给现有整集视频流水线（runEpisodeVideoPipeline）。不重复实现提示词/视频逻辑，
// 开始前只确认一次，之后自动衔接；拆分失败则中止，不触发生成。
const oneClickStoryboardVideo = ref({ running: false, confirmOpen: false })
const oneClickStoryboardVideoLabel = computed(() => {
  if (!oneClickStoryboardVideo.value.running) return t('episode.breakdown.oneClickGenerateAll')
  if (rn.value) return t('episode.breakdown.oneClickPhaseBreaking')
  if (videoPromptBatch.value.running) return t('episode.breakdown.oneClickPhasePrompts')
  if (episodeVideoPipeline.value.running) return t('episode.breakdown.oneClickPhaseVideos')
  return t('episode.breakdown.oneClickRunning')
})
const oneClickStoryboardVideoConfirmMessage = computed(() => {
  const shotStat = sbs.value.length
    ? t('episode.breakdown.oneClickConfirmShots', { n: sbs.value.length, dur: totalDuration.value })
    : t('episode.breakdown.oneClickConfirmShotsEmpty')
  const overwrite = sbs.value.length
    ? t('episode.breakdown.oneClickConfirmOverwrite', { n: sbs.value.length })
    : t('episode.breakdown.oneClickConfirmBreak')
  return `${overwrite}\n${shotStat}\n${t('episode.breakdown.oneClickConfirmNote')}`
})
function openOneClickStoryboardVideoConfirm() {
  if (oneClickStoryboardVideo.value.running || rn.value || storyboardReviewBusy.value) return
  if (episodeVideoPipeline.value.running || videoPromptBatch.value.running) return
  if (!epId.value) return
  oneClickStoryboardVideo.value.confirmOpen = true
}
async function startOneClickStoryboardVideo() {
  if (oneClickStoryboardVideo.value.running) return
  oneClickStoryboardVideo.value.confirmOpen = false
  oneClickStoryboardVideo.value.running = true
  try {
    // 注入完成回调替代默认 onBreakdownDone：拆分后的提示词补齐由流水线统一负责，
    // 避免默认回调抢先触发 batchVideoPrompts 与流水线重复跑。
    let breakdownSucceeded = false
    await doBreakdown(() => { breakdownSucceeded = true })
    if (!breakdownSucceeded) return
    await refresh()
    try { applyLatestStoryboardRun(await episodeAPI.agentRunning(epId.value)) } catch { /* 耗时状态刷新失败不影响衔接 */ }
    const targetIds = sbs.value.filter(sb => !hasVid(sb) && !isPendingVideo(sb.id)).map(sb => sb.id)
    if (!targetIds.length) { toast.info(t('episode.vid.allDone')); return }
    await runEpisodeVideoPipeline(targetIds)
  } catch (e) {
    toastError(e)
  } finally {
    oneClickStoryboardVideo.value.running = false
  }
}

async function autofixV8Storyboards() {
  if (AUTOFIX_PAUSED || !v8ReviewAvailable.value || storyboardReviewBusy.value) return
  const executionEngine = selectedV8Engine()
  if (!executionEngine) return
  storyboardReview.running = true
  storyboardReview.error = ''
  storyboardReview.notice = ''
  Object.assign(storyboardReviewProgress, {
    phase: 'reading',
    processedCount: 0,
    totalCount: sbs.value.length,
    candidateCount: sbs.value.length,
    replacementCount: 0,
    lastRange: '',
    elapsedSeconds: 0,
    localStartedAt: Date.now(),
  })
  pollStoryboardAutofix(epId.value)
  try {
    const data = await storyboardReviewAPI.autofix(
      dramaId,
      epId.value,
      chatModelOverride(),
      chatConfigId(),
      executionEngine,
    )
    const result = data?.autofixResult || data?.autofix_result
    if (!result) throw new Error('检查修复 Agent 未返回发布结果')
    const modified = Number(result.modifiedCount ?? result.modified_count ?? 0)
    const inserted = Number(result.insertedCount ?? result.inserted_count ?? 0)
    await refresh()
    storyboardReview.notice = modified || inserted
      ? t('episode.breakdown.autofixDone', { modified, inserted })
      : t('episode.breakdown.autofixNoChanges')
    toast.success(storyboardReview.notice)
  } catch (e) {
    storyboardReview.error = t('episode.breakdown.reviewError', { message: mapError(e) })
    toastError(e)
  } finally {
    storyboardReview.running = false
  }
}

/** 拆分完成后刷新；H3 等待用户手动生成提示词，其他模型仍自动补漏 */
async function onBreakdownDone() {
  await refresh()
  try { applyLatestStoryboardRun(await episodeAPI.agentRunning(epId.value)) } catch { /* 耗时行刷新失败不影响流程 */ }
  if (breakdownUsesTwoStage) return
  const missing = sbs.value.filter(sb => !(sb.video_prompt || sb.videoPrompt || '').trim())
  if (missing.length) batchVideoPrompts()
}

/**
 * 刷新页面会丢失内存里的按钮锁，但后端拆镜任务可能还在跑：
 * 加载时查询在飞任务恢复锁定（与 syncExtractStatus 同款模式），轮询到完成再解锁刷新。
 */
async function syncAgentInflight() {
  if (!epId.value || rn.value) return
  try {
    const res = await episodeAPI.agentRunning(epId.value)
    applyLatestStoryboardRun(res)
    const running = Array.isArray(res?.running) ? res.running : []
    const runningBreakdown = running.find(tp => tp === 'storyboard_breaker' || tp === 'storyboard_v8')
    if (runningBreakdown) {
      rn.value = true
      rt.value = runningBreakdown
      toast.info(t('episode.sb.inflightResumed'))
      pollAgentInflight(runningBreakdown, epId.value)
    } else if (running.includes('storyboard_v8_reviewer')) {
      storyboardReview.running = true
      toast.info(t('episode.breakdown.autofixResumed'))
      pollStoryboardAutofix(epId.value)
    }
  } catch {}
}
function pollStoryboardAutofix(episodeId, attempts = 240) {
  const tick = async (left) => {
    if (storyboardReviewProgress.localStartedAt) {
      storyboardReviewProgress.elapsedSeconds = Math.floor((Date.now() - storyboardReviewProgress.localStartedAt) / 1000)
    }
    try {
      const res = await episodeAPI.agentRunning(episodeId)
      const tasks = Array.isArray(res?.tasks) ? res.tasks : []
      const task = tasks.find(item => item?.type === 'storyboard_v8_reviewer')
      const progress = task?.autofix_progress
      if (task) Object.assign(storyboardReviewProgress, {
        elapsedSeconds: Number(task.elapsed_seconds || storyboardReviewProgress.elapsedSeconds),
        queueStatus: String(task.queue_status || ''),
        queuePosition: Number(task.queue_position || 0),
      })
      if (progress) Object.assign(storyboardReviewProgress, {
        phase: String(progress.phase || 'reading'),
        processedCount: Number(progress.processed_count || 0),
        totalCount: Number(progress.total_count || storyboardReviewProgress.totalCount),
        candidateCount: Number(progress.candidate_count || storyboardReviewProgress.candidateCount),
        replacementCount: Number(progress.replacement_count || 0),
        lastRange: String(progress.last_range || ''),
      })
      if (!(Array.isArray(res?.running) ? res.running : []).includes('storyboard_v8_reviewer')) {
        storyboardReview.running = false
        if (String(epId.value) === String(episodeId)) await refresh()
        return
      }
    } catch {}
    if (left > 0) setTimeout(() => tick(left - 1), 2500)
    else storyboardReview.running = false
  }
  setTimeout(() => tick(attempts), 2500)
}
function pollAgentInflight(type, episodeId, attempts = 240) {
  const tick = async (left) => {
    try {
      const res = await episodeAPI.agentRunning(episodeId)
      applyLatestStoryboardRun(res)
      if (!(Array.isArray(res?.running) ? res.running : []).includes(type)) {
        if (rn.value && rt.value === type) { rn.value = false; rt.value = null }
        if (String(epId.value) === String(episodeId)) {
          await refresh()
          if (['failed', 'interrupted'].includes(String(res?.latest_storyboard_run?.status || ''))) {
            toast.error(t('episode.breakdown.recentFailed'))
          } else {
            toast.success(t('episode.sb.inflightDone'))
          }
        }
        return
      }
    } catch {}
    if (left > 0) setTimeout(() => tick(left - 1), 2500)
    else if (rn.value && rt.value === type) { rn.value = false; rt.value = null }
  }
  setTimeout(() => tick(attempts), 2500)
}

// 按需为单个分镜生成视频提示词：由独立的视频提示词 Agent 读取分镜字段并保存
async function genVideoPrompt(sb) {
  if (!sb || videoPromptGeneratingIds.value.includes(sb.id)) return
  const idx = sbs.value.indexOf(sb) + 1
  const cfg = selectedVideoConfig.value
  const videoProvider = String(cfg?.provider || '').toLowerCase()
  const isH3Video = videoProvider === 'minimax'
    || (!videoProvider && /^minimax-h3/i.test(effectiveVideoModelLabel.value || ''))
  const modelName = effectiveVideoModelLabel.value || (videoProvider === 'minimax' ? 'MiniMax H3' : videoProvider || '默认')
  const isDoubaoVideo = ['volcengine', 'doubao', 'doubao_local'].includes(videoProvider)
    || (!videoProvider && /seedance|doubao/i.test(modelName))
  const videoPromptAgentType = isDoubaoVideo
    ? 'doubao_video_prompt_generator'
    : 'video_prompt_generator'
  const label = cfg
    ? (isH3Video ? `MiniMax H3 (${modelName})` : `${modelName} (${videoProvider})`)
    : '默认'
  const charNames = getStoryboardCharacters(sb).map(c => c.name).join('、') || '无'
  const propNames = getStoryboardProps(sb).map(p => p.name).join('、') || '无'
  videoPromptGeneratingIds.value.push(sb.id)
  try {
    const formatInstruction = isDoubaoVideo
      ? `不要创作或改写。只按 reference_media_order.images 的已定顺序在分镜 description 原文前加上“参考图1是：{name}；参考图2是：{name}。”，名称只取对应项的 name，description 必须逐字保留。无参考图时直接使用 description 原文。`
      : `必须根据当前分镜的 description、atmosphere、duration 及真实绑定素材重新创作。视觉裁决必须遵守返回的 visual_source_policy：剧情、动作和对白以当前分镜为准，基础外观以最终资产图为准；资产 has_final_image=true 时只通过对应参考图严格继承，不得从剧本或 description 复述可能冲突的旧服装、旧配色、旧材质或旧场景布局，只保留本镜明确发生的淋湿、破损、沾血、摘配饰、镜内换装等动态变化。严格按照系统注入的 MiniMax H3 官方 h3-prompt-writing Skill 生成并保存 video_prompt。`
    await api.post(`/agent/${videoPromptAgentType}/chat`, {
      message: `请为分镜 #${idx}(ID:${sb.id})生成视频提示词(video_prompt)。视频模型:${label},请根据该模型的特性和时长限制生成。

该分镜信息:时长 ${sb.duration || 10}s;场景:${getSceneName(sb) || '未绑定'};角色:${charNames};道具:${propNames}。

这是一次重新生成：不要读取、引用、复用或模仿该分镜已有的 video_prompt。请调用 read_storyboard_context，参数 storyboard_id=${sb.id}，只读取当前分镜，不得参考其他分镜。${formatInstruction}`,
      drama_id: dramaId,
      episode_id: epId.value,
      storyboard_id: sb.id,
      model: chatModelOverride() || undefined,
      config_id: chatConfigId() || undefined,
    })
    toast.success(t('episode.sb.promptGenerated', { n: idx }))
    await refresh()
  } catch (e) {
    toastError(e)
  } finally {
    videoPromptGeneratingIds.value = videoPromptGeneratingIds.value.filter(id => id !== sb.id)
  }
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms))
}

function watchAsyncResult(check, attempts = 24, delay = 2500) {
  void (async () => {
    for (let i = 0; i < attempts; i++) {
      await sleep(delay)
      await refresh()
      if (check()) return
    }
  })()
}

// 批量生成默认只补齐缺图；若该类别已经全部有图，则把全部资产作为重绘目标。
// 这样项目改换风格后无需逐个点“重新生成”，仍可用同一个批量入口应用新风格。
function batchImagePlan(items, isPending) {
  const available = items.filter(item => !isPending(item.id))
  const missing = available.filter(item => !(item.image_url || item.imageUrl))
  const targets = missing.length ? missing : available
  return {
    ids: targets.map(item => item.id),
    previousImages: new Map(targets.map(item => [item.id, item.image_url || item.imageUrl || ''])),
  }
}

function batchImageFinished(item, previousImage) {
  const currentImage = item?.image_url || item?.imageUrl || ''
  return !!currentImage && (!previousImage || currentImage !== previousImage)
}

async function genCharImg(id) {
  try {
    if (assetDetail.value.open && assetDetail.value.type === 'character' && assetDetail.value.item?.id === id && assetDetailHasChanges()) {
      const saved = await saveAssetDetail({ silent: true })
      if (!saved) return
    }
    if (!isPendingCharImage(id)) pendingCharImageIds.value.push(id)
    const char = chars.value.find(c => c.id === id)
    const previousImage = rawAssetImagePath(char)
    // 提示词缺失时由后端在生图请求内生成（占位任务先行，卡片全程可见），不再前端预跑一遍
    setImageSubmitting('character', id, true)
    try {
      await characterAPI.generateImage(id, epId.value, bareModelName(imageModel.value) || undefined, ownerConfigId(imageModelOptions.value, imageModel.value), chatModelOverride(), chatConfigId())
    } finally {
      setImageSubmitting('character', id, false)
    }
    toast.success(t('episode.image.generatingChar'))
    await refresh()
    watchAsyncResult(() => {
      const char = chars.value.find(c => c.id === id)
      const done = batchImageFinished(char, previousImage)
      if (done) {
        pendingCharImageIds.value = pendingCharImageIds.value.filter(item => item !== id)
        if (assetDetail.value.open && assetDetail.value.type === 'character' && assetDetail.value.item?.id === id) void loadAssetImageHistory()
      }
      return done
    })
  } catch (e) {
    pendingCharImageIds.value = pendingCharImageIds.value.filter(item => item !== id)
    toastError(e)
  }
}
function batchCharImages() {
  const { ids, previousImages } = batchImagePlan(visualChars.value, isPendingCharImage)
  if (!ids.length) { toast.info(t('episode.image.allCharsDone')); return }
  pendingCharImageIds.value = [...new Set([...pendingCharImageIds.value, ...ids])]
  ids.forEach(id => setImageSubmitting('character', id, true))
  characterAPI.batchImages(ids, epId.value, bareModelName(imageModel.value) || undefined, ownerConfigId(imageModelOptions.value, imageModel.value), chatModelOverride(), chatConfigId()).then(async () => {
    ids.forEach(id => setImageSubmitting('character', id, false))
    toast.success(t('episode.image.batchGeneratingChar'))
    await refresh()
    watchAsyncResult(() => ids.every(id => {
      const char = chars.value.find(c => c.id === id)
      const done = batchImageFinished(char, previousImages.get(id))
      if (done) pendingCharImageIds.value = pendingCharImageIds.value.filter(item => item !== id)
      return done
    }), 36)
  }).catch(e => {
    ids.forEach(id => setImageSubmitting('character', id, false))
    pendingCharImageIds.value = pendingCharImageIds.value.filter(item => !ids.includes(item))
    toastError(e)
  })
}
async function genSceneImg(id) {
  try {
    if (assetDetail.value.open && assetDetail.value.type === 'scene' && assetDetail.value.item?.id === id && assetDetailHasChanges()) {
      const saved = await saveAssetDetail({ silent: true })
      if (!saved) return
    }
    if (!isPendingSceneImage(id)) pendingSceneImageIds.value.push(id)
    const scene = scenes.value.find(s => s.id === id)
    const previousImage = rawAssetImagePath(scene)
    // 提示词缺失时由后端在生图请求内生成（占位任务先行，卡片全程可见），不再前端预跑一遍
    setImageSubmitting('scene', id, true)
    try {
      await sceneAPI.generateImage(id, epId.value, bareModelName(imageModel.value) || undefined, ownerConfigId(imageModelOptions.value, imageModel.value), chatModelOverride(), chatConfigId())
    } finally {
      setImageSubmitting('scene', id, false)
    }
    toast.success(t('episode.image.generatingScene'))
    await refresh()
    watchAsyncResult(() => {
      const scene = scenes.value.find(s => s.id === id)
      const done = batchImageFinished(scene, previousImage)
      if (done) {
        pendingSceneImageIds.value = pendingSceneImageIds.value.filter(item => item !== id)
        if (assetDetail.value.open && assetDetail.value.type === 'scene' && assetDetail.value.item?.id === id) void loadAssetImageHistory()
      }
      return done
    })
  } catch (e) {
    pendingSceneImageIds.value = pendingSceneImageIds.value.filter(item => item !== id)
    toastError(e)
  }
}
function isPendingPropImage(id) {
  return pendingPropImageIds.value.includes(id)
}
async function genPropImg(id) {
  try {
    if (!isPendingPropImage(id)) pendingPropImageIds.value.push(id)
    const prop = propItems.value.find(p => p.id === id)
    const previousImage = rawAssetImagePath(prop)
    // 提示词缺失时由后端在生图请求内生成（占位任务先行，卡片全程可见），不再前端预跑一遍
    setImageSubmitting('prop', id, true)
    try {
      await propAPI.generateImage(id, epId.value, bareModelName(imageModel.value) || undefined, ownerConfigId(imageModelOptions.value, imageModel.value), chatModelOverride(), chatConfigId())
    } finally {
      setImageSubmitting('prop', id, false)
    }
    toast.success(t('episode.image.generatingProp'))
    await refresh()
    watchAsyncResult(() => {
      const prop = propItems.value.find(p => p.id === id)
      const done = batchImageFinished(prop, previousImage)
      if (done) {
        pendingPropImageIds.value = pendingPropImageIds.value.filter(item => item !== id)
        if (assetDetail.value.open && assetDetail.value.type === 'prop' && assetDetail.value.item?.id === id) void loadAssetImageHistory()
      }
      return done
    })
  } catch (e) {
    pendingPropImageIds.value = pendingPropImageIds.value.filter(item => item !== id)
    toastError(e)
  }
}
function batchSceneImages() {
  const { ids, previousImages } = batchImagePlan(scenes.value, isPendingSceneImage)
  if (!ids.length) { toast.info(t('episode.image.allScenesDone')); return }
  pendingSceneImageIds.value = [...new Set([...pendingSceneImageIds.value, ...ids])]
  ids.forEach(id => {
    setImageSubmitting('scene', id, true)
    sceneAPI.generateImage(id, epId.value, bareModelName(imageModel.value) || undefined, ownerConfigId(imageModelOptions.value, imageModel.value), chatModelOverride(), chatConfigId()).then(() => refresh()).catch(e => toastError(e)).finally(() => setImageSubmitting('scene', id, false))
  })
  toast.success(t('episode.image.batchGeneratingScene'))
  watchAsyncResult(() => ids.every(id => {
    const scene = scenes.value.find(s => s.id === id)
    const done = batchImageFinished(scene, previousImages.get(id))
    if (done) pendingSceneImageIds.value = pendingSceneImageIds.value.filter(item => item !== id)
    return done
  }), 36)
}
function batchPropImages() {
  const { ids, previousImages } = batchImagePlan(propItems.value, isPendingPropImage)
  if (!ids.length) { toast.info(t('episode.image.allPropsDone')); return }
  pendingPropImageIds.value = [...new Set([...pendingPropImageIds.value, ...ids])]
  ids.forEach(id => {
    setImageSubmitting('prop', id, true)
    propAPI.generateImage(id, epId.value, bareModelName(imageModel.value) || undefined, ownerConfigId(imageModelOptions.value, imageModel.value), chatModelOverride(), chatConfigId()).then(() => refresh()).catch(e => toastError(e)).finally(() => setImageSubmitting('prop', id, false))
  })
  toast.success(t('episode.image.batchGeneratingProp'))
  watchAsyncResult(() => ids.every(id => {
    const prop = propItems.value.find(p => p.id === id)
    const done = batchImageFinished(prop, previousImages.get(id))
    if (done) pendingPropImageIds.value = pendingPropImageIds.value.filter(item => item !== id)
    return done
  }), 36)
}
function getVideoUrl(s) { return s?.video_url || s?.videoUrl || s?.composed_video_url || s?.composedVideoUrl || null }
function hasVid(s) { return !!getVideoUrl(s) }

// ===== 分镜视频历史（一个分镜可能生成多个视频,sys_task 留存全部记录）=====
const sbVideoHistory = ref([])
const previewVideoUrl = ref('') // 正在预览的历史视频(相对路径);空 = 预览当前主视频

// 注意:/tasks 返回原始行(camelCase),/episodes/:id/generation-tasks 返回 snake_case,两种命名都兼容
function taskVideoPath(t) { return t?.local_path || t?.localPath || t?.result_url || t?.resultUrl || '' }
function taskCreatedAt(t) { return t?.created_at || t?.createdAt || '' }
function isCurrentVideo(t) { const p = taskVideoPath(t); return !!p && p === getVideoUrl(selectedSb.value) }
async function loadSbVideoHistory() {
  previewVideoUrl.value = ''
  if (!selectedSb.value?.id) { sbVideoHistory.value = []; return }
  try {
    const rows = await taskAPI.list({ type: 'video', storyboard_id: selectedSb.value.id })
    sbVideoHistory.value = (Array.isArray(rows) ? rows : [])
      .filter(t => t.status === 'completed' && taskVideoPath(t))
      .sort((a, b) => taskCreatedAt(b).localeCompare(taskCreatedAt(a)))
  } catch { sbVideoHistory.value = [] }
}

watch(() => [selectedSb.value?.id, getVideoUrl(selectedSb.value)], () => { loadSbVideoHistory() })

function previewHistoryVideo(t) {
  previewVideoUrl.value = isCurrentVideo(t) ? '' : taskVideoPath(t)
}

// 历史视频的真实生成种子：存于任务 params.seed（旧任务可能没有）
function taskVideoSeed(t) {
  try {
    const p = typeof t?.params === 'string' ? JSON.parse(t.params) : (t?.params || {})
    const seed = Number(p?.seed)
    return Number.isInteger(seed) && seed >= 0 && seed <= MAX_VIDEO_SEED ? seed : null
  } catch { return null }
}

async function applyHistorySeed(h) {
  const sb = selectedSb.value
  const seed = taskVideoSeed(h)
  if (!sb || seed === null || isPendingVideo(sb.id)) return
  if (seed === storyboardVideoSeed(sb)) {
    toast.success(t('episode.vid.historySeedSame', { seed }))
    return
  }
  try {
    const updated = await storyboardAPI.update(sb.id, { video_seed: seed })
    const persisted = storyboardVideoSeed(updated)
    if (persisted !== seed) throw new Error(t('episode.inspector.seedSaveFailed'))
    sb.video_seed = persisted
    sb.videoSeed = persisted
    toast.success(t('episode.vid.historySeedApplied', { seed: persisted }))
  } catch (e) { toastError(e) }
}

async function setAsMainVideo(historyItem = null) {
  const sb = selectedSb.value
  const targetUrl = historyItem ? taskVideoPath(historyItem) : previewVideoUrl.value
  if (!sb || !targetUrl) return
  if (targetUrl === getVideoUrl(sb)) {
    previewVideoUrl.value = ''
    return
  }
  try {
    await storyboardAPI.update(sb.id, { video_url: targetUrl })
    sb.video_url = targetUrl
    sb.videoUrl = targetUrl
    previewVideoUrl.value = ''
    toast.success(t('episode.vid.setMainDone'))
  } catch (e) { toastError(e, { fallback: 'episode.vid.setMainFailed' }) }
}

const videoDelete = ref({ open: false, item: null, loading: false })
function askDeleteHistoryVideo(h) { videoDelete.value = { open: true, item: h, loading: false } }
async function confirmDeleteHistoryVideo() {
  const h = videoDelete.value.item
  if (!h || videoDelete.value.loading) return
  videoDelete.value.loading = true
  try {
    await taskAPI.del(h.id)
    sbVideoHistory.value = sbVideoHistory.value.filter(x => x.id !== h.id)
    if (previewVideoUrl.value === taskVideoPath(h)) previewVideoUrl.value = ''
    videoDelete.value = { open: false, item: null, loading: false }
    toast.success(t('episode.vid.historyDeleted'))
  } catch (e) {
    videoDelete.value.loading = false
    toastError(e, { fallback: 'common.deleteFailed' })
  }
}

function formatHistoryTime(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  const p = n => String(n).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

function getShotReferenceMedia(sb) {
  const refs = []
  const seen = new Set()
  const pushRef = (name, imageUrl) => {
    if (!imageUrl || seen.has(imageUrl) || refs.length >= refImageLimit.value) return
    seen.add(imageUrl)
    refs.push({ name: String(name || '').trim(), imageUrl })
  }
  const scene = getStoryboardScene(sb)
  pushRef(scene?.location, scene?.image_url || scene?.imageUrl)
  for (const char of getStoryboardCharacters(sb)) {
    pushRef(char.name, charStateImageForShot(sb, char))
  }
  for (const prop of getStoryboardProps(sb)) {
    pushRef(prop.name, prop?.image_url || prop?.imageUrl)
  }
  return refs
}

function getShotReferenceImages(sb) {
  return getShotReferenceMedia(sb).map(item => item.imageUrl)
}

function getShotReferenceAudios(sb) {
  const refs = []
  for (const char of getStoryboardCharacters(sb)) {
    const value = char?.audio_url || char?.audioUrl
    if (value && !refs.includes(value)) refs.push(value)
  }
  return refs.slice(0, 3)
}

// 右侧参考素材面板：本集全部可绑定素材（场景单选、角色/道具多选），bound 标记是否已绑定
// kind 为英文 code（逻辑值）；typeLabel 为显示名（渲染时求值）
function shotBindableAssets(sb) {
  const out = []
  for (const char of visualChars.value) {
    // 缩略图与提交参考图同源：显示该镜选中状态的图（未选/未出图回退底图）
    const imageUrl = charStateImageForShot(sb, char)
    out.push({
      key: `character-${char.id}`,
      id: char.id,
      kind: 'character',
      typeLabel: t('common.role'),
      name: char.name || t('episode.asset.unnamedChar'),
      meta: char.role || t('episode.asset.charPortrait'),
      imageUrl,
      ready: !!imageUrl,
      bound: getStoryboardCharacterIds(sb).includes(char.id),
      states: (char.states || []).filter(s => s.image_url || s.imageUrl),
      selectedStateId: getShotCharacterStateId(sb, char.id),
    })
  }
  for (const scene of scenes.value) {
    const imageUrl = scene.image_url || scene.imageUrl || ''
    out.push({
      key: `scene-${scene.id}`,
      id: scene.id,
      kind: 'scene',
      typeLabel: t('common.scene'),
      name: `${scene.location} · ${scene.time || t('episode.asset.noTime')}`,
      meta: scene.time || t('episode.asset.sceneImage'),
      imageUrl,
      ready: !!imageUrl,
      bound: (sb?.scene_id || sb?.sceneId) === scene.id,
    })
  }
  for (const prop of propItems.value) {
    const imageUrl = prop.image_url || prop.imageUrl || ''
    out.push({
      key: `prop-${prop.id}`,
      id: prop.id,
      kind: 'prop',
      typeLabel: t('common.prop'),
      name: prop.name || t('episode.asset.unnamedProp'),
      meta: prop.type || t('episode.asset.propSingleImage'),
      imageUrl,
      ready: !!imageUrl,
      bound: getStoryboardPropIds(sb).includes(prop.id),
    })
  }
  // 固定顺序（角色→场景→道具，按资产原顺序）：点击绑定/解绑不重排，避免跳动
  return out
}

// 右侧参考素材面板渲染用：当前分镜可绑定的全部素材
const refBindableAssets = computed(() => {
  const sb = selectedSb.value
  return sb ? shotBindableAssets(sb) : []
})

// 右栏「绑定参考图」：当前分镜已绑定素材（生成时作为参考图提交），按分组顺序平铺展示
const boundRefAssets = computed(() => refBindableAssets.value.filter(a => a.bound))

// 参考面板分组顺序（kind code 驱动，label 渲染时求值）
const REF_KINDS = computed(() => ([
  { kind: 'character', label: t('common.role') },
  { kind: 'scene', label: t('common.scene') },
  { kind: 'prop', label: t('common.prop') },
]))

// 右侧面板切换绑定：场景单选（切换/解绑），角色/道具多选（kind code 判断，不依赖显示文案）
function toggleShotBind(sb, asset) {
  if (asset.kind === 'scene') {
    const current = sb?.scene_id || sb?.sceneId
    updateField(sb, 'scene_id', current === asset.id ? null : asset.id)
    return
  }
  if (asset.kind === 'character') {
    toggleStoryboardCharacter(sb, asset.id)
    return
  }
  toggleStoryboardProp(sb, asset.id)
}

// 视频提示词 @ 引用候选：仅当前分镜已绑定的角色与道具（按名字引用）、场景（按地点引用），展示顺序：角色 → 场景 → 道具
// kind 为逻辑值（MentionTextarea 按 kind 着色/选图标），group 为显示文案
const mentionOptions = computed(() => {
  const sb = selectedSb.value
  if (!sb) return []
  const scene = getStoryboardScene(sb)
  return [
    ...getStoryboardCharacters(sb).map(c => ({
      label: c.name,
      value: c.name,
      kind: 'character',
      group: t('common.role'),
      image: thumbOf(assetImageSrc(c)),
    })),
    ...(scene ? [{
      label: `${scene.location} · ${scene.time || t('episode.asset.noTime')}`,
      value: scene.location,
      kind: 'scene',
      group: t('common.scene'),
      image: thumbOf(assetImageSrc(scene)),
    }] : []),
    ...getStoryboardProps(sb).map(p => ({
      label: p.name,
      value: p.name,
      kind: 'prop',
      group: t('common.prop'),
      image: thumbOf(assetImageSrc(p)),
    })),
  ]
})

// 按参考图顺序（场景图在前、角色图居中、道具图在后）为 @名字 建立索引映射，供视频提示词引用替换
function getShotReferenceIndexMap(sb) {
  const nameToIndex = {}
  getShotReferenceMedia(sb).forEach((a, i) => { if (a.name && !(a.name in nameToIndex)) nameToIndex[a.name] = i + 1 })
  return nameToIndex
}

function resolveDoubaoVideoPrompt(sb) {
  const description = String(sb?.description || '')
  const prefix = getShotReferenceMedia(sb)
    .map((item, index) => `参考图${index + 1}是：${item.name}`)
    .join('；')
  return prefix ? `${prefix}。\n${description}` : description
}

// 将视频提示词里的 @名字 替换为 @图片N名字（N 为参考图序号，1 起），生成时使用
function resolveVideoPromptRefs(sb) {
  const prompt = sb.video_prompt || sb.videoPrompt || ''
  const map = getShotReferenceIndexMap(sb)
  const names = Object.keys(map).sort((a, b) => b.length - a.length)
  const resolved = names.length ? prompt.replace(/@([^\s@]+)/g, (m, raw) => {
    for (const name of names) {
      if (raw.startsWith(name)) {
        return `@图片${map[name]}${name}${raw.slice(name.length)}`
      }
    }
    return m
  }) : prompt
  return resolved
}

// 分镜时长（视频生成参数区直接编辑并保存到分镜）：
// 统一限制 2-15s，列表/批量/单次生成统一读取该值
function onVideoDurationChange(e) {
  const sb = selectedSb.value
  if (!sb) return
  const min = 2
  const max = 15
  let v = Math.round(Number(e.target.value))
  if (!Number.isFinite(v)) v = Number(sb.duration || 10)
  v = Math.min(max, Math.max(min, v))
  e.target.value = v
  updateField(sb, 'duration', v)
}

const MAX_VIDEO_SEED = 2_147_483_647

function storyboardVideoSeed(sb) {
  const value = Number(sb?.video_seed ?? sb?.videoSeed)
  return Number.isInteger(value) && value >= 0 && value <= MAX_VIDEO_SEED ? value : null
}

function nextVideoSeed(current) {
  let seed
  do {
    if (globalThis.crypto?.getRandomValues) {
      seed = globalThis.crypto.getRandomValues(new Uint32Array(1))[0] & MAX_VIDEO_SEED
    } else {
      seed = Math.floor(Math.random() * (MAX_VIDEO_SEED + 1))
    }
  } while (seed === current)
  return seed
}

async function randomizeVideoSeed(sb) {
  if (!sb || isPendingVideo(sb.id)) return
  const seed = nextVideoSeed(storyboardVideoSeed(sb))
  try {
    const updated = await storyboardAPI.update(sb.id, { video_seed: seed })
    const persistedSeed = storyboardVideoSeed(updated)
    if (persistedSeed !== seed) throw new Error(t('episode.inspector.seedSaveFailed'))
    sb.video_seed = persistedSeed
    sb.videoSeed = persistedSeed
    toast.success(t('episode.inspector.seedUpdated', { seed: persistedSeed }))
  } catch (err) {
    toastError(err)
  }
}

function pickFile(accept, cb) {
  const input = document.createElement('input')
  input.type = 'file'
  input.accept = accept
  input.onchange = () => { const f = input.files?.[0]; if (f) cb(f) }
  input.click()
}

// ===== 资产图片拖拽上传（拖图片到资产卡片 / 预览面板 / 造型缩略图即可）=====
const ASSET_IMAGE_ACCEPT = 'image/jpeg,image/png,image/webp,.jpg,.jpeg,.png,.webp'
const isAssetImageFile = (file) => ['image/jpeg', 'image/png', 'image/webp'].includes(String(file?.type || '').toLowerCase())
  || /\.(jpe?g|png|webp)$/i.test(String(file?.name || ''))

const assetDropTarget = ref(null) // 当前拖拽悬停中的目标 `${kind}:${id}`
function isAssetDropActive(kind, id) { return assetDropTarget.value === `${kind}:${id}` }
function activateAssetImageDrop(e, kind, id) {
  if (!Array.from(e.dataTransfer?.types || []).includes('Files') || isUploadingAsset(kind, id)) return
  if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy'
  assetDropTarget.value = `${kind}:${id}`
}
function deactivateAssetImageDrop(e, kind, id) {
  // 目标内部子元素之间移动也会触发 dragleave，忽略这类「假离开」
  if (e.relatedTarget instanceof Node && e.currentTarget?.contains?.(e.relatedTarget)) return
  if (isAssetDropActive(kind, id)) assetDropTarget.value = null
}
function pickImageFileFromDrop(e) {
  const files = Array.from(e.dataTransfer?.files || [])
  if (!files.length) return null
  const file = files.find(isAssetImageFile)
  if (!file) toast.error(t('episode.upload.imageFileOnly'))
  return file
}
function dropAssetImage(e, kind, id) {
  assetDropTarget.value = null
  const file = pickImageFileFromDrop(e)
  if (file) applyAssetImageFile(kind, id, file)
}
function dropCostumeImage(e, st) {
  assetDropTarget.value = null
  const char = assetDetail.value.item
  const file = pickImageFileFromDrop(e)
  if (file && char) applyCostumeImageFile(char, st, file)
}

// ===== 资产图片手动上传（角色形象 / 场景图 / 道具图）=====
// 上传类型显示名渲染时求值
// ─── 服装/造型状态（角色卡与资产详情弹窗；底图之外的状态）────────────────
const pendingCostumeImageKeys = ref([])
const costumeCreating = ref(false)
const costumeSaving = ref(false)
const costumeDraft = ref({ name: '', description: '' })
function isPendingCostumeImage(charId, stateId) { return pendingCostumeImageKeys.value.includes(`${charId}:${stateId}`) }
async function reloadCharacterStates(charId) {
  const res = await characterStateAPI.list(charId)
  const states = res?.states || []
  for (const c of chars.value) if (c.id === charId) c.states = states
  if (assetDetail.value.item?.id === charId) assetDetail.value.item.states = states
}
async function createCostume() {
  const char = assetDetail.value.item
  if (!char || costumeSaving.value || !costumeDraft.value.name.trim()) return
  costumeSaving.value = true
  try {
    await characterStateAPI.create(char.id, { name: costumeDraft.value.name.trim(), description: costumeDraft.value.description.trim() })
    costumeDraft.value = { name: '', description: '' }
    costumeCreating.value = false
    await reloadCharacterStates(char.id)
  } catch (e) { toastError(e) } finally { costumeSaving.value = false }
}
async function saveCostumeField(st, field, value) {
  const char = assetDetail.value.item
  if (!char) return
  if (String(st[field] ?? '') === String(value ?? '')) return
  try {
    await characterStateAPI.update(char.id, st.id, { [field]: value })
    await reloadCharacterStates(char.id)
  } catch (e) { toastError(e) }
}
async function deleteCostume(st) {
  const char = assetDetail.value.item
  if (!char) return
  try {
    await characterStateAPI.del(char.id, st.id)
    await reloadCharacterStates(char.id)
  } catch (e) { toastError(e) }
}
async function genCostumeImg(st) {
  const char = assetDetail.value.item
  if (!char) return
  const key = `${char.id}:${st.id}`
  if (pendingCostumeImageKeys.value.includes(key)) return
  pendingCostumeImageKeys.value.push(key)
  try {
    const previous = st.image_url || st.imageUrl || ''
    await characterStateAPI.generateImage(char.id, st.id, epId.value, bareModelName(imageModel.value) || undefined, ownerConfigId(imageModelOptions.value, imageModel.value), chatModelOverride(), chatConfigId())
    toast.success(t('episode.asset.costumeGenerating'))
    await refresh()
    watchAsyncResult(() => {
      const fresh = (chars.value.find(c => c.id === char.id)?.states || []).find(s => s.id === st.id)
      const current = fresh?.image_url || fresh?.imageUrl || ''
      return !!current && current !== previous
    })
  } catch (e) { toastError(e) } finally {
    pendingCostumeImageKeys.value = pendingCostumeImageKeys.value.filter(k => k !== key)
  }
}
function uploadCostumeImg(st) {
  const char = assetDetail.value.item
  if (!char) return
  pickFile(ASSET_IMAGE_ACCEPT, (file) => { applyCostumeImageFile(char, st, file) })
}

// 按钮选择与拖拽共用的造型图落库路径
async function applyCostumeImageFile(char, st, file) {
  const key = `costume:${st.id}`
  if (uploadingAssetKeys.value.includes(key)) return
  uploadingAssetKeys.value.push(key)
  try {
    const res = await uploadAPI.image(file)
    // 手动上传图是新的视觉事实；旧描述/提示词属于上一张图，不能继续沿用。
    await characterStateAPI.update(char.id, st.id, {
      image_url: res.path,
      local_path: res.path,
      description: '',
      final_prompt: '',
      manual_upload: true,
    })
    toast.success(t('episode.upload.assetDone', { type: t('episode.asset.costumeImage') }))
    await reloadCharacterStates(char.id)
  } catch (e) {
    toastError(e)
  } finally {
    uploadingAssetKeys.value = uploadingAssetKeys.value.filter(k => k !== key)
  }
}

const assetUploadLabelMap = computed(() => ({
  character: t('episode.asset.charPortrait'),
  scene: t('episode.asset.sceneImage'),
  prop: t('episode.asset.propImage'),
}))
const manualUploadVisualReset = {
  character: { appearance: '', styling: '', final_prompt: '' },
  scene: { prompt: '', lighting: '', final_prompt: '' },
  prop: { description: '', final_prompt: '' },
}
const uploadingAssetKeys = ref([])
function isUploadingAsset(kind, id) { return uploadingAssetKeys.value.includes(`${kind}:${id}`) }

function resetOpenAssetDraftAfterUpload(kind, id) {
  if (!assetDetail.value.open || assetDetail.value.type !== kind || assetDetail.value.item?.id !== id) return
  if (kind === 'character') {
    Object.assign(assetDetailDraft.value, { appearance: '', styling: '' })
    assetPromptDraft.value = ''
    assetPromptDirty.value = false
  } else if (kind === 'scene') {
    Object.assign(assetDetailDraft.value, { prompt: '', lighting: '' })
    assetPromptDraft.value = ''
    assetPromptDirty.value = false
  } else {
    assetDetailDraft.value.description = ''
    assetPromptDraft.value = ''
    assetPromptDirty.value = false
  }
}

function uploadAssetImage(kind, id) {
  pickFile(ASSET_IMAGE_ACCEPT, (file) => { applyAssetImageFile(kind, id, file) })
}

// 按钮选择与拖拽共用的落库路径
async function applyAssetImageFile(kind, id, file) {
  const key = `${kind}:${id}`
  if (uploadingAssetKeys.value.includes(key)) return
  uploadingAssetKeys.value.push(key)
  try {
    const res = await uploadAPI.image(file)
    // 与生图回写保持一致：存相对路径（static/...），前端展示时补前导斜杠
    const payload = {
      image_url: res.path,
      local_path: res.path,
      manual_upload: true,
      ...(manualUploadVisualReset[kind] || {}),
    }
    if (kind === 'character') await characterAPI.update(id, payload)
    else if (kind === 'scene') await sceneAPI.update(id, payload)
    else await propAPI.update(id, payload)
    // 本地先回写该资产，卡片立即显示新图；全量刷新退到后台补齐其余数据
    const local = (kind === 'character' ? chars.value : kind === 'scene' ? scenes.value : props.value)
      .find(x => x.id === id)
    if (local) Object.assign(local, payload)
    if (assetDetail.value.open && assetDetail.value.type === kind && assetDetail.value.item?.id === id) {
      Object.assign(assetDetail.value.item, payload)
    }
    toast.success(t('episode.upload.assetDone', { type: assetUploadLabelMap.value[kind] || '' }))
    void refresh()
    resetOpenAssetDraftAfterUpload(kind, id)
    if (assetDetail.value.open && assetDetail.value.type === kind && assetDetail.value.item?.id === id) {
      await loadAssetImageHistory()
    }
  } catch (e) {
    toastError(e)
  } finally {
    uploadingAssetKeys.value = uploadingAssetKeys.value.filter(k => k !== key)
  }
}

function uploadAssetAudio(id) {
  pickFile('audio/mpeg,audio/wav,audio/mp4,audio/x-m4a,audio/aac,.mp3,.wav,.m4a,.aac', async (file) => {
    const key = `character-audio:${id}`
    if (!uploadingAssetKeys.value.includes(key)) uploadingAssetKeys.value.push(key)
    try {
      const res = await uploadAPI.audio(file)
      await characterAPI.update(id, { audio_url: res.path })
      toast.success('角色音频已绑定')
      await refresh()
    } catch (e) {
      toastError(e)
    } finally {
      uploadingAssetKeys.value = uploadingAssetKeys.value.filter(k => k !== key)
    }
  })
}

function clearAssetAudio(id) {
  const key = `character-audio:${id}`
  if (!uploadingAssetKeys.value.includes(key)) uploadingAssetKeys.value.push(key)
  characterAPI.update(id, { audio_url: null }).then(async () => {
    toast.success('角色音频已取消绑定')
    await refresh()
  }).catch((e) => {
    toastError(e)
  }).finally(() => {
    uploadingAssetKeys.value = uploadingAssetKeys.value.filter(k => k !== key)
  })
}

async function genVid(sb, opts = {}) {
  const referenceImages = getShotReferenceImages(sb)
  // 参考素材完全来自分镜绑定的角色/场景/道具图片
  const params = {
    storyboard_id: sb.id,
    drama_id: dramaId,
    // 豆包不接受 MiniMax H3 Ref2VA：即使数据库里还是旧 H3 提示词，提交时也强制使用豆包格式。
    prompt: isDoubaoLocalVideo.value ? resolveDoubaoVideoPrompt(sb) : resolveVideoPromptRefs(sb),
    duration: Number(sb.duration || 10),
    aspect_ratio: dramaAspectRatio.value,
    generate_audio: true,
    model: effectiveVideoModelLabel.value || undefined,
    config_id: selectedVideoConfig.value?.id,
    reference_image_urls: referenceImages,
    reference_audio_urls: isDoubaoLocalVideo.value ? [] : getShotReferenceAudios(sb),
  }
  if (!params.prompt && !referenceImages.length) {
    toast.error(t('episode.vid.needRefOrPrompt'))
    return false
  }
  try {
    if (isDoubaoLocalVideo.value) {
      const storedPrompt = String(sb.video_prompt || sb.videoPrompt || '')
      if (storedPrompt !== params.prompt) {
        // 实际提交给豆包的最终提示词同时落库，并同步当前响应式对象，确保提示词框与任务 payload 一致。
        await storyboardAPI.update(sb.id, { video_prompt: params.prompt })
        sb.video_prompt = params.prompt
        sb.videoPrompt = params.prompt
      }
    }
    delete failedVideoMessages.value[sb.id]
    if (!isPendingVideo(sb.id)) pendingVideoIds.value.push(sb.id)
    if (!submittingVideoIds.value.includes(sb.id)) submittingVideoIds.value.push(sb.id)
    const generation = await taskAPI.generate({ type: 'video', ...params })
    submittingVideoIds.value = submittingVideoIds.value.filter(id => id !== sb.id)
    if (!opts.silent) toast.success(t('episode.vid.generating'))
    await refresh()
    if (!generation?.id) console.warn('Video task response did not include an id; status will be recovered from the episode task list')
    return true
  } catch (e) {
    submittingVideoIds.value = submittingVideoIds.value.filter(id => id !== sb.id)
    pendingVideoIds.value = pendingVideoIds.value.filter(item => item !== sb.id)
    failedVideoMessages.value = {
      ...failedVideoMessages.value,
      [sb.id]: e.message || t('episode.vid.genFailed'),
    }
    toastError(e, { fallback: 'episode.vid.genFailed' })
    return false
  }
}
const batchDownloading = ref(false)
async function batchDownloadVideos() {
  if (!exportSelectedReadyIds.value.length || batchDownloading.value) return
  batchDownloading.value = true
  try {
    await api.download('/storyboards/batch-download', { drama_id: dramaId, ids: exportSelectedReadyIds.value }, 'storyboard-videos.zip')
  } catch (e) {
    toastError(e)
  } finally {
    batchDownloading.value = false
  }
}

async function doMerge(ids) {
  const storyboardIds = Array.isArray(ids) ? ids : undefined
  if (storyboardIds && !storyboardIds.length) {
    toast.error(t('episode.export.selectFirst'))
    return
  }
  try {
    await mergeAPI.merge(epId.value, storyboardIds)
    toast.success(t('episode.export.mergingToast'))
  } catch (e) {
    toastError(e, { fallback: 'episode.export.mergeFailed' })
    return
  }
  const poll = setInterval(async () => {
    try { mergeData.value = await mergeAPI.status(epId.value) } catch {}
    if (mergeData.value?.status === 'completed' || mergeData.value?.status === 'failed') {
      clearInterval(poll)
      if (mergeData.value.status === 'completed') {
        toast.success(t('episode.export.mergeDone'))
        loadExportMerges()
      } else {
        toastError(mergeData.value?.error_msg || mergeData.value?.errorMsg, { fallback: 'episode.export.mergeFailed' })
      }
    }
  }, 3000)
}
async function loadConfigs() {
  try {
    const [imgCfgs, vidCfgs] = await Promise.all([
      aiConfigAPI.list('image'),
      aiConfigAPI.list('video'),
    ])
    imageConfigs.value = imgCfgs || []
    videoConfigs.value = vidCfgs || []
  } catch (e) { console.error('Failed to load AI configs', e) }
}

onMounted(async () => {
  // HMR 可能保留旧 Driver.js 实例的全局 pointer-events 锁；进入工作台先强制清理。
  stopTour()
  await refresh()
  await loadConfigs()
  loadClaudeRuntime()
  await syncExtractStatus()
  syncAgentInflight()
  resumeEpisodeVideoPipeline()
})

// ===== 应用内引导（工作台）：沿左侧进度栏展示独立制作步骤 =====
const EPISODE_TOUR = [
  { element: '.studio-topbar-main', titleKey: 'tour.episode.topbar.title', descKey: 'tour.episode.topbar.desc', popoverSide: 'bottom' },
  { element: '.pipe-section:nth-of-type(1)', titleKey: 'tour.episode.script.title', descKey: 'tour.episode.script.desc', popoverSide: 'right' },
  { element: '.pipe-section:nth-of-type(2) .pipe-item:nth-of-type(1)', titleKey: 'tour.episode.assets.title', descKey: 'tour.episode.assets.desc', popoverSide: 'right' },
  { element: '.pipe-section:nth-of-type(2) .pipe-item:nth-of-type(2)', titleKey: 'tour.episode.breakdown.title', descKey: 'tour.episode.breakdown.desc', popoverSide: 'right' },
  { element: '.pipe-section:nth-of-type(2) .pipe-item:last-child', titleKey: 'tour.episode.videos.title', descKey: 'tour.episode.videos.desc', popoverSide: 'right' },
  { element: '.studio-actions .tour-help-btn', titleKey: 'tour.episode.help.title', descKey: 'tour.episode.help.desc', popoverSide: 'bottom', popoverAlign: 'end' },
]
</script>

<style scoped>
/* ===== Studio Layout ===== */
.studio {
  display: flex;
  flex-direction: column;
  height: 100vh;
  overflow: hidden;
  padding: 8px;
  gap: 8px;
  background: var(--surface-base);
  /* 选中态:中性反色(浅色近黑/深色近白),与进行中(蓝脉冲)/已完成(冷蓝勾)区分,
     遵循「颜色只承担状态指示」——选中不是状态,保持无色 */
  --sel: var(--text-0);
  --sel-bg: var(--bg-active);
  --sel-text: var(--text-0);
  --sel-glow: var(--bg-hover);
}

.studio-topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  flex-shrink: 0;
  min-height: 40px;
  padding: 4px 10px;
  border-radius: var(--radius-lg);
  background: var(--header-bg);
  border: 1px solid var(--border);
  box-shadow: var(--shadow-card);
  backdrop-filter: blur(20px) saturate(180%);
  -webkit-backdrop-filter: blur(20px) saturate(180%);
}

.studio-topbar-main,
.sidebar,
.main {
  background: var(--surface-raised);
  border: 1px solid var(--border);
  box-shadow: var(--shadow-card);
}

.studio-topbar-main {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 0;
  border: 0;
  box-shadow: none;
  backdrop-filter: none;
  background: transparent;
  min-width: 0;
}

/* 用更高特异性压过后面的 .back-btn{height:40px}，保持顶栏紧凑 */
.studio-topbar .topbar-back {
  width: auto;
  min-width: 72px;
  padding: 0 12px;
  height: 26px;
  border-radius: var(--radius-pill);
  white-space: nowrap;
  font-size: .6875rem;
}

.studio-identity {
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.studio-overline {
  display: none;
  font-size: .5rem;
  font-weight: 700;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--text-3);
}

.studio-title-row {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.studio-title {
  font-size: .8125rem;
  line-height: 1;
  letter-spacing: -0.04em;
  white-space: nowrap;
}

.studio-episode-chip {
  display: inline-flex;
  align-items: center;
  height: 18px;
  padding: 0 8px;
  border-radius: var(--radius-pill);
  background: var(--accent-bg);
  color: var(--accent-text);
  font-size: .5625rem;
  font-weight: 700;
}

.studio-episode-title {
  max-width: 220px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: .6875rem;
  font-weight: 600;
  line-height: 1;
  color: var(--text-1);
  padding-left: 6px;
  border-left: 1px solid var(--border);
}

.studio-meta-row {
  display: flex;
  align-items: center;
  gap: 4px;
  flex-wrap: nowrap;
  min-width: 0;
}

.studio-meta-pill {
  display: inline-flex;
  align-items: center;
  height: 18px;
  padding: 0 8px;
  border-radius: var(--radius-pill);
  background: var(--accent-bg);
  color: var(--accent-text);
  font-size: .5rem;
  font-weight: 600;
  white-space: nowrap;
}

.studio-meta-pill.is-stage {
  background: var(--accent-bg);
  color: var(--accent-text);
}
.studio-meta-pill.is-progress {
  background: var(--success-bg);
  color: var(--success);
}
.studio-meta-inline {
  font-size: .5625rem;
  color: var(--text-3);
  font-weight: 600;
  white-space: nowrap;
}

.studio-topbar-side {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
}

.studio-actions {
  display: flex;
  gap: 6px;
}
.studio-topbar .btn {
  height: 26px;
  padding: 0 9px;
  font-size: .65625rem;
  white-space: nowrap;
}

.studio-body {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);  /* 列宽跟随侧栏实际宽度（收起时 46px） */
  gap: 8px;
  min-height: 0;
  flex: 1;
}

/* ===== Sidebar ===== */
.sidebar {
  width: 208px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  min-height: 0;
  border-radius: var(--radius);
}
.back-btn {
  min-width: 40px; width: auto; height: 40px; flex-shrink: 0;
  display: flex; align-items: center; justify-content: center;
  gap: 6px;
  padding: 0 12px;
  border: none; border-radius: var(--radius-pill);
  background: var(--overlay-track); color: var(--text-1);
  cursor: pointer; transition: all 0.18s var(--ease-out);
  font-size: .75rem;
  font-weight: 650;
  line-height: 1;
}
.back-btn:hover {
  background: var(--bg-active);
  color: var(--text-0);
}
.back-btn:focus-visible {
  outline: none;
  box-shadow: 0 0 0 3.5px var(--button-focus);
}

/* Pipeline Nav */
.pipeline { flex: 1; overflow-y: auto; padding: 12px 10px 8px; display: flex; flex-direction: column; gap: 8px; }
.pipe-section { display: flex; flex-direction: column; gap: 2px; }
.pipe-section-label {
  display: flex; align-items: center; gap: 5px;
  font-size: .65625rem; font-weight: 700; color: var(--text-3);
  text-transform: uppercase; letter-spacing: 0.06em;
  padding: 0 7px 2px;
}
/* 分组标题保持中性灰,状态色只在左侧小指示器上 */
.pipe-section-state {
  width: 13px; height: 13px; border-radius: 999px; flex-shrink: 0;
  display: inline-flex; align-items: center; justify-content: center;
}
.pipe-section.is-done .pipe-section-state {
  background: var(--success-bg); color: var(--success);
  border: 1px solid var(--success-bg);
}
.pipe-section-dot {
  width: 5px; height: 5px; border-radius: 999px;
  background: var(--text-3); opacity: 0.55;
}
.pipe-section-pulse {
  width: 6px; height: 6px; border-radius: 999px;
  background: var(--accent);
  animation: pipeSectionPulse 1.6s var(--ease-out) infinite;
}
@keyframes pipeSectionPulse {
  0% { box-shadow: 0 0 0 0 var(--accent-glow); }
  70% { box-shadow: 0 0 0 5px transparent; }
  100% { box-shadow: 0 0 0 0 transparent; }
}
.pipe-section-tag {
  font-size: .59375rem; font-weight: 600; letter-spacing: 0.03em;
  color: var(--text-2); background: var(--bg-2);
  border-radius: 999px; padding: 1px 5px;
  text-transform: none;
}
/* 子步骤进行中:与大环节同步的脉冲点 */
.pipe-item.doing { color: var(--text-1); }
.pipe-item.doing .pipe-icon {
  background: var(--accent-bg);
  border-color: var(--accent-glow);
}
.pipe-item-pulse {
  width: 6px; height: 6px; border-radius: 999px;
  background: var(--accent);
  animation: pipeSectionPulse 1.6s var(--ease-out) infinite;
}
.pipe-item {
  position: relative;
  display: grid; grid-template-columns: auto 1fr auto; align-items: center; gap: 10px;
  padding: 7px 10px;
  border-radius: var(--radius);
  font-size: .8125rem; font-weight: 600;
  background: transparent; border: 1px solid transparent; color: var(--text-2); cursor: pointer;
  transition: all 0.18s var(--ease-out); width: 100%; text-align: left;
}
.pipe-item:hover {
  background: var(--bg-hover);
  border-color: transparent;
  color: var(--text-0);
}
.pipe-item.active {
  background: var(--sel-bg);
  color: var(--sel-text);
  border-color: transparent;
  box-shadow: none;
}
/* H3 品牌签名：激活步骤左侧 3px 品牌色圆角指示条 */
.pipe-item.active::before {
  content: '';
  position: absolute;
  left: 0;
  top: 9px;
  bottom: 9px;
  width: 3px;
  border-radius: 999px;
  background: var(--accent);
}
.pipe-item:focus-visible {
  outline: none;
  border-color: var(--action-primary);
  box-shadow: 0 0 0 3px var(--button-focus), var(--button-shadow);
}
.pipe-item.done { color: var(--text-2); }
.pipe-item-sub {
  grid-template-columns: auto minmax(0, 1fr);
  align-items: center;
  padding: 5px 8px;
  position: relative;
  min-height: 34px;
}

.pipe-item-sub:not(:last-child)::after {
  content: '';
  position: absolute;
  left: 15px;
  top: 23px;
  bottom: -6px;
  width: 1px;
  background: var(--border);
}

.pipe-icon {
  width: 16px; height: 16px; border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  background: var(--bg-2); border: 1px solid var(--border);
  color: var(--text-3); flex-shrink: 0; transition: all 0.15s;
  position: relative;
  z-index: 1;
}
.pipe-item.active .pipe-icon { background: var(--sel); border-color: var(--sel); color: var(--surface-raised); }
.pipe-item.done .pipe-icon { background: var(--success-bg); border-color: var(--success-bg); color: var(--success); }
.pipe-item.active.done .pipe-icon { background: var(--sel); border-color: var(--sel); color: var(--surface-raised); }
.icon-active { background: var(--sel) !important; border-color: var(--sel) !important; color: var(--surface-raised) !important; }
.icon-done { background: var(--success-bg) !important; border-color: var(--success-bg) !important; color: var(--success) !important; }
.pipe-item.active.done .icon-done { background: var(--sel) !important; border-color: var(--sel) !important; color: var(--surface-raised) !important; }

.pipe-label { flex: 1; font-size: .78125rem; }
.pipe-copy { min-width: 0; display: flex; flex-direction: column; gap: 1px; }
.pipe-sub {
  display: none;
  font-size: .625rem;
  line-height: 1.35;
  color: var(--text-3);
  font-weight: 500;
}
.pipe-badge {
  font-size: .5625rem; font-weight: 700; padding: 1px 5px;
  border-radius: 99px; background: var(--bg-3); color: var(--text-3);
  font-family: var(--font-mono);
}
.pipe-badge.badge-done { background: var(--success-bg); color: var(--success); }
.pipe-spinner { width: 10px; height: 10px; border: 1.5px solid var(--accent-bg); border-top-color: var(--accent); border-radius: 50%; animation: spin 0.8s linear infinite; }

/* Sidebar Bottom */
.sidebar-bottom {
  padding: 9px 10px 10px;
  border-top: 1px solid var(--border);
  display: flex; flex-direction: column; gap: 7px;
  flex-shrink: 0;
  background: var(--surface-soft);
}
/* 收起/展开按钮 */
.sidebar-toggle {
  display: flex; align-items: center; justify-content: center; gap: 5px;
  width: 100%; min-height: 22px;
  border: none; border-radius: 6px;
  background: transparent; color: var(--text-3);
  font: 600 .6875rem var(--font-body);
  cursor: pointer; transition: background 0.14s, color 0.14s;
}
.sidebar-toggle:hover { background: var(--bg-hover); color: var(--text-0); }
.sidebar-toggle-icon { transition: transform 0.22s var(--ease-out); flex-shrink: 0; }
.sidebar.collapsed .sidebar-toggle-icon { transform: rotate(180deg); }

/* ===== 收起态：窄图标栏 ===== */
.sidebar { transition: width 0.22s var(--ease-out); }
.sidebar.collapsed { width: 46px; }
.sidebar.collapsed .pipeline { padding: 12px 5px 8px; gap: 10px; }
.sidebar.collapsed .pipe-section-label { justify-content: center; padding: 0 0 2px; }
.sidebar.collapsed .pipe-section-label > span:not(.pipe-section-state) { display: none; }
.sidebar.collapsed .pipe-item {
  grid-template-columns: auto; justify-content: center;
  padding: 6px 0; min-height: 0;
}
.sidebar.collapsed .pipe-item .pipe-copy { display: none; }
.sidebar.collapsed .pipe-item-sub:not(:last-child)::after { display: none; }
.sidebar.collapsed .pipe-icon { width: 22px; height: 22px; }
/* 收起态：进行中步骤的角标脉冲点 */
.pipe-mini-pulse {
  position: absolute; top: -3px; right: -3px;
  width: 7px; height: 7px; border-radius: 50%;
  background: var(--accent);
  border: 1.5px solid var(--surface-raised);
  animation: pipeSectionPulse 1.6s var(--ease-out) infinite;
}
.sidebar.collapsed .sidebar-progress { display: none; }
.sidebar.collapsed .sidebar-bottom { padding: 9px 6px 10px; align-items: center; }
.sidebar.collapsed .refresh-btn { width: 28px; min-height: 28px; padding: 0; font-size: 0; gap: 0; }
/* 步骤跑马灯：四段主流程进度条，当前段流动光效 */
.sidebar-progress {
  display: flex;
  flex-direction: column;
  gap: 7px;
  padding: 2px 2px 4px;
}
.sidebar-progress-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
}
.sidebar-progress-title {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: .78125rem;
  font-weight: 700;
  color: var(--text-1);
}
.sidebar-progress-count {
  flex-shrink: 0;
  font-family: var(--font-mono);
  font-size: .6875rem;
  color: var(--text-3);
}
.sidebar-progress-track {
  display: flex;
  gap: 4px;
}
.sidebar-progress-seg {
  position: relative;
  flex: 1;
  height: 5px;
  padding: 0;
  border: none;
  border-radius: 999px;
  background: var(--overlay-track);
  cursor: pointer;
  overflow: hidden;
  transition: background 0.2s var(--ease-out), transform 0.15s var(--ease-out);
}
.sidebar-progress-seg:hover { transform: scaleY(1.6); }
.sidebar-progress-seg:focus-visible {
  outline: none;
  box-shadow: 0 0 0 3px var(--button-focus);
}
.sidebar-progress-seg.done { background: var(--success); }
.sidebar-progress-seg.current { background: var(--accent-bg); }
/* 跑马灯流动光：当前段内的渐变高光持续滑动 */
.sidebar-progress-seg.current .sidebar-progress-seg-fill {
  position: absolute;
  inset: 0;
  background: linear-gradient(90deg,
    var(--accent) 0%,
    color-mix(in srgb, var(--accent) 30%, #fff 70%) 50%,
    var(--accent) 100%);
  background-size: 220% 100%;
  animation: seg-marquee 1.5s linear infinite;
}
.sidebar-progress-seg:not(.current) .sidebar-progress-seg-fill { display: none; }
@keyframes seg-marquee {
  from { background-position: 220% 0; }
  to { background-position: -220% 0; }
}
.sidebar-progress-labels {
  display: flex;
  gap: 4px;
}
.sidebar-progress-labels span {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  text-align: center;
  font-size: .65625rem;
  color: var(--text-3);
}
.sidebar-progress-labels span.done { color: var(--success); }
.sidebar-progress-labels span.on {
  color: var(--accent-text);
  font-weight: 700;
}
.refresh-btn {
  width: 100%; display: flex; align-items: center; justify-content: center; gap: 6px;
  min-height: 28px;
  padding: 0 10px; font-size: .78125rem; font-weight: 650; color: var(--button-text);
  background: var(--button-bg); border: 1px solid var(--button-border); border-radius: var(--button-radius);
  cursor: pointer; transition: all 0.18s var(--ease-out);
  box-shadow: var(--button-shadow);
}
.refresh-btn:hover {
  background: var(--button-bg-hover);
  border-color: var(--button-border-hover);
  color: var(--button-text-hover);
  box-shadow: var(--button-shadow-hover);
}
.refresh-btn:focus-visible {
  outline: none;
  border-color: var(--action-primary);
  box-shadow: 0 0 0 3px var(--button-focus), var(--button-shadow-hover);
}

/* ===== Main Content ===== */
.main { flex: 1; display: flex; flex-direction: column; overflow: hidden; min-width: 0; min-height: 0; border-radius: var(--radius); }
.content-panel { flex: 1; display: flex; flex-direction: column; overflow: hidden; position: relative; min-height: 0; }
.stage-subnav {
  align-self: flex-start;
  margin: 4px 12px 0;
  max-width: calc(100% - 24px);
  overflow-x: auto;
  flex-shrink: 0;
}
.stage-subnav-item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  white-space: nowrap;
  /* 压过全局 .seg-item{padding:7px 16px}，收紧子导航高度 */
  padding: 4px 13px;
  font-size: .75rem;
}
.stage-subnav-item.active {
  background: var(--seg-active-bg);
  color: var(--text-0);
  box-shadow: 0 1px 4px rgba(0,0,0,0.12);
}
.stage-subnav-item.done {
  color: var(--text-1);
}
.stage-subnav-item.active.done {
  color: var(--text-0);
}
.stage-subnav-dot {
  width: 6px;
  height: 6px;
  border-radius: 999px;
  background: var(--success);
}

/* Toolbar */
.step-toolbar {
  display: flex; align-items: center; gap: 10px;
  min-height: 44px;
  padding: 8px 12px; border-bottom: 1px solid var(--border);
  background: var(--surface-raised); flex-shrink: 0;
}
.prod-toolbar { background: var(--surface-raised); }
.toolbar-left { display: flex; align-items: center; gap: 8px; flex: 1; }
.toolbar-right { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.step-indicator { display: flex; align-items: center; gap: 8px; }
.step-num {
  width: 26px; height: 26px; border-radius: 10px;
  display: inline-flex; align-items: center; justify-content: center;
  background: var(--accent-bg);
  font-family: var(--font-mono); font-size: .625rem; font-weight: 800; color: var(--accent-text); letter-spacing: 0.05em;
}
.step-name { font-size: .78125rem; font-weight: 700; color: var(--text-1); font-family: var(--font-display); }
.char-count { font-size: .6875rem; color: var(--text-3); font-family: var(--font-mono); }
.rewrite-frozen-tag {
  display: inline-flex; align-items: center; height: 20px; padding: 0 7px;
  border: 1px solid color-mix(in srgb, #f59e0b 38%, var(--border)); border-radius: 999px;
  background: color-mix(in srgb, #f59e0b 10%, var(--surface-raised));
  color: #d97706; font-size: .625rem; font-weight: 700;
}
.rewrite-frozen-notice {
  flex-shrink: 0; padding: 8px 14px;
  border-bottom: 1px solid color-mix(in srgb, #f59e0b 22%, var(--border));
  background: color-mix(in srgb, #f59e0b 7%, var(--bg-input));
  color: var(--text-2); font-size: .6875rem; line-height: 1.6;
}
/* 剧本未保存标记 */
.script-dirty-dot { display: inline-flex; align-items: center; gap: 5px; font-size: .6875rem; color: var(--status-queued); }
.script-dirty-dot i { width: 6px; height: 6px; border-radius: 50%; background: var(--status-queued); }

/* Editor Area */
.step-editor { flex: 1; display: flex; flex-direction: column; min-height: 0; }
.fill-textarea {
  flex: 1; border: none; border-radius: 0; padding: 26px 28px;
  font-size: .84375rem; line-height: 1.9; resize: none; outline: none;
  font-family: var(--font-body); background: var(--bg-input); color: var(--text-0);
}
.fill-textarea:focus { box-shadow: none; }
/* AI 改写步骤：行号编辑框占满剩余高度，Agent 修改内嵌框贴底部；diff 预览沿用编辑器底色 */
.step-script-body { flex: 1; display: flex; flex-direction: column; min-height: 0; }
.step-script-agent { padding: 0 24px 14px; }
.step-script-body .prompt-diff-view { background: var(--bg-input); }
/* AI 改写右侧栏：本集要求 + 文档状态（参考逐集改编工作台 adapt-context-pane） */
.step-script-work { flex: 1; display: flex; min-height: 0; position: relative; }
.step-script-main { flex: 1; min-width: 0; display: flex; flex-direction: column; }
.script-ctx {
  width: 296px; flex-shrink: 0; min-width: 0;
  display: flex; flex-direction: column; min-height: 0;
  background: var(--surface-raised); border-left: 1px solid var(--border);
}
.script-ctx-head {
  flex-shrink: 0; display: flex; align-items: center; justify-content: space-between; gap: 8px;
  padding: 10px 14px; border-bottom: 1px solid var(--border);
}
.script-ctx-head small { display: block; font-size: .625rem; color: var(--text-3); }
.script-ctx-head strong { font-size: .75rem; color: var(--text-0); }
.script-ctx-scroll { flex: 1; min-height: 0; overflow-y: auto; }
.script-ctx-section { display: flex; flex-direction: column; gap: 10px; padding: 12px 14px; border-bottom: 1px solid var(--border); }
.script-ctx-section header span { font-size: .6875rem; font-weight: 700; color: var(--text-1); }
.script-ctx-section label { display: flex; flex-direction: column; gap: 5px; font-size: .71875rem; color: var(--text-2); }
.script-ctx-section label span { display: flex; gap: 6px; }
.script-ctx-section label small { color: var(--text-3); font-size: .625rem; }
.script-ctx-section input, .script-ctx-section textarea {
  width: 100%; border: 1px solid var(--border); border-radius: 8px; outline: 0;
  background: var(--bg-input); color: var(--text-0); font: inherit; font-size: .75rem;
}
.script-ctx-section input { height: 32px; padding: 0 34px 0 9px; }
.script-ctx-section textarea { min-height: 96px; padding: 8px 9px; resize: vertical; line-height: 1.7; }
.script-ctx-section input:focus, .script-ctx-section textarea:focus { border-color: var(--accent); box-shadow: 0 0 0 3px var(--button-focus); }
.script-ctx-row { display: flex; align-items: center; justify-content: space-between; gap: 8px; font-size: .71875rem; color: var(--text-2); }
.script-ctx-row strong { font-size: .6875rem; color: var(--text-0); text-align: right; }
.script-ctx-row em { font-style: normal; margin-left: 6px; color: var(--status-queued); font-size: .625rem; }
.script-ctx-actions { flex-shrink: 0; padding: 12px 14px; border-top: 1px solid var(--border); }
.script-ctx-btn { width: 100%; justify-content: center; }
.script-ctx .script-ctx-close { display: none; }
.ctx-toggle { display: none; }
@media (max-width: 1080px) {
  .ctx-toggle { display: inline-flex; }
  .script-ctx {
    position: absolute; top: 0; right: 0; bottom: 0; z-index: 30;
    transform: translateX(102%); transition: transform 0.18s var(--ease-out);
    box-shadow: var(--shadow-xl);
  }
  .script-ctx.open { transform: translateX(0); }
  .script-ctx .script-ctx-close { display: inline-flex; }
}

/* Step Empty State */
.step-empty {
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  flex: 1; min-height: 300px; gap: 10px; padding: 46px;
  animation: fadeIn 0.3s var(--ease-out);
}
.empty-visual {
  width: 72px; height: 72px; border-radius: 22px;
  background: var(--bg-1); color: var(--accent);
  border: 1px solid var(--border);
  box-shadow: var(--shadow-sm);
  display: flex; align-items: center; justify-content: center;
  margin-bottom: 8px;
}
.empty-title { font-size: 1.375rem; font-weight: 700; font-family: var(--font-display); color: var(--text-0); }
.empty-desc { font-size: .8125rem; color: var(--text-2); max-width: 420px; text-align: center; line-height: 1.8; }
.step-empty-actions { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; justify-content: center; }

/* Step Loading */
.step-loading {
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  flex: 1; gap: 12px;
}
.loading-text { font-size: .8125rem; color: var(--text-2); }

.storyboard-ref-list {
  min-height: 0;
  overflow-y: auto;
  padding: 10px;
  display: flex;
  flex-direction: column;
  gap: 5px;
}
/* 嵌入中栏时：不自带内边距；高度封顶内部滚动，避免把下方提示词顶出首屏 */
.storyboard-ref-list.is-embedded { overflow: visible; padding: 0; }
.video-main-grid .storyboard-ref-list.is-embedded { max-height: 300px; overflow-y: auto; }
/* 视频列表选择模式快捷操作：复用分段芯片样式，去掉顶部虚线分隔 */
.video-quick-actions { margin: 0 12px 10px; padding-top: 0; border-top: none; }
.storyboard-ref-group {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 5px;
}
.storyboard-ref-group + .storyboard-ref-group {
  margin-top: 4px;
}
.storyboard-ref-group-label {
  grid-column: 1 / -1;
  font-size: .6875rem;
  font-weight: 600;
  color: var(--text-3);
  letter-spacing: 0.02em;
  padding: 0 2px;
}
.storyboard-ref-goto {
  flex-shrink: 0;
  align-self: center;
  border: none;
  background: transparent;
  padding: 0 2px;
  border-radius: var(--radius-sm, 6px);
  font-size: .65625rem;
  font-weight: 600;
  color: var(--accent);
  cursor: pointer;
}
.storyboard-ref-goto:hover {
  background: var(--accent-bg);
}
.storyboard-ref-costume-select {
  align-self: start;
  width: 100%;
  margin-top: 4px;
  padding: 3px 6px;
  font-size: .6875rem;
  color: var(--text-2);
  background: var(--bg-2, var(--bg-1));
  border: 1px solid var(--border);
  border-radius: var(--radius-sm, 6px);
  cursor: pointer;
}
/* ─── 服装/造型状态 UI ─── */
.character-costume-strip {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  align-items: center;
  margin-top: 6px;
  cursor: pointer;
}
.costume-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 1px 7px;
  border-radius: 999px;
  border: 1px solid var(--border);
  background: var(--bg-2, var(--bg-1));
  font-size: .65625rem;
  font-weight: 600;
  color: var(--text-3);
  max-width: 120px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.costume-chip.is-ready {
  color: var(--text-2);
  border-color: var(--accent-border, var(--border));
}
.costume-chip.is-base {
  color: var(--text-3);
  font-weight: 500;
}
.asset-costume-panel {
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px dashed var(--border);
}
.asset-costume-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.asset-costume-hint {
  margin: 4px 0 8px;
  font-size: .6875rem;
  color: var(--text-3);
  line-height: 1.5;
}
.asset-costume-row {
  display: grid;
  grid-template-columns: 64px minmax(0, 1fr) auto;
  gap: 8px;
  align-items: start;
  padding: 8px 0;
  border-top: 1px solid var(--border);
}
.asset-costume-row.is-creating {
  grid-template-columns: minmax(0, 1fr) auto;
}
.asset-costume-thumb {
  width: 64px;
  height: 64px;
  border-radius: var(--radius-sm, 6px);
  border: 1px solid var(--border);
  overflow: hidden;
  background: var(--bg-2, var(--bg-1));
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
}
.asset-costume-thumb img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}
.asset-costume-thumb-empty {
  font-size: 1rem;
  color: var(--text-3);
}
.asset-costume-main {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}
.asset-costume-actions {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.storyboard-ref-item {
  display: grid;
  grid-template-columns: 30px minmax(0, 1fr);
  gap: 8px;
  align-items: center;
  padding: 5px 7px;
  border-radius: var(--radius);
  border: 1px solid var(--border);
  background: var(--surface-raised);
  cursor: pointer;
  transition: border-color 0.15s var(--ease-out), opacity 0.15s var(--ease-out);
}
.storyboard-ref-item:hover { border-color: var(--accent); }
.storyboard-ref-item:not(.bound) {
  background: transparent;
  border-color: var(--border);
  opacity: 0.72;
}
.storyboard-ref-item:not(.bound):hover { opacity: 1; border-color: var(--accent); }
.storyboard-ref-item:not(.bound) .storyboard-ref-main .storyboard-ref-name { color: var(--text-2); }
.storyboard-ref-item.bound {
  border-color: var(--accent);
  background: var(--accent-bg);
}
.storyboard-ref-item.bound:hover { border-color: var(--accent); }
.storyboard-ref-thumb {
  width: 30px;
  aspect-ratio: 1;
  border-radius: var(--radius-sm);
  border: 1px solid var(--surface-outline);
  overflow: hidden;
  background: var(--bg-2);
  color: var(--text-3);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: .625rem;
  font-weight: 800;
}
.storyboard-ref-thumb:disabled { cursor: default; }
.storyboard-ref-thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
.storyboard-ref-main { min-width: 0; display: flex; align-items: baseline; flex-wrap: wrap; gap: 2px 6px; }
.storyboard-ref-name {
  flex: none;
  max-width: 42%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: .75rem;
  font-weight: 700;
  color: var(--text-0);
}
.storyboard-ref-state {
  flex-shrink: 0;
  margin-left: auto;
  font-size: .625rem;
  color: var(--text-3);
}
.storyboard-ref-state.is-ready { color: var(--success); }
.storyboard-ref-meta {
  flex: 1;
  min-width: 0;
  font-size: .65625rem;
  color: var(--text-3);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.storyboard-ref-empty {
  padding: 14px 10px;
  color: var(--text-3);
  font-size: .75rem;
  line-height: 1.5;
  border: 1px dashed var(--surface-outline);
  border-radius: var(--radius);
}
.detail-panel { flex: 1; display: flex; flex-direction: column; overflow-y: auto; min-width: 0; }
.detail-head { display: flex; align-items: center; gap: 8px; padding: 9px 14px; border-bottom: 1px solid var(--border); flex-shrink: 0; }
.detail-head-copy { display: flex; flex-direction: column; gap: 2px; }
.detail-head-title { font-size: .875rem; font-weight: 700; color: var(--text-0); }
.detail-body { padding: 14px 16px; display: flex; flex-direction: column; gap: 12px; }
.detail-hero {
  display: grid;
  grid-template-columns: minmax(0, 1.2fr) minmax(220px, 0.9fr);
  gap: 12px;
  padding: 12px;
  border-radius: var(--radius-lg);
  background: var(--surface-raised);
  border: 1px solid var(--border);
}
.detail-hero-copy { display: flex; flex-direction: column; gap: 8px; min-width: 0; }
.detail-hero-label {
  font-size: .625rem; font-weight: 700; letter-spacing: 0.12em;
  text-transform: uppercase; color: var(--text-3);
}
.detail-hero-text { font-size: .8125rem; color: var(--text-1); line-height: 1.7; }
.detail-status-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.detail-section {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 12px 14px;
  border-radius: var(--radius-lg);
  background: var(--surface-raised);
  border: 1px solid var(--border);
}
.detail-section-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  flex-wrap: wrap;
}
.detail-section-title { font-size: .75rem; font-weight: 700; color: var(--text-0); }
.detail-section-copy { font-size: .6875rem; color: var(--text-3); }

/* Field */
.field { display: flex; flex-direction: column; gap: 5px; }
.field-label { font-size: .75rem; font-weight: 500; color: var(--text-1); }
.field-row { display: flex; gap: 12px; }
.field-grid { display: grid; gap: 12px; }
.field-grid-2 { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.field-grid-4 { grid-template-columns: repeat(4, minmax(0, 1fr)); }
.locked-config {
  display: inline-flex;
  align-items: center;
  height: 30px;
  padding: 0 12px;
  border-radius: 999px;
  background: var(--surface-muted);
  border: 1px solid var(--surface-outline);
  color: var(--text-1);
  font-size: .6875rem;
  font-weight: 600;
}
.locked-config-banner {
  margin-bottom: 8px;
  font-size: .75rem;
  color: var(--text-2);
}

/* Production tabs */
.prod-tabs { display: flex; gap: 4px; background: var(--bg-2); border-radius: var(--radius); padding: 2px; }
.prod-tab {
  display: flex; align-items: center; gap: 4px; min-height: 26px; padding: 0 10px; font-size: .6875rem;
  border: 1px solid transparent; background: transparent; color: var(--text-2); cursor: pointer;
  border-radius: calc(var(--radius) - 2px); transition: all 0.18s var(--ease-out); font-weight: 650;
  line-height: 1;
}
.prod-tab:hover { color: var(--text-0); background: var(--button-bg); border-color: var(--button-border); }
.prod-tab.active { background: var(--accent-bg); color: var(--accent-text); font-weight: 650; border-color: var(--accent-glow); box-shadow: none; }
.prod-tab:focus-visible {
  outline: none;
  border-color: var(--action-primary);
  box-shadow: 0 0 0 3px var(--button-focus), var(--button-shadow);
}
.prod-tab-badge { font-size: .625rem; font-family: var(--font-mono); padding: 0 4px; background: var(--bg-3); border-radius: 99px; }
.prod-tab.active .prod-tab-badge { background: var(--accent-bg); color: var(--accent-text); }

/* Production content */
.prod-content { flex: 1; overflow-y: auto; padding: 10px 12px 12px; display: flex; flex-direction: column; gap: 10px; }
.prod-section-bar { display: flex; align-items: center; gap: 7px; flex-wrap: wrap; }
.video-sync-warning {
  padding: 8px 10px;
  border: 1px solid var(--warning-bg);
  border-radius: var(--radius);
  background: var(--warning-bg);
  color: var(--warning);
  font-size: .6875rem;
  line-height: 1.5;
}

/* 资产栏动作：提取（虚线中性）与批量生成（强调色）视觉分组 */
.asset-bar-actions { align-items: center; }
.asset-bar-divider { width: 1px; height: 16px; margin: 0 4px; background: var(--surface-outline-strong); }
.asset-btn-extract {
  background: transparent;
  color: var(--text-2);
  box-shadow: none;
  border: 1px dashed var(--surface-outline-strong);
}
.asset-btn-extract:hover { background: var(--surface-muted); color: var(--text-1); }
.asset-btn-batch {
  background: var(--accent-bg);
  color: var(--accent-text);
  box-shadow: none;
}
.asset-btn-batch:hover { background: var(--accent); color: var(--on-accent); }

/* 资产分区标题：新增入口 + 卡片删除按钮 */
.asset-section-title { display: flex; align-items: center; gap: 8px; }
.style-prefix-row { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; margin: 4px 0 10px; }
.style-prefix-label { font-size: .75rem; font-weight: 600; color: var(--text-2); white-space: nowrap; }
.style-prefix-input { flex: 0 1 420px; min-width: 180px; font-size: .75rem; }
.style-prefix-preset-select { flex: 0 0 180px; width: 180px; }
.style-prefix-hint { flex: 1 1 300px; font-size: .6875rem; color: var(--text-3); }
.asset-add-btn {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  padding: 2px 8px;
  border-radius: 999px;
  border: 1px dashed var(--surface-outline-strong);
  background: transparent;
  color: var(--text-3);
  font-size: .6875rem;
  font-weight: 600;
  cursor: pointer;
  transition: color 0.15s, border-color 0.15s;
}
.asset-add-btn:hover { color: var(--accent-text); border-color: var(--accent-text); }
.character-asset-card, .asset-click-card { position: relative; }
.asset-del-btn {
  position: absolute;
  top: 8px;
  right: 8px;
  z-index: 3;
  width: 22px;
  height: 22px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  border: none;
  background: rgba(0, 0, 0, 0.5);
  color: #fff;
  cursor: pointer;
  opacity: 0;
  transition: opacity 0.15s, background 0.15s;
}
.asset-del-btn:hover { background: var(--action-danger); }
.character-asset-card:hover .asset-del-btn,
.asset-click-card:hover .asset-del-btn { opacity: 1; }

/* 分镜勾选：选择后批量生成视频提示词 */
.shot-check {
  flex: none;
  width: 14px;
  height: 14px;
  border-radius: 4px;
  border: 1.5px solid var(--surface-outline-strong);
  background: transparent;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: var(--on-accent);
  cursor: pointer;
  transition: background 0.15s, border-color 0.15s;
}
.shot-check:hover { border-color: var(--accent); }
.shot-check.on { background: var(--accent); border-color: var(--accent); }
.shot-quick-btn {
  border: none;
  background: transparent;
  color: var(--accent-text);
  font-size: .6875rem;
  font-weight: 600;
  cursor: pointer;
  padding: 0;
  white-space: nowrap;
}
.shot-quick-btn:hover { text-decoration: underline; }
/* 多选模式：头部快捷操作独立一行，分段芯片样式 */
.shot-quick-actions {
  display: flex;
  gap: 6px;
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px dashed var(--surface-outline);
}
.shot-quick-actions .shot-quick-btn {
  flex: 1;
  padding: 5px 0;
  border-radius: 6px;
  background: var(--bg-2);
  color: var(--text-2);
  text-align: center;
  transition: background 0.15s, color 0.15s;
}
.shot-quick-actions .shot-quick-btn:hover {
  background: var(--accent-bg);
  color: var(--accent-text);
  text-decoration: none;
}

/* 新增资产弹窗 */
.asset-create-dialog { width: 440px; max-width: calc(100vw - 48px); }
.asset-create-body { display: flex; flex-direction: column; gap: 10px; }

/* 导入项目资产弹窗 */
.asset-import-dialog { width: 520px; max-width: calc(100vw - 48px); }
.asset-import-body { display: flex; flex-direction: column; gap: 14px; max-height: 56vh; overflow-y: auto; }
.asset-import-loading { display: flex; justify-content: center; padding: 24px 0; }
.asset-import-empty { padding: 20px 0; text-align: center; color: var(--text-3); font-size: .78125rem; }
.asset-import-section { display: flex; flex-direction: column; gap: 2px; }
.asset-import-section-title {
  display: flex; align-items: center; gap: 6px;
  margin: 0 0 4px; font-size: .75rem; font-weight: 700; color: var(--text-1);
}
.asset-import-count {
  font-size: .65625rem; font-weight: 600; color: var(--text-3);
  background: var(--bg-2); border-radius: 99px; padding: 1px 7px;
}
.asset-import-row {
  display: flex; align-items: center; gap: 8px;
  padding: 6px 8px; border-radius: var(--radius);
  font-size: .78125rem; color: var(--text-0); cursor: pointer;
  transition: background 0.14s;
}
.asset-import-row:hover { background: var(--bg-hover); }
.asset-import-row input[type="checkbox"] { flex-shrink: 0; accent-color: var(--accent); }
.asset-import-thumb {
  width: 44px; height: 30px; flex-shrink: 0;
  border: 1px solid var(--border); border-radius: 6px;
  background: var(--bg-2); overflow: hidden;
}
.asset-import-thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
.asset-import-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.asset-import-sub { font-size: .6875rem; flex-shrink: 0; margin-left: auto; max-width: 40%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

/* 自选资产导出弹窗 */
.asset-export-dialog { width: 680px; max-width: calc(100vw - 48px); }
.asset-export-hint { margin: 5px 0 0; color: var(--text-3); font-size: .71875rem; font-weight: 400; }
.asset-export-tools {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  padding: 8px 16px; border-bottom: 1px solid var(--border);
  color: var(--text-2); font-size: .75rem;
}
.asset-export-tool-actions { display: flex; align-items: center; gap: 4px; }
.asset-export-footer { align-items: center; }
.asset-export-submit-actions { display: flex; align-items: center; gap: 8px; }
.asset-export-body { display: flex; flex-direction: column; gap: 16px; max-height: 58vh; overflow-y: auto; }
.asset-export-section { display: flex; flex-direction: column; gap: 7px; }
.asset-export-section-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.asset-export-section-title {
  display: flex; align-items: center; gap: 7px;
  margin: 0; color: var(--text-1); font-size: .75rem; font-weight: 700;
}
.asset-export-toggle {
  padding: 2px 0; border: 0; background: transparent;
  color: var(--accent-text); font-size: .71875rem; cursor: pointer;
}
.asset-export-toggle:hover { text-decoration: underline; }
.asset-export-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 6px; }
.asset-export-card {
  display: flex; align-items: center; gap: 9px; min-width: 0;
  padding: 7px 8px; border: 1px solid var(--border); border-radius: var(--radius);
  background: var(--surface-raised); cursor: pointer; transition: border-color .14s, background .14s;
}
.asset-export-card:hover { border-color: var(--surface-outline-strong); background: var(--surface-muted); }
.asset-export-card.selected { border-color: var(--accent-glow); background: var(--accent-bg); }
.asset-export-card input[type="checkbox"] { flex-shrink: 0; accent-color: var(--accent); }
.asset-export-thumb {
  width: 42px; height: 32px; flex-shrink: 0; overflow: hidden;
  border: 1px solid var(--border); border-radius: 6px; background: var(--bg-2);
}
.asset-export-thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
.asset-export-card-copy { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.asset-export-card-copy strong,
.asset-export-card-copy span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.asset-export-card-copy strong { color: var(--text-0); font-size: .75rem; font-weight: 650; }
.asset-export-card-copy span { color: var(--text-3); font-size: .65625rem; }
@media (max-width: 620px) {
  .asset-export-grid { grid-template-columns: 1fr; }
  .asset-export-footer { align-items: stretch; flex-direction: column-reverse; }
  .asset-export-submit-actions { display: grid; grid-template-columns: 1fr; }
}

/* Asset grid */
.asset-section-title {
  margin-top: 2px;
  font-size: .75rem;
  font-weight: 800;
  color: var(--text-1);
  letter-spacing: 0.04em;
}
.prop-name-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.prop-name-row .asset-name {
  min-width: 0;
}
.asset-props-empty {
  padding: 14px;
  border: 1px dashed var(--surface-outline);
  border-radius: var(--radius);
  color: var(--text-3);
  font-size: .75rem;
  text-align: center;
}
.asset-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(190px, 1fr)); gap: 12px; align-items: stretch; }
.character-asset-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(230px, 260px));
  justify-content: start;
  gap: 10px;
}
.asset-card {
  display: flex; flex-direction: column; overflow: hidden;
  transition: transform 0.18s var(--ease-out), box-shadow 0.18s var(--ease-out), border-color 0.18s var(--ease-out);
}
.asset-card:hover { transform: translateY(-2px); box-shadow: var(--shadow-lift); }
.asset-click-card,
.character-asset-card {
  cursor: pointer;
}
.asset-click-card:focus-visible,
.character-asset-card:focus-visible {
  outline: none;
  border-color: var(--accent-glow);
  box-shadow: 0 0 0 3px var(--button-focus), var(--shadow-panel);
}

/* ===== 资产图片拖拽上传 ===== */
.is-asset-drop-active { position: relative; }
.asset-drop-overlay {
  position: absolute; inset: 0; z-index: 3;
  display: flex; align-items: center; justify-content: center;
  padding: 0 8px; text-align: center;
  font-size: .75rem; font-weight: 600; line-height: 1.4;
  color: var(--accent-text);
  background: var(--accent-bg);
  backdrop-filter: blur(2px);
  border: 2px dashed var(--accent);
  border-radius: inherit;
  pointer-events: none;
}
.asset-drop-overlay--compact { font-size: .625rem; border-width: 1.5px; }
.character-asset-card {
  display: flex;
  flex-direction: column;
  overflow: hidden;
  min-height: 0;
  transition: transform 0.18s var(--ease-out), box-shadow 0.18s var(--ease-out), border-color 0.18s var(--ease-out);
}
.character-asset-card:hover {
  transform: translateY(-2px);
  box-shadow: var(--shadow-lift);
  border-color: var(--border-strong);
}
.character-portrait {
  position: relative;
  width: 100%;
  aspect-ratio: 16 / 9;
  align-self: start;
  margin: 0;
  border: 1px solid var(--surface-outline);
  border-radius: var(--radius);
  background: var(--bg-2);
  overflow: hidden;
}
.character-portrait img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}
.character-portrait-empty {
  width: 100%;
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--text-3);
}
.character-asset-main {
  min-width: 0;
  width: 100%;
  padding: 8px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.character-asset-overview {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.character-asset-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  min-width: 0;
}
.character-title-block {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.character-name-row {
  display: flex;
  align-items: center;
  gap: 5px;
  min-width: 0;
  flex-wrap: wrap;
}
.character-name {
  font-size: .8125rem;
  line-height: 1.25;
  color: var(--text-0);
}
.character-audio-bound {
  display: inline-flex;
  align-items: center;
  height: 17px;
  padding: 0 5px;
  border-radius: 4px;
  background: color-mix(in srgb, var(--accent) 14%, transparent);
  color: var(--accent);
  font-size: .5625rem;
  line-height: 1;
}
.character-audio-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
  min-height: 30px;
  padding: 5px 6px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: color-mix(in srgb, var(--panel) 72%, var(--bg));
  min-width: 0;
}
.character-audio-label,
.character-audio-controls {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}
.character-audio-label {
  flex-shrink: 0;
  color: var(--text-2);
  font-size: .625rem;
  font-weight: 600;
}
.character-audio-icon {
  color: var(--accent);
  font-size: .875rem;
  line-height: 1;
}
.character-audio-unbound {
  color: var(--text-3);
  font-weight: 400;
}
.character-audio-controls { flex: 1; justify-content: flex-end; }
.character-audio-play { flex-shrink: 0; }
.character-audio-player {
  display: block;
  flex: 1;
  min-width: 0;
  width: 170px;
  height: 26px;
}
.character-audio-clear {
  flex-shrink: 0;
  white-space: nowrap;
}
.character-gen-btn { flex-shrink: 0; align-self: center; }
.studio-model-picks {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-right: 12px;
  padding-right: 12px;
  border-right: 1px solid var(--border);
}
.asset-final-prompt {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding-top: 7px;
  border-top: 1px solid var(--border);
  font-size: .65625rem;
  line-height: 1.5;
}
.afp-label {
  font-size: .59375rem;
  font-weight: 700;
  letter-spacing: 0.05em;
  color: var(--text-3);
}
.afp-text {
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  overflow: hidden;
  word-break: break-word;
  color: var(--text-2);
}
.afp-text.dim { color: var(--text-3); }
.asset-final {
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  overflow: hidden;
  word-break: break-word;
  color: var(--text-2);
}
.asset-final .afp-label { margin-right: 4px; }
.afp-stale {
  display: inline-flex;
  align-items: center;
  margin: 0 4px;
  padding: 0 5px;
  border-radius: 999px;
  background: var(--warning-bg);
  color: var(--warning);
  font-size: .5625rem;
  font-weight: 700;
  white-space: nowrap;
}
.character-visual-summary {
  max-width: 100%;
  display: flex;
  gap: 8px;
  overflow: hidden;
  color: var(--text-3);
  font-size: .65625rem;
  line-height: 1.45;
  white-space: nowrap;
}
.character-visual-summary span {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
}
.asset-cover { position: relative; aspect-ratio: 1; background: var(--bg-2); overflow: hidden; }
.asset-cover.wide { aspect-ratio: 16/9; }
.asset-cover img { width: 100%; height: 100%; object-fit: cover; }
.previewable-image { cursor: zoom-in; transition: transform 0.18s var(--ease-out), filter 0.18s var(--ease-out); }
.previewable-image:hover { transform: scale(1.015); filter: saturate(1.04); }
.asset-cover-badge {
  position: absolute;
  top: 7px;
  left: 7px;
  display: inline-flex;
  align-items: center;
  padding: 2px 7px;
  border-radius: 999px;
  background: rgba(255,255,255,0.85);
  backdrop-filter: blur(10px);
  -webkit-backdrop-filter: blur(10px);
  box-shadow: 0 1px 4px rgba(0,0,0,0.08);
  color: var(--text-2);
  font-size: .59375rem;
  font-weight: 700;
}
.asset-cover-badge.is-ready {
  background: var(--success-bg);
  color: var(--tag-success-text);
}
.asset-cover-badge.is-pending {
  background: var(--accent-bg);
  color: var(--accent-text);
}
.asset-cover-badge.is-failed,
.asset-detail-state.is-failed {
  background: var(--error-bg);
  color: var(--error);
}
.asset-cover-empty { width: 100%; height: 100%; display: flex; align-items: center; justify-content: center; color: var(--text-3); }
.asset-body {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 9px 11px 8px;
  min-width: 0;
}
.asset-name {
  font-size: .8125rem;
  font-weight: 650;
  color: var(--text-0);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.asset-meta { font-size: .6875rem; line-height: 1.5; }
.asset-desc {
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  overflow: hidden;
  word-break: break-word;
}
.asset-light {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.asset-foot { display: flex; align-items: center; gap: 4px; padding: 7px 11px; border-top: 1px solid var(--border); }

/* Frame grid */
.frame-grid { display: flex; flex-direction: column; gap: 8px; }
.frame-row {
  display: flex; align-items: center; gap: 14px;
  padding: 12px 14px; cursor: pointer;
  border-radius: var(--radius-lg);
  transition: all 0.15s;
  border: 1.5px solid transparent;
}
.frame-row:hover { background: var(--bg-0); border-color: var(--border); }
.frame-row.active {
  background: var(--bg-0);
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-glow);
}
.frame-info { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 6px; }
.frame-top { display: flex; align-items: center; gap: 8px; }
.frame-num {
  font-size: .8125rem; font-family: var(--font-mono); font-weight: 800;
  color: var(--accent);
}
.frame-badge {
  font-size: .6875rem; font-weight: 600; padding: 2px 8px;
  border-radius: 20px;
  background: var(--accent-bg); color: var(--accent);
  border: 1px solid var(--accent-glow);
  white-space: nowrap;
}
.frame-desc {
  font-size: .75rem; line-height: 1.5; color: var(--text-1);
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
  overflow: hidden;
}
.frame-meta { display: flex; align-items: center; gap: 6px; }
.frame-thumbs { display: flex; gap: 8px; flex-shrink: 0; }
.frame-thumb-wrap { display: flex; flex-direction: column; gap: 3px; align-items: center; }
.frame-thumb-label { font-size: .625rem; font-weight: 600; color: var(--text-3); }
.frame-thumb {
  position: relative; width: 130px; aspect-ratio: 16/9;
  border-radius: 6px; overflow: hidden;
  background: var(--bg-2); cursor: pointer;
  transition: all 0.15s; border: 1.5px solid var(--border);
}
.frame-thumb:hover { border-color: var(--accent); box-shadow: 0 2px 8px rgba(0,0,0,0.2); }
.frame-thumb img { width: 100%; height: 100%; object-fit: cover; }
.frame-thumb-empty { width: 100%; height: 100%; display: flex; align-items: center; justify-content: center; color: var(--text-3); }
.frame-re {
  position: absolute; top: 3px; right: 3px; width: 18px; height: 18px;
  border-radius: 50%; background: rgba(0,0,0,0.5); color: #fff;
  display: none; align-items: center; justify-content: center;
}
.frame-thumb:hover .frame-re { display: flex; }
.frame-scroll { flex: 1; overflow-y: auto; padding: 10px 12px; }
.dot { width: 7px; height: 7px; border-radius: 50%; background: var(--bg-3); flex-shrink: 0; }
.dot.ok { background: var(--success); }
.dot.pending {
  background: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-glow);
}
.dot.is-done { background: var(--status-complete); }
.dot.is-processing { border: 1.5px solid var(--status-processing); border-top-color: transparent; background: transparent; }
.dot.is-queued { border: 1px solid var(--status-queued); border-radius: 2px; background: transparent; box-shadow: inset 0 -2px var(--status-queued); }
.dot.is-canceling { border: 1px dotted var(--status-canceling); background: transparent; }
.dot.is-failed, .dot.is-blocked { border-radius: 1px; background: var(--error); transform: rotate(45deg); }
.dot.is-canceled, .dot.is-ready { background: var(--status-offline); }
@media (prefers-reduced-motion: no-preference) { .dot.is-processing { animation: task-status-spin .8s linear infinite; } }
@keyframes task-status-spin { to { transform: rotate(360deg); } }

/* Video tasks */
.video-task-workbench {
  position: relative;
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(460px, 55%);
  overflow: hidden;
  border: 1px solid var(--surface-outline);
  border-radius: var(--radius-lg);
  background: var(--surface-raised);
}
.video-task-workbench.has-player {
  grid-template-columns: var(--vleft, 236px) minmax(0, 1fr);
}
.video-task-side {
  min-width: 0;
  min-height: 0;
  display: grid;
  grid-template-columns: minmax(0, 1fr) var(--vright, 340px);
  border-left: 1px solid var(--border);
  background: var(--surface-muted);
}
/* 三栏拖拽分隔条：透明热区覆盖分界，悬停/拖动时亮起 */
.video-col-divider {
  position: absolute;
  top: 0;
  bottom: 0;
  width: 9px;
  z-index: 6;
  cursor: col-resize;
  touch-action: none;
}
.video-col-divider.is-left { left: calc(var(--vleft, 236px) - 4px); }
.video-col-divider.is-right { right: calc(var(--vright, 340px) - 4px); }
.video-col-divider::after {
  content: '';
  position: absolute;
  top: 0;
  bottom: 0;
  left: 4px;
  width: 1px;
  background: transparent;
  transition: background 0.15s, box-shadow 0.15s;
}
.video-col-divider:hover::after,
.video-col-divider:active::after {
  background: var(--accent);
  box-shadow: 0 0 6px var(--accent-glow);
}
:global(body.is-video-col-dragging) { cursor: col-resize; user-select: none; }
/* 中列：纯编辑区（分镜描述/氛围/视频提示词），占满高度 */
.video-main-col {
  min-width: 0;
  min-height: 0;
  display: flex;
}
.video-main-scroll {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 16px;
  padding: 14px 16px 16px;
  background: var(--surface-raised);
}
/* 生成前检查动线集中一屏：上双栏（画面描述/氛围 ｜ 参考绑定），下整宽（视频提示词） */
.video-main-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.05fr) minmax(0, 1fr);
  gap: 16px;
  align-items: start;
}
.video-player-history {
  min-height: 0;
  padding: 8px 12px 10px;
  border-bottom: 1px solid var(--border);
  background: var(--surface-raised);
}
.video-player-history-head {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 7px;
  font-size: .6875rem;
  font-weight: 700;
  color: var(--text-2);
}
.video-player-history-count {
  padding: 0 6px;
  border-radius: 999px;
  background: var(--overlay-track);
  color: var(--text-3);
  font-size: .625rem;
  font-weight: 750;
}
.video-player-history-hint {
  margin-left: auto;
  color: var(--text-3);
  font-size: .625rem;
  font-weight: 500;
}
.video-player-history-list {
  display: flex;
  gap: 8px;
  overflow-x: auto;
  padding-bottom: 2px;
}
.video-history-item {
  position: relative;
  flex: 0 0 auto;
  width: 96px;
  aspect-ratio: 16 / 9;
  overflow: hidden;
  border: 1.5px solid var(--surface-outline);
  border-radius: var(--radius);
  background: var(--media-surface);
  cursor: pointer;
  transition: border-color 0.16s var(--ease-out), box-shadow 0.16s var(--ease-out);
}
.video-history-item:hover { border-color: var(--border-strong); }
.video-history-item video {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}
.video-history-item.current {
  border-color: var(--accent);
}
.video-history-item.viewing {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-glow);
}
.video-history-time {
  position: absolute;
  left: 4px;
  bottom: 4px;
  padding: 1px 4px;
  border-radius: 3px;
  background: rgba(0,0,0,0.6);
  color: #fff;
  font-family: var(--font-mono);
  font-size: .5625rem;
}
.video-history-seed {
  position: absolute;
  right: 4px;
  bottom: 4px;
  max-width: calc(100% - 96px);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  padding: 1px 4px;
  border: 0;
  border-radius: 3px;
  background: rgba(0,0,0,0.6);
  color: #cfe3ff;
  font-family: var(--font-mono);
  font-size: .5625rem;
  cursor: pointer;
}
.video-history-seed:hover { background: var(--accent); color: var(--on-accent); }
.video-history-seed.same { color: #9aa7b8; }
.video-history-badge {
  position: absolute;
  right: 4px;
  top: 4px;
  padding: 1px 5px;
  border-radius: 999px;
  background: var(--accent);
  color: var(--on-accent);
  font-size: .5625rem;
  font-weight: 700;
}
.video-history-del {
  position: absolute;
  right: 3px;
  top: 3px;
  width: 16px;
  height: 16px;
  display: none;
  align-items: center;
  justify-content: center;
  border: 0;
  border-radius: 50%;
  background: rgba(0,0,0,0.62);
  color: #fff;
  font-size: .6875rem;
  line-height: 1;
  cursor: pointer;
}
.video-history-item:hover .video-history-del { display: flex; }
.video-task-player {
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
  border-bottom: 1px solid var(--border);
  background: var(--surface-raised);
}
.video-player-head {
  min-height: 44px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 6px 12px;
  padding: 8px 14px;
  border-bottom: 1px solid var(--surface-outline);
}
.video-player-head-info {
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 8px;
}
.video-player-title { color: var(--text-0); font-size: .8125rem; font-weight: 700; white-space: nowrap; }
.video-player-sub { color: var(--text-3); font-size: .6875rem; white-space: nowrap; }
.video-player-stage {
  position: relative;
  flex: none;
  aspect-ratio: 16 / 9;
  max-height: 220px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--media-surface);
}
.video-player-video {
  width: 100%;
  height: 100%;
  max-width: 100%;
  max-height: 100%;
  object-fit: contain;
  display: block;
}
/* 空态：保留 16:9 播放框，内容居中（图标 + 文案 + 生成按钮） */
.video-player-empty {
  width: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  text-align: center;
  gap: 6px;
  padding: 14px 16px;
  color: var(--text-3);
}
.video-player-empty-copy { display: flex; flex-direction: column; align-items: center; }
.video-player-empty-title { color: var(--text-1); font-size: .78125rem; font-weight: 700; }
.video-player-empty-desc { margin-top: 2px; font-size: .6875rem; line-height: 1.5; }
.video-player-empty-action { flex-shrink: 0; margin-top: 4px; }
.video-task-list {
  min-height: 0;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  border: 0;
  border-radius: 0;
  background: var(--surface-raised);
}
.video-task-head {
  min-height: 48px;
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px 12px;
  padding: 10px 12px;
  border-bottom: 1px solid var(--surface-outline);
}
.video-task-title {
  font-size: .8125rem;
  line-height: 1.2;
  font-weight: 850;
  color: var(--text-0);
}
.video-task-meta {
  margin-top: 3px;
  font-size: .6875rem;
  line-height: 1.35;
  color: var(--text-3);
}
.video-task-metrics {
  margin-left: auto;
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 6px;
  flex-wrap: wrap;
}
.video-task-metric,
.video-task-status {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  min-height: 22px;
  padding: 0 8px;
  border: 1px solid var(--surface-outline);
  border-radius: 999px;
  background: var(--overlay-track);
  color: var(--text-2);
  font-size: .6875rem;
  font-weight: 750;
  white-space: nowrap;
}
.video-task-metric.is-done,
.video-task-status.is-done {
  color: var(--status-complete);
  border-color: var(--status-complete-border);
  background: var(--status-complete-bg);
}
.video-task-metric.is-pending,
.video-task-status.is-processing {
  color: var(--status-processing);
  border-color: var(--status-processing-border);
  background: var(--status-processing-bg);
}
.video-task-status.is-queued {
  color: var(--status-queued);
  border-color: var(--status-queued-border);
  background: var(--status-queued-bg);
}
.video-task-status.is-canceling {
  color: var(--status-canceling);
  border-color: var(--status-canceling-border);
  background: var(--status-canceling-bg);
}
.video-task-status.is-ready {
  color: var(--status-offline);
  border-color: var(--status-offline-border);
  background: var(--status-offline-bg);
}
.video-task-metric.is-failed,
.video-task-status.is-failed,
.video-task-status.is-blocked {
  color: var(--error);
  border-color: rgba(255,125,133,.28);
  background: var(--error-bg);
}
.video-task-table {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
}
/* 统计徽章兼作筛选器 */
button.video-task-metric { cursor: pointer; font-family: inherit; transition: box-shadow 0.15s; }
button.video-task-metric:hover { box-shadow: 0 0 0 2px var(--sel-glow); }
button.video-task-metric.on { box-shadow: 0 0 0 2px var(--accent); }
/* 批量视频：选择模式 */
.btn.is-on { border-color: var(--accent); color: var(--accent-text); background: var(--accent-bg); }
.video-retry-failed { color: var(--warning); border-color: var(--warning-bg); }
.video-task-row.is-selected { background: var(--sel-bg); box-shadow: inset 0 0 0 1.5px var(--sel); }
.video-task-check {
  position: absolute;
  top: 5px;
  right: 5px;
  z-index: 2;
  background: rgba(0,0,0,0.45);
  border-color: rgba(255,255,255,0.55);
}
.video-task-check.on { background: var(--accent); border-color: var(--accent); }
/* 生成前生效配置小结 */
.video-inspector-effective {
  margin: 0;
  padding: 4px 8px;
  border: 1px solid var(--accent-glow);
  border-radius: var(--radius);
  background: var(--accent-bg);
  color: var(--accent-text);
  font-family: var(--font-mono);
  font-size: .65625rem;
  font-weight: 600;
  line-height: 1.4;
  text-align: center;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
/* 生成参数：强调卡片，时长是当前分镜生成的核心参数 */
.video-params-card {
  padding: 7px 10px;
  border: 1px solid var(--accent-glow);
  border-radius: var(--radius-lg);
  background: var(--accent-bg);
}
/* 审核失败引导 */
.video-task-error-hint {
  margin-top: 3px;
  color: var(--accent-text);
  font-weight: 600;
}
/* 批量生成确认弹窗 */
.batch-video-dialog { width: 420px; max-width: calc(100vw - 48px); }
.batch-video-body { display: flex; flex-direction: column; gap: 10px; }
.batch-video-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: .8125rem;
  color: var(--text-2);
  padding: 8px 12px;
  border: 1px solid var(--surface-outline);
  border-radius: var(--radius);
  background: var(--bg-input);
}
.batch-video-row strong { color: var(--text-0); font-weight: 600; }
.batch-video-note { margin: 4px 0 0; font-size: .6875rem; color: var(--text-3); line-height: 1.6; }
.video-task-row {
  display: grid;
  grid-template-columns: 56px minmax(0, 1fr) auto;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  border-top: 1px solid var(--surface-outline);
  transition: background 0.16s var(--ease-out), border-color 0.16s var(--ease-out);
  cursor: pointer;
}
.video-task-row:first-child {
  border-top: 0;
}
.video-task-row:hover,
.video-task-row.is-pending {
  background: var(--bg-hover);
}
.video-task-row.is-failed {
  background: var(--error-bg);
}
.video-task-row.active {
  background: var(--sel-bg);
  box-shadow: inset 0 0 0 1.5px var(--sel), 0 0 0 3px var(--sel-glow);
}
.video-task-row:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
}
.video-task-preview {
  position: relative;
  width: 56px;
  aspect-ratio: 16 / 9;
  overflow: hidden;
  border: 1px solid var(--surface-outline);
  border-radius: var(--radius-sm);
  background: var(--bg-2);
}
.video-task-preview video,
.video-task-preview img {
  width: 100%;
  height: 100%;
  display: block;
  object-fit: cover;
}
.video-task-empty {
  width: 100%;
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--text-3);
}
.video-task-index {
  position: absolute;
  left: 3px;
  top: 3px;
  padding: 0 4px;
  border-radius: 3px;
  background: rgba(0,0,0,0.56);
  color: #fff;
  font-family: var(--font-mono);
  font-size: .5625rem;
  font-weight: 800;
}
.video-task-main {
  min-width: 0;
}
.video-task-line {
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 8px;
}
.video-task-name {
  min-width: 0;
  font-size: .75rem;
  line-height: 1.35;
  color: var(--text-0);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.video-task-meta-line {
  display: flex;
  align-items: center;
  gap: 5px;
  margin-top: 2px;
  font-size: .65625rem;
  color: var(--text-3);
  min-width: 0;
  white-space: nowrap;
  overflow: hidden;
}
.video-task-loc {
  color: var(--text-2);
}
.video-task-sep { color: var(--text-3); opacity: 0.5; }
.video-task-error {
  margin-top: 5px;
  font-size: .6875rem;
  line-height: 1.45;
  color: var(--error);
}
.video-task-error.is-warning { color: var(--warning); }
.video-task-error-hint {
  margin-top: 3px;
  color: var(--accent-text, var(--accent));
  font-weight: 600;
}
.video-task-status {
  justify-self: end;
  align-self: center;
}
/* 行内紧凑状态（窄列表用）：小圆点 + 文字 */
.video-task-state {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  flex-shrink: 0;
  font-size: .6875rem;
  font-weight: 600;
  color: var(--text-3);
  white-space: nowrap;
}
.video-task-state.is-done { color: var(--success); }
.video-task-state.is-processing { color: var(--status-processing); }
.video-task-state.is-queued { color: var(--status-queued); }
.video-task-state.is-canceling { color: var(--status-canceling); }
.video-task-state.is-failed { color: var(--error); }
.video-task-state.is-ready, .video-task-state.is-canceled { color: var(--status-offline); }
.video-task-action {
  justify-self: end;
  align-self: center;
  min-width: 0;
  width: 24px;
  height: 24px;
  padding: 0;
}
.video-task-inspector {
  min-width: 0;
  min-height: 0;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  border-left: 1px solid var(--border);
  background: var(--surface-muted);
}
.video-task-inspector .video-task-player,
.video-task-inspector .video-player-history {
  flex: none;
}
.video-inspector-head {
  min-height: 56px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--surface-outline);
}
.video-inspector-title { color: var(--text-0); font-size: .875rem; font-weight: 700; }
.video-inspector-sub { margin-top: 2px; color: var(--text-3); font-size: .6875rem; }
.video-inspector-body { flex: 1; min-height: 0; overflow-y: auto; display: flex; flex-direction: column; gap: 16px; padding: 16px 18px 18px; }
/* 时长参数 + 生成操作常驻底部：不随检查器滚动 */
.video-inspector-footer {
  flex: none;
  display: flex;
  flex-direction: column;
  gap: 7px;
  padding: 9px 14px 11px;
  border-top: 1px solid var(--border);
  background: var(--surface-muted);
}
.video-inspector-section { display: flex; flex-direction: column; gap: 7px; }
.video-inspector-label { color: var(--text-0); font-size: .75rem; font-weight: 700; }
.video-inspector-label-hero {
  color: var(--accent);
  font-size: .8125rem;
  display: flex;
  align-items: center;
  gap: 6px;
}
.video-inspector-label-hero::before {
  content: '';
  width: 3px;
  height: 13px;
  border-radius: 2px;
  background: var(--accent);
}
.video-inspector-prompt-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.video-inspector-prompt-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 6px;
  min-width: 0;
}
/* MentionTextarea 内部元素需 :deep 穿透（scoped 样式默认到不了子组件内部） */
:deep(.video-inspector-prompt) {
  min-height: 176px;
  font-size: .8125rem;
  line-height: 1.6;
}
.video-inspector-params { display: grid; gap: 8px; }
.video-inspector-params div { display: flex; justify-content: space-between; gap: 12px; font-size: .75rem; }
.video-inspector-params dt { color: var(--text-3); }
.video-inspector-params dd { margin: 0; color: var(--text-1); text-align: right; }
.video-inspector-action { width: 100%; min-height: 32px; height: 32px; padding: 0 12px; font-size: .78125rem; }
/* 绑定参考图：当前分镜已绑定素材的图片平铺（生成时作为参考图提交） */
.video-bound-refs { display: grid; grid-template-columns: repeat(auto-fill, minmax(64px, 1fr)); gap: 8px; }
.video-bound-ref {
  position: relative;
  aspect-ratio: 1;
  overflow: hidden;
  padding: 0;
  border: 1px solid var(--surface-outline);
  border-radius: var(--radius);
  background: var(--bg-2);
  color: var(--text-3);
  cursor: pointer;
  transition: border-color 0.15s var(--ease-out);
}
.video-bound-ref:hover { border-color: var(--accent); }
.video-bound-ref:disabled { cursor: default; }
.video-bound-ref:disabled:hover { border-color: var(--surface-outline); }
.video-bound-ref img { width: 100%; height: 100%; object-fit: cover; display: block; }
.video-bound-ref small {
  position: absolute;
  left: 0;
  right: 0;
  bottom: 0;
  padding: 2px 5px;
  background: rgba(0,0,0,0.58);
  color: #fff;
  font-size: .59375rem;
  text-align: left;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.video-bound-ref-empty { width: 100%; height: 100%; display: flex; align-items: center; justify-content: center; font-size: .6875rem; }
.video-bound-refs-empty { padding: 10px; border: 1px dashed var(--surface-outline); border-radius: var(--radius); color: var(--text-3); font-size: .6875rem; line-height: 1.5; }
.video-param-row { display: flex; align-items: center; justify-content: space-between; gap: 10px; font-size: .75rem; white-space: nowrap; }
.video-param-name { color: var(--text-1); font-weight: 600; flex-shrink: 0; }
.video-param-value { color: var(--text-1); text-align: right; font-size: .6875rem; }
.video-param-control { display: inline-flex; align-items: center; gap: 6px; }
.video-param-unit { font-size: .6875rem; color: var(--text-2); }
.video-duration-input { width: 56px; height: 24px; padding: 2px 6px; font-size: .75rem; font-weight: 700; font-family: var(--font-mono); text-align: center; }
.video-seed-row { margin-top: 8px; }
.video-seed-value { color: var(--text-2); font-size: .6875rem; }
.video-seed-randomize { min-height: 24px; width: 24px; padding: 0; }

/* Prod grid */
.prod-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(190px, 1fr)); gap: 12px; }
.prod-card {
  display: flex; flex-direction: column; overflow: hidden;
  transition: transform 0.18s var(--ease-out), box-shadow 0.18s var(--ease-out), border-color 0.18s var(--ease-out);
  border-radius: 20px;
  background: var(--surface-raised);
  border: 1px solid var(--surface-outline);
}
.prod-card:hover { transform: translateY(-2px); box-shadow: var(--shadow-lift); }
.prod-cover { position: relative; aspect-ratio: 16/9; background: var(--bg-2); overflow: hidden; }
.prod-cover img { width: 100%; height: 100%; object-fit: cover; }
.prod-video { width: 100%; height: 100%; object-fit: cover; background: var(--media-surface); display: block; }
.prod-cover-empty { width: 100%; height: 100%; display: flex; align-items: center; justify-content: center; color: var(--text-3); }
.prod-idx {
  position: absolute; top: 5px; left: 5px; font-size: .625rem; font-weight: 700;
  font-family: var(--font-mono); background: rgba(0,0,0,0.5); color: #fff; padding: 1px 5px; border-radius: 3px;
}
.prod-overlay-badge {
  position: absolute; bottom: 5px; right: 5px; font-size: .625rem; font-weight: 600;
  background: var(--success); color: var(--on-accent); padding: 1px 5px; border-radius: 3px;
}
.prod-info { padding: 10px 12px 8px; }
.prod-desc { font-size: .75rem; line-height: 1.4; }
.prod-meta-line { margin-top: 5px; font-size: .625rem; color: var(--text-3); }
.prod-dots { display: flex; align-items: center; gap: 4px; margin-top: 5px; color: var(--text-3); }
.prod-error {
  margin-top: 6px;
  font-size: .6875rem;
  line-height: 1.45;
  color: var(--error);
}
.prod-actions { display: flex; gap: 6px; padding: 8px 10px 10px; border-top: 1px solid var(--surface-outline); }
.prod-actions .btn { flex: 1; justify-content: center; }

/* Asset detail dialog */
.asset-detail-overlay {
  z-index: 118;
  padding: 28px;
}
.asset-detail-dialog {
  width: min(1040px, calc(100vw - 56px));
  max-height: calc(100vh - 56px);
}
.asset-detail-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 14px 16px;
  border-bottom: 1px solid var(--surface-outline);
}
.asset-detail-title-block {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.asset-detail-kicker {
  color: var(--text-3);
  font-size: .625rem;
  font-weight: 800;
  letter-spacing: 0.12em;
  text-transform: uppercase;
}
.asset-detail-title {
  margin: 0;
  color: var(--text-0);
  font-size: 1.125rem;
  line-height: 1.2;
  font-family: var(--font-display);
}
.asset-detail-head-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}
.asset-detail-body {
  min-height: 0;
  overflow: auto;
  padding: 16px;
}
.asset-detail-shell {
  display: grid;
  grid-template-columns: minmax(280px, 380px) minmax(0, 1fr);
  gap: 14px;
  align-items: start;
}
.asset-detail-preview-panel,
.asset-detail-editor-panel {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.asset-detail-preview-panel {
  position: sticky;
  top: 0;
}
.asset-detail-section-title {
  min-height: 24px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  color: var(--text-1);
  font-size: .75rem;
  font-weight: 820;
  letter-spacing: 0.02em;
}
.asset-detail-section-title .dim {
  font-size: .6875rem;
  font-weight: 560;
  letter-spacing: 0;
  text-align: right;
}
.asset-detail-state {
  min-height: 20px;
  display: inline-flex;
  align-items: center;
  padding: 0 7px;
  border-radius: 999px;
  background: var(--overlay-track);
  color: var(--text-3);
  font-size: .625rem;
  font-weight: 760;
  white-space: nowrap;
}
.asset-detail-state.is-ready {
  color: var(--success);
  background: var(--success-bg);
}
.asset-detail-state.is-stale {
  color: var(--warning);
  background: var(--warning-bg);
}
.asset-detail-media-frame {
  position: relative;
  width: 100%;
  aspect-ratio: 16 / 9;
  display: block;
  padding: 0;
  border: 1px solid var(--surface-outline);
  border-radius: var(--radius);
  background: var(--bg-2);
  color: var(--text-3);
  overflow: hidden;
  cursor: zoom-in;
}
.asset-detail-media-frame:disabled {
  cursor: default;
  opacity: 1;
}
.asset-detail-media-frame:focus-visible {
  outline: none;
  border-color: var(--action-primary);
  box-shadow: 0 0 0 3px var(--button-focus);
}
.asset-detail-media-frame img {
  width: 100%;
  height: 100%;
  display: block;
  object-fit: cover;
}
.asset-detail-media-empty {
  width: 100%;
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--text-3);
}
.asset-detail-desc {
  margin: 0;
  color: var(--text-1);
  font-size: .8125rem;
  line-height: 1.7;
}
.asset-detail-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
}
.asset-detail-field {
  min-width: 0;
  padding: 9px 10px;
  border: 1px solid var(--surface-outline);
  border-radius: var(--radius);
  background: var(--surface-muted);
}
.asset-detail-field span,
.asset-detail-edit-field span,
.asset-detail-text-block span,
.asset-detail-shot-head {
  display: block;
  color: var(--text-3);
  font-size: .625rem;
  font-weight: 780;
  letter-spacing: 0.04em;
}
.asset-detail-field strong {
  display: block;
  margin-top: 5px;
  min-width: 0;
  overflow: hidden;
  color: var(--text-0);
  font-size: .75rem;
  line-height: 1.35;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.asset-detail-text-block {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
}
.asset-detail-edit-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}
.asset-detail-edit-grid--character,
.asset-detail-edit-grid--scene {
  grid-template-columns: 1fr;
}
.asset-detail-edit-field {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 7px;
}
.asset-detail-textarea {
  min-height: 138px;
  resize: vertical;
}
.asset-detail-edit-grid--character .asset-detail-textarea,
.asset-detail-edit-grid--scene .asset-detail-textarea {
  min-height: 164px;
}
.asset-detail-meta-row {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
}
.asset-detail-meta-item {
  min-width: 0;
  padding: 9px 10px;
  border: 1px solid var(--surface-outline);
  border-radius: var(--radius);
  background: var(--surface-muted);
}
.asset-detail-meta-item span {
  display: block;
  color: var(--text-3);
  font-size: .625rem;
  font-weight: 780;
  letter-spacing: 0.04em;
}
.asset-detail-meta-item strong {
  display: block;
  margin-top: 4px;
  min-width: 0;
  color: var(--text-0);
  font-size: .75rem;
  line-height: 1.35;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.asset-image-history {
  display: flex;
  flex-direction: column;
  gap: 9px;
  padding-top: 4px;
  border-top: 1px solid var(--surface-outline);
}
.asset-history-count {
  min-width: 22px;
  height: 20px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 0 6px;
  border-radius: 999px;
  background: var(--accent-bg);
  color: var(--accent-text);
  font-size: .625rem;
  font-weight: 760;
}
.asset-history-content {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.asset-history-compare-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
}
.asset-history-compare-item {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 5px;
}
.asset-history-label {
  color: var(--text-3);
  font-size: .625rem;
  font-weight: 720;
}
.asset-history-preview-flag {
  align-self: flex-start;
  padding: 2px 8px;
  border-radius: 999px;
  background: var(--accent-bg);
  color: var(--accent-text);
  font-size: .625rem;
  font-weight: 720;
}
.asset-history-compare-image {
  width: 100%;
  aspect-ratio: 16 / 9;
  padding: 0;
  overflow: hidden;
  border: 1px solid var(--surface-outline);
  border-radius: 8px;
  background: var(--bg-2);
  color: var(--text-3);
  cursor: zoom-in;
}
.asset-history-compare-image:disabled { cursor: default; opacity: 1; }
.asset-history-compare-image img {
  width: 100%;
  height: 100%;
  display: block;
  object-fit: cover;
}
.asset-history-selected-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  color: var(--text-3);
  font-size: .625rem;
}
.asset-history-selected-meta span:last-child { margin-left: auto; }
.asset-history-actions {
  display: flex;
  align-items: center;
  gap: 6px;
}
.asset-history-strip {
  display: flex;
  gap: 6px;
  padding-bottom: 2px;
  overflow-x: auto;
}
.asset-history-thumb {
  position: relative;
  width: 58px;
  height: 42px;
  flex: 0 0 auto;
  padding: 0;
  overflow: hidden;
  border: 1px solid var(--surface-outline);
  border-radius: 7px;
  background: var(--bg-2);
  cursor: pointer;
}
.asset-history-thumb:hover,
.asset-history-thumb.selected { border-color: var(--accent); }
.asset-history-thumb.selected { box-shadow: 0 0 0 2px var(--button-focus); }
.asset-history-thumb.current::after {
  position: absolute;
  inset: 0;
  border: 1px solid var(--success);
  border-radius: inherit;
  content: '';
  pointer-events: none;
}
.asset-history-thumb img { width: 100%; height: 100%; display: block; object-fit: cover; }
.asset-history-thumb span {
  position: absolute;
  right: 2px;
  bottom: 2px;
  padding: 1px 4px;
  border-radius: 999px;
  background: var(--success);
  color: #fff;
  font-size: .5rem;
  font-weight: 760;
}
.asset-history-empty {
  min-height: 44px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 8px;
  border: 1px dashed var(--surface-outline);
  border-radius: 8px;
  color: var(--text-3);
  font-size: .6875rem;
}
.asset-detail-text-block > div,
.asset-detail-shot-list {
  padding: 10px;
  border: 1px solid var(--surface-outline);
  border-radius: var(--radius);
  background: var(--surface-muted);
}
.asset-detail-text-block p {
  margin: 5px 0 0;
  color: var(--text-2);
  font-size: .75rem;
  line-height: 1.55;
}
.asset-detail-shot-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.asset-detail-shot-items {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.asset-detail-shot-row {
  display: grid;
  grid-template-columns: 36px minmax(0, 1fr) auto;
  align-items: center;
  gap: 8px;
  color: var(--text-2);
  font-size: .6875rem;
}
.asset-detail-shot-row strong {
  min-width: 0;
  overflow: hidden;
  color: var(--text-1);
  font-weight: 650;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.asset-detail-shot-row small,
.asset-detail-empty {
  color: var(--text-3);
  font-size: .6875rem;
}
.asset-detail-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 12px 16px;
  border-top: 1px solid var(--surface-outline);
}
.asset-detail-secondary-actions,
.asset-detail-primary-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}
.asset-detail-prompt-panel {
  min-width: 0;
  margin-top: 14px;
  padding-top: 14px;
  border-top: 1px solid var(--surface-outline);
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.asset-detail-prompt-head-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}
.asset-detail-copy-btn {
  gap: 5px;
}
.asset-detail-prompt-textarea {
  min-height: 96px;
  max-height: 260px;
  overflow: auto;
  padding: 12px 14px;
  border: 1px solid var(--surface-outline);
  border-radius: var(--radius);
  background: var(--surface-muted);
  color: var(--text-1);
  font-size: .75rem;
  line-height: 1.65;
  white-space: pre-wrap;
  word-break: break-word;
  resize: vertical;
  width: 100%;
}
.asset-detail-prompt-hint {
  margin: 0;
  color: var(--text-3);
  font-size: .6875rem;
  line-height: 1.5;
}
.asset-detail-readonly-grid {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.asset-detail-readonly {
  min-width: 0;
  padding: 10px 12px;
  border: 1px solid var(--surface-outline);
  border-radius: var(--radius);
  background: var(--surface-muted);
}
.asset-detail-readonly span {
  display: block;
  color: var(--text-3);
  font-size: .625rem;
  font-weight: 780;
  letter-spacing: 0.04em;
}
.asset-detail-readonly p {
  margin: 6px 0 0;
  color: var(--text-1);
  font-size: .75rem;
  line-height: 1.65;
  white-space: pre-wrap;
  word-break: break-word;
  user-select: text;
}
.asset-detail-readonly p.dim {
  color: var(--text-3);
}

/* Image viewer */
.image-viewer-overlay {
  z-index: 120;
  padding: 28px;
}
.image-viewer-dialog {
  width: min(1100px, calc(100vw - 56px));
  max-height: calc(100vh - 56px);
  background: var(--header-bg);
  backdrop-filter: blur(20px) saturate(180%);
  -webkit-backdrop-filter: blur(20px) saturate(180%);
}
.image-viewer-head {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 16px 18px;
  border-bottom: 1px solid var(--surface-outline);
}
.image-viewer-title {
  font-size: .875rem;
  font-weight: 700;
  color: var(--text-1);
  font-family: var(--font-display);
}
.image-viewer-body {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 20px;
  overflow: auto;
  min-height: 0;
}
.image-viewer-img {
  display: block;
  max-width: 100%;
  max-height: calc(100vh - 140px);
  border-radius: 18px;
  box-shadow: var(--shadow-xl);
  background: var(--surface-muted);
}
.asset-compare-overlay {
  z-index: 124;
  padding: 20px;
}
.asset-compare-dialog {
  width: min(1500px, calc(100vw - 40px));
  height: min(900px, calc(100vh - 40px));
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--header-bg);
}
.asset-compare-dialog .image-viewer-head .btn { margin-left: auto; }
.asset-compare-body {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: repeat(2, minmax(420px, 1fr));
  gap: 14px;
  padding: 14px;
  overflow: auto;
}
.asset-compare-figure {
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin: 0;
}
.asset-compare-figure figcaption {
  min-height: 24px;
  display: flex;
  align-items: center;
  gap: 10px;
  color: var(--text-3);
  font-size: .6875rem;
}
.asset-compare-figure figcaption strong {
  color: var(--text-1);
  font-size: .75rem;
}
.asset-compare-figure figcaption span { margin-left: auto; }
.asset-compare-stage {
  flex: 1;
  min-height: 360px;
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: auto;
  padding: 10px;
  border: 1px solid var(--surface-outline);
  border-radius: var(--radius-lg);
  background: var(--bg-2);
}
.asset-compare-stage img {
  display: block;
  max-width: 100%;
  max-height: 100%;
  object-fit: contain;
}

/* Export */
.export-split { flex: 1; display: flex; min-height: 0; }
.export-main { flex: 1; min-height: 0; overflow-y: auto; display: flex; flex-direction: column; align-items: stretch; gap: 18px; padding: 16px 20px 24px; }
.export-section { display: flex; flex-direction: column; min-height: 0; }
.export-section-grow { flex: 1; }
.export-section-head { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; }
.export-section-title { font-size: .8125rem; font-weight: 800; color: var(--text-0); }
.export-merge-strip { display: flex; gap: 12px; overflow-x: auto; padding-bottom: 4px; }
.merge-card {
  flex: 0 0 auto;
  width: 260px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 8px;
  border-radius: var(--radius);
  background: var(--surface-raised);
  border: 1px solid var(--border);
}
.merge-card video {
  width: 100%;
  aspect-ratio: 16 / 9;
  border-radius: 6px;
  background: var(--media-surface);
  display: block;
}
.merge-card-pending {
  width: 100%;
  aspect-ratio: 16 / 9;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 0 12px;
  border-radius: 6px;
  background: var(--surface-muted);
  color: var(--text-3);
  font-size: .6875rem;
  text-align: center;
}
.merge-card-pending.is-failed { color: var(--error); background: var(--error-bg); }
.merge-card-meta { display: flex; align-items: center; gap: 6px; font-size: .6875rem; color: var(--text-3); min-width: 0; }
.merge-card-meta .btn { margin-left: auto; }
.merge-card.playable { cursor: pointer; }
.merge-card.playable:hover { border-color: var(--border-strong); box-shadow: var(--shadow-card); }
.merge-card-thumb { position: relative; }
.merge-card-play {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 6px;
  background: rgba(0,0,0,0.28);
  color: #fff;
  opacity: 0;
  transition: opacity 0.15s var(--ease-out);
  pointer-events: none;
}
.merge-card.playable:hover .merge-card-play { opacity: 1; }
.merge-viewer-dialog { width: min(1080px, calc(100vw - 56px)); }
.merge-viewer-body {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 18px;
  min-height: 0;
}
.merge-viewer-video {
  width: 100%;
  max-height: calc(100vh - 220px);
  border-radius: var(--radius);
  background: var(--media-surface);
  display: block;
}
.export-merge-empty {
  padding: 14px;
  border: 1px dashed var(--border);
  border-radius: var(--radius);
  color: var(--text-3);
  font-size: .75rem;
  text-align: center;
}
.export-grid {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  align-content: start;
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: 12px;
}
.exp-card { display: flex; flex-direction: column; align-items: stretch; gap: 6px; padding: 8px; border-radius: var(--radius); background: var(--surface-raised); border: 1px solid var(--border); }
.exp-card:hover { border-color: var(--border-strong); box-shadow: var(--shadow-card); }
.exp-card.playable { cursor: pointer; }
.exp-card.selected { border-color: var(--accent); box-shadow: 0 0 0 2px var(--accent-glow); }
.exp-check {
  position: absolute;
  right: 6px;
  top: 6px;
  width: 18px;
  height: 18px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 50%;
  border: 1.5px solid rgba(255,255,255,0.9);
  background: rgba(0,0,0,0.35);
  color: #fff;
}
.exp-check.on { background: var(--accent); border-color: var(--accent); }
.exp-thumb {
  position: relative;
  width: 100%;
  aspect-ratio: 16 / 9;
  overflow: hidden;
  border: 1px solid var(--surface-outline);
  border-radius: 6px;
  background: var(--media-surface);
}
.exp-thumb video { width: 100%; height: 100%; object-fit: cover; display: block; }
.exp-thumb-empty { width: 100%; height: 100%; display: flex; align-items: center; justify-content: center; color: var(--text-3); }
.exp-thumb-index {
  position: absolute;
  left: 5px;
  top: 5px;
  padding: 1px 5px;
  border-radius: 4px;
  background: rgba(0,0,0,0.56);
  color: #fff;
  font-family: var(--font-mono);
  font-size: .625rem;
  font-weight: 800;
}
.exp-thumb-duration {
  position: absolute;
  right: 5px;
  bottom: 5px;
  padding: 1px 4px;
  border-radius: 3px;
  background: rgba(0,0,0,0.6);
  color: #fff;
  font-family: var(--font-mono);
  font-size: .5625rem;
}
/* 镜头预览：悬停浮现的居中播放钮，点击打开预览弹窗（不影响卡片勾选） */
.exp-play {
  position: absolute;
  left: 50%;
  top: 50%;
  transform: translate(-50%, -50%) scale(0.9);
  width: 36px;
  height: 36px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 50%;
  background: rgba(0,0,0,0.62);
  color: #fff;
  opacity: 0;
  cursor: pointer;
  transition: opacity 0.15s, transform 0.15s, background 0.15s;
}
.exp-card:hover .exp-play { opacity: 1; transform: translate(-50%, -50%) scale(1); }
.exp-play:hover { background: var(--accent); }
/* 导出完成手动标记按钮 */
.export-done-btn.on {
  background: var(--success-bg, var(--accent-bg));
  color: var(--success, var(--accent-text));
  border-color: transparent;
  font-weight: 600;
}
.exp-row-line { display: flex; align-items: center; gap: 8px; min-width: 0; }

/* Shared */
.dim { color: var(--text-3); }

/* ===== 拆分分解：分镜结果可视化表格 ===== */
.breakdown-content { min-width: 0; }
.breakdown-bar-copy { display: flex; align-items: baseline; gap: 10px; min-width: 0; }
.breakdown-agent-switch { display: flex; gap: 4px; }
/* 一键成片按钮：运行中显示阶段文案，禁止换行撑破工具栏 */
.one-click-storyboard-video-btn { white-space: nowrap; }
.breakdown-bar-title { color: var(--text-0); font-size: .8125rem; font-weight: 650; white-space: nowrap; }
.breakdown-progress {
  margin: 14px 16px 4px; padding: 14px 16px 12px;
  border: 1px solid color-mix(in srgb, var(--accent) 28%, var(--surface-outline));
  border-radius: 11px; background: color-mix(in srgb, var(--accent) 5%, var(--surface-1));
}
.breakdown-progress-head { display: flex; align-items: center; gap: 10px; }
.breakdown-progress-head strong { display: block; color: var(--text-0); font-size: .75rem; }
.breakdown-progress-head p { margin: 3px 0 0; color: var(--text-2); font-size: .6875rem; }
.breakdown-progress-icon {
  display: grid; place-items: center; width: 30px; height: 30px; flex: 0 0 auto;
  border-radius: 8px; background: var(--accent-bg); color: var(--accent-text);
}
.breakdown-progress-time { margin-left: auto; color: var(--text-2); font-size: .6875rem; }
.breakdown-progress-track {
  position: relative; height: 4px; margin-top: 12px; overflow: hidden;
  border-radius: 999px; background: var(--surface-3);
}
.breakdown-progress-track span {
  position: absolute; inset-block: 0; width: 36%; border-radius: inherit;
  background: var(--accent); animation: breakdown-progress-slide 1.5s ease-in-out infinite;
}
.storyboard-review-progress .breakdown-progress-track span {
  inset-inline: 0; width: 100%; transform-origin: left center;
  animation: none; transition: transform 180ms ease-out;
}
.breakdown-progress-foot {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  margin-top: 8px; color: var(--text-3); font-size: .625rem;
}
.breakdown-progress-count { color: var(--accent-text); white-space: nowrap; }
.breakdown-run-failure {
  display: flex; align-items: flex-start; gap: 10px;
  margin: 14px 16px 4px; padding: 12px 14px;
  border: 1px solid color-mix(in srgb, var(--error) 38%, var(--surface-outline));
  border-radius: 10px; background: var(--error-bg); color: var(--error);
}
.breakdown-run-failure svg { flex: 0 0 auto; margin-top: 1px; }
.breakdown-run-failure strong { display: block; font-size: .75rem; }
.breakdown-run-failure p {
  margin: 4px 0; color: var(--text-1); font-size: .6875rem; line-height: 1.55;
  white-space: pre-wrap; overflow-wrap: anywhere;
}
.breakdown-run-failure span { color: var(--text-2); font-size: .625rem; }
.breakdown-run-duration {
  margin: 10px 16px 0; font-size: .6875rem; color: var(--text-2);
}
.storyboard-review-error,
.storyboard-review-notice {
  display: flex; align-items: center; gap: 9px;
  margin: 12px 16px 0; padding: 11px 13px; border-radius: 10px; font-size: .75rem;
}
.storyboard-review-error {
  border: 1px solid color-mix(in srgb, var(--error) 36%, var(--surface-outline));
  background: var(--error-bg); color: var(--error);
}
.storyboard-review-notice {
  border: 1px solid color-mix(in srgb, var(--success) 30%, var(--surface-outline));
  background: color-mix(in srgb, var(--success) 7%, var(--surface-1)); color: var(--text-1);
}
.storyboard-review-notice .btn { margin-left: auto; }
.storyboard-review-panel {
  margin: 12px 16px 2px; border: 1px solid var(--surface-outline); border-radius: 11px;
  background: var(--surface-1); overflow: hidden;
}
.storyboard-review-head {
  display: flex; align-items: flex-start; justify-content: space-between; gap: 16px;
  padding: 13px 14px; border-bottom: 1px solid var(--surface-outline); background: var(--surface-soft);
}
.storyboard-review-title { display: flex; align-items: flex-start; gap: 9px; min-width: 0; }
.storyboard-review-title > svg { flex: 0 0 auto; margin-top: 1px; color: var(--accent-text); }
.storyboard-review-title strong { display: block; color: var(--text-0); font-size: .75rem; }
.storyboard-review-title p { margin: 3px 0 0; color: var(--text-2); font-size: .6875rem; line-height: 1.45; }
.storyboard-review-status {
  flex: 0 0 auto; padding: 3px 7px; border-radius: 999px;
  background: var(--error-bg); color: var(--error); font-size: .625rem; font-weight: 650;
}
.storyboard-review-status.is-passed {
  background: color-mix(in srgb, var(--success) 10%, transparent); color: var(--success);
}
.storyboard-review-list { margin: 0; padding: 0; list-style: none; max-height: 320px; overflow-y: auto; }
.storyboard-review-list li { padding: 11px 14px; border-bottom: 1px solid var(--surface-outline); }
.storyboard-review-list li:last-child { border-bottom: 0; }
.storyboard-review-list p { margin: 5px 0 2px; color: var(--text-1); font-size: .6875rem; line-height: 1.55; }
.storyboard-review-list li > span { color: var(--text-3); font-size: .625rem; line-height: 1.45; }
.storyboard-review-issue-meta { display: flex; align-items: center; gap: 6px; }
.storyboard-review-severity { font-size: .625rem; font-weight: 650; }
.storyboard-review-severity.is-error { color: var(--error); }
.storyboard-review-severity.is-warning { color: var(--warning); }
.storyboard-review-source { color: var(--text-3); font-size: .625rem; }
.storyboard-review-actions {
  display: flex; justify-content: flex-end; gap: 7px; padding: 10px 14px;
  border-top: 1px solid var(--surface-outline); background: var(--surface-soft);
}
@keyframes breakdown-progress-slide {
  0% { transform: translateX(-110%); }
  55% { transform: translateX(150%); }
  100% { transform: translateX(310%); }
}
.breakdown-metrics {
  display: grid; grid-template-columns: repeat(4, minmax(130px, 1fr)); gap: 10px;
  padding: 14px 16px 4px;
}
.breakdown-metric {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  padding: 12px 14px; border: 1px solid var(--surface-outline); border-radius: 10px;
  background: var(--surface-soft); color: var(--text-2); font-size: .75rem;
}
.breakdown-metric strong { color: var(--text-0); font: 650 1.125rem/1 var(--font-mono, monospace); }
.breakdown-table-wrap {
  margin: 10px 16px 18px; overflow: auto; flex: 1; min-height: 0;
  border: 1px solid var(--surface-outline); border-radius: 11px; background: var(--surface-1);
}
.breakdown-table {
  width: 100%; min-width: 1260px; border-collapse: separate; border-spacing: 0;
  table-layout: fixed; color: var(--text-1); font-size: .75rem;
}
.breakdown-table th {
  position: sticky; top: 0; z-index: 2; padding: 10px 11px;
  border-bottom: 1px solid var(--surface-outline); background: var(--surface-2);
  color: var(--text-3); font-size: .625rem; font-weight: 650; letter-spacing: .04em;
  text-align: left; white-space: nowrap;
}
.breakdown-table td {
  padding: 10px 11px; border-bottom: 1px solid var(--surface-outline);
  vertical-align: top; line-height: 1.5;
}
.breakdown-table tbody tr { cursor: pointer; outline: none; transition: background .15s; }
.breakdown-table tbody tr:last-child td { border-bottom: 0; }
.breakdown-table tbody tr:hover,
.breakdown-table tbody tr:focus-visible { background: var(--surface-soft); }
.breakdown-table th:nth-child(1) { width: 62px; }
.breakdown-table th:nth-child(2) { width: 130px; }
.breakdown-table th:nth-child(3) { width: 68px; }
.breakdown-table th:nth-child(4) { width: 155px; }
.breakdown-table th:nth-child(5) { width: 135px; }
.breakdown-table th:nth-child(6) { width: 120px; }
.breakdown-table th:nth-child(7) { width: 260px; }
.breakdown-table th:nth-child(8) { width: 170px; }
.breakdown-table th:nth-child(9) { width: 90px; }
.breakdown-table th.is-action { width: 76px; }
.breakdown-table td.is-action { text-align: right; }
.breakdown-shot-number { color: var(--accent-text); font: 650 .75rem/1.4 var(--font-mono, monospace); }
.breakdown-shot-title { display: block; color: var(--text-0); font-size: .75rem; line-height: 1.45; }
.breakdown-duration { color: var(--text-1); font-family: var(--font-mono, monospace); }
.breakdown-chip-list { display: flex; flex-wrap: wrap; gap: 4px; }
.breakdown-cell-chip {
  display: inline-flex; max-width: 100%; padding: 2px 7px; border-radius: 999px;
  background: var(--surface-3); color: var(--text-2); font-size: .625rem;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.breakdown-cell-chip.is-ready { color: var(--success); background: color-mix(in srgb, var(--success) 10%, transparent); }
.breakdown-cell-chip.is-person { color: var(--accent-text); background: var(--accent-bg); }
.breakdown-empty-cell { color: var(--text-3); font-size: .6875rem; }
.breakdown-description,
.breakdown-atmosphere {
  display: -webkit-box; margin: 0; overflow: hidden; color: var(--text-1);
  -webkit-box-orient: vertical; -webkit-line-clamp: 3;
}
.breakdown-atmosphere { color: var(--text-2); -webkit-line-clamp: 2; }
.breakdown-prompt-state { display: inline-flex; align-items: center; gap: 5px; color: var(--text-3); white-space: nowrap; }
.breakdown-prompt-state.is-ready { color: var(--success); }

@media (max-width: 1080px) {
  .breakdown-metrics { grid-template-columns: repeat(2, minmax(130px, 1fr)); }
  .studio-body {
    grid-template-columns: 1fr;
  }

  .video-task-workbench.has-player {
    grid-template-columns: var(--vleft, 208px) minmax(0, 1fr);
  }

  .video-task-side {
    grid-template-columns: minmax(0, 1fr) var(--vright, 260px);
  }

  .video-main-grid {
    grid-template-columns: 1fr;
  }

  .split-layout,
  .export-split {
    flex-direction: column;
  }

  .sb-scene-select { max-width: none; flex: 1; }

  .detail-panel {
    min-height: 420px;
  }

  .field-grid-4 {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .character-asset-grid {
    grid-template-columns: repeat(auto-fill, minmax(420px, 1fr));
  }

  .video-task-head {
    align-items: flex-start;
    flex-direction: column;
  }

  .video-task-metrics {
    width: 100%;
    margin-left: 0;
    justify-content: flex-start;
  }

  .video-task-row {
    grid-template-columns: 56px minmax(0, 1fr);
  }

  .video-task-action {
    justify-self: start;
  }

  .image-viewer-overlay {
    padding: 16px;
  }

  .image-viewer-dialog {
    width: calc(100vw - 32px);
    max-height: calc(100vh - 32px);
  }

  .asset-detail-overlay {
    padding: 16px;
  }

  .asset-detail-dialog {
    width: calc(100vw - 32px);
    max-height: calc(100vh - 32px);
  }

  .asset-detail-shell {
    grid-template-columns: 1fr;
  }

  .asset-detail-preview-panel {
    position: static;
  }

  .asset-detail-grid,
  .asset-detail-edit-grid,
  .asset-detail-text-block {
    grid-template-columns: 1fr;
  }

}

/* ===== 任务列表面板 ===== */
.gen-task-row {
  grid-template-columns: 84px minmax(0, 1fr) auto;
  cursor: default;
}
.gen-task-row .video-task-preview img {
  cursor: zoom-in;
}
.gen-task-actions {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 8px;
}
.gen-task-cancel {
  color: var(--error);
  border-color: var(--error);
}
.gen-task-recall {
  color: var(--accent-text);
  border-color: var(--accent);
}
.gen-task-cancel:hover {
  background: var(--error-bg);
}
.video-task-metric.is-canceled,
.video-task-status.is-canceled {
  color: var(--status-offline);
  border-color: var(--status-offline-border);
  background: var(--status-offline-bg);
}
.gen-task-row.is-canceled {
  opacity: .72;
}

/* 任务触发按钮(顶栏) */
.task-drawer-trigger {
  position: relative;
}
.task-drawer-badge {
  min-width: 16px;
  height: 16px;
  padding: 0 4px;
  border-radius: 999px;
  background: var(--accent);
  color: var(--action-primary-text);
  font-size: .625rem;
  font-weight: 700;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  line-height: 1;
}

/* 右侧抽屉 */
.task-drawer-overlay {
  position: fixed;
  inset: 0;
  z-index: 118;
  display: flex;
  justify-content: flex-end;
  background: var(--scrim);
  backdrop-filter: blur(8px);
  -webkit-backdrop-filter: blur(8px);
  animation: fadeIn 0.18s var(--ease-out);
}
.task-drawer {
  width: min(560px, 100vw);
  height: 100%;
  display: flex;
  flex-direction: column;
  background: var(--panel-bg);
  border-left: 1px solid var(--panel-border);
  box-shadow: var(--shadow-xl);
  animation: taskDrawerIn 0.22s var(--ease-out);
}
@keyframes taskDrawerIn {
  from { transform: translateX(24px); opacity: 0; }
  to { transform: translateX(0); opacity: 1; }
}
.task-drawer-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 14px 16px;
  border-bottom: 1px solid var(--surface-outline);
}
.task-drawer-head-actions {
  display: flex;
  align-items: center;
  gap: 6px;
}
.task-drawer-metrics {
  padding: 10px 16px;
  border-bottom: 1px solid var(--surface-outline);
}
.task-drawer-body {
  flex: 1;
  overflow-y: auto;
}
.task-drawer-empty {
  flex: 1;
  justify-content: center;
}

@media (max-width: 860px) {
  .studio {
    padding: 8px;
    gap: 8px;
  }

  .studio-topbar-main {
    align-items: flex-start;
  }

  .studio-topbar {
    flex-direction: column;
    align-items: stretch;
  }

  .studio-topbar-side {
    justify-content: space-between;
  }

  .sidebar {
    max-height: 340px;
  }

  .studio-topbar-side,
  .studio-actions {
    flex-wrap: wrap;
  }

  .toolbar-right,
  .export-bar {
    flex-wrap: wrap;
  }

  .asset-grid,
  .character-asset-grid,
  .prod-grid {
    grid-template-columns: 1fr;
  }

  .character-asset-card {
    min-height: 0;
  }

  .character-asset-overview {
    grid-template-columns: 1fr;
  }

  .character-portrait {
    width: auto;
  }

  .character-asset-main {
    padding: 10px;
  }

  .character-asset-head {
    align-items: stretch;
    flex-direction: column;
  }

  .video-task-workbench {
    grid-template-columns: 1fr;
    overflow-y: auto;
  }

  /* 窄屏纵向堆叠后无栏间分界，隐藏拖拽分隔条 */
  .video-col-divider { display: none; }

  .video-task-inspector {
    border-top: 1px solid var(--surface-outline);
    border-left: 0;
  }

  .video-task-side {
    display: flex;
    flex-direction: column;
    border-top: 1px solid var(--surface-outline);
    border-left: 0;
  }

  /* 窄屏：中列改为纵向堆叠，由外层 workbench 整体滚动 */
  .video-main-col {
    display: flex;
    flex-direction: column;
    flex: none;
  }

  .video-main-scroll {
    flex: none;
    overflow: visible;
  }

  .video-task-side .video-task-inspector {
    flex: none; /* 窄屏下由外层 workbench 整体滚动，检查器按内容撑开，不参与 flex 收缩 */
    border-top: 0;
    display: flex;
    flex-direction: column;
    overflow: visible;
  }

  .video-inspector-body {
    overflow: visible;
  }

  .video-task-player {
    flex: none; /* 禁止收缩：否则 stage 的 min-height 会使其溢出播放器并遮挡下方检查器 */
  }

  .video-player-stage {
    flex: none;
    max-height: 220px;
  }

  .frame-row {
    flex-direction: column;
    align-items: stretch;
  }

  .detail-hero {
    grid-template-columns: 1fr;
  }

  .field-grid-2,
  .field-grid-4 {
    grid-template-columns: 1fr;
  }

  .frame-thumbs {
    width: 100%;
  }

  .frame-thumb {
    width: 100%;
  }

}
</style>
