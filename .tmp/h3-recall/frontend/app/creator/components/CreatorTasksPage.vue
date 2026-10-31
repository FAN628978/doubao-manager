<template>
  <div class="creator-hub">
    <div class="creator-page">
      <header class="creator-page-head">
        <div>
          <div class="creator-breadcrumb"><span>我的</span><ChevronRight :size="12" /><strong>我的任务</strong></div>
          <div class="creator-title-row"><h1>我的任务</h1><span class="creator-live-badge"><Activity :size="11" />实时数据</span></div>
          <p>最近提交的生成任务按时间倒序展示，状态每 5 秒自动更新。</p>
        </div>
        <div class="creator-head-actions">
          <span v-if="activeTaskCount > 0" class="creator-running-summary">进行中 <strong>{{ activeTaskCount }}</strong></span>
          <button class="btn" :disabled="loading" @click="handleRefresh"><RefreshCw :size="14" :class="{ spin: loading }" />刷新</button>
        </div>
      </header>

      <div class="creator-toolbar">
        <div class="creator-segments">
          <button v-for="item in filters" :key="item.id" :class="{ active: activeFilter === item.id }" @click="activeFilter = item.id">{{ item.title }}</button>
        </div>
        <span>已显示 {{ recentTasks.length }} / {{ total }} 条</span>
      </div>

      <section class="creator-panel creator-credit-table">
        <div class="creator-panel-head">
          <div><h2>最近任务</h2><p>包含图片、视频生成与爆款拆解记录</p></div>
          <span v-if="activeTaskCount > 0">{{ activeTaskCount }} 个正在进行</span>
        </div>
        <p v-if="error && recentTasks.length" class="creator-task-error" role="alert">{{ error }}</p>
        <div class="creator-table creator-task-table">
          <div class="head"><span>任务</span><span>类型</span><span>所属项目</span><span>提交时间</span><span>状态</span><span>操作</span></div>
          <div v-for="task in recentTasks" :key="task.id">
            <span><strong>#{{ task.id }}</strong><small>{{ task.model || task.provider || '未记录模型' }}</small></span>
            <span>{{ task.type === 'viral_breakdown' ? '爆款拆解' : task.type === 'video' ? '视频生成' : '图片生成' }}</span>
            <span>{{ task.dramaTitle || '独立任务' }}</span>
            <span>{{ formatTime(task.createdAt) }}</span>
            <span>
              <em class="creator-status" :class="statusTone(task.status)">
                <Clock3 v-if="task.status === 'pending'" :size="11" />
                <LoaderCircle v-else-if="task.status === 'processing'" class="spin" :size="11" />
                <Hourglass v-else-if="task.status === 'canceling'" :size="11" />
                <CircleCheck v-else-if="task.status === 'completed'" :size="11" />
                <CircleX v-else-if="task.status === 'failed'" :size="11" />
                <Ban v-else-if="task.status === 'canceled'" :size="11" />
                {{ statusLabel(task.status) }}
              </em>
            </span>
            <span class="creator-table-actions">
              <button v-if="canRecall(task)" class="creator-inline-btn" :disabled="recallingIds.includes(Number(task.id))" @click="recallTask(task)">
                <RefreshCw :size="12" :class="{ spin: recallingIds.includes(Number(task.id)) }" />{{ recallingIds.includes(Number(task.id)) ? '召回中' : '召回视频' }}
              </button>
              <button v-if="canCancel(task)" class="creator-inline-btn" @click="cancelTask(task)">取消任务</button>
            </span>
          </div>
        </div>
        <div v-if="!recentTasks.length" class="creator-empty creator-table-empty">
          <LoaderCircle v-if="loading" class="spin" :size="20" />
          <ListTodo v-else :size="30" />
          <strong>{{ loading ? '正在加载任务…' : '暂无匹配任务' }}</strong>
          <p v-if="error">{{ error }}</p>
        </div>
        <div v-if="hasMore" ref="loadMoreTrigger" class="creator-task-load-more">
          <button type="button" class="btn btn-sm" :disabled="loadingMore" @click="loadMoreMyTasks">
            <LoaderCircle v-if="loadingMore" class="spin" :size="13" />
            {{ loadingMore ? '正在加载…' : '加载更多任务' }}
          </button>
        </div>
        <p v-else-if="recentTasks.length" class="creator-task-list-end">已加载全部任务</p>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { toast } from 'vue-sonner'
import { Activity, Ban, ChevronRight, CircleCheck, CircleX, Clock3, Hourglass, ListTodo, LoaderCircle, RefreshCw } from 'lucide-vue-next'
import { taskAPI } from '~/composables/useApi'
import { useMyTasks, type CreatorTask } from '~/composables/useMyTasks'

const filters = [
  { id: 'all', title: '全部任务' },
  { id: 'running', title: '进行中' },
  { id: 'completed', title: '已完成' },
  { id: 'failed', title: '失败任务' },
]
const activeFilter = ref('all')
const loadMoreTrigger = ref<HTMLElement | null>(null)
const recallingIds = ref<number[]>([])
const { recentTasks, activeTaskCount, loading, loadingMore, error, total, hasMore, resetMyTasks, refreshMyTasks, loadMoreMyTasks } = useMyTasks()
let loadMoreObserver: IntersectionObserver | null = null

const filterStatus = (filter: string): '' | 'active' | 'completed' | 'failed' => filter === 'running' ? 'active' : filter === 'completed' || filter === 'failed' ? filter : ''

watch(activeFilter, async filter => {
  await resetMyTasks(filterStatus(filter))
  if (error.value) toast.error(error.value)
}, { immediate: true })

watch([hasMore, loadMoreTrigger, loadingMore], async ([more, , moreLoading]) => {
  if (!more || moreLoading) return
  await nextTick()
  observeLoadMoreTrigger()
})

function observeLoadMoreTrigger() {
  loadMoreObserver?.disconnect()
  if (!loadMoreTrigger.value || !hasMore.value) return
  loadMoreObserver = new IntersectionObserver(entries => {
    if (entries.some(entry => entry.isIntersecting)) loadMoreMyTasks()
  }, { rootMargin: '160px 0px' })
  loadMoreObserver.observe(loadMoreTrigger.value)
}

async function handleRefresh() {
  await refreshMyTasks()
  if (error.value) toast.error(error.value)
}

async function cancelTask(task: CreatorTask) {
  try { await taskAPI.cancel(Number(task.id)); toast.success('取消请求已提交'); await refreshMyTasks(true) }
  catch (reason: any) { toast.error(reason?.message || '取消失败') }
}

const canRecall = (task: CreatorTask) => task.type === 'video'
  && String(task.provider || '').toLowerCase() === 'doubao_local'
  && task.status === 'failed'
  && !!task.taskId

async function recallTask(task: CreatorTask) {
  const id = Number(task.id)
  if (!canRecall(task) || recallingIds.value.includes(id)) return
  if (!window.confirm('将使用原豆包 job_id 查询并召回视频，不会重新提交生成任务。确认继续？')) return
  recallingIds.value = [...recallingIds.value, id]
  try {
    const recalled = await taskAPI.recallVideo(id)
    toast.success(recalled?.status === 'completed' ? '视频已召回并保存' : '已恢复原任务状态，继续等待豆包结果')
    await resetMyTasks(filterStatus(activeFilter.value), true)
  } catch (reason: any) {
    toast.error(reason?.message || '召回视频失败')
  } finally {
    recallingIds.value = recallingIds.value.filter(value => value !== id)
  }
}

// 爆款拆解当前由独立的 FFmpeg + Claude 流程执行，尚未接入通用任务取消器。
const canCancel = (task: CreatorTask) => task.type !== 'viral_breakdown' && ['pending', 'processing'].includes(task.status)
const statusLabel = (status: string) => ({ pending: '排队中', processing: '进行中', canceling: '取消中', completed: '已完成', failed: '失败', canceled: '已取消' }[status] || status || '未知')
const statusTone = (status: string) => ({ pending: 'queued', processing: 'processing', canceling: 'canceling', completed: 'success', failed: 'danger', canceled: 'neutral' }[status] || 'neutral')
const formatTime = (value?: string) => value ? new Date(value).toLocaleString('zh-CN') : '—'

onMounted(observeLoadMoreTrigger)
onBeforeUnmount(() => loadMoreObserver?.disconnect())
</script>

<style scoped>
.creator-running-summary { height:32px; display:inline-flex; align-items:center; gap:6px; padding:0 10px; border:1px solid var(--accent-bg); border-radius:var(--radius-pill); background:var(--accent-bg); color:var(--accent-text); font-size:.625rem; font-weight:650; }
.creator-running-summary strong { font-variant-numeric:tabular-nums; }
.creator-task-table > div { min-width: 800px; grid-template-columns: 130px 90px minmax(160px,1fr) 160px 90px 90px; }
.creator-task-table small { display:block; margin-top:3px; color:var(--text-3); font-size:.5625rem; }
.creator-task-error { margin:0; padding:9px 14px; border-bottom:1px solid var(--error); background:var(--error-bg); color:var(--error); font-size:.625rem; }
.creator-task-load-more { min-height:54px; display:flex; align-items:center; justify-content:center; padding:10px; border-top:1px solid var(--border); }
.creator-task-list-end { margin:0; padding:14px; border-top:1px solid var(--border); color:var(--text-3); font-size:.625rem; text-align:center; }
</style>
