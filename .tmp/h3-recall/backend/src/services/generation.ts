/**
 * 统一生成任务服务 — 图片/视频生成共用 sys_task 表与同一条生命周期：
 * 创建 →（MiniMax 先进入 H3 本地队列）→ 提交上游 → 轮询 → 下载落盘 → 回写业务表
 */
import { db, getInsertId, schema, sqlite } from '../db/index.js'
import { and, asc, desc, eq, inArray, isNull, sql } from 'drizzle-orm'
import { getActiveConfig, getConfigById, getConfigForProvider } from './ai.js'
import { getAIServiceDisabledMessage } from './app-settings.js'
import { now } from '../utils/response.js'
import { downloadFile, fetchImageAsCompressedDataUrl, generateImageThumb, readImageAsCompressedDataUrl, readMediaAsDataUrl, saveBase64Image } from '../utils/storage.js'
import { extractVideoPoster } from '../utils/video-poster.js'
import { getImageAdapter, getVideoAdapter } from './adapters/registry'
import { MiniMaxVideoAdapter } from './adapters/minimax-video.js'
import type { AIConfig, ProviderRequest } from './adapters/types'
import { joinProviderUrl } from './adapters/url.js'
import { logTaskError, logTaskPayload, logTaskProgress, logTaskStart, logTaskSuccess, logTaskWarn, redactUrl } from '../utils/task-logger.js'
import { renumberImageRefMarkers } from '../utils/prompt-refs.js'
import { chargeGenerationTask, refundGenerationTask } from './membership.js'

type TaskType = 'image' | 'video'

const taskLabel = (type: TaskType) => (type === 'image' ? 'ImageTask' : 'VideoTask')

// 轮询节奏：图片 5s×120（上限 10 分钟）；视频 10s×1800（上限 300 分钟）。
// ComfyUI 进程停止或网络断开时必须有明确终态，不能让任务永久停在 processing。
const POLL_PROFILES: Record<TaskType, { attempts: number; intervalMs: number; maxDurationMs: number | null }> = {
  image: { attempts: 120, intervalMs: 5000, maxDurationMs: 600_000 },
  video: { attempts: 1800, intervalMs: 10_000, maxDurationMs: 18_000_000 },
}

// 自托管图片引擎（单卡 vLLM-Omni / ComfyUI）扛不住并发突发：批量出图一次压十几路
// 1920x1080 会先 CUDA OOM，再把整个 diffusion worker 打死（Stage-0 has no live
// replica），此后所有出图秒失败，只能等引擎重启。所以本地端点的图片任务在 H3 侧
// 排队，槽位满时任务停在 pending，空出一个跑一个；云端 API 不限流，仍立即提交。
const IMAGE_MAX_CONCURRENCY = Math.max(1, Math.floor(Number(process.env.H3_IMAGE_MAX_CONCURRENCY) || 3))
// key = 引擎 origin，多个自托管引擎各自独立计数
interface ImageQueueEntry { id: number, config: AIConfig }
const imageSlots = new Map<string, { running: number, waiting: ImageQueueEntry[] }>()

function imageEndpointKey(config: AIConfig): string {
  return (config.dispatchBaseUrl || config.baseUrl || '').trim().replace(/\/+$/, '')
}

// 回环/私有网段/裸 IP 视为自托管引擎；公网域名走云 API，不限流
function isLocalImageEndpoint(config: AIConfig): boolean {
  const raw = imageEndpointKey(config)
  if (!raw) return false
  let host: string
  try {
    host = new URL(raw.includes('://') ? raw : `http://${raw}`).hostname.toLowerCase().replace(/^\[|\]$/g, '')
  } catch {
    return false
  }
  return host === 'localhost' || host === '0.0.0.0' || host.endsWith('.local')
    || host === '::1'
    || /^127\.|^10\.|^192\.168\.|^172\.(1[6-9]|2[0-9]|3[01])\./.test(host)
    || /^(\d{1,3}\.){3}\d{1,3}$/.test(host)
}

function imageSlotState(key: string) {
  let state = imageSlots.get(key)
  if (!state) {
    state = { running: 0, waiting: [] }
    imageSlots.set(key, state)
  }
  return state
}

/** 图片任务统一提交入口：本地引擎过并发闸门，云端直接提交 */
function submitImageTask(id: number, config: AIConfig) {
  if (!isLocalImageEndpoint(config)) {
    kickProcessTask(id, config, 'image')
    return
  }
  const key = imageEndpointKey(config)
  const state = imageSlotState(key)
  if (state.running >= IMAGE_MAX_CONCURRENCY) {
    state.waiting.push({ id, config })
    logTaskProgress('ImageTask', 'local-queue-wait', {
      id,
      limit: IMAGE_MAX_CONCURRENCY,
      running: state.running,
      waiting: state.waiting.length,
    })
    return
  }
  startImageTask({ id, config }, key)
}

function startImageTask({ id, config }: ImageQueueEntry, key: string) {
  const state = imageSlotState(key)
  // 先占位再查库，保证并发下不会超发；任务已终态时槽位立即归还
  state.running++
  void (async () => {
    const promoted = await db.update(schema.sysTask)
      .set({ status: 'processing', errorMsg: null, updatedAt: now() })
      .where(and(eq(schema.sysTask.id, id), eq(schema.sysTask.status, 'pending')))
    if (!promoted.changes) return
    await processTask(id, config)
  })().catch(err => {
    logTaskError('ImageTask', 'process', { id, error: err?.message || String(err) })
    console.error(`ImageTask ${id} failed:`, err)
  }).finally(() => {
    state.running = Math.max(0, state.running - 1)
    while (state.running < IMAGE_MAX_CONCURRENCY && state.waiting.length) {
      const next = state.waiting.shift()
      if (next) startImageTask(next, key)
    }
  })
}

const MINIMAX_DISPATCH_INTERVAL_MS = 2_000
const MINIMAX_WORKER_SETTLE_MS = 3_000
let miniMaxDispatchTimer: NodeJS.Timeout | null = null
let miniMaxDispatchRunning = false
let miniMaxDispatchRequested = false
const miniMaxSubmittingWorkers = new Set<string>()
const miniMaxWorkerHoldUntil = new Map<string, number>()

interface GenerateImageParams {
  storyboardId?: number
  dramaId?: number
  sceneId?: number
  characterId?: number
  /** 非空 ⇒ 状态图任务：回写 character_states 而非角色底图（characterId 仍需携带，用于历史与鉴权回溯） */
  stateId?: number
  propId?: number
  prompt: string
  model?: string
  size?: string
  referenceImages?: string[]
  frameType?: string
  /** 发起时的资产字段快照（样貌/妆造、场景描述/光影、道具类型/描述），历史图片还原用 */
  assetSnapshot?: Record<string, unknown>
  configId?: number
  /** 发起用户（AI 配置可见域过滤）；bypass 省略 = 不过滤 */
  userId?: number
  /** 占位任务 id（createImageTaskPlaceholder）：提示词 Agent 阶段先建行让资产卡显示"生成中"，此处提升为 processing 并提交，不再新插行 */
  taskId?: number
}

interface GenerateVideoParams {
  storyboardId?: number
  dramaId?: number
  prompt: string
  /** 自由镜头的分镜描述原文（提示词由 Agent 生成时 prompt 已非人话，列表标题需要它）；仅存 params 展示用 */
  description?: string
  model?: string
  referenceMode?: string
  imageUrl?: string
  firstFrameUrl?: string
  lastFrameUrl?: string
  referenceImageUrls?: string[]
  /** 参考图用户命名（与 referenceImageUrls 同序，可空串）；分镜生成台恢复任务时回填 @图片N 重编锚点 */
  referenceImageLabels?: string[]
  referenceVideoUrls?: string[]
  referenceAudioUrls?: string[]
  referenceFileUrl?: string
  referenceLinkUrl?: string
  generateAudio?: boolean
  duration?: number
  aspectRatio?: string
  resolution?: string
  seed?: number
  promptExtend?: boolean
  watermark?: boolean
  /** 自由镜头（分镜生成台）的风格前缀文本（从预设导入或手编）；提交时已拼进 prompt，存这里供重新生成/回填复用 */
  stylePrefix?: string
  configId?: number
  /** 发起用户（AI 配置可见域过滤）；bypass 省略 = 不过滤 */
  userId?: number
}

interface VideoTaskFields {
  storyboardId?: number
  dramaId?: number
  ownerId?: number
  configId?: number
  prompt: string
  model?: string | null
}

interface VideoTaskClaim {
  id: number
  reused: boolean
}

/**
 * 原子认领一个分镜的视频任务。
 *
 * 整集流水线可能因多标签页、页面返回恢复或网络重放同时提交同一分镜。
 * 普通的“先查再插”仍有竞态，因此这里使用 BEGIN IMMEDIATE，把检查与插入
 * 放在同一个 SQLite 写事务内。同一分镜已有未完成任务时直接复用；终态任务
 * 不阻止用户之后主动重生成。
 */
export function claimVideoTask(
  provider: string,
  fields: VideoTaskFields,
  params: Record<string, unknown>,
): VideoTaskClaim {
  const claim = sqlite.transaction((): VideoTaskClaim => {
    if (fields.storyboardId != null) {
      const existing = sqlite.prepare(`
        SELECT id FROM sys_task
        WHERE type = 'video'
          AND storyboard_id = ?
          AND status IN ('pending', 'processing', 'canceling')
        ORDER BY id
        LIMIT 1
      `).get(fields.storyboardId) as { id: number } | undefined
      if (existing) return { id: Number(existing.id), reused: true }
    }

    const ts = now()
    const isLocalMiniMaxVideo = provider.toLowerCase() === 'minimax'
    const result = sqlite.prepare(`
      INSERT INTO sys_task (
        type, storyboard_id, drama_id, owner_id, config_id, provider, prompt, model, params,
        status, created_at, updated_at
      ) VALUES ('video', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    `).run(
      fields.storyboardId ?? null,
      fields.dramaId ?? null,
      fields.ownerId ?? null,
      fields.configId ?? null,
      provider,
      fields.prompt,
      fields.model ?? null,
      JSON.stringify(params),
      isLocalMiniMaxVideo ? 'pending' : 'processing',
      ts,
      ts,
    )
    return { id: Number(result.lastInsertRowid), reused: false }
  })
  return claim.immediate()
}

export async function generateImage(params: GenerateImageParams): Promise<number> {
  // 指定配置（集锁定）可能已停用/删除/厂商收敛，失效时回退到当前启用配置，避免生成被旧引用卡死
  // 配置解析限定在发起用户的可见域（本人 + 全局），越权 configId 视同失效走回退
  const config = params.configId
    ? (await getConfigById(params.configId, params.userId)) ?? await getActiveConfig('image', params.userId)
    : await getActiveConfig('image', params.userId)
  if (!config) {
    const msg = getAIServiceDisabledMessage('image')
      || '未配置图片模型，请先到「设置」页添加并启用 AI 服务'
    if (params.taskId != null) await failTask(params.taskId, msg)
    throw new Error(msg)
  }

  const fields = {
    storyboardId: params.storyboardId,
    dramaId: params.dramaId,
    sceneId: params.sceneId,
    characterId: params.characterId,
    stateId: params.stateId,
    propId: params.propId,
    ownerId: params.userId,
    configId: config.id,
    prompt: params.prompt,
    model: params.model || config.model,
  }
  const taskParams = {
    size: params.size || '1920x1080',
    frameType: params.frameType,
    referenceImages: params.referenceImages,
    assetSnapshot: params.assetSnapshot,
  }

  let id: number
  if (params.taskId != null) {
    // 占位任务提升：提示词已就绪，回填完整参数后与常规任务一样提交
    id = params.taskId
    await db.update(schema.sysTask).set({
      ...fields,
      provider: config.provider,
      params: JSON.stringify(taskParams),
      // 本地引擎：先回 pending 排队，闸门取到槽位才提升 processing
      status: isLocalImageEndpoint(config) ? 'pending' : 'processing',
      errorMsg: null,
      completedAt: null,
      updatedAt: now(),
    }).where(eq(schema.sysTask.id, id))
    try {
      chargeGenerationTask(params.userId, id)
    } catch (error) {
      await rejectTaskForBilling(id, (error as Error).message)
      throw error
    }
    submitImageTask(id, config)
  } else {
    id = await createTask('image', config, fields, taskParams)
  }

  logTaskStart('ImageTask', 'enqueue', {
    id,
    provider: config.provider,
    storyboardId: params.storyboardId,
    sceneId: params.sceneId,
    characterId: params.characterId,
    frameType: params.frameType,
    model: params.model || config.model,
  })
  logTaskPayload('ImageTask', 'enqueue params', {
    id,
    config: { provider: config.provider, model: config.model, baseUrl: config.baseUrl },
    params,
  })
  return id
}

export async function generateVideo(params: GenerateVideoParams): Promise<number> {
  // 指定配置（集锁定）可能已停用/删除/厂商收敛，失效时回退到当前启用配置
  const config = params.configId
    ? (await getConfigById(params.configId, params.userId)) ?? await getActiveConfig('video', params.userId)
    : await getActiveConfig('video', params.userId)
  if (!config) {
    const disabledMessage = getAIServiceDisabledMessage('video')
    if (disabledMessage) throw new Error(disabledMessage)
    throw new Error('未配置视频模型，请先到「设置」页添加并启用 AI 服务')
  }

  const taskParams = {
    description: params.description || undefined,
    referenceMode: params.referenceMode || 'reference',
    imageUrl: params.imageUrl,
    firstFrameUrl: params.firstFrameUrl,
    lastFrameUrl: params.lastFrameUrl,
    referenceImageUrls: params.referenceImageUrls,
    referenceImageLabels: params.referenceImageLabels,
    referenceVideoUrls: params.referenceVideoUrls,
    referenceAudioUrls: params.referenceAudioUrls,
    referenceFileUrl: params.referenceFileUrl,
    referenceLinkUrl: params.referenceLinkUrl,
    generateAudio: params.generateAudio === false ? 0 : 1,
    duration: params.duration,
    aspectRatio: params.aspectRatio,
    // 统一存为项目内部格式，各适配器再转换为官方大小写与枚举。
    resolution: normalizeStoredVideoResolution(params.resolution),
    seed: params.seed,
    promptExtend: params.promptExtend,
    watermark: params.watermark,
    stylePrefix: params.stylePrefix,
  }
  const claim = claimVideoTask(config.provider, {
    storyboardId: params.storyboardId,
    dramaId: params.dramaId,
    ownerId: params.userId,
    configId: config.id,
    prompt: params.prompt,
    model: params.model || config.model,
  }, taskParams)
  const id = claim.id

  if (claim.reused) {
    logTaskProgress('VideoTask', 'deduplicated', {
      id,
      storyboardId: params.storyboardId,
      dramaId: params.dramaId,
    })
    return id
  }

  try {
    chargeGenerationTask(params.userId, id)
  } catch (error) {
    await rejectTaskForBilling(id, (error as Error).message)
    if (config.provider.toLowerCase() === 'minimax') await refreshLocalMiniMaxQueuePositions()
    throw error
  }

  if (config.provider.toLowerCase() === 'minimax') {
    await refreshLocalMiniMaxQueuePositions()
    scheduleMiniMaxDispatch()
  } else {
    kickProcessTask(id, config, 'video')
  }

  logTaskStart('VideoTask', 'enqueue', {
    id,
    provider: config.provider,
    storyboardId: params.storyboardId,
    dramaId: params.dramaId,
    referenceMode: params.referenceMode || 'reference',
    duration: params.duration || 5,
  })
  logTaskPayload('VideoTask', 'enqueue params', {
    id,
    config: { provider: config.provider, model: config.model, baseUrl: config.baseUrl },
    params,
  })
  return id
}

async function createTask(
  type: TaskType,
  config: AIConfig,
  fields: {
    storyboardId?: number
    dramaId?: number
    sceneId?: number
    characterId?: number
    stateId?: number
    propId?: number
    ownerId?: number
    configId?: number
    prompt: string
    model?: string | null
  },
  params: Record<string, unknown>,
): Promise<number> {
  const ts = now()
  const isLocalMiniMaxVideo = type === 'video' && config.provider.toLowerCase() === 'minimax'
  // 本地图片引擎同样先落 pending，由并发闸门按空槽提升为 processing
  const queuedLocalImage = type === 'image' && isLocalImageEndpoint(config)
  const res = await db.insert(schema.sysTask).values({
    type,
    ...fields,
    provider: config.provider,
    params: JSON.stringify(params),
    status: isLocalMiniMaxVideo || queuedLocalImage ? 'pending' : 'processing',
    createdAt: ts,
    updatedAt: ts,
  })

  const id = getInsertId(res)
  try {
    chargeGenerationTask(fields.ownerId, id)
  } catch (error) {
    await rejectTaskForBilling(id, (error as Error).message)
    throw error
  }
  if (isLocalMiniMaxVideo) {
    await refreshLocalMiniMaxQueuePositions()
    scheduleMiniMaxDispatch()
  } else if (type === 'image') {
    submitImageTask(id, config)
  } else {
    kickProcessTask(id, config, type)
  }
  return id
}

async function rejectTaskForBilling(id: number, message: string) {
  const ts = now()
  await db.update(schema.sysTask)
    .set({ status: 'failed', queueAhead: null, errorMsg: message, completedAt: ts, updatedAt: ts })
    .where(and(eq(schema.sysTask.id, id), inArray(schema.sysTask.status, ['pending', 'processing'])))
}

function kickProcessTask(id: number, config: AIConfig, type: TaskType) {
  processTask(id, config).catch(err => {
    logTaskError(taskLabel(type), 'process', { id, error: err.message })
    console.error(`${taskLabel(type)} ${id} failed:`, err)
  })
}

function normalizeWorkerUrl(value: string): string {
  return value.replace(/\/+$/, '')
}

function scheduleMiniMaxDispatch(delayMs = 0) {
  if (miniMaxDispatchTimer) {
    if (delayMs > 0) return
    clearTimeout(miniMaxDispatchTimer)
  }
  miniMaxDispatchTimer = setTimeout(() => {
    miniMaxDispatchTimer = null
    void runMiniMaxDispatcher()
  }, delayMs)
  miniMaxDispatchTimer.unref()
}

async function refreshLocalMiniMaxQueuePositions() {
  // 用户优先级是本地待调度任务的第一排序条件，同级再按创建时间 FIFO。
  // 已经提交/正在提交的任务无法重排，只作为所有本地待调度任务的前方占用计数。
  const activeRows = await db.select({
    id: schema.sysTask.id,
    status: schema.sysTask.status,
    taskId: schema.sysTask.taskId,
  }).from(schema.sysTask)
    .where(and(
      eq(schema.sysTask.type, 'video'),
      eq(schema.sysTask.provider, 'minimax'),
      inArray(schema.sysTask.status, ['pending', 'processing']),
    ))
  const submittedCount = activeRows.filter(row => row.taskId || row.status === 'processing').length
  const queuedRows = await localMiniMaxQueue()
  await Promise.all(queuedRows.map((row, index) => db.update(schema.sysTask)
    .set({ queueAhead: submittedCount + index, updatedAt: now() })
    .where(and(eq(schema.sysTask.id, row.id), eq(schema.sysTask.status, 'pending'), isNull(schema.sysTask.taskId)))))
}

async function localMiniMaxQueue() {
  const userPriority = sql<number>`COALESCE((
    SELECT u.video_queue_priority FROM users u WHERE u.id = ${schema.sysTask.ownerId}
  ), 0)`
  return db.select().from(schema.sysTask)
    .where(and(
      eq(schema.sysTask.type, 'video'),
      eq(schema.sysTask.provider, 'minimax'),
      eq(schema.sysTask.status, 'pending'),
      isNull(schema.sysTask.taskId),
    ))
    .orderBy(desc(userPriority), asc(schema.sysTask.createdAt), asc(schema.sysTask.id))
}

/** 用户优先级变化后立即重算可见队列位置，并唤醒本地调度器。 */
export async function refreshMiniMaxQueuePriorities() {
  await refreshLocalMiniMaxQueuePositions()
  scheduleMiniMaxDispatch()
}

async function runMiniMaxDispatcher() {
  if (miniMaxDispatchRunning) {
    miniMaxDispatchRequested = true
    return
  }
  miniMaxDispatchRunning = true
  let hasQueuedTasks = false
  try {
    const queued = await localMiniMaxQueue()
    hasQueuedTasks = queued.length > 0
    if (!queued.length) return

    const config = await getConfigForProvider('video', 'minimax', await ownerForTask(queued[0]))
    if (!config) {
      await failTask(queued[0].id, '找不到可用的 MiniMax H3 · ComfyUI 集群配置')
      hasQueuedTasks = queued.length > 1
      return
    }

    const adapter = getVideoAdapter('minimax') as MiniMaxVideoAdapter
    const currentTime = Date.now()
    const workers = (await adapter.getDispatchableBaseUrls(config))
      .map(normalizeWorkerUrl)
      .filter(baseUrl => !miniMaxSubmittingWorkers.has(baseUrl)
        && (miniMaxWorkerHoldUntil.get(baseUrl) || 0) <= currentTime)

    let nextTaskIndex = 0
    let claimedCount = 0
    for (const baseUrl of workers) {
      let record = queued[nextTaskIndex++]
      while (record) {
        const claimed = await db.update(schema.sysTask)
          .set({ status: 'processing', queueAhead: null, errorMsg: null, updatedAt: now() })
          .where(and(
            eq(schema.sysTask.id, record.id),
            eq(schema.sysTask.status, 'pending'),
            isNull(schema.sysTask.taskId),
          ))
        if (claimed.changes) break
        record = queued[nextTaskIndex++]
      }
      if (!record) break

      claimedCount += 1
      miniMaxSubmittingWorkers.add(baseUrl)
      logTaskProgress('VideoTask', 'local-queue-dispatch', { id: record.id, baseUrl })
      void processTask(record.id, { ...config, dispatchBaseUrl: baseUrl }).finally(() => {
        miniMaxSubmittingWorkers.delete(baseUrl)
        miniMaxWorkerHoldUntil.set(baseUrl, Date.now() + MINIMAX_WORKER_SETTLE_MS)
        scheduleMiniMaxDispatch(MINIMAX_WORKER_SETTLE_MS)
      })
    }
    hasQueuedTasks = queued.length > claimedCount
    await refreshLocalMiniMaxQueuePositions()
  } catch (err: any) {
    hasQueuedTasks = true
    logTaskWarn('VideoTask', 'local-queue-dispatch-retry', { error: err?.message || String(err) })
  } finally {
    miniMaxDispatchRunning = false
    if (miniMaxDispatchRequested) {
      miniMaxDispatchRequested = false
      scheduleMiniMaxDispatch()
    } else if (hasQueuedTasks) {
      scheduleMiniMaxDispatch(MINIMAX_DISPATCH_INTERVAL_MS)
    }
  }
}

/**
 * 生图占位任务：generate-image 路由在跑提示词 Agent 之前先建 pending 行，
 * 前端（卡片徽标/任务面板/刷新恢复）从 sys_task 即可看到"生成中"；
 * 提示词就绪后 generateImage({ taskId }) 提升为 processing 并提交。
 * 进程中断时 recoverProcessingTasks 会把图片占位行判失败；MiniMax 视频本地队列除外。
 */
export async function createImageTaskPlaceholder(params: {
  storyboardId?: number
  dramaId?: number
  sceneId?: number
  characterId?: number
  stateId?: number
  propId?: number
}): Promise<number> {
  const ts = now()
  const res = await db.insert(schema.sysTask).values({
    type: 'image',
    storyboardId: params.storyboardId,
    dramaId: params.dramaId,
    sceneId: params.sceneId,
    characterId: params.characterId,
    stateId: params.stateId,
    propId: params.propId,
    status: 'pending',
    createdAt: ts,
    updatedAt: ts,
  })
  return getInsertId(res)
}

/** 同一目标已有未完成（pending/processing）图片任务时返回其 id：重复提交幂等复用，防刷新后再点重复生图 */
export async function findActiveImageTask(target: {
  sceneId?: number
  characterId?: number
  /** 底图任务传 null，状态图任务传 stateId */
  stateId?: number | null
  propId?: number
  storyboardId?: number
}): Promise<number | null> {
  const conds = [eq(schema.sysTask.type, 'image'), inArray(schema.sysTask.status, ['pending', 'processing'])]
  if (target.storyboardId != null) conds.push(eq(schema.sysTask.storyboardId, target.storyboardId))
  if (target.sceneId != null) conds.push(eq(schema.sysTask.sceneId, target.sceneId))
  if (target.propId != null) conds.push(eq(schema.sysTask.propId, target.propId))
  if (target.characterId != null) {
    conds.push(eq(schema.sysTask.characterId, target.characterId))
    conds.push(target.stateId == null ? isNull(schema.sysTask.stateId) : eq(schema.sysTask.stateId, target.stateId))
  }
  const [row] = await db.select({ id: schema.sysTask.id }).from(schema.sysTask)
    .where(and(...conds)).orderBy(schema.sysTask.id).limit(1)
  return row?.id ?? null
}

/** 占位任务在提交前终结（提示词 Agent 抛错等）：写失败终态，避免永久 pending */
export async function failPlaceholderImageTask(id: number, message: string) {
  await failTask(id, message)
}

function parseTaskParams(raw: string | null | undefined): Record<string, any> {
  if (!raw) return {}
  try {
    return JSON.parse(raw) || {}
  } catch {
    return {}
  }
}

async function processTask(id: number, config: AIConfig) {
  try {
    const [record] = await db.select().from(schema.sysTask).where(eq(schema.sysTask.id, id))
    if (!record) return
    const type = record.type as TaskType
    const label = taskLabel(type)
    const params = parseTaskParams(record.params)
    logTaskProgress(label, 'build-request', {
      id,
      provider: config.provider,
      storyboardId: record.storyboardId,
      sceneId: record.sceneId,
      characterId: record.characterId,
    })

    let providerRequest: ProviderRequest

    if (type === 'image') {
      const adapter = getImageAdapter(config.provider)
      const resolvedReferenceImages = await normalizeReferenceImages(params.referenceImages)
      providerRequest = await adapter.buildGenerateRequest(config, {
        id: record.id,
        model: record.model,
        prompt: record.prompt,
        size: params.size,
        frameType: params.frameType,
        referenceImages: resolvedReferenceImages.length ? JSON.stringify(resolvedReferenceImages) : null,
      })
    } else {
      const adapter = getVideoAdapter(config.provider)
      const [storyboardMeta] = record.storyboardId
        ? await db.select({ episodeId: schema.storyboards.episodeId })
          .from(schema.storyboards)
          .where(eq(schema.storyboards.id, record.storyboardId))
          .limit(1)
        : []
      const resolvedImageUrl = await normalizeVideoReferenceUrl(params.imageUrl, config.provider)
      const resolvedFirstFrameUrl = await normalizeVideoReferenceUrl(params.firstFrameUrl, config.provider)
      const resolvedLastFrameUrl = await normalizeVideoReferenceUrl(params.lastFrameUrl, config.provider)
      const rawReferenceImageUrls = Array.isArray(params.referenceImageUrls)
        ? params.referenceImageUrls.map((item: unknown) => String(item || '')).filter((item: string) => item.trim())
        : []
      const resolvedReferenceImageUrls = await normalizeVideoReferenceUrls(params.referenceImageUrls, config.provider)
      // @图片N 是前端按提交列表固定的编号；规范化去重后必须同步重排，避免文本挂错图
      const promptForProvider = resolvedReferenceImageUrls.length !== rawReferenceImageUrls.length
        ? renumberImageRefMarkers(record.prompt, rawReferenceImageUrls)
        : record.prompt
      // ComfyUI 在内网运行，无法访问 H3 的 static 路径；将本地音频内联后交给 Studio 上传。
      // 其他云厂商仍使用公网 URL，避免把大文件塞进云 API 请求。
      const isDoubaoLocal = config.provider?.toLowerCase() === 'doubao_local'
      if (isDoubaoLocal && (
        (Array.isArray(params.referenceVideoUrls) && params.referenceVideoUrls.length)
        || (Array.isArray(params.referenceAudioUrls) && params.referenceAudioUrls.length)
        || params.referenceFileUrl
        || params.referenceLinkUrl
      )) {
        throw new Error('豆包本地管理台仅支持提示词、首尾帧和参考图片，不支持参考视频、音频、文件或链接')
      }
      const resolvedReferenceVideoUrls = resolvePublicMediaUrls(params.referenceVideoUrls, 'video')
      const localComfyProvider = config.provider?.toLowerCase() === 'minimax'
      const resolvedReferenceAudioUrls = localComfyProvider
        ? await normalizeAudioReferenceUrls(params.referenceAudioUrls)
        : resolvePublicMediaUrls(params.referenceAudioUrls, 'audio')
      const resolvedReferenceFileUrl = resolvePublicMediaUrl(params.referenceFileUrl, 'file')
      providerRequest = await adapter.buildGenerateRequest(config, {
        id: record.id,
        dramaId: record.dramaId,
        episodeId: storyboardMeta?.episodeId,
        storyboardId: record.storyboardId,
        model: record.model,
        prompt: promptForProvider,
        referenceMode: params.referenceMode,
        imageUrl: resolvedImageUrl,
        firstFrameUrl: resolvedFirstFrameUrl,
        lastFrameUrl: resolvedLastFrameUrl,
        referenceImageUrls: resolvedReferenceImageUrls.length ? JSON.stringify(resolvedReferenceImageUrls) : null,
        referenceVideoUrls: resolvedReferenceVideoUrls.length ? JSON.stringify(resolvedReferenceVideoUrls) : null,
        referenceAudioUrls: resolvedReferenceAudioUrls.length ? JSON.stringify(resolvedReferenceAudioUrls) : null,
        referenceFileUrl: resolvedReferenceFileUrl,
        referenceLinkUrl: params.referenceLinkUrl,
        generateAudio: params.generateAudio,
        duration: params.duration,
        aspectRatio: params.aspectRatio,
        resolution: params.resolution,
        seed: params.seed,
        promptExtend: params.promptExtend,
        watermark: params.watermark,
      })
    }

    const { url, method, headers, body } = providerRequest

    logTaskProgress(label, 'request', {
      id,
      provider: config.provider,
      method,
      url: redactUrl(url),
      model: record.model,
    })

    const isMultipart = body instanceof FormData
    logTaskPayload(label, 'request payload', {
      id, method, url, headers,
      // multipart 表单（如 OpenAI /v1/images/edits）无法 JSON 化，记录字段摘要
      body: isMultipart ? `[multipart/form-data: ${[...(body as FormData).keys()].join(', ')}]` : body,
    })

    let resp: Response
    try {
      resp = await fetch(url, {
        method,
        headers,
        body: isMultipart ? (body as FormData) : JSON.stringify(body),
        signal: AbortSignal.timeout(600_000),
      })
    } finally {
      if (type === 'video') getVideoAdapter(config.provider).releaseGenerateReservation?.(providerRequest)
    }

    if (!resp.ok) {
      const detail = await resp.text()
      if (config.provider.toLowerCase() === 'minimax' && (resp.status === 401 || resp.status === 403)) {
        throw new Error(`ComfyUI 鉴权失败（HTTP ${resp.status}），请确认 H3 视频配置中的 API Key 与 ComfyUI 配置一致`)
      }
      throw new Error(`API error ${resp.status}: ${detail}`)
    }
    const result = await resp.json() as any
    logTaskPayload(label, 'response payload', { id, provider: config.provider, result })

    if (type === 'image') {
      const adapter = getImageAdapter(config.provider)
      const { isAsync, taskId, imageUrl } = adapter.parseGenerateResponse(result)

      if (!isAsync && imageUrl) {
        logTaskProgress(label, 'sync-complete', { id, imageUrl })
        await handleImageComplete(record, imageUrl)
        return
      }

      if (!isAsync && !imageUrl) {
        // 同步模式但无 URL（Gemini 等返回 base64）
        const b64 = adapter.extractImageBase64(result)
        if (b64) {
          logTaskProgress(label, 'sync-base64-complete', { id, mimeType: b64.mimeType })
          await handleImageCompleteBase64(record, b64.data, b64.mimeType)
          return
        }
        throw new Error('No image URL or base64 data in response')
      }

      await markPolling(id, taskId)
      pollTask(record, config, taskId!)
      return
    }

    const adapter = getVideoAdapter(config.provider)
    const { isAsync, taskId, videoUrl } = adapter.parseGenerateResponse(result, providerRequest)

    if (!isAsync && videoUrl) {
      logTaskProgress(label, 'sync-complete', { id, videoUrl })
      await handleVideoComplete(record, videoUrl, params.duration, config)
      return
    }

    const initialStatus = ['minimax', 'doubao_local'].includes(config.provider.toLowerCase()) ? 'pending' : 'processing'
    await markPolling(id, taskId, initialStatus)
    pollTask(record, config, taskId!, { initialStatus })
  } catch (err: any) {
    await failTask(id, err.message)
  }
}

async function markPolling(id: number, taskId: string | undefined, status: 'pending' | 'processing' = 'processing') {
  await db.update(schema.sysTask)
    .set({ taskId, status, queueAhead: null, errorMsg: null, updatedAt: now() })
    .where(eq(schema.sysTask.id, id))
  logTaskProgress('SysTask', 'poll-start', { id, taskId })
}

async function failTask(id: number, message: string) {
  logTaskError('SysTask', 'failed', { id, error: message })
  const ts = now()
  const [task] = await db.select({
    sceneId: schema.sysTask.sceneId,
    type: schema.sysTask.type,
    provider: schema.sysTask.provider,
  }).from(schema.sysTask).where(eq(schema.sysTask.id, id)).limit(1)
  const failed = await db.update(schema.sysTask)
    .set({ status: 'failed', queueAhead: null, errorMsg: message, completedAt: ts, updatedAt: ts })
    .where(and(eq(schema.sysTask.id, id), inArray(schema.sysTask.status, ['pending', 'processing'])))
  if (failed.changes && task?.sceneId) {
    await db.update(schema.scenes).set({ status: 'failed', updatedAt: ts }).where(eq(schema.scenes.id, task.sceneId))
  }
  if (failed.changes && task?.type === 'video' && task.provider?.toLowerCase() === 'minimax') {
    await refreshLocalMiniMaxQueuePositions()
    scheduleMiniMaxDispatch()
  }
  if (failed.changes) {
    refundTaskSafely(id, '生成失败退回')
  }
}

function refundTaskSafely(id: number, reason: string) {
  try { refundGenerationTask(id, reason) }
  catch (error) { logTaskError('Membership', 'refund-failed', { id, error: (error as Error).message }) }
}

type SysTaskRecord = typeof schema.sysTask.$inferSelect

function timeoutMessage(profile: { maxDurationMs: number | null }): string {
  const minutes = profile.maxDurationMs ? Math.round(profile.maxDurationMs / 60_000) : 0
  return minutes ? `生成超时：等待已超过 ${minutes} 分钟，请重试` : '生成超时：轮询已达上限，请重试'
}

async function pollTask(record: SysTaskRecord, config: AIConfig, taskId: string, opts: { initialElapsedMs?: number; initialStatus?: 'pending' | 'processing' } = {}) {
  const type = record.type as TaskType
  const label = taskLabel(type)
  const adapter = type === 'image' ? getImageAdapter(config.provider) : getVideoAdapter(config.provider)
  const sharedProfile = POLL_PROFILES[type]
  const customProfile = type === 'video' ? getVideoAdapter(config.provider).pollProfile : undefined
  const profile = customProfile
    ? { attempts: Math.ceil(customProfile.maxDurationMs / customProfile.intervalMs), intervalMs: customProfile.intervalMs, maxDurationMs: customProfile.maxDurationMs }
    : sharedProfile
  const isMiniMaxComfy = type === 'video' && config.provider.toLowerCase() === 'minimax'
  const isDoubaoLocal = type === 'video' && config.provider.toLowerCase() === 'doubao_local'
  // 重启恢复时把任务已消耗的时间折进来，避免复活早已超时的旧任务
  const startedAt = Date.now() - (opts.initialElapsedMs || 0)
  let consecutivePollFailures = 0
  let consecutiveMissingChecks = 0
  let doubaoAutomationRetries = 0
  let currentActiveStatus: 'pending' | 'processing' = opts.initialStatus || (record.status === 'pending' ? 'pending' : 'processing')
  let currentActiveError = record.errorMsg || null
  let currentQueueAhead = record.queueAhead ?? null

  const syncMiniMaxActiveState = async (status: 'pending' | 'processing', errorMsg: string | null = null, queueAhead: number | null = null) => {
    if (!isMiniMaxComfy || (currentActiveStatus === status && currentActiveError === errorMsg && currentQueueAhead === queueAhead)) return
    await db.update(schema.sysTask)
      .set({ status, queueAhead, errorMsg, updatedAt: now() })
      .where(and(eq(schema.sysTask.id, record.id), inArray(schema.sysTask.status, ['pending', 'processing'])))
    currentActiveStatus = status
    currentActiveError = errorMsg
    currentQueueAhead = queueAhead
    logTaskProgress(label, 'comfyui-status-sync', { id: record.id, taskId, status, queueAhead: queueAhead ?? undefined, warning: errorMsg || undefined })
  }

  const syncDoubaoConnectionWarning = async (errorMsg: string | null) => {
    if (!isDoubaoLocal || currentActiveError === errorMsg) return
    await db.update(schema.sysTask)
      .set({ errorMsg, updatedAt: now() })
      .where(and(eq(schema.sysTask.id, record.id), inArray(schema.sysTask.status, ['pending', 'processing'])))
    currentActiveError = errorMsg
  }

  for (let i = 0; i < profile.attempts; i++) {
    if (!(await isTaskActive(record.id))) return
    if (profile.maxDurationMs && Date.now() - startedAt >= profile.maxDurationMs) {
      await failTask(record.id, timeoutMessage(profile))
      return
    }
    await new Promise(r => setTimeout(r, profile.intervalMs))
    if (!(await isTaskActive(record.id))) return
    try {
      const { url, method, headers } = adapter.buildPollRequest(config, taskId)
      logTaskProgress(label, 'poll-request', {
        id: record.id,
        taskId,
        provider: config.provider,
        method,
        url: redactUrl(url),
        attempt: i + 1,
      })
      const remainingMs = profile.maxDurationMs
        ? Math.max(1_000, profile.maxDurationMs - (Date.now() - startedAt))
        : 600_000
      const resp = await fetch(url, {
        method,
        headers,
        signal: AbortSignal.timeout(isDoubaoLocal ? Math.min(60_000, remainingMs) : remainingMs),
      })
      if (!resp.ok) {
        if (['comfyui', 'minimax'].includes(config.provider.toLowerCase()) && (resp.status === 401 || resp.status === 403)) {
          await failTask(record.id, `ComfyUI 鉴权失败（HTTP ${resp.status}），请确认 H3 视频配置中的 API Key 与 ComfyUI 配置一致`)
          return
        }
        if (config.provider.toLowerCase() === 'doubao_local' && (resp.status === 401 || resp.status === 403)) {
          await failTask(record.id, `豆包本地管理台鉴权失败（HTTP ${resp.status}），请检查 X-API-Key 配置`)
          return
        }
        consecutivePollFailures += 1
        if (isMiniMaxComfy && consecutivePollFailures >= 3) {
          await syncMiniMaxActiveState(currentActiveStatus, `ComfyUI 状态同步中断（HTTP ${resp.status}），后台任务状态未知，正在继续重试`)
        } else if (isDoubaoLocal && consecutivePollFailures >= 3) {
          await syncDoubaoConnectionWarning(`豆包管理台连接中断（HTTP ${resp.status}），原任务仍保留；SSH 重连后会继续同步，也可手动召回视频`)
        } else if (!isMiniMaxComfy && consecutivePollFailures >= 6) {
          await failTask(record.id, `上游轮询连续失败（HTTP ${resp.status}），请确认视频生成服务仍在运行后重试`)
          return
        }
        continue
      }
      const result = await resp.json() as any

      // 图片/视频 PollResponse 结构不同，这里统一按 any 取值后按 type 分支
      let pollResp: any = adapter.parsePollResponse(result)

      // 原生 ComfyUI 的 /history 只包含已结束任务。查不到时必须继续查 /queue，
      // 才能区分排队、执行中与真正丢失，不能一直等到本地超时。
      if (isMiniMaxComfy && pollResp.status === 'missing') {
        const videoAdapter = adapter as ReturnType<typeof getVideoAdapter>
        if (!videoAdapter.buildQueueRequest || !videoAdapter.parseQueueResponse) {
          await failTask(record.id, 'MiniMax H3 适配器缺少 ComfyUI 队列状态同步能力')
          return
        }
        const queueRequest = videoAdapter.buildQueueRequest(config, taskId)
        const queueResp = await fetch(queueRequest.url, {
          method: queueRequest.method,
          headers: queueRequest.headers,
          signal: AbortSignal.timeout(remainingMs),
        })
        if (!queueResp.ok) {
          if (queueResp.status === 401 || queueResp.status === 403) {
            await failTask(record.id, `ComfyUI 鉴权失败（HTTP ${queueResp.status}），请确认 H3 视频配置中的 API Key 与 ComfyUI 配置一致`)
            return
          }
          throw new Error(`ComfyUI 队列状态同步失败（HTTP ${queueResp.status}）`)
        }
        pollResp = videoAdapter.parseQueueResponse(await queueResp.json(), taskId)
      }

      consecutivePollFailures = 0

      if (isMiniMaxComfy && pollResp.status === 'missing') {
        consecutiveMissingChecks += 1
        if (consecutiveMissingChecks >= 3) {
          await failTask(record.id, 'ComfyUI 任务不存在或已被清理，请重新生成')
          return
        }
        logTaskWarn(label, 'comfyui-task-missing', { id: record.id, taskId, checks: consecutiveMissingChecks })
        continue
      }

      consecutiveMissingChecks = 0
      if (isMiniMaxComfy && (pollResp.status === 'pending' || pollResp.status === 'processing')) {
        await syncMiniMaxActiveState(pollResp.status, null, pollResp.queueAhead ?? null)
        continue
      }
      if (!isMiniMaxComfy && (pollResp.status === 'pending' || pollResp.status === 'processing')) {
        if (currentActiveStatus !== pollResp.status || currentActiveError !== null) {
          await db.update(schema.sysTask)
            .set({ status: pollResp.status, queueAhead: null, errorMsg: null, updatedAt: now() })
            .where(and(eq(schema.sysTask.id, record.id), inArray(schema.sysTask.status, ['pending', 'processing'])))
          currentActiveStatus = pollResp.status
          currentActiveError = null
        }
        continue
      }

      if (pollResp.status === 'completed') {
        if (type === 'image') {
          if (pollResp.imageUrl) {
            logTaskSuccess(label, 'poll-complete', { id: record.id, taskId, imageUrl: pollResp.imageUrl })
            await handleImageComplete(record, pollResp.imageUrl)
            return
          }
          if (adapter.provider === 'gemini') {
            // Gemini 可能返回 base64
            const b64 = (adapter as ReturnType<typeof getImageAdapter>).extractImageBase64(result)
            if (b64) {
              logTaskSuccess(label, 'poll-base64-complete', { id: record.id, taskId, mimeType: b64.mimeType })
              await handleImageCompleteBase64(record, b64.data, b64.mimeType)
              return
            }
          }
        } else if (pollResp.videoUrl) {
          logTaskSuccess(label, 'poll-complete', { id: record.id, taskId, videoUrl: pollResp.videoUrl })
          try {
            const resultConfig = isMiniMaxComfy && 'resolveTaskBaseUrl' in adapter && adapter.resolveTaskBaseUrl
              ? { ...config, baseUrl: adapter.resolveTaskBaseUrl(config, taskId) }
              : config
            await handleVideoComplete(record, pollResp.videoUrl, pollResp.duration, resultConfig)
          } catch (err: any) {
            // ComfyUI 可能在写完输出后被关闭，导致 /view 下载失败。此时继续轮询
            // 只会永久显示“生成中”，直接落库为失败并保留可操作的错误原因。
            await failTask(record.id, `视频已在上游完成，但下载结果失败：${err?.message || '未知错误'}；请确认 ComfyUI 仍在运行后重试`)
          }
          return
        } else if (type === 'video') {
          // 上游宣告完成却没给出视频地址（如 Studio 响应缺字段），继续轮询只会空转到超时
          await failTask(record.id, '上游任务已完成，但未返回视频地址，请重试')
          return
        }
      }
      if (pollResp.status === 'failed') {
        const retryDoubaoJob = 'buildRetryRequest' in adapter && typeof adapter.buildRetryRequest === 'function'
          ? adapter.buildRetryRequest.bind(adapter)
          : null
        const canRetryDoubaoAutomation = type === 'video'
          && config.provider.toLowerCase() === 'doubao_local'
          && doubaoAutomationRetries < 2
          && /\[WEB_AUTOMATION_ERROR\]/.test(String(pollResp.error || ''))
          && retryDoubaoJob !== null
        if (canRetryDoubaoAutomation) {
          const retryRequest = retryDoubaoJob(config, taskId)
          const retryResp = await fetch(retryRequest.url, {
            method: retryRequest.method,
            headers: retryRequest.headers,
            signal: AbortSignal.timeout(30_000),
          })
          if (!retryResp.ok) throw new Error(`豆包管理台自动重试失败（HTTP ${retryResp.status}）`)
          doubaoAutomationRetries += 1
          logTaskWarn(label, 'doubao-web-automation-retry', {
            id: record.id,
            taskId,
            retry: doubaoAutomationRetries,
            maxRetries: 2,
            error: pollResp.error,
          })
          continue
        }
        // 上游明确失败（如内容审核拦截）属终态：立即落库，不重试不等待超时
        await failTask(record.id, pollResp.error || 'Generation failed')
        return
      }
    } catch (err: any) {
      consecutivePollFailures += 1
      if (isMiniMaxComfy && consecutivePollFailures >= 3) {
        await syncMiniMaxActiveState(currentActiveStatus, 'ComfyUI 状态同步中断，后台任务状态未知，正在继续重试')
      } else if (isDoubaoLocal && consecutivePollFailures >= 3) {
        await syncDoubaoConnectionWarning(`豆包管理台连接中断，原任务仍保留；SSH 重连后会继续同步，也可手动召回视频：${err.message}`)
      } else if (!isMiniMaxComfy && consecutivePollFailures >= 6) {
        await failTask(record.id, `上游轮询连接失败：${err.message}；请确认视频生成服务仍在运行后重试`)
        return
      }
      const exhausted = i === profile.attempts - 1
        || (profile.maxDurationMs != null && Date.now() - startedAt >= profile.maxDurationMs)
      if (exhausted) {
        await failTask(record.id, `Timeout: ${err.message}`)
        return
      }
      logTaskWarn(label, 'poll-retry', { id: record.id, taskId, attempt: i + 1, error: err.message })
    }
  }
  await failTask(record.id, timeoutMessage(profile))
}

async function isTaskActive(id: number): Promise<boolean> {
  const [row] = await db.select({ status: schema.sysTask.status }).from(schema.sysTask)
    .where(eq(schema.sysTask.id, id)).limit(1)
  return row?.status === 'pending' || row?.status === 'processing'
}

/** 仅取消 MiniMax H3 / ComfyUI 视频任务。 */
export async function cancelComfyVideoTask(id: number): Promise<{ mode: 'local' | 'job' | 'pending' | 'running' }> {
  const [record] = await db.select().from(schema.sysTask).where(eq(schema.sysTask.id, id)).limit(1)
  if (!record) throw new Error('任务不存在')
  if (record.type !== 'video' || record.provider?.toLowerCase() !== 'minimax') {
    throw new Error('仅支持取消 ComfyUI 视频任务')
  }
  if (record.status !== 'pending' && record.status !== 'processing') throw new Error('当前任务已结束，无法取消')
  if (!record.taskId && record.status === 'pending') {
    const ts = now()
    const canceled = await db.update(schema.sysTask)
      .set({ status: 'canceled', queueAhead: null, errorMsg: null, completedAt: ts, updatedAt: ts })
      .where(and(eq(schema.sysTask.id, id), eq(schema.sysTask.status, 'pending'), isNull(schema.sysTask.taskId)))
    if (!canceled.changes) throw new Error('任务状态已变化，请刷新列表后重试')
    await refreshLocalMiniMaxQueuePositions()
    scheduleMiniMaxDispatch()
    refundTaskSafely(id, '任务取消退回')
    logTaskSuccess('VideoTask', 'local-queue-canceled', { id })
    return { mode: 'local' }
  }
  if (!record.taskId) throw new Error('视频任务正在提交到 ComfyUI，请稍后再试')

  const claimedAt = now()
  const claim = await db.update(schema.sysTask)
    .set({ status: 'canceling', errorMsg: null, updatedAt: claimedAt })
    .where(and(eq(schema.sysTask.id, id), inArray(schema.sysTask.status, ['pending', 'processing'])))
  if (!claim.changes) throw new Error('任务状态已变化，请刷新列表后重试')

  try {
    const config = await getConfigForProvider('video', 'minimax', await ownerForTask(record))
    if (!config) throw new Error('找不到该任务使用的 ComfyUI 配置')
    const mode = await new MiniMaxVideoAdapter().cancelTask(config, record.taskId)
    const ts = now()
    await db.update(schema.sysTask)
      .set({ status: 'canceled', queueAhead: null, errorMsg: null, completedAt: ts, updatedAt: ts })
      .where(and(eq(schema.sysTask.id, id), eq(schema.sysTask.status, 'canceling')))
    await refreshLocalMiniMaxQueuePositions()
    scheduleMiniMaxDispatch()
    refundTaskSafely(id, '任务取消退回')
    logTaskSuccess('VideoTask', 'canceled', { id, taskId: record.taskId, mode })
    return { mode }
  } catch (err) {
    await db.update(schema.sysTask)
      .set({ status: record.status, updatedAt: now() })
      .where(and(eq(schema.sysTask.id, id), eq(schema.sysTask.status, 'canceling')))
    throw err
  }
}

async function handleImageComplete(record: SysTaskRecord, imageUrl: string) {
  const localPath = await downloadFile(imageUrl, 'images')
  // 列表页缩略图（前端按命名约定推导地址，失败不影响主流程）
  await generateImageThumb(localPath)

  await db.update(schema.sysTask)
    .set({ resultUrl: imageUrl, localPath, status: 'completed', queueAhead: null, completedAt: now(), updatedAt: now() })
    .where(eq(schema.sysTask.id, record.id))

  logTaskSuccess('ImageTask', 'downloaded', { id: record.id, provider: record.provider, localPath })

  await writeBackImageAssets(record, localPath)
}

async function handleImageCompleteBase64(record: SysTaskRecord, base64Data: string, mimeType: string) {
  const localPath = await saveBase64Image(base64Data, mimeType, 'images')
  await generateImageThumb(localPath)

  await db.update(schema.sysTask)
    .set({ localPath, status: 'completed', queueAhead: null, completedAt: now(), updatedAt: now() })
    .where(eq(schema.sysTask.id, record.id))

  logTaskSuccess('ImageTask', 'saved-base64', { id: record.id, provider: record.provider, mimeType, localPath })

  await writeBackImageAssets(record, localPath)
}

// 图片完成后回写业务表：分镜(按 frameType)、角色、场景、道具
async function writeBackImageAssets(record: SysTaskRecord, localPath: string) {
  const params = parseTaskParams(record.params)
  if (record.storyboardId) {
    const sbUpdate: Record<string, any> = { updatedAt: now() }
    if (params.frameType === 'first_frame') sbUpdate.firstFrameImage = localPath
    else if (params.frameType === 'last_frame') sbUpdate.lastFrameImage = localPath
    else sbUpdate.composedImage = localPath
    await db.update(schema.storyboards).set(sbUpdate).where(eq(schema.storyboards.id, record.storyboardId))
  }
  if (record.stateId) {
    // 状态图任务：只回写状态行，不得覆盖角色底图（stateId 任务同时携带 characterId）
    await db.update(schema.characterStates).set({ imageUrl: localPath, localPath, updatedAt: now() }).where(eq(schema.characterStates.id, record.stateId))
  }
  if (record.characterId && !record.stateId) {
    await db.update(schema.characters).set({ imageUrl: localPath, updatedAt: now() }).where(eq(schema.characters.id, record.characterId))
  }
  if (record.sceneId) {
    await db.update(schema.scenes).set({ imageUrl: localPath, status: 'completed', updatedAt: now() }).where(eq(schema.scenes.id, record.sceneId))
  }
  if (record.propId) {
    await db.update(schema.props).set({ imageUrl: localPath, updatedAt: now() }).where(eq(schema.props.id, record.propId))
  }
}

// ComfyUI 把执行结果写入 history 时，VHS_VideoCombine 的成品文件可能还没落盘完
// （实测 success 与文件可见之间差 1~2 秒），单次下载会撞上 404。对可恢复的错误重试约 60 秒。
const RETRYABLE_DOWNLOAD_STATUS = new Set([404, 408, 429])
function isRetryableDownloadError(err: any): boolean {
  const status = typeof err?.status === 'number' ? err.status : undefined
  if (status === undefined) return true // 网络异常/超时同样值得重试
  return RETRYABLE_DOWNLOAD_STATUS.has(status) || status >= 500
}

async function downloadWithRetry(
  url: string,
  subDir: string,
  headers: Record<string, string> | undefined,
  opts: { taskId?: number; attempts?: number; delayMs?: number; extension?: string } = {},
): Promise<string> {
  const attempts = opts.attempts ?? 12
  const delayMs = opts.delayMs ?? 5_000
  for (let attempt = 1; ; attempt++) {
    try {
      return await downloadFile(url, subDir, headers, opts.extension)
    } catch (err: any) {
      if (attempt >= attempts || !isRetryableDownloadError(err)) throw err
      logTaskWarn('VideoTask', 'download-retry', { id: opts.taskId, attempt, error: err?.message || String(err) })
      await new Promise(r => setTimeout(r, delayMs))
    }
  }
}

async function handleVideoComplete(record: SysTaskRecord, videoUrl: string, duration: number | null | undefined, config: AIConfig) {
  const adapter = getVideoAdapter(config.provider)
  const providerDownload = adapter.resolveVideoDownload?.(config, videoUrl)
  // MiniMax H3 原生 ComfyUI 返回相对输出路径，下载时沿用连接密钥。
  const resolvedUrl = providerDownload?.url || (videoUrl.startsWith('/') ? joinProviderUrl(config.baseUrl, '', videoUrl) : videoUrl)
  const headers = providerDownload?.headers || (config.provider.toLowerCase() === 'minimax' && config.apiKey
    ? { Authorization: `Bearer ${config.apiKey}` }
    : undefined)
  const localPath = await downloadWithRetry(resolvedUrl, 'videos', headers, {
    taskId: record.id,
    extension: providerDownload?.extension,
  })
  // 海报帧供列表/封面展示，避免前端为显示首帧缓冲整个视频
  await extractVideoPoster(localPath)

  if (record.storyboardId) {
    const params = parseTaskParams(record.params)
    // MiniMax 重生成保留当前主视频，用户可从历史结果中手动切换。
    let keepMain = false
    if (config.provider.toLowerCase() === 'minimax') {
      const [sb] = await db.select({ videoUrl: schema.storyboards.videoUrl })
        .from(schema.storyboards)
        .where(eq(schema.storyboards.id, record.storyboardId))
        .limit(1)
      keepMain = !!sb?.videoUrl
    }
    if (!keepMain) {
      await db.update(schema.storyboards)
        .set({ videoUrl: localPath, duration: duration || undefined, updatedAt: now() })
        .where(eq(schema.storyboards.id, record.storyboardId))
    }
  }

  // 分镜主视频先回写，再把任务置为终态。否则两次写入之间另一个整集流水线
  // 会同时看到“分镜尚无视频”和“没有进行中任务”，从而重复创建任务。
  await db.update(schema.sysTask)
    .set({ resultUrl: resolvedUrl, localPath, status: 'completed', queueAhead: null, errorMsg: null, completedAt: now(), updatedAt: now() })
    .where(eq(schema.sysTask.id, record.id))

  if (config.provider.toLowerCase() === 'minimax') {
    await refreshLocalMiniMaxQueuePositions()
    scheduleMiniMaxDispatch()
  }

  logTaskSuccess('VideoTask', 'downloaded', { id: record.id, localPath, storyboardId: record.storyboardId, duration })
}

// ─── 参考素材归一化 ───────────────────────────────────────────────

async function normalizeReferenceImages(refs: string[] | null | undefined): Promise<string[]> {
  if (!Array.isArray(refs) || !refs.length) return []

  const deduped = Array.from(
    new Set(
      refs
        .map((item) => String(item || '').trim())
        .filter(Boolean),
    ),
  )

  const normalized = await Promise.all(deduped.map(async (value) => {
    if (value.startsWith('data:image/')) return value
    if (value.startsWith('static/') || value.startsWith('/static/')) {
      const localPath = value.startsWith('/static/') ? value.slice(1) : value
      try {
        return await readImageAsCompressedDataUrl(localPath, {
          maxWidth: 768,
          maxHeight: 768,
          quality: 68,
        })
      } catch (err) {
        logTaskWarn('ImageTask', 'reference-read-failed', { path: localPath, error: (err as Error).message })
        return null
      }
    }
    // 远程 URL：下载压缩为 data URL，保证 multipart 上传（OpenAI edits）/ inline_data（Gemini）都可用
    if (/^https?:\/\//.test(value)) {
      try {
        return await fetchImageAsCompressedDataUrl(value, {
          maxWidth: 768,
          maxHeight: 768,
          quality: 68,
        })
      } catch (err) {
        logTaskWarn('ImageTask', 'reference-fetch-failed', { url: value, error: (err as Error).message })
        return null
      }
    }
    return value
  }))

  return normalized.filter((item): item is string => !!item).slice(0, 6)
}

async function normalizeVideoReferenceUrl(value: string | null | undefined, provider?: string): Promise<string | null> {
  const raw = String(value || '').trim()
  if (!raw) return null
  if (raw.startsWith('data:image/')) return raw
  if (raw.startsWith('static/') || raw.startsWith('/static/')) {
    const localPath = raw.startsWith('/static/') ? raw.slice(1) : raw
    try {
      // 参考图（角色三视图等）的服装/材质细节直接决定成片一致性，压缩留足分辨率与质量余量
      return await readImageAsCompressedDataUrl(localPath, {
        maxWidth: 1536,
        maxHeight: 1536,
        quality: 80,
      })
    } catch (err) {
      logTaskWarn('VideoTask', 'reference-read-failed', { path: localPath, error: (err as Error).message })
      // 静默丢图会让提示词里预排的 @图片N 整体错位、悄悄挂错参考图，宁可显式失败
      throw new Error(`参考图片读取失败：${localPath}（${(err as Error).message}）`)
    }
  }
  if (provider?.toLowerCase() === 'minimax' && /^https?:\/\//.test(raw)) {
    try {
      return await fetchImageAsCompressedDataUrl(raw, {
        maxWidth: 1536,
        maxHeight: 1536,
        quality: 80,
      })
    } catch (err) {
      logTaskWarn('VideoTask', 'reference-fetch-failed', { url: raw, error: (err as Error).message })
      throw new Error(`参考图片下载失败：${raw.slice(0, 200)}（${(err as Error).message}）`)
    }
  }
  return raw
}

async function normalizeVideoReferenceUrls(refs: string[] | null | undefined, provider?: string): Promise<string[]> {
  if (!Array.isArray(refs) || !refs.length) return []
  const normalized = await Promise.all(
    Array.from(new Set(refs.map((item) => String(item || '').trim()).filter(Boolean))).map((item) => normalizeVideoReferenceUrl(item, provider)),
  )
  return normalized.filter((item): item is string => !!item)
}

async function normalizeAudioReferenceUrl(value: string | null | undefined): Promise<string | null> {
  const raw = String(value || '').trim()
  if (!raw) return null
  if (/^data:audio\//i.test(raw)) return raw
  if (raw.startsWith('static/') || raw.startsWith('/static/')) {
    const localPath = raw.startsWith('/static/') ? raw.slice(1) : raw
    try {
      return readMediaAsDataUrl(localPath)
    } catch (err) {
      logTaskWarn('VideoTask', 'reference-audio-read-failed', { path: localPath, error: (err as Error).message })
      throw new Error(`参考音频读取失败：${localPath}（${(err as Error).message}）`)
    }
  }
  return raw
}

async function normalizeAudioReferenceUrls(refs: string[] | null | undefined): Promise<string[]> {
  if (!Array.isArray(refs) || !refs.length) return []
  const normalized = await Promise.all(
    Array.from(new Set(refs.map((item) => String(item || '').trim()).filter(Boolean))).map(normalizeAudioReferenceUrl),
  )
  return normalized.filter((item): item is string => !!item).slice(0, 3)
}

/**
 * 将参考视频/音频解析为 Seedance API 可访问的 URL。
 * http(s)/dataURL 直通；本地 static 路径需要 PUBLIC_BASE_URL 拼成公网地址，
 * 未配置时抛出可操作的中文错误（落入 catch 写入 error_msg 供前端展示）。
 */
function resolvePublicMediaUrl(value: string | null | undefined, kind: 'video' | 'audio' | 'file'): string | null {
  const raw = String(value || '').trim()
  if (!raw) return null
  if (raw.startsWith('http://') || raw.startsWith('https://') || raw.startsWith('data:')) return raw
  if (raw.startsWith('static/') || raw.startsWith('/static/')) {
    const base = (process.env.PUBLIC_BASE_URL || '').trim().replace(/\/+$/, '')
    if (!base) {
      const label = kind === 'video' ? '视频' : kind === 'audio' ? '音频' : '文件'
      throw new Error(
        `参考${label}为本地路径 ${raw}，但后端未配置 PUBLIC_BASE_URL，上游视频生成 API 无法访问内网地址。` +
        `请在 backend/.env 配置 PUBLIC_BASE_URL（如 https://your-domain.com）后重试，或改用公网 URL。`,
      )
    }
    const p = raw.startsWith('/') ? raw : `/${raw}`
    return `${base}${p}`
  }
  return raw
}

function resolvePublicMediaUrls(refs: string[] | null | undefined, kind: 'video' | 'audio'): string[] {
  if (!Array.isArray(refs) || !refs.length) return []
  const items = Array.from(new Set(refs.map((item) => String(item || '').trim()).filter(Boolean)))
  return items.map((item) => resolvePublicMediaUrl(item, kind)).filter((item): item is string => !!item)
}

function normalizeStoredVideoResolution(resolution: string | null | undefined): string | undefined {
  const value = String(resolution || '').trim().toLowerCase()
  if (value === '480p' || value === '720p' || value === '1080p') return value
  if (value === '2k') return '2K'
  return undefined
}

/** 任务归属用户（配置可见域）：drama_id 直存优先，缺失时按挂靠资源回溯；解析不到返回 undefined = 不过滤 */
async function ownerForTask(record: typeof schema.sysTask.$inferSelect): Promise<number | undefined> {
  let dramaId = record.dramaId
  if (!dramaId && record.storyboardId) {
    const [sb] = await db.select({ episodeId: schema.storyboards.episodeId })
      .from(schema.storyboards).where(eq(schema.storyboards.id, record.storyboardId)).limit(1)
    if (sb) {
      const [ep] = await db.select({ dramaId: schema.episodes.dramaId })
        .from(schema.episodes).where(eq(schema.episodes.id, sb.episodeId)).limit(1)
      dramaId = ep?.dramaId ?? null
    }
  }
  if (!dramaId && record.sceneId) {
    const [row] = await db.select({ dramaId: schema.scenes.dramaId }).from(schema.scenes)
      .where(eq(schema.scenes.id, record.sceneId)).limit(1)
    dramaId = row?.dramaId ?? null
  }
  if (!dramaId && record.characterId) {
    const [row] = await db.select({ dramaId: schema.characters.dramaId }).from(schema.characters)
      .where(eq(schema.characters.id, record.characterId)).limit(1)
    dramaId = row?.dramaId ?? null
  }
  if (!dramaId && record.propId) {
    const [row] = await db.select({ dramaId: schema.props.dramaId }).from(schema.props)
      .where(eq(schema.props.id, record.propId)).limit(1)
    dramaId = row?.dramaId ?? null
  }
  if (!dramaId) return record.ownerId ?? undefined
  const [drama] = await db.select({ ownerId: schema.dramas.ownerId }).from(schema.dramas)
    .where(eq(schema.dramas.id, dramaId)).limit(1)
  return drama?.ownerId ?? undefined
}

async function providerRequestJson(request: ProviderRequest, timeoutMs = 120_000): Promise<any> {
  const response = await fetch(request.url, {
    method: request.method,
    headers: request.headers,
    body: request.body == null ? undefined : JSON.stringify(request.body),
    signal: AbortSignal.timeout(timeoutMs),
  })
  const text = await response.text()
  let result: any = null
  try { result = text ? JSON.parse(text) : {} } catch { result = {} }
  if (!response.ok) {
    const detail = String(result?.detail || result?.message || text || `HTTP ${response.status}`).trim()
    throw new Error(`豆包管理台召回失败（HTTP ${response.status}）：${detail}`)
  }
  return result
}

/**
 * 召回已提交的豆包网页任务：始终复用 taskId，不创建、不重新提交生成任务。
 * 已出片则下载回 H3；仍在生成则恢复轮询；needs_review 时调用管理台的复查归档入口。
 */
export async function recallDoubaoVideoTask(id: number) {
  const [record] = await db.select().from(schema.sysTask).where(eq(schema.sysTask.id, id)).limit(1)
  if (!record) throw new Error('任务不存在')
  if (record.type !== 'video' || record.provider?.toLowerCase() !== 'doubao_local') {
    throw new Error('仅豆包网页版视频任务支持召回')
  }
  if (!record.taskId) throw new Error('该任务尚未取得豆包 job_id，无法召回')
  if (record.status === 'completed') return record
  if (record.status === 'pending' || record.status === 'processing') return record
  if (record.status === 'canceled' || record.status === 'canceling') throw new Error('已取消的任务不能召回')

  const claimed = await db.update(schema.sysTask).set({
    status: 'processing',
    queueAhead: null,
    errorMsg: '正在使用原豆包 job_id 召回视频',
    completedAt: null,
    updatedAt: now(),
  }).where(and(eq(schema.sysTask.id, id), eq(schema.sysTask.status, 'failed')))
  if (!claimed.changes) {
    const [current] = await db.select().from(schema.sysTask).where(eq(schema.sysTask.id, id)).limit(1)
    return current
  }

  try {
    const ownerId = await ownerForTask(record)
    const config = record.configId
      ? await getConfigById(record.configId, ownerId)
      : await getConfigForProvider('video', record.provider, ownerId)
    if (!config) throw new Error('豆包服务配置已失效或被停用，请先恢复原配置')

    const adapter = getVideoAdapter(config.provider)
    let upstream = await providerRequestJson(adapter.buildPollRequest(config, record.taskId), 30_000)
    let state = adapter.parsePollResponse(upstream)

    if (state.status === 'failed') {
      if (!adapter.buildRecallRequest) throw new Error(state.error || '豆包任务需要人工处理，无法自动召回')
      upstream = await providerRequestJson(adapter.buildRecallRequest(config, record.taskId))
      state = adapter.parsePollResponse(upstream)
    }

    if (state.status === 'completed' && state.videoUrl) {
      await handleVideoComplete(record, state.videoUrl, state.duration, config)
    } else if (state.status === 'pending' || state.status === 'processing') {
      await db.update(schema.sysTask).set({
        status: state.status,
        queueAhead: null,
        errorMsg: null,
        completedAt: null,
        updatedAt: now(),
      }).where(eq(schema.sysTask.id, id))
      const [fresh] = await db.select().from(schema.sysTask).where(eq(schema.sysTask.id, id)).limit(1)
      if (!fresh) throw new Error('召回后未找到 H3 任务记录')
      pollTask(fresh, config, record.taskId, { initialStatus: state.status }).catch(err => {
        logTaskError('VideoTask', 'doubao-recall-poll', { id, taskId: record.taskId, error: err?.message || String(err) })
      })
    } else {
      throw new Error(state.error || '豆包任务尚无可召回的视频结果')
    }

    const [updated] = await db.select().from(schema.sysTask).where(eq(schema.sysTask.id, id)).limit(1)
    logTaskSuccess('VideoTask', 'doubao-recalled', { id, taskId: record.taskId, status: updated?.status })
    return updated
  } catch (error: any) {
    await failTask(id, error?.message || '召回视频失败')
    throw error
  }
}

/**
 * 进程重启后恢复被打断的生成任务（tsx watch 重启 / 崩溃后调用）：
 * 轮询只活在内存里，但上游（ComfyUI 等）的结果是持久的——已提交（有 taskId）的
 * 任务重新挂回轮询。MiniMax 本地排队任务与本地图片闸门排队任务继续等待调度；
 * 其他无 taskId 的中断任务（含图片占位行）判失败，避免重复提交或永久停在 processing。
 */
export async function recoverProcessingTasks() {
  const rows = await db.select().from(schema.sysTask).where(inArray(schema.sysTask.status, ['pending', 'processing']))
  if (!rows.length) {
    scheduleMiniMaxDispatch()
    return
  }

  const localMiniMaxQueued = rows.filter(r => !r.taskId
    && r.status === 'pending'
    && r.type === 'video'
    && r.provider?.toLowerCase() === 'minimax')
  // 图片闸门队列同样活在内存里：未提交的本地图片任务重新入队（保持 FIFO），
  // 云端图片无队列，落回 abandoned 判失败
  // 只恢复已提交过闸门队列的行；提示词 Agent 阶段的占位行（provider 为空）仍判失败
  const localImageQueued = rows.filter(r => !r.taskId && r.status === 'pending'
    && r.type === 'image' && !!r.provider && !!(r.prompt || '').trim())
  const localQueueIds = new Set(localMiniMaxQueued.map(record => record.id))
  const resumableImages: Array<{ id: number, config: AIConfig }> = []
  for (const record of localImageQueued) {
    const config = await getConfigForProvider('image', record.provider, await ownerForTask(record))
    if (config && isLocalImageEndpoint(config)) {
      localQueueIds.add(record.id)
      resumableImages.push({ id: record.id, config })
    }
  }
  const abandoned = rows.filter(r => !r.taskId && !localQueueIds.has(r.id))
  if (abandoned.length) {
    await db.update(schema.sysTask)
      .set({ status: 'failed', queueAhead: null, errorMsg: '服务重启，生成任务中断，请重试', completedAt: now(), updatedAt: now() })
      .where(inArray(schema.sysTask.id, abandoned.map(r => r.id)))
    console.log(`🔁 已清理 ${abandoned.length} 个未提交即中断的生成任务`)
  }

  for (const record of rows.filter(r => r.taskId)) {
    const type = record.type as TaskType
    const label = taskLabel(type)
    const elapsed = Math.max(0, Date.now() - new Date(record.createdAt).getTime())
    // 归属解析：恢复发生在后台，按任务挂靠的项目 owner 限定配置可见域；
    // drama 溯源失败/全局项目返回 undefined = 不过滤（历史行为）
    const ownerId = await ownerForTask(record)
    const config = record.configId
      ? await getConfigById(record.configId, ownerId)
      : await getConfigForProvider(type, record.provider, ownerId)
    if (!config) {
      await failTask(record.id, '服务重启后未能恢复：关联的 AI 服务配置已失效或被停用，请检查设置后重试')
      continue
    }
    const adapterProfile = type === 'video' ? getVideoAdapter(config.provider).pollProfile : undefined
    const profile = adapterProfile
      ? { attempts: Math.ceil(adapterProfile.maxDurationMs / adapterProfile.intervalMs), intervalMs: adapterProfile.intervalMs, maxDurationMs: adapterProfile.maxDurationMs }
      : (POLL_PROFILES[type] || POLL_PROFILES.video)
    if (profile.maxDurationMs && elapsed >= profile.maxDurationMs) {
      await failTask(record.id, timeoutMessage(profile))
      continue
    }
    console.log(`🔁 恢复生成任务 ${label} #${record.id}（provider=${record.provider}, taskId=${record.taskId}）`)
    pollTask(record, config, record.taskId!, { initialElapsedMs: elapsed }).catch(err => {
      logTaskError(label, 'recover', { id: record.id, error: err?.message || String(err) })
    })
  }
  if (resumableImages.length) {
    // 按创建顺序重新入队，闸门自然限流
    console.log(`🔁 恢复 ${resumableImages.length} 个排队中的本地图片任务`)
    for (const { id, config } of resumableImages.sort((a, b) => a.id - b.id)) submitImageTask(id, config)
  }
  await refreshLocalMiniMaxQueuePositions()
  scheduleMiniMaxDispatch()
}
