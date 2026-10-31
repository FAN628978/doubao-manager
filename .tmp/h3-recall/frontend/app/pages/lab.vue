<template>
  <div class="shot-playground">
    <!-- 顶栏 58px -->
    <header class="lab-head">
      <button class="back-btn" title="返回项目列表" aria-label="返回项目列表" @click="navigateTo('/')">
        <ArrowLeft :size="16" />
      </button>
      <h1 class="lab-title">分镜精细制作</h1>
      <span class="lab-sub">上传参考图、写分镜描述，不建项目也能逐镜精修视频</span>
      <!-- 管理员视角：右上角按用户筛选镜头任务（数据口径与全自动任务队列一致） -->
      <div v-if="isPrivilegedView" class="lab-owner-select">
        <BaseSelect v-model="ownerScope" :options="ownerOptions" :searchable="true" aria-label="按用户筛选镜头任务" />
      </div>
    </header>

    <div class="shot-workspace">
      <!-- 左：任务侧栏 -->
      <aside class="task-sidebar">
        <div class="shot-list-head">
          <div class="shot-list-heading-row">
            <div class="shot-list-heading">
              <div class="shot-list-title">镜头任务 <span class="shot-list-count tabular-nums">{{ scopedTasks.length }}</span></div>
              <div class="shot-list-meta">选择镜头，查看结果或继续编辑</div>
            </div>
            <div class="shot-list-actions">
              <button type="button" class="btn btn-icon btn-sm shot-refresh" title="刷新列表" aria-label="刷新镜头任务列表" @click="loadHistory">
                <RefreshCw :size="13" />
              </button>
              <button type="button" class="btn btn-sm btn-primary shot-create" @click="startNew">
                <Plus :size="13" /> 新建镜头
              </button>
            </div>
          </div>
          <div class="shot-list-filters" aria-label="筛选镜头任务">
            <button type="button" class="shot-filter" :class="{ on: listFilter === '' }" :aria-pressed="listFilter === ''" @click="listFilter = ''">
              全部 <span class="tabular-nums">{{ scopedTasks.length }}</span>
            </button>
            <button type="button" class="shot-filter is-pending" :class="{ on: listFilter === 'pending' }" :aria-pressed="listFilter === 'pending'" @click="toggleFilter('pending')">
              生成中 <span class="tabular-nums">{{ countBy('pending') }}</span>
            </button>
            <button type="button" class="shot-filter is-done" :class="{ on: listFilter === 'done' }" :aria-pressed="listFilter === 'done'" @click="toggleFilter('done')">
              完成 <span class="tabular-nums">{{ countBy('done') }}</span>
            </button>
            <button type="button" class="shot-filter is-failed" :class="{ on: listFilter === 'failed' }" :aria-pressed="listFilter === 'failed'" @click="toggleFilter('failed')">
              失败 <span class="tabular-nums">{{ countBy('failed') }}</span>
            </button>
          </div>
        </div>
        <div class="task-list" role="list" aria-label="镜头任务列表">
          <div v-if="loadingHistory" class="shot-empty">
            <LoaderCircle :size="18" class="spin" />
            <span>正在加载镜头任务</span>
          </div>
          <div v-else-if="!filteredTasks.length" class="shot-empty">
            <Video :size="22" />
            <strong>{{ scopedTasks.length ? '没有符合条件的镜头' : '还没有镜头任务' }}</strong>
            <span>{{ scopedTasks.length ? '换个状态查看其他任务' : ownerScope === 'mine' ? '你还没有发起过镜头任务' : typeof ownerScope === 'number' ? '该用户还没有镜头任务' : '从一段镜头描述开始创作' }}</span>
            <button v-if="scopedTasks.length" type="button" class="btn btn-sm" @click="listFilter = ''">查看全部</button>
            <button v-else type="button" class="btn btn-sm btn-primary" @click="startNew"><Plus :size="12" /> 新建第一个镜头</button>
          </div>
          <article
            v-for="task in filteredTasks"
            :key="task.id"
            :class="['shot-row', 'is-' + taskState(task), { active: selected?.id === task.id && !isNew }]"
            role="listitem"
          >
            <button
              type="button"
              class="shot-row-select"
              :aria-label="`打开镜头 ${task.id}：${taskPreview(task)}`"
              :aria-current="selected?.id === task.id && !isNew ? 'true' : undefined"
              @click="selectTask(task)"
            >
              <div class="shot-row-preview">
                <video
                  v-if="task.status === 'completed' && taskSrc(task)"
                  :src="taskSrc(task)"
                  :poster="posterOf(taskSrc(task)) || undefined"
                  preload="none"
                  playsinline
                  muted
                  tabindex="-1"
                />
                <div v-else class="shot-row-empty">
                  <LoaderCircle v-if="isActive(task)" :size="17" class="spin" />
                  <Video v-else :size="17" />
                </div>
                <span class="shot-row-index tabular-nums">#{{ String(task.id).padStart(3, '0') }}</span>
              </div>
              <div class="shot-row-main">
                <strong class="shot-row-name" :title="taskPreview(task)">{{ taskPreview(task) }}</strong>
                <div class="shot-row-meta-line">
                  <span :class="['shot-row-state', 'is-' + taskState(task)]">
                    <i :class="['dot', taskState(task) === 'pending' && 'pending']" />{{ taskStatusLabel(task) }}
                  </span>
                  <span class="shot-row-detail tabular-nums">{{ taskDuration(task) }} 秒</span>
                  <span v-if="isPrivilegedView && task.ownerName" class="shot-row-owner" :title="'发起人：' + task.ownerName">{{ task.ownerId === myId ? '我的' : task.ownerName }}</span>
                </div>
                <div class="shot-row-provider" :title="`${task.provider || '未知服务'}${task.model ? ' · ' + task.model : ''}`">
                  {{ task.provider || '未知服务' }}{{ task.model ? ' · ' + task.model : '' }}
                </div>
                <div v-if="task.status === 'failed'" class="shot-row-error" :title="task.errorMsg">
                  {{ task.errorMsg || '生成失败，请重试' }}
                </div>
              </div>
            </button>
            <div class="shot-row-actions">
              <button v-if="canRecall(task)" type="button" class="btn btn-icon btn-sm shot-row-action" title="使用原豆包 job_id 召回视频（不会重新生成）" :aria-label="`召回镜头 ${task.id} 视频`" :disabled="recallingIds.includes(task.id)" @click.stop="recallTask(task)">
                <RefreshCw :size="12" :class="{ spin: recallingIds.includes(task.id) }" />
              </button>
              <button v-if="canCancel(task)" type="button" class="btn btn-icon btn-sm shot-row-action" title="取消任务（排队/生成中，仅本地 MiniMax 队列支持）" :aria-label="`取消镜头 ${task.id}`" @click.stop="cancelTask(task)">
                <Square :size="10" fill="currentColor" />
              </button>
              <button type="button" class="btn btn-icon btn-sm shot-row-action" title="按原参数重新生成" :aria-label="`重新生成镜头 ${task.id}`" :disabled="isActive(task)" @click="regen(task)">
                <Clapperboard :size="12" />
              </button>
              <button type="button" class="btn btn-icon btn-sm shot-row-action is-danger" title="删除该镜头任务" :aria-label="`删除镜头 ${task.id}`" @click="taskToDelete = task">
                <Trash2 :size="12" />
              </button>
            </div>
          </article>
        </div>
      </aside>

      <!-- 右：主区（上：参考图+预览 / 下：编辑器） -->
      <div class="shot-main">
        <!-- 风格前缀：与工作台「视频制作」同款交互——输入框直接编辑 + 从预设导入，横条摆在参考图/描述面板上方 -->
        <div class="style-bar">
          <span class="inspector-label">风格前缀</span>
          <input
            v-model="stylePrefix"
            class="textarea style-prefix-input"
            placeholder="风格前缀，拼在提示词最前（生成提示词与提交生成都用它）；留空不加前缀"
          />
          <div class="style-select">
            <BaseSelect
              :model-value="''"
              :options="stylePresetOptions"
              placeholder="从风格预设导入"
              searchable
              @update:model-value="importStylePrefix"
            />
          </div>
          <span class="style-hint">导入即填入输入框，可继续手编；提交时幂等拼接、不会重复</span>
        </div>
        <div class="shot-top-panels">
          <!-- 参考图 -->
          <section class="panel refs-panel">
            <div class="inspector-head">
              <span class="inspector-label">参考图 <span class="tag mono">{{ refImages.length }}/9</span></span>
              <span class="ref-hint">输入 @ 可选择当前参考图或媒体资产库图片；选择资产库图片会自动加入这里</span>
            </div>
            <div class="ref-grid">
              <div
                v-for="(img, i) in refImages"
                :key="img.path + i"
                class="ref-item"
                :draggable="dragLock !== i"
                @dragstart="dragFrom = i"
                @dragover.prevent
                @drop.prevent="reorderRef(i)"
              >
                <div class="ref-thumb" title="点击放大查看" @click="openRefViewer(img, i)">
                  <img :src="thumbOf(img.url)" :alt="`参考图 ${i + 1}`" />
                  <!-- 序号角标常驻：填了名称也看得见自己是图几，拖拽排序后随位置更新 -->
                  <span class="ref-tag">图{{ i + 1 }}</span>
                  <button class="ref-x" title="移除" @click.stop="refImages.splice(i, 1)"><X :size="12" /></button>
                </div>
                <input
                  v-model.trim="img.name"
                  class="ref-name"
                  placeholder="命名 · 可 @ 引用"
                  maxlength="24"
                  :title="`给该参考图命名（如角色名/场景名）：提示词 Agent 按名称绑定，提交时按当前顺序重编 @图片N`"
                  draggable="false"
                  @mousedown="dragLock = i"
                  @mouseup="dragLock = -1"
                  @blur="dragLock = -1"
                />
              </div>
              <button v-if="refImages.length < MAX_REFS" class="ref-add" :disabled="uploadingRef" title="上传参考图" @click="refFileInput?.click()">
                <LoaderCircle v-if="uploadingRef" :size="18" class="spin" />
                <Plus v-else :size="18" />
              </button>
            </div>
            <input ref="refFileInput" class="sr-only" type="file" accept="image/*" multiple @change="chooseRefFiles" />
          </section>

          <!-- 分镜描述：创作动线放在「参考图 → 描述 → 成片」的中间位 -->
          <section class="panel desc-panel">
            <div class="inspector-head">
              <span class="inspector-label">分镜描述</span>
            </div>
            <MentionTextarea
              v-model="description"
              :options="mentionOptions"
              :rows="6"
              input-class="textarea shot-description"
              placeholder="用自己的话描述镜头的画面与运动；输入 @ 可选择已上传图片，例如：近景，@女主 缓缓回头，雨夜霓虹背景，镜头缓慢推近…"
              @select="attachMentionAsset"
            />
          </section>

          <!-- 成片预览 -->
          <section class="panel player-panel">
            <div class="player-head">
              <div class="player-head-info">
                <span class="player-title">{{ isNew || !selected ? '新镜头' : '#' + String(selected.id).padStart(3, '0') }}</span>
                <span v-if="!isNew && selected" :class="['shot-row-state', 'is-' + taskState(selected)]">
                  <span :class="['dot', taskState(selected) === 'done' && 'ok', taskState(selected) === 'pending' && 'pending']" />
                  {{ taskStatusLabel(selected) }}
                </span>
                <span v-if="!isNew && selected && isPrivilegedView && selected.ownerName" class="shot-row-owner" :title="'发起人：' + selected.ownerName">{{ selected.ownerId === myId ? '我的' : selected.ownerName }}</span>
              </div>
              <template v-if="!isNew && selected">
                <button v-if="canRecall(selected)" class="btn btn-sm" :disabled="recallingIds.includes(selected.id)" @click="recallTask(selected)">
                  <RefreshCw :size="11" :class="{ spin: recallingIds.includes(selected.id) }" /> {{ recallingIds.includes(selected.id) ? '召回中…' : '召回视频' }}
                </button>
                <button v-if="canCancel(selected)" class="btn btn-sm" title="取消任务（排队/生成中，仅本地 MiniMax 队列支持）" @click="cancelTask(selected)">
                  <Square :size="10" fill="currentColor" /> 取消
                </button>
                <a v-if="selected.status === 'completed' && taskSrc(selected)" class="btn btn-sm" :href="taskSrc(selected)" :download="`shot-${selected.id}.mp4`">
                  <Download :size="11" /> 下载
                </a>
                <button class="btn btn-sm" title="删除该镜头任务" @click="taskToDelete = selected"><Trash2 :size="11" /></button>
              </template>
            </div>
            <div class="player-stage">
              <video
                v-if="!isNew && selected && selected.status === 'completed' && taskSrc(selected)"
                :key="taskSrc(selected)"
                :src="taskSrc(selected)"
                :poster="posterOf(taskSrc(selected)) || undefined"
                controls
                preload="metadata"
                playsinline
                class="player-video"
              />
              <div v-else class="player-empty">
                <LoaderCircle v-if="!isNew && selected && isActive(selected)" :size="20" class="spin" />
                <Video v-else :size="20" />
                <div class="player-empty-copy">
                  <div class="player-empty-title">
                    {{ !isNew && selected && isActive(selected)
                      ? (selected.status === 'pending' ? `排队中${selected.queueAhead != null ? ' · 前面还有 ' + selected.queueAhead + ' 个' : ''}` : '视频生成中…')
                      : (isNew || !selected ? '生成结果在这里播放' : '该镜头还没有可播放的结果') }}
                  </div>
                  <div v-if="!isNew && selected && selected.status === 'failed'" class="player-empty-desc is-error">{{ selected.errorMsg || '生成失败' }}</div>
                </div>
              </div>
            </div>
          </section>
        </div>

        <!-- 分镜编辑器：左描述+提示词，右参数列 -->
        <section class="panel shot-editor">
          <div class="editor-texts">
            <div class="editor-field prompt-field">
              <div class="inspector-head">
                <span class="inspector-label">视频提示词</span>
                <button class="btn btn-sm prompt-gen-btn" :disabled="promptGenerating" title="用视频提示词 Agent 把描述改写为模型最佳提示词" @click="genPrompt">
                  <LoaderCircle v-if="promptGenerating" :size="11" class="spin" />
                  <Wand2 v-else :size="11" />
                  {{ promptGenerating ? '生成中…' : '生成提示词' }}
                </button>
              </div>
              <MentionTextarea
                v-model="videoPrompt"
                :options="mentionOptions"
                :rows="10"
                input-class="textarea shot-prompt"
                placeholder="提交生成时优先使用该提示词；输入 @ 可选择当前参考图或媒体资产库图片。也可点「生成提示词」让 Agent 按目标模型最佳表述改写。"
                @select="attachMentionAsset"
              />
            </div>
          </div>
          <div class="editor-params">
            <ModelSelect
              v-model="videoModel"
              label="视频模型"
              :options="videoModelOptions"
              :show-config="videoModelMultiCfg"
              default-label="默认（设置页启用配置）"
            />
            <label class="param-field">
              <span class="inspector-label">时长 {{ duration }}s</span>
              <input v-model.number="duration" type="range" min="2" max="15" step="1" class="duration-slider" />
            </label>
            <label class="param-field">
              <span class="inspector-label">比例</span>
              <BaseSelect v-model="aspectRatio" :options="aspectOptions" :searchable="false" />
            </label>
            <label v-if="effectiveProvider !== 'doubao_local'" class="param-field">
              <span class="inspector-label">分辨率</span>
              <BaseSelect v-model="resolution" :options="resolutionOptions" :searchable="false" />
            </label>
            <label v-if="audioCapable" class="audio-check" title="由视频模型直接配音（火山/阿里 Wan 支持；MiniMax H3 工作流不配音，自动隐藏）">
              <input v-model="generateAudio" type="checkbox" />
              生成音频
            </label>
            <button class="btn btn-primary generate-video" :disabled="submitting" @click="generate">
              <LoaderCircle v-if="submitting" :size="14" class="spin" />
              <Clapperboard v-else :size="14" />
              {{ !isNew && selected ? '重新生成' : '生成视频' }}
            </button>
          </div>
        </section>
      </div>
    </div>

    <!-- 参考图放大查看 -->
    <div v-if="refViewer.open" class="ref-viewer-overlay" @click.self="refViewer.open = false">
      <div class="ref-viewer-dialog">
        <div class="ref-viewer-head">
          <span class="ref-viewer-title">{{ refViewer.title }}</span>
          <button class="btn btn-icon btn-sm" title="关闭" @click="refViewer.open = false"><X :size="14" /></button>
        </div>
        <img class="ref-viewer-img" :src="refViewer.src" :alt="refViewer.title" />
      </div>
    </div>

    <ConfirmDialog
      :open="!!taskToDelete"
      title="删除镜头任务"
      :message="taskToDelete && isActive(taskToDelete)
        ? '该任务正在生成中：删除记录后列表不再显示，但已提交的生成仍会跑完、文件会落盘。删除不可恢复。'
        : '删除后该条任务记录将不可恢复（已生成的文件仍会保留在磁盘）。'"
      :loading="deleting"
      @confirm="confirmDelete"
      @cancel="taskToDelete = null"
    />
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { toast } from 'vue-sonner'
import { toastError } from '~/composables/useToast'
import {
  ArrowLeft, Clapperboard, Download, LoaderCircle, Plus, RefreshCw, Square, Trash2, Video, Wand2, X,
} from 'lucide-vue-next'
import { aiConfigAPI, assetAPI, labAPI, stylePresetAPI, taskAPI, uploadAPI, userAPI } from '~/composables/useApi'
import { posterOf, thumbOf } from '~/composables/useMedia'
import { useAuth } from '~/composables/useAuth'
import { LEGACY_LAB_DRAFT_KEY, labDraftStorageKey } from '~/utils/labDraft'
import ModelSelect from '~/components/ModelSelect.vue'
import BaseSelect from '~/components/BaseSelect.vue'
import MentionTextarea from '~/components/MentionTextarea.vue'
import ConfirmDialog from '~/components/ConfirmDialog.vue'

const MAX_REFS = 9

// 各厂商可选分辨率档位（与 episode.vue RESOLUTION_TIERS 同步）
const RESOLUTION_TIERS = {
  volcengine: ['480p', '720p'],
  minimax: ['720p', '1080p'],
  aliyun: ['480p', '720p', '1080p'],
}

// ── 输入区状态（选中镜头时回填该镜参数，编辑后重新生成） ──
const refImages = ref([])           // [{ path, url, name }] 顺序即提交给视频模型的 reference 顺序，name 用于 @图片N 重编
const libraryImages = ref([])       // 媒体资产库图片；@ 选择时按需加入 refImages，不预占 9 张配额
const refFileInput = ref(null)
const uploadingRef = ref(false)
const dragFrom = ref(-1)
// Chrome 已知问题：draggable 父元素内的 input 无法选中文字，按下命名框时临时关掉该格拖拽
const dragLock = ref(-1)
const description = ref('')         // 自己的分镜描述（提示词 Agent 的输入）
const videoPrompt = ref('')         // 提交生成时优先使用的提示词
const stylePrefix = ref('')         // 风格前缀文本（从预设导入或手编）；'' = 不加前缀
const legacyDraftStyle = ref('')    // 旧草稿里的预设 value（restoreDraft 早于预设加载，暂存待升级）

// ── 风格前缀：与工作台「视频制作」同款交互——自由文本编辑，预设只作为导入源（工作台是项目级覆盖，这里无项目、纯本地草稿） ──
const stylePresets = ref([])
const STYLE_PRESET_NONE = '__none__'
const stylePresetOptions = computed(() => [
  { label: '无风格（清空）', value: STYLE_PRESET_NONE },
  ...stylePresets.value.map(p => ({ label: p.name, value: p.id })),
])
const presetPrefixOf = (preset) => preset?.video_prompt || preset?.prompt || ''
function importStylePrefix(presetId) {
  if (presetId === STYLE_PRESET_NONE) { stylePrefix.value = ''; return }
  const preset = stylePresets.value.find(p => String(p.id) === String(presetId))
  if (!preset) return
  stylePrefix.value = presetPrefixOf(preset)
  toast.success(`已导入「${preset.name}」的风格前缀，可继续编辑`)
}
const duration = ref(5)
const aspectRatio = ref('16:9')
const resolution = ref('720p')
const generateAudio = ref(true)
const submitting = ref(false)
const promptGenerating = ref(false)
const auth = useAuth()
void auth.ensureStatus()

const aspectOptions = ['16:9', '9:16', '1:1', '4:3', '3:4'].map(v => ({ label: v, value: v }))

// ── 编辑态草稿持久化（localStorage）：刷新/误关不再丢未提交的镜头 ─────
// 只存编辑语义，不存模型选择（复合键里的 configId 可能已失效，宁可回退默认配置）
function draftStorageKey() {
  return labDraftStorageKey(auth.status.value?.enabled, auth.user.value?.id)
}
function saveDraft() {
  try {
    const key = draftStorageKey()
    if (!key) return
    localStorage.setItem(key, JSON.stringify({
      description: description.value,
      videoPrompt: videoPrompt.value,
      refImages: refImages.value.map(r => ({ path: r.path, url: r.url, name: String(r.name || '') })),
      duration: duration.value,
      aspectRatio: aspectRatio.value,
      resolution: resolution.value,
      generateAudio: generateAudio.value,
      stylePrefix: stylePrefix.value,
    }))
  } catch { /* 隐私模式等存储不可用：丢草稿可接受，不弹错 */ }
}
let draftTimer = null
watch([description, videoPrompt, refImages, duration, aspectRatio, resolution, generateAudio, stylePrefix], () => {
  clearTimeout(draftTimer)
  draftTimer = setTimeout(saveDraft, 400)
}, { deep: true })
function restoreDraft() {
  try {
    const key = draftStorageKey()
    if (!key) return

    let raw = localStorage.getItem(key)
    if (auth.status.value?.enabled === false && !raw) {
      // 只有无用户体系的单机模式才能安全承接旧共享草稿。
      raw = localStorage.getItem(LEGACY_LAB_DRAFT_KEY)
      if (raw) localStorage.setItem(key, raw)
    }
    // 旧 key 无法判断归属：多用户模式下不恢复给任何账号，并清除以防后续账号读到。
    localStorage.removeItem(LEGACY_LAB_DRAFT_KEY)
    if (!raw) return
    const d = JSON.parse(raw)
    if (Array.isArray(d.refImages)) {
      refImages.value = d.refImages
        .filter(r => r && r.path)
        .map(r => ({ path: r.path, url: r.url || r.path, name: String(r.name || '') }))
    }
    description.value = String(d.description || '')
    videoPrompt.value = String(d.videoPrompt || '')
    if (Number(d.duration) >= 2 && Number(d.duration) <= 15) duration.value = Number(d.duration)
    if (d.aspectRatio) aspectRatio.value = String(d.aspectRatio)
    if (d.resolution) resolution.value = String(d.resolution)
    generateAudio.value = d.generateAudio !== false && d.generateAudio !== 0
    stylePrefix.value = String(d.stylePrefix || '')
    // 旧草稿存的是预设 value（stylePreset）：预设此时还没加载，暂存等 onMounted 加载后升级
    if (!stylePrefix.value && d.stylePreset) legacyDraftStyle.value = String(d.stylePreset)
  } catch { /* 草稿损坏按全新状态开始 */ }
}

// ── 视频模型选择（与工作台同款复合键 configId/provider/model） ──
const videoConfigs = ref([])
const videoModel = ref('')

function configModels(cfg) {
  const raw = cfg?.model
  if (!raw) return []
  if (Array.isArray(raw)) return raw.filter(Boolean)
  try { const m = JSON.parse(raw); return Array.isArray(m) ? m.filter(Boolean) : [m].filter(Boolean) } catch { return [raw].filter(Boolean) }
}
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
function bareModelName(key) {
  if (!key) return ''
  const parts = key.split('/')
  if (/^\d+$/.test(parts[0]) && parts.length >= 3) return parts.slice(2).join('/')
  return parts.length >= 2 ? parts.slice(1).join('/') : key
}

const videoModelOptions = computed(() => collectModelOptions(videoConfigs.value))
const videoModelMultiCfg = computed(() => new Set(videoModelOptions.value.map(o => o.configId)).size > 1)
const selectedModelOption = computed(() => videoModelOptions.value.find(o => o.key === videoModel.value) || null)
// 未选模型时后端回退到用户启用配置；分辨率档位按其 provider 猜测（取优先级最高的启用配置）
const effectiveProvider = computed(() => {
  if (selectedModelOption.value) return selectedModelOption.value.provider
  const first = [...videoConfigs.value].filter(c => c.is_active && c.service_enabled !== false).sort((a, b) => (b.priority || 0) - (a.priority || 0))[0]
  return first?.provider || ''
})
const resolutionOptions = computed(() => {
  const tiers = RESOLUTION_TIERS[effectiveProvider.value] || ['480p', '720p', '1080p']
  return tiers.map(v => ({ label: v, value: v }))
})
// 仅火山/阿里 Wan 的适配器消费 generate_audio；MiniMax H3 等工作流不配音，隐藏无效开关
const audioCapable = computed(() => ['volcengine', 'aliyun'].includes(effectiveProvider.value))
function pruneResolution() {
  if (!resolutionOptions.value.some(o => o.value === resolution.value)) {
    resolution.value = resolutionOptions.value.some(o => o.value === '720p') ? '720p' : resolutionOptions.value[0]?.value || ''
  }
}
watch(effectiveProvider, pruneResolution)

// ── 任务列表 / 选中 / 轮询 ─────────────────────────────────
// 管理员视角：后端对管理员返回全部用户的镜头任务（与全自动任务队列同款能力），
// 列表标注发起人并支持「只看我的」筛选，取消/删除/重新生成均可代管
const isPrivilegedView = computed(() => auth.status.value?.enabled === false || !!auth.user.value?.isAdmin || !!auth.user.value?.isSubAdmin)
const myId = computed(() => auth.user.value?.id ?? -1)
// '' = 全部用户，'mine' = 我的，其余为具体用户 id（右上角下拉）
const ownerScope = ref('')

// 下拉候选 = 全量用户列表，没有任务的用户也能选；
// 接口未就绪时先按任务派生兜底，单机免登录模式（无用户体系）不显示候选
const allUsers = ref([])
async function loadAllUsers() {
  try { allUsers.value = await userAPI.list() } catch { /* 失败退化为任务派生 */ }
}
watch(isPrivilegedView, (v) => { if (v && !allUsers.value.length) void loadAllUsers() }, { immediate: true })
const ownerOptions = computed(() => {
  let users
  if (allUsers.value.length) {
    users = allUsers.value.map(u => ({ value: u.id, label: u.displayName || u.username }))
  } else {
    const seen = new Map()
    for (const t of tasks.value) {
      if (t.ownerId == null || seen.has(t.ownerId)) continue
      seen.set(t.ownerId, t.ownerName || `用户 #${t.ownerId}`)
    }
    users = [...seen.entries()].map(([value, label]) => ({ value, label }))
  }
  users.sort((a, b) => String(a.label).localeCompare(String(b.label), 'zh-Hans-CN'))
  return [
    { value: '', label: '全部用户' },
    { value: 'mine', label: '我的' },
    ...users,
  ]
})

const tasks = ref([])
const selected = ref(null)
const isNew = ref(true)
const loadingHistory = ref(true)
const listFilter = ref('')
const deleting = ref(false)
const taskToDelete = ref(null)
let pollTimer = null

const isActive = (t) => ['pending', 'processing', 'canceling'].includes(t.status)
function taskState(t) {
  if (t.status === 'completed') return 'done'
  if (t.status === 'failed') return 'failed'
  if (t.status === 'canceled') return 'canceled'
  return 'pending'
}
function taskStatusLabel(t) {
  if (t.status === 'completed') return '已完成'
  if (t.status === 'failed') return '失败'
  if (t.status === 'canceled') return '已取消'
  if (t.status === 'pending') return t.queueAhead != null ? `排队中（前面 ${t.queueAhead} 个）` : '排队中'
  if (t.status === 'canceling') return '取消中'
  return '生成中'
}

// ── 取消：与分镜精细制作同款语义——仅 MiniMax（本地 ComfyUI 队列）任务支持，后端路由同此约束 ──
const cancelingIds = ref([])
const recallingIds = ref([])
function canCancel(t) {
  return String(t?.provider || '').toLowerCase() === 'minimax'
    && (t.status === 'pending' || t.status === 'processing')
    && !cancelingIds.value.includes(t.id)
}
async function cancelTask(t) {
  if (!canCancel(t)) return
  // 点击后立即进入取消中：隐藏按钮防重复请求，失败回滚
  cancelingIds.value = [...cancelingIds.value, t.id]
  const previous = t.status
  t.status = 'canceling'
  try {
    await taskAPI.cancel(t.id)
    toast.success('任务已取消')
  } catch (e) {
    t.status = previous
    toastError(e)
  } finally {
    cancelingIds.value = cancelingIds.value.filter(id => id !== t.id)
    void loadHistory()
  }
}

function canRecall(t) {
  return String(t?.provider || '').toLowerCase() === 'doubao_local'
    && t?.status === 'failed'
    && !!(t?.taskId || t?.task_id)
}
async function recallTask(t) {
  if (!canRecall(t) || recallingIds.value.includes(t.id)) return
  if (!window.confirm('将使用原豆包 job_id 查询并召回视频，不会重新提交生成任务。确认继续？')) return
  recallingIds.value = [...recallingIds.value, t.id]
  try {
    const recalled = await taskAPI.recallVideo(t.id)
    Object.assign(t, recalled || {})
    if (selected.value?.id === t.id) selected.value = { ...selected.value, ...(recalled || {}) }
    toast.success(recalled?.status === 'completed' ? '视频已召回并保存' : '已恢复原任务状态，继续等待豆包结果')
    await loadHistory()
  } catch (e) {
    toastError(e)
  } finally {
    recallingIds.value = recallingIds.value.filter(id => id !== t.id)
  }
}

// 管理员按发起人收口：先按用户筛选，再做状态筛选（状态计数同口径）
const scopedTasks = computed(() => {
  if (!isPrivilegedView.value || ownerScope.value === '') return tasks.value
  if (ownerScope.value === 'mine') return tasks.value.filter(t => t.ownerId === myId.value)
  return tasks.value.filter(t => t.ownerId === ownerScope.value)
})
const filteredTasks = computed(() => {
  if (!listFilter.value) return scopedTasks.value
  return scopedTasks.value.filter(t => taskState(t) === listFilter.value)
})
function countBy(state) { return scopedTasks.value.filter(t => taskState(t) === state).length }
function toggleFilter(state) { listFilter.value = listFilter.value === state ? '' : state }

async function loadHistory() {
  try {
    const rows = await taskAPI.list({ type: 'video', scope: 'standalone' })
    tasks.value = (rows || []).sort((a, b) => b.id - a.id)
    if (selected.value) {
      const fresh = tasks.value.find(t => t.id === selected.value.id)
      if (fresh && !isNew.value) selected.value = fresh
    }
  } catch (e) {
    toastError(e)
  } finally {
    loadingHistory.value = false
  }
}

function startPolling() {
  pollTimer = setInterval(async () => {
    const active = tasks.value.filter(isActive)
    if (!active.length) return
    await Promise.all(active.map(async (t) => {
      try {
        const fresh = await taskAPI.get(t.id)
        if (fresh) Object.assign(t, fresh)
        if (selected.value?.id === fresh?.id && !isNew.value) selected.value = { ...selected.value, ...fresh }
      } catch { /* 单次轮询失败等下一轮 */ }
    }))
  }, 3000)
}

onMounted(async () => {
  restoreDraft()
  void loadHistory()
  void loadLibraryImages()
  startPolling()
  try {
    videoConfigs.value = await aiConfigAPI.list('video')
    pruneResolution()
  } catch (e) {
    toastError(e)
  }
  try {
    stylePresets.value = await stylePresetAPI.list()
  } catch { /* 拉不到预设只是没有导入源，前缀仍可手编 */ }
  if (legacyDraftStyle.value) {
    const preset = stylePresets.value.find(p => p.value === legacyDraftStyle.value)
    if (preset) stylePrefix.value = presetPrefixOf(preset)
    legacyDraftStyle.value = ''
  }
})
onBeforeUnmount(() => { if (pollTimer) clearInterval(pollTimer) })

// ── 选中镜头：回填该镜参数到编辑器（同工作台点行编辑语义） ──
function taskParams(t) {
  try { return JSON.parse(t.params || '{}') } catch { return {} }
}
const taskSrc = (t) => (t.localPath ? '/' + t.localPath : t.resultUrl || '')
const taskDuration = (t) => taskParams(t).duration || '?'

// 列表行摘要：优先分镜描述（人话标题）；旧任务没有描述时，六段提示词退到参考图命名拼接
function taskPreview(t) {
  const params = taskParams(t)
  const desc = String(params.description || '').trim()
  if (desc) return desc
  const p = String(t.prompt || '').trim()
  const labels = (params.referenceImageLabels || []).map(s => String(s || '').trim()).filter(Boolean)
  if (/^subject_definitions/i.test(p)) {
    return labels.join(' · ') || '（六段提示词镜头）'
  }
  return p || labels.join(' · ') || '（仅参考图）'
}

function selectTask(task) {
  selected.value = task
  isNew.value = false
  const params = taskParams(task)
  // 描述原文随任务落库（params.description）：点回历史镜头要完整回填，可改后重新生成
  // （更早的没有存描述的任务回填为空，提示词框仍有全文可读）
  description.value = String(params.description || '')
  videoPrompt.value = task.prompt || ''
  duration.value = params.duration || 5
  aspectRatio.value = params.aspectRatio || '16:9'
  resolution.value = params.resolution || '720p'
  generateAudio.value = params.generateAudio !== 0
  stylePrefix.value = String(params.stylePrefix || '')
  videoModel.value = ''
  const known = new Set(videoModelOptions.value.map(o => o.model))
  if (task.model && known.has(task.model)) {
    videoModel.value = videoModelOptions.value.find(o => o.model === task.model)?.key || ''
  }
  // 命名随任务 params 持久化；更早的任务没存过命名，重新命名即可恢复重编
  const labels = Array.isArray(params.referenceImageLabels) ? params.referenceImageLabels : []
  refImages.value = (params.referenceImageUrls || []).map((p, i) => ({ path: p, url: p, name: String(labels[i] || '') }))
}

function startNew() {
  selected.value = null
  isNew.value = true
  description.value = ''
  videoPrompt.value = ''
  refImages.value = []
  duration.value = 5
  videoModel.value = ''
}

// ── 参考图上传 ─────────────────────────────────────────────
async function uploadRefFiles(files) {
  const imgs = [...files].filter(f => f.type.startsWith('image/')).slice(0, MAX_REFS - refImages.value.length)
  if (!imgs.length) return
  uploadingRef.value = true
  try {
    // 并行上传（串行会把每张的耗时加总）；结果按选择顺序落位——参考图顺序即视频模型的引用顺序
    // 初始名称用文件名（去扩展名），用户可改；名称是 @图片N 重编的锚点，别默认空着
    const results = await Promise.all(imgs.map(async (f) => {
      try {
        const res = await uploadAPI.image(f)
        return { path: res.path, url: res.url || res.path, name: fileBaseName(f.name) }
      } catch {
        return null
      }
    }))
    const ok = results.filter(Boolean)
    refImages.value.push(...ok)
    const failed = imgs.filter((_, i) => !results[i]).map(f => f.name)
    if (failed.length) toast.error(`${failed.length} 张参考图上传失败：${failed.join('、')}`)
  } finally {
    uploadingRef.value = false
  }
}
function chooseRefFiles(e) {
  void uploadRefFiles(e.target.files || [])
  e.target.value = ''
}
// 文件名去扩展名作参考图初始命名（与命名框 maxlength 对齐截断）
function fileBaseName(name) {
  return String(name || '').replace(/\.[^.]+$/, '').trim().slice(0, 24)
}

function reorderRef(to) {
  const from = dragFrom.value
  dragFrom.value = -1
  if (from < 0 || from === to) return
  const [moved] = refImages.value.splice(from, 1)
  refImages.value.splice(to, 0, moved)
}

// 参考图放大查看：用原图 url（列表格子里才是缩略图）
const refViewer = ref({ open: false, src: '', title: '' })
function openRefViewer(img, i) {
  refViewer.value = {
    open: true,
    src: img.url,
    title: `参考图 ${i + 1}${String(img.name || '').trim() ? ' · ' + String(img.name).trim() : ''}`,
  }
}
function onRefViewerKeydown(e) {
  if (e.key === 'Escape' && refViewer.value.open) refViewer.value.open = false
}
onMounted(() => window.addEventListener('keydown', onRefViewerKeydown))
onBeforeUnmount(() => window.removeEventListener('keydown', onRefViewerKeydown))

// 与工作台 resolveVideoPromptRefs 同一机制：提交时按 refImages 当前顺序重编 @图片N。
// Agent 产出的 `@图片N名称` 或用户手写的 `@名称` 都归一化为 `@图片K名称`（K=该名称当前序号），
// 提示词生成后再拖拽排序 / 删图 / 改挂都不会让引用悄悄挂错图；未命名的引用原样保留。
function resolvePromptRefs(prompt) {
  const named = refImages.value
    .map((r, i) => ({ name: String(r.name || '').trim(), idx: i + 1 }))
    .filter(r => r.name)
    .sort((a, b) => b.name.length - a.name.length)
  if (!named.length) return prompt
  return prompt.replace(/@(?:图片\d+)?([^\s@，。；、！？：；「」『』“”（）()\n]+)/g, (m, rest) => {
    for (const { name, idx } of named) {
      if (rest.startsWith(name)) return `@图片${idx}${name}${rest.slice(name.length)}`
    }
    return m
  })
}

function uniqueMentionName(rawName, usedNames) {
  const base = fileBaseName(rawName) || '资产图片'
  let candidate = base
  let suffix = 2
  while (usedNames.has(candidate)) {
    const tail = `-${suffix++}`
    candidate = `${base.slice(0, Math.max(1, 24 - tail.length))}${tail}`
  }
  usedNames.add(candidate)
  return candidate
}

async function loadLibraryImages() {
  try {
    libraryImages.value = (await assetAPI.list()).filter(asset => asset.type === 'image' && (asset.url || asset.local_path))
  } catch { /* 资产库不可用时仍可使用本次上传的参考图 */ }
}

// @ 引用候选：当前参考图 + 媒体资产库图片。资产库候选仅在仍有参考图配额时出现；
// 选择后 attachMentionAsset 会把它加入 refImages，确保文字引用与实际提交图片始终绑定。
const mentionOptions = computed(() => {
  const usedNames = new Set()
  const attachedUrls = new Set(refImages.value.flatMap(r => [r.path, r.url].filter(Boolean)))
  const current = refImages.value
    .map((r, i) => ({ name: String(r.name || '').trim(), idx: i + 1, url: r.url }))
    .filter(r => r.name)
    .map(r => {
      usedNames.add(r.name)
      return {
        label: `${r.name} · 图${r.idx}`,
        value: r.name,
        kind: 'image',
        group: '当前参考图',
        image: thumbOf(r.url),
      }
    })

  if (refImages.value.length >= MAX_REFS) return current
  const library = libraryImages.value
    .filter(asset => !attachedUrls.has(asset.url) && !attachedUrls.has(asset.local_path))
    .map(asset => {
      const name = uniqueMentionName(asset.name || `资产图片${asset.id}`, usedNames)
      return {
        label: name,
        value: name,
        kind: 'image',
        group: '媒体资产库',
        image: thumbOf(asset.thumbnail_url || asset.url || asset.local_path),
        libraryAsset: asset,
      }
    })
  return [...current, ...library]
})

function attachMentionAsset(option) {
  const asset = option?.libraryAsset
  if (!asset) return
  if (refImages.value.length >= MAX_REFS) {
    toast.warning(`参考图最多 ${MAX_REFS} 张`)
    return
  }
  const path = asset.url || asset.local_path
  if (!path || refImages.value.some(ref => ref.path === path || ref.url === path)) return
  refImages.value.push({ path, url: asset.url || asset.local_path, name: String(option.value || '').slice(0, 24) })
  toast.success(`已从媒体资产库加入「${option.value}」`)
}

// ── 视频提示词生成（Agent 改写描述，正文回填提示词框） ─────
async function genPrompt() {
  if (!description.value) {
    toast.error('请先填写分镜描述，再生成提示词')
    return
  }
  promptGenerating.value = true
  try {
    const res = await labAPI.generateVideoPrompt({
      description: description.value,
      duration: duration.value,
      aspect_ratio: aspectRatio.value,
      resolution: resolution.value || undefined,
      model: bareModelName(videoModel.value) || undefined,
      config_id: selectedModelOption.value?.configId,
      reference_count: refImages.value.length,
      references: refImages.value.map((r, i) => ({ index: i + 1, name: String(r.name || '').trim() })),
      // 生成结果直接拼当前前缀文本（预设导入或手编），所见即所得
      style_prefix: stylePrefix.value.trim() || undefined,
    })
    if (res?.video_prompt) {
      videoPrompt.value = res.video_prompt
      toast.success('提示词已生成，可继续编辑后提交')
    }
  } catch (e) {
    toastError(e)
  } finally {
    promptGenerating.value = false
  }
}

// ── 生成 / 重新生成 / 删除 ─────────────────────────────────
async function generate() {
  const referenceImageUrls = refImages.value.map(r => r.path)
  // 提示词优先，且提交前按当前参考图顺序重编 @图片N；描述直通时无需解析
  const submittedPrompt = String(videoPrompt.value ? resolvePromptRefs(videoPrompt.value) : description.value || '').trim()
  if (!submittedPrompt && !referenceImageUrls.length) {
    toast.error('请至少提供分镜描述或一张参考图')
    return
  }
  submitting.value = true
  try {
    const task = await taskAPI.generate({
      type: 'video',
      prompt: submittedPrompt,
      // 描述原文随任务落库：六段提示词不可读，列表标题靠它
      description: String(description.value || '').trim() || undefined,
      reference_image_urls: referenceImageUrls,
      reference_image_labels: refImages.value.map(r => String(r.name || '').trim()),
      duration: duration.value,
      aspect_ratio: aspectRatio.value,
      resolution: resolution.value || undefined,
      generate_audio: audioCapable.value ? generateAudio.value : undefined,
      // 风格前缀文本：后端拼在提示词最前（幂等判重），并随 params 落库供重生成/回填复用
      style_prefix: stylePrefix.value.trim() || undefined,
      model: bareModelName(videoModel.value) || undefined,
      config_id: selectedModelOption.value?.configId,
    })
    if (task?.id) {
      tasks.value.unshift(task)
      selected.value = task
      isNew.value = false
    }
    toast.success('已提交生成任务')
  } catch (e) {
    toastError(e)
  } finally {
    submitting.value = false
  }
}

// 列表行内按钮：按该镜原有参数直接再提交一单，不动当前编辑区
async function regen(task) {
  const params = taskParams(task)
  try {
    const fresh = await taskAPI.generate({
      type: 'video',
      prompt: task.prompt || '',
      description: taskParams(task).description || undefined,
      reference_image_urls: params.referenceImageUrls || [],
      reference_video_urls: params.referenceVideoUrls || [],
      reference_audio_urls: params.referenceAudioUrls || [],
      duration: params.duration || undefined,
      aspect_ratio: params.aspectRatio || undefined,
      resolution: params.resolution || undefined,
      generate_audio: params.generateAudio !== 0,
      config_id: task.configId || task.config_id || undefined,
      // 原任务的前缀照旧：task.prompt 已含前缀，后端幂等判重不会二次拼接
      style_prefix: params.stylePrefix || undefined,
      model: task.model || undefined,
    })
    if (fresh?.id) tasks.value.unshift(fresh)
    toast.success('已按原参数重新提交')
  } catch (e) {
    toastError(e)
  }
}

async function confirmDelete() {
  if (!taskToDelete.value) return
  deleting.value = true
  try {
    await taskAPI.del(taskToDelete.value.id)
    tasks.value = tasks.value.filter(t => t.id !== taskToDelete.value.id)
    if (selected.value?.id === taskToDelete.value.id) startNew()
    taskToDelete.value = null
  } catch (e) {
    toastError(e)
  } finally {
    deleting.value = false
  }
}
</script>

<style scoped>
/* 页面整体 */
.shot-playground {
  height: 100dvh;
  min-height: 620px;
  box-sizing: border-box;
  padding: 8px 18px 17px;
  display: grid;
  grid-template-rows: 52px minmax(0, 1fr);
  gap: 12px;
  overflow: hidden;
  background: var(--bg-base);
}

.lab-head { display: flex; align-items: center; gap: 10px; }
.back-btn {
  display: inline-flex; align-items: center; justify-content: center;
  width: 30px; height: 30px; border-radius: 50%; flex-shrink: 0;
  border: none; background: rgba(0,0,0,0.05); color: var(--text-1); cursor: pointer;
}
.back-btn:hover { background: var(--bg-hover); color: var(--text-0); }
.lab-title { margin: 0; font-size: 1rem; font-weight: 700; color: var(--text-0); }
.lab-sub { font-size: .75rem; color: var(--text-1); }
/* 管理员：右上角用户筛选下拉 */
.lab-owner-select { margin-left: auto; width: 180px; flex-shrink: 0; }

/* 顶部工作区：左任务栏 + 右内容区 */
/* 侧栏只承载浏览与切换，收窄让位于创作区 */
.shot-workspace {
  min-height: 0;
  display: grid;
  grid-template-columns: 320px minmax(0, 1fr);
  gap: 12px;
}

/* 右侧内容区 */
/* 预览行按视口高度伸缩：小屏不挤编辑器，大屏给足看片空间 */
.shot-main {
  min-width: 0;
  min-height: 0;
  display: grid;
  grid-template-rows: auto clamp(248px, 34dvh, 380px) minmax(0, 1fr);
  gap: 12px;
}

/* 视觉风格横条：紧凑一行，不与面板抢高度 */
.style-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}
.style-prefix-input { flex: 1; min-width: 160px; max-width: 560px; min-height: 0; height: 30px; }
.style-select { width: 190px; flex-shrink: 0; }
.style-hint { font-size: .65625rem; color: var(--text-1); opacity: .65; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

/* 参考图 / 分镜描述 / 成片预览 三栏 */
/* 播放器按 16:9 审看即可不抢宽，省下的空间给描述输入区 */
.shot-top-panels {
  min-width: 0;
  display: grid;
  grid-template-columns: minmax(240px, 1.1fr) minmax(320px, 1.6fr) minmax(300px, 1fr);
  gap: 12px;
}

.desc-panel { padding: 12px; display: flex; flex-direction: column; gap: 8px; min-width: 0; min-height: 0; }

.panel { border: 1px solid var(--border); border-radius: calc(var(--radius) * 1.4); background: var(--bg-1, var(--bg-base)); min-width: 0; }

/* 左侧任务栏 */
.task-sidebar {
  min-height: 0;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  border: 1px solid var(--border);
  border-radius: calc(var(--radius) * 1.4);
  background: var(--surface-raised, var(--bg-1, var(--bg-base)));
  box-shadow: var(--shadow-xs);
}

.task-list {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 10px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  background: var(--surface-muted, var(--bg-base));
}

/* 分镜编辑区域：左参数列 + 右文本区 */
.shot-editor {
  min-width: 0;
  min-height: 0;
  overflow-y: auto;
  padding: 12px;
  display: grid;
  grid-template-columns: minmax(0, 1fr) 236px;
  gap: 14px;
}

.editor-params {
  display: flex;
  flex-direction: column;
  gap: 12px;
  align-items: stretch;
  min-width: 0;
}
.editor-params .generate-video { margin-top: auto; }

.editor-texts {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-width: 0;
  min-height: 0;
}

/* 输入框：描述填满顶栏面板，提示词是主要编辑对象、吃掉编辑器剩余高度 */
:deep(.shot-description) {
  width: 100%;
  flex: 1;
  min-height: 0;
  box-sizing: border-box;
  resize: none;
}

.prompt-field { flex: 1; min-height: 96px; }

/* 作用于 MentionTextarea 的镜像层与输入层（两者共享该类，需 :deep 穿透子组件） */
:deep(.shot-prompt) {
  width: 100%;
  flex: 1;
  min-height: 96px;
  height: auto;
  box-sizing: border-box;
}

/* 主操作按钮 */
.generate-video {
  width: 100%;
  height: 34px;
  border: 0;
  border-radius: 18px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
}

/* 小屏幕 */
@media (max-width: 1100px) {
  .shot-workspace {
    grid-template-columns: 272px minmax(0, 1fr);
  }

  .shot-top-panels {
    grid-template-columns: minmax(0, 1fr);
    grid-template-rows: 150px 120px 210px;
  }

  .shot-main {
    grid-template-rows: auto auto minmax(0, 1fr);
    overflow-y: auto;
  }

  .style-select { width: 180px; }
  .style-hint { display: none; }

  .shot-editor {
    grid-template-columns: 1fr;
  }

  .shot-list-heading-row { flex-direction: column; }
  .shot-list-actions { width: 100%; }
  .shot-list-actions .shot-create { flex: 1; }

  .editor-params .generate-video { margin-top: 4px; }
}

/* ── 任务列表行 ── */
.shot-list-head {
  padding: 14px 14px 12px;
  display: flex;
  flex-direction: column;
  gap: 13px;
  border-bottom: 1px solid var(--border);
}
.shot-list-heading-row { display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; }
.shot-list-heading { min-width: 0; }
.shot-list-title { display: flex; align-items: center; gap: 7px; font-size: .875rem; font-weight: 700; color: var(--text-0); text-wrap: balance; }
.shot-list-count {
  min-width: 20px; height: 20px; padding: 0 6px; border-radius: var(--radius-pill, 999px);
  display: inline-flex; align-items: center; justify-content: center;
  background: var(--surface-muted, var(--bg-base)); color: var(--text-1); font-size: .65625rem; font-weight: 650;
}
.shot-list-meta { margin-top: 2px; font-size: .65625rem; color: var(--text-2, var(--text-1)); white-space: nowrap; }
.shot-list-actions { display: flex; align-items: center; gap: 6px; flex-shrink: 0; }
.shot-list-actions .shot-refresh { width: 30px; min-width: 30px; height: 30px; min-height: 30px; padding: 0; }
.shot-list-actions .shot-create { min-height: 30px; padding-inline: 11px; }
.shot-list-filters {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 3px;
  padding: 3px;
  border-radius: calc(var(--radius) + 2px);
  background: var(--surface-muted, var(--bg-base));
}
.shot-filter {
  appearance: none;
  min-width: 0;
  min-height: 28px;
  padding: 0 5px;
  border: 0;
  border-radius: var(--radius);
  background: transparent;
  color: var(--text-1);
  font: 600 .65625rem/1 var(--font-body);
  white-space: nowrap;
  cursor: pointer;
}
.shot-filter:hover { color: var(--text-0); background: var(--bg-hover); }
.shot-filter.on { color: var(--text-0); background: var(--surface-raised, var(--bg-1)); box-shadow: var(--shadow-xs); }
.shot-filter.is-pending.on { color: var(--warning); }
.shot-filter.is-done.on { color: var(--success); }
.shot-filter.is-failed.on { color: var(--error); }
.shot-filter:focus-visible { outline: none; box-shadow: 0 0 0 3px var(--button-focus); }

.shot-empty {
  min-height: 210px;
  padding: 32px 16px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  text-align: center;
  font-size: .71875rem;
  color: var(--text-2, var(--text-1));
}
.shot-empty strong { font-size: .78125rem; color: var(--text-0); font-weight: 650; }
.shot-empty .btn { margin-top: 4px; }

.shot-row {
  position: relative;
  display: grid;
  grid-template-columns: minmax(0, 1fr) 28px;
  align-items: stretch;
  gap: 2px;
  min-height: 82px;
  border: 1px solid var(--border);
  border-radius: calc(var(--radius) + 2px);
  background: var(--surface-raised, var(--bg-1));
  box-shadow: var(--shadow-xs);
}
.shot-row:hover { border-color: var(--border-strong); }
.shot-row.active { border-color: var(--accent); background: var(--accent-bg); }
.shot-row-select {
  appearance: none;
  width: 100%;
  min-width: 0;
  padding: 9px 4px 9px 9px;
  display: grid;
  grid-template-columns: 76px minmax(0, 1fr);
  align-items: center;
  gap: 10px;
  border: 0;
  border-radius: calc(var(--radius) + 1px);
  background: transparent;
  color: inherit;
  font: inherit;
  text-align: left;
  cursor: pointer;
}
.shot-row-select:focus-visible { outline: none; box-shadow: inset 0 0 0 2px var(--accent); }
.shot-row-preview { position: relative; width: 76px; aspect-ratio: 16/9; border-radius: var(--radius); overflow: hidden; background: var(--media-surface); }
.shot-row-preview video { width: 100%; height: 100%; object-fit: cover; display: block; }
.shot-row-empty { width: 100%; height: 100%; display: flex; align-items: center; justify-content: center; color: var(--text-1); background: var(--bg-2, var(--bg-base)); }
.shot-row-index { position: absolute; left: 4px; bottom: 4px; font-size: .5625rem; font-family: monospace; padding: 1px 5px; border-radius: 5px; background: rgba(0,0,0,.66); color: #fff; }
.shot-row-main { min-width: 0; display: flex; flex-direction: column; gap: 4px; }
.shot-row-name {
  min-height: 32px;
  display: -webkit-box;
  overflow: hidden;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  color: var(--text-0);
  font-size: .71875rem;
  font-weight: 650;
  line-height: 1.4;
  text-wrap: pretty;
}
.shot-row-meta-line { display: flex; align-items: center; gap: 6px; min-width: 0; }
.shot-row-detail { color: var(--text-2, var(--text-1)); font-size: .625rem; }
/* 管理员视角：发起人标注 + 视角切换 chip */
.shot-row-owner {
  max-width: 84px; overflow: hidden; padding: 1px 6px; border-radius: 999px;
  color: var(--text-2, var(--text-1)); background: var(--surface-muted, var(--surface-soft));
  font-size: .59375rem; text-overflow: ellipsis; white-space: nowrap;
}
.shot-row-provider { overflow: hidden; color: var(--text-2, var(--text-1)); font-size: .59375rem; text-overflow: ellipsis; white-space: nowrap; }
.truncate { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.shot-row-state {
  min-width: 0;
  padding: 2px 6px;
  border-radius: var(--radius-pill, 999px);
  display: inline-flex;
  align-items: center;
  gap: 4px;
  color: var(--text-1);
  background: var(--surface-muted, var(--bg-base));
  font-size: .59375rem;
  font-weight: 650;
  white-space: nowrap;
}
.shot-row-state.is-done { color: var(--success, #2e9e5b); }
.shot-row-state.is-failed { color: var(--error); }
.shot-row-state.is-pending { color: var(--warning, #d99024); }
.shot-row-state.is-canceled { color: var(--text-1); }
.dot { width: 5px; height: 5px; border-radius: 50%; background: currentColor; display: inline-block; flex-shrink: 0; }
.dot.pending { animation: lab-pulse 1.1s ease-in-out infinite; }
.shot-row-error { overflow: hidden; color: var(--error); font-size: .59375rem; text-overflow: ellipsis; white-space: nowrap; }
.shot-row-actions { padding: 8px 5px 8px 0; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 5px; }
.shot-row-action { width: 26px; min-width: 26px; height: 26px; min-height: 26px; padding: 0; opacity: .62; box-shadow: none; }
.shot-row-action:hover, .shot-row-action:focus-visible { opacity: 1; }
.shot-row-action.is-danger:hover, .shot-row-action.is-danger:focus-visible { color: var(--error); background: var(--error-bg); }
.tabular-nums { font-variant-numeric: tabular-nums; }

/* ── 参考图面板 ── */
.refs-panel { padding: 12px; display: flex; flex-direction: column; gap: 10px; min-height: 0; }
.inspector-label { font-size: .75rem; font-weight: 650; color: var(--text-1); display: inline-flex; align-items: center; gap: 6px; }
.inspector-head { display: flex; align-items: center; gap: 8px; }
.ref-hint { font-size: .625rem; color: var(--text-1); opacity: .65; margin-left: auto; text-align: right; }
.prompt-gen-btn { margin-left: auto; display: inline-flex; align-items: center; gap: 4px; }
.ref-grid { flex: 1; min-height: 0; overflow-y: auto; display: grid; grid-template-columns: repeat(auto-fill, minmax(96px, 1fr)); gap: 8px; align-content: start; }
.ref-item { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.ref-thumb { position: relative; aspect-ratio: 1; border-radius: var(--radius); overflow: hidden; border: 1px solid var(--border); cursor: zoom-in; }
.ref-thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
.ref-name {
  width: 100%; min-width: 0; box-sizing: border-box; text-align: center;
  font-size: .65625rem; padding: 2px 4px; border-radius: var(--radius);
  border: 1px solid var(--border); background: var(--bg-2, transparent); color: var(--text-0);
}
.ref-name:focus { outline: none; border-color: var(--accent); }
.ref-name::placeholder { color: var(--text-1); opacity: .55; }
.ref-tag { position: absolute; left: 3px; bottom: 3px; font-size: .5625rem; padding: 1px 5px; border-radius: 6px; background: rgba(0,0,0,.62); color: #fff; }
.ref-x {
  position: absolute; top: 3px; right: 3px; width: 18px; height: 18px; border-radius: 50%;
  display: inline-flex; align-items: center; justify-content: center;
  border: none; background: rgba(0,0,0,.62); color: #fff; cursor: pointer;
}
.ref-x:hover { background: var(--error, #d33); }
.ref-add {
  aspect-ratio: 1; border-radius: var(--radius); border: 1px dashed var(--border);
  background: transparent; color: var(--text-1); cursor: pointer;
  display: inline-flex; align-items: center; justify-content: center;
}
.ref-add:hover:not(:disabled) { border-color: var(--accent); color: var(--accent); }
.ref-add:disabled { opacity: .6; cursor: default; }

/* ── 成片预览面板 ── */
.player-panel { padding: 12px; display: flex; flex-direction: column; gap: 8px; min-height: 0; }
.player-head { display: flex; align-items: center; gap: 8px; min-height: 24px; }
.player-head-info { display: flex; align-items: center; gap: 8px; min-width: 0; flex: 1; }
.player-title { font-size: .78125rem; font-weight: 700; color: var(--text-0); }
.player-stage { flex: 1; min-height: 0; border-radius: var(--radius); overflow: hidden; background: var(--media-surface); }
.player-video { width: 100%; height: 100%; display: block; background: var(--media-surface); }
.player-empty {
  height: 100%; display: flex; flex-direction: column; gap: 8px; align-items: center; justify-content: center;
  color: var(--text-1); background: var(--bg-2, var(--bg-base)); padding: 8px; text-align: center;
}
.player-empty-copy { display: flex; flex-direction: column; gap: 4px; }
.player-empty-title { font-size: .75rem; color: var(--text-0); }
.player-empty-desc { font-size: .71875rem; color: var(--text-1); max-width: 320px; }
.player-empty-desc.is-error { color: var(--error); }

/* ── 编辑器 ── */
.editor-field { display: flex; flex-direction: column; gap: 6px; }
.textarea {
  border: 1px solid var(--border); border-radius: var(--radius); background: var(--bg-2, transparent);
  color: var(--text-0); font: inherit; font-size: .78125rem; line-height: 1.6; padding: 8px 10px;
}
.textarea:focus { outline: none; border-color: var(--accent); }
.param-field { min-width: 0; display: flex; flex-direction: column; gap: 6px; }
.duration-slider { width: 100%; accent-color: var(--accent); }
.audio-check { display: flex; align-items: center; gap: 7px; font-size: .78125rem; color: var(--text-1); cursor: pointer; }

/* 参考图放大查看 */
.ref-viewer-overlay {
  position: fixed; inset: 0; z-index: 60;
  background: rgba(0, 0, 0, 0.66); backdrop-filter: blur(2px);
  display: flex; align-items: center; justify-content: center; padding: 24px;
}
.ref-viewer-dialog {
  display: flex; flex-direction: column; gap: 8px;
  max-width: min(1100px, 94vw); max-height: 94dvh;
  background: var(--bg-1, #111); border: 1px solid var(--border); border-radius: calc(var(--radius) * 1.4);
  padding: 10px 12px 12px; min-width: 0;
}
.ref-viewer-head { display: flex; align-items: center; gap: 8px; }
.ref-viewer-title { font-size: .78125rem; font-weight: 650; color: var(--text-0); min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ref-viewer-head .btn { margin-left: auto; }
.ref-viewer-img {
  max-width: 100%; max-height: calc(94dvh - 64px); object-fit: contain;
  border-radius: var(--radius); background: var(--media-surface); display: block; margin: 0 auto; min-height: 0;
}

.sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
.spin { animation: lab-spin 1s linear infinite; }
@keyframes lab-spin { to { transform: rotate(360deg); } }
@keyframes lab-pulse { 0%, 100% { opacity: 1; } 50% { opacity: .3; } }
</style>
