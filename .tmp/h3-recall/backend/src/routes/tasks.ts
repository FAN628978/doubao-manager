import { Hono } from 'hono'
import { and, eq, isNull, sql } from 'drizzle-orm'
import { db, schema, sqlite } from '../db/index.js'
import { success, created, badRequest, forbidden, notFound, now } from '../utils/response.js'
import { cancelComfyVideoTask, generateImage, generateVideo, recallDoubaoVideoTask } from '../services/generation.js'
import { getActiveConfig, getConfigById } from '../services/ai.js'
import { getDramaStylePrompt } from '../services/style-preset.js'
import { logTaskError, logTaskPayload, logTaskStart, logTaskSuccess } from '../utils/task-logger.js'
import { currentUser } from '../middleware/auth.js'
import { projectSettingsOwnerId, requireDramaOf, requireStoryboard, canAccessAllProjects, canAccessTask } from '../utils/access.js'
import { randomVideoSeed } from '../utils/video-seed.js'
import { getAIServiceDisabledMessage } from '../services/app-settings.js'

const app = new Hono()

type TaskType = 'image' | 'video'

const WAN_MEDIA_TYPES = new Set([
  'first_frame',
  'last_frame',
  'reference_image',
  'reference_video',
  'reference_audio',
  'file',
  'link',
])

const ADMIN_TASK_STATUSES = new Set(['pending', 'processing', 'canceling', 'completed', 'failed', 'canceled'])
const MY_TASK_STATUS_FILTERS = new Set([...ADMIN_TASK_STATUSES, 'active'])

// GET /tasks/admin/queue — 超级管理员查看所有用户的任务队列与归属信息。
app.get('/admin/queue', async (c) => {
  const user = currentUser(c)
  if (!user.isAdmin) return forbidden(c)

  const status = String(c.req.query('status') || '').trim()
  const type = String(c.req.query('type') || '').trim()
  const page = Math.max(1, Math.floor(Number(c.req.query('page')) || 1))
  const pageSize = Math.max(10, Math.min(100, Math.floor(Number(c.req.query('page_size')) || 50)))
  if (status && !ADMIN_TASK_STATUSES.has(status)) return badRequest(c, '任务状态筛选值无效')
  if (type && type !== 'image' && type !== 'video' && type !== 'viral_breakdown') return badRequest(c, '任务类型筛选值无效')

  const conditions: string[] = []
  const params: Array<string | number> = []
  if (status) { conditions.push('t.status = ?'); params.push(status) }
  if (type) { conditions.push('t.type = ?'); params.push(type) }
  const where = conditions.length ? `WHERE ${conditions.join(' AND ')}` : ''
  const dramaExpr = `COALESCE(
    t.drama_id,
    (SELECT e.drama_id FROM storyboards sb JOIN episodes e ON e.id = sb.episode_id WHERE sb.id = t.storyboard_id),
    (SELECT s.drama_id FROM scenes s WHERE s.id = t.scene_id),
    (SELECT ch.drama_id FROM characters ch WHERE ch.id = t.character_id),
    (SELECT p.drama_id FROM props p WHERE p.id = t.prop_id)
  )`
  // 集数编号：分镜/场景任务可回溯到具体集，资产类任务为空（前端仅跳转项目页）
  const episodeExpr = `COALESCE(
    (SELECT e.episode_number FROM storyboards sb JOIN episodes e ON e.id = sb.episode_id WHERE sb.id = t.storyboard_id),
    (SELECT e2.episode_number FROM scenes s JOIN episodes e2 ON e2.id = s.episode_id WHERE s.id = t.scene_id)
  )`
  const total = Number((sqlite.prepare(`SELECT COUNT(*) AS n FROM sys_task t ${where}`).get(...params) as { n: number }).n)
  const items = sqlite.prepare(`
    SELECT t.id, t.type, t.status, COALESCE(t.owner_id, d.owner_id) AS ownerId,
           ${dramaExpr} AS dramaId, ${episodeExpr} AS episodeNumber, t.storyboard_id AS storyboardId,
           t.provider, t.model, t.prompt, t.params, t.task_id AS taskId,
           t.result_url AS resultUrl, t.local_path AS localPath,
           t.queue_ahead AS queueAhead, t.credit_cost AS creditCost,
           t.error_msg AS errorMsg, t.created_at AS createdAt,
           t.updated_at AS updatedAt, t.completed_at AS completedAt,
           COALESCE(u.display_name, u.username, '未知用户') AS ownerName,
           u.username AS ownerUsername, COALESCE(u.video_queue_priority, 0) AS ownerQueuePriority,
           d.title AS dramaTitle
    FROM sys_task t
    LEFT JOIN dramas d ON d.id = ${dramaExpr}
    LEFT JOIN users u ON u.id = COALESCE(t.owner_id, d.owner_id)
    ${where}
    ORDER BY t.id DESC
    LIMIT ? OFFSET ?
  `).all(...params, pageSize, (page - 1) * pageSize) as Array<Record<string, unknown>>
  const countRows = sqlite.prepare(`
    SELECT status, COUNT(*) AS n FROM sys_task GROUP BY status
  `).all() as Array<{ status: string; n: number }>
  const counts = Object.fromEntries(countRows.map(row => [row.status, Number(row.n)]))
  return success(c, { items, total, page, pageSize, counts })
})

// GET /tasks/mine/queue — 创作端「我的任务」分页列表与状态汇总。
app.get('/mine/queue', async (c) => {
  const user = currentUser(c)
  const status = String(c.req.query('status') || '').trim()
  const page = Math.max(1, Math.floor(Number(c.req.query('page')) || 1))
  const pageSize = Math.max(10, Math.min(50, Math.floor(Number(c.req.query('page_size')) || 20)))
  if (status && !MY_TASK_STATUS_FILTERS.has(status)) return badRequest(c, '任务状态筛选值无效')

  const dramaExpr = `COALESCE(
    t.drama_id,
    (SELECT e.drama_id FROM storyboards sb JOIN episodes e ON e.id = sb.episode_id WHERE sb.id = t.storyboard_id),
    (SELECT s.drama_id FROM scenes s WHERE s.id = t.scene_id),
    (SELECT ch.drama_id FROM characters ch WHERE ch.id = t.character_id),
    (SELECT p.drama_id FROM props p WHERE p.id = t.prop_id)
  )`
  const freeTaskExpr = `(t.drama_id IS NULL AND t.storyboard_id IS NULL AND t.scene_id IS NULL
    AND t.character_id IS NULL AND t.prop_id IS NULL)`
  const ownerConditions: string[] = []
  const ownerParams: number[] = []
  if (!user.bypass) {
    ownerConditions.push(`(EXISTS (
      SELECT 1 FROM dramas owned_drama
      WHERE owned_drama.id = ${dramaExpr} AND owned_drama.owner_id = ? AND owned_drama.deleted_at IS NULL
    ) OR (${freeTaskExpr} AND t.owner_id = ?))`)
    ownerParams.push(user.id, user.id)
  }
  const ownerWhere = ownerConditions.length ? `WHERE ${ownerConditions.join(' AND ')}` : ''
  const statusClause = status === 'active'
    ? `t.status IN ('pending', 'processing', 'canceling')`
    : status ? 't.status = ?' : ''
  const listConditions = [...ownerConditions, ...(statusClause ? [statusClause] : [])]
  const listParams: Array<string | number> = [...ownerParams, ...(status && status !== 'active' ? [status] : [])]
  const listWhere = listConditions.length ? `WHERE ${listConditions.join(' AND ')}` : ''

  const total = Number((sqlite.prepare(`SELECT COUNT(*) AS n FROM sys_task t ${listWhere}`).get(...listParams) as { n: number }).n)
  const items = sqlite.prepare(`
    SELECT t.id, t.type, t.status, t.provider, t.model, t.prompt, t.params,
           t.task_id AS taskId, t.result_url AS resultUrl, t.local_path AS localPath,
           t.queue_ahead AS queueAhead, t.credit_cost AS creditCost, t.error_msg AS errorMsg,
           t.created_at AS createdAt, t.updated_at AS updatedAt, t.completed_at AS completedAt,
           ${dramaExpr} AS dramaId, d.title AS dramaTitle
    FROM sys_task t
    LEFT JOIN dramas d ON d.id = ${dramaExpr}
    ${listWhere}
    ORDER BY t.id DESC
    LIMIT ? OFFSET ?
  `).all(...listParams, pageSize, (page - 1) * pageSize) as Array<Record<string, unknown>>
  const countRows = sqlite.prepare(`
    SELECT t.status, COUNT(*) AS n FROM sys_task t ${ownerWhere} GROUP BY t.status
  `).all(...ownerParams) as Array<{ status: string; n: number }>
  const counts = Object.fromEntries(countRows.map(row => [row.status, Number(row.n)]))

  return success(c, { items, total, page, pageSize, counts })
})

/**
 * 兼容项目原有扁平入参，也支持 Wan 3.0 官方 model/input/parameters 结构。
 */
function normalizeVideoRequest(body: any) {
  const input = body.input && typeof body.input === 'object' && !Array.isArray(body.input) ? body.input : {}
  const parameters = body.parameters && typeof body.parameters === 'object' && !Array.isArray(body.parameters)
    ? body.parameters
    : {}
  const media = Array.isArray(input.media) ? input.media : []

  const mediaUrls = (type: string) => media
    .filter((item: any) => item?.type === type)
    .map((item: any) => item?.url)

  return {
    ...body,
    prompt: body.prompt ?? input.prompt ?? '',
    reference_image_urls: body.reference_image_urls ?? mediaUrls('reference_image'),
    reference_image_labels: Array.isArray(body.reference_image_labels)
      ? body.reference_image_labels.map((s: unknown) => String(s ?? '').trim())
      : undefined,
    reference_video_urls: body.reference_video_urls ?? mediaUrls('reference_video'),
    reference_audio_urls: body.reference_audio_urls ?? mediaUrls('reference_audio'),
    first_frame_url: body.first_frame_url ?? mediaUrls('first_frame')[0],
    last_frame_url: body.last_frame_url ?? mediaUrls('last_frame')[0],
    file_url: body.file_url ?? mediaUrls('file')[0],
    link_url: body.link_url ?? mediaUrls('link')[0],
    generate_audio: body.generate_audio ?? parameters.audio,
    duration: body.duration ?? parameters.duration,
    aspect_ratio: body.aspect_ratio ?? parameters.ratio,
    resolution: body.resolution ?? parameters.resolution,
    seed: body.seed ?? parameters.seed,
    prompt_extend: body.prompt_extend ?? parameters.prompt_extend,
    watermark: body.watermark ?? parameters.watermark,
    official_media: media,
  }
}

function validateVideoRequest(body: any, provider?: string): string | null {
  for (const key of ['reference_image_urls', 'reference_video_urls', 'reference_audio_urls']) {
    if (body[key] !== undefined && !Array.isArray(body[key])) return `${key} 必须为数组`
    if (Array.isArray(body[key]) && body[key].some((url: any) => typeof url !== 'string' || !url.trim())) {
      return `${key} 中的每个 URL 都必须为非空字符串`
    }
  }

  if (body.official_media.length) {
    for (const item of body.official_media) {
      if (!item || typeof item !== 'object' || !WAN_MEDIA_TYPES.has(item.type) || typeof item.url !== 'string' || !item.url.trim()) {
        return 'input.media 中的每项都必须包含官方支持的 type 和非空 url'
      }
    }
    for (const type of ['first_frame', 'last_frame', 'file', 'link']) {
      if (body.official_media.filter((item: any) => item.type === type).length > 1) {
        return `Wan 3.0 input.media 中 ${type} 最多 1 项`
      }
    }
  }

  const imgs = body.reference_image_urls.length
  const vids = body.reference_video_urls.length
  const auds = body.reference_audio_urls.length
  const first = Boolean(body.first_frame_url)
  const last = Boolean(body.last_frame_url)
  const file = Boolean(body.file_url)
  const link = Boolean(body.link_url)

  if ((first || last) && (provider || '').toLowerCase() === 'volcengine') {
    return '当前视频模型不支持首帧/尾帧模式'
  }

  if ((provider || '').toLowerCase() === 'doubao_local') {
    if (vids || auds || file || link) return '豆包本地管理台仅支持提示词、首尾帧和参考图片'
    if (last && !first) return '豆包尾帧必须与首帧同时传入'
    if ((first || last) && imgs) return '豆包首尾帧模式不能与普通参考图片混用'
    if (imgs > 9) return '豆包参考生视频最多支持 9 张图片'
    if (!String(body.prompt || '').trim()) return '豆包视频提示词必填'
  } else if ((provider || '').toLowerCase() === 'aliyun') {
    if (imgs > 10 || vids > 5 || auds > 5) return 'Wan 3.0 参考素材超限：图片≤10、视频≤5、音频≤5'
    if (last && !first) return 'Wan 3.0 尾帧必须与首帧同时传入'
    if (file && link) return 'Wan 3.0 file 与 link 不能同时传入'
    if ((first || last) && (imgs + vids + auds > 0 || file || link)) {
      return 'Wan 3.0 的 first_frame/last_frame 不能与 reference_image/reference_video/reference_audio/file/link 混用'
    }
    const total = imgs + vids + auds + Number(first) + Number(last) + Number(file) + Number(link)
    if (total > 20) return 'Wan 3.0 input.media 最多 20 项'
  } else {
    if (imgs > 9 || vids > 3 || auds > 3) return '参考素材超限：图片≤9、视频≤3、音频≤3'
    if (auds > 0 && imgs + vids === 0 && !first && !last) return '参考音频需要至少 1 个参考图片或视频'
  }

  if (!String(body.prompt || '').trim() && imgs + vids + auds === 0 && !first && !last && !file && !link) {
    return '视频生成需要至少一个参考素材或 prompt'
  }
  return null
}

// POST /tasks — 发起生成任务（body.type: image | video）
app.post('/', async (c) => {
  const body = await c.req.json()
  const type = body.type as TaskType
  if (type !== 'image' && type !== 'video') return badRequest(c, 'type 必须为 image 或 video')
  const disabledMessage = getAIServiceDisabledMessage(type)
  if (disabledMessage) return badRequest(c, disabledMessage)

  if (type === 'image') {
    if (!body.prompt) return badRequest(c, '提示词必填')
  }

  const videoBody = type === 'video' ? normalizeVideoRequest(body) : null
  if (videoBody && videoBody.duration !== undefined) {
    const duration = Number(videoBody.duration)
    if (!Number.isFinite(duration) || duration < 2 || duration > 15) {
      return badRequest(c, '视频时长必须为 2-15 秒')
    }
    videoBody.duration = duration
  }

  // 引用资源归属校验 + 创建时补全 task 归属（sys_task.drama_id 历史上可空，导致列表隔离需回溯 join）
  let taskDramaId: number | null = body.drama_id != null ? Number(body.drama_id) : null
  if (body.storyboard_id) {
    const denied = await requireStoryboard(c, Number(body.storyboard_id))
    if (denied) return denied
  }
  if (body.scene_id) {
    const [sc] = await db.select({ dramaId: schema.scenes.dramaId }).from(schema.scenes).where(eq(schema.scenes.id, Number(body.scene_id))).limit(1)
    const denied = await requireDramaOf(c, sc?.dramaId, '场景')
    if (denied) return denied
    taskDramaId ??= sc?.dramaId ?? null
  }
  if (body.character_id) {
    const [ch] = await db.select({ dramaId: schema.characters.dramaId }).from(schema.characters).where(eq(schema.characters.id, Number(body.character_id))).limit(1)
    const denied = await requireDramaOf(c, ch?.dramaId, '角色')
    if (denied) return denied
    taskDramaId ??= ch?.dramaId ?? null
  }
  // 服装状态图任务：经所属角色鉴权，并回填 character_id 保证历史回溯与项目隔离
  let stateCharacterId: number | undefined
  if (body.state_id) {
    const [st] = await db.select({ characterId: schema.characterStates.characterId }).from(schema.characterStates)
      .where(eq(schema.characterStates.id, Number(body.state_id))).limit(1)
    if (!st) return badRequest(c, '服装状态不存在')
    const [ch] = await db.select({ dramaId: schema.characters.dramaId }).from(schema.characters).where(eq(schema.characters.id, st.characterId)).limit(1)
    const denied = await requireDramaOf(c, ch?.dramaId, '角色')
    if (denied) return denied
    stateCharacterId = st.characterId
    taskDramaId ??= ch?.dramaId ?? null
  }
  if (body.prop_id) {
    const [pr] = await db.select({ dramaId: schema.props.dramaId }).from(schema.props).where(eq(schema.props.id, Number(body.prop_id))).limit(1)
    const denied = await requireDramaOf(c, pr?.dramaId, '道具')
    if (denied) return denied
    taskDramaId ??= pr?.dramaId ?? null
  }
  if (taskDramaId) {
    const denied = await requireDramaOf(c, taskDramaId)
    if (denied) return denied
  }

  try {
    // 请求显式指定 config_id（工作台模型下拉跨厂商切换）时优先；
    // 未指定才回退到集锁定配置，避免锁定配置与所选模型错配（如锁定 Seedance 却传 MiniMax 模型名）
    let configId: number | undefined = body.config_id
    let episodeResolution: string | undefined
    let storyboardDramaId: number | undefined
    if (body.storyboard_id) {
      const [sb] = await db.select().from(schema.storyboards).where(eq(schema.storyboards.id, Number(body.storyboard_id)))
      if (sb) {
        const [ep] = await db.select().from(schema.episodes).where(eq(schema.episodes.id, sb.episodeId))
        const locked = type === 'image' ? ep?.imageConfigId : ep?.videoConfigId
        if (locked != null && configId == null) configId = locked
        if (type === 'video' && ep?.resolution) episodeResolution = ep.resolution
        storyboardDramaId = ep?.dramaId ?? undefined
        taskDramaId ??= storyboardDramaId
        if (videoBody) {
          let seed = sb.videoSeed
          if (sb.videoSeed == null) {
            const candidate = randomVideoSeed()
            // 并发提交只允许第一个请求初始化种子；后续请求读取已落库值，
            // 避免复用同一视频任务时分镜 seed 却被另一个请求覆盖。
            await db.update(schema.storyboards)
              .set({ videoSeed: candidate, updatedAt: now() })
              .where(and(eq(schema.storyboards.id, sb.id), isNull(schema.storyboards.videoSeed)))
            const [fresh] = await db.select({ videoSeed: schema.storyboards.videoSeed })
              .from(schema.storyboards)
              .where(eq(schema.storyboards.id, sb.id))
              .limit(1)
            seed = fresh?.videoSeed ?? candidate
          }
          videoBody.seed = seed
        }
      }
    }

    // 管理员代操作时仍按项目所有者解析个人覆盖层。
    const uid = await projectSettingsOwnerId(c, taskDramaId)
    if (type === 'video' && videoBody) {
      const effectiveConfig = configId
        ? (await getConfigById(configId, uid)) ?? await getActiveConfig('video', uid)
        : await getActiveConfig('video', uid)
      const validationError = validateVideoRequest(videoBody, effectiveConfig?.provider)
      if (validationError) return badRequest(c, validationError)
    }

    logTaskStart('TaskAPI', 'generate', {
      type,
      storyboardId: body.storyboard_id,
      sceneId: body.scene_id,
      characterId: body.character_id,
      dramaId: body.drama_id,
    })
    logTaskPayload('TaskAPI', 'request body', body)

    // video_prompt 只保存镜头正文；提交视频生成任务时再确定性地拼接项目当前生效的风格前缀。
    // 无项目的自由镜头（分镜生成台）由请求 style_prefix 直传前缀文本（前端已从预设导入或手编），后端不再解析预设。
    let videoPrompt = videoBody?.prompt
    const standaloneStyle = !taskDramaId ? String(body.style_prefix || '').trim() : ''
    if (type === 'video' && String(videoPrompt || '').trim()) {
      const stylePrompt = String(taskDramaId ? await getDramaStylePrompt(taskDramaId, 'video') : standaloneStyle).trim()
      const promptBody = String(videoPrompt).trim()
      // 防止重试或旧数据已经带有完全相同的前缀时重复拼接。
      if (
        stylePrompt
        && promptBody !== stylePrompt
        && !promptBody.startsWith(`${stylePrompt}\n`)
        && !promptBody.startsWith(`${stylePrompt}，\n`)
      ) {
        videoPrompt = `${stylePrompt}\n${promptBody}`
      } else {
        videoPrompt = promptBody
      }
    }

    const id = type === 'image'
      ? await generateImage({
        storyboardId: body.storyboard_id,
        dramaId: taskDramaId ?? undefined,
        sceneId: body.scene_id,
        characterId: body.character_id ?? stateCharacterId,
        stateId: body.state_id != null ? Number(body.state_id) : undefined,
        prompt: body.prompt,
        model: body.model,
        size: body.size,
        referenceImages: body.reference_images,
        frameType: body.frame_type,
        configId,
        userId: uid,
      })
      : await generateVideo({
        storyboardId: body.storyboard_id,
        dramaId: taskDramaId ?? undefined,
        prompt: videoPrompt,
        description: String(videoBody!.description || '').trim() || undefined,
        model: videoBody!.model,
        referenceMode: 'reference',
        imageUrl: videoBody!.image_url,
        firstFrameUrl: videoBody!.first_frame_url,
        lastFrameUrl: videoBody!.last_frame_url,
        referenceImageUrls: videoBody!.reference_image_urls,
        referenceImageLabels: videoBody!.reference_image_labels,
        referenceVideoUrls: videoBody!.reference_video_urls,
        referenceAudioUrls: videoBody!.reference_audio_urls,
        referenceFileUrl: videoBody!.file_url,
        referenceLinkUrl: videoBody!.link_url,
        generateAudio: videoBody!.generate_audio,
        duration: videoBody!.duration,
        aspectRatio: videoBody!.aspect_ratio,
        resolution: episodeResolution || videoBody!.resolution,
        seed: videoBody!.seed,
        promptExtend: videoBody!.prompt_extend,
        watermark: videoBody!.watermark,
        stylePrefix: standaloneStyle || undefined,
        configId,
        userId: uid,
      })

    const [record] = await db.select().from(schema.sysTask)
      .where(eq(schema.sysTask.id, id))
    logTaskSuccess('TaskAPI', 'generate', { taskId: id, type, provider: record?.provider })
    return created(c, record)
  } catch (err: any) {
    logTaskError('TaskAPI', 'generate', { type, error: err.message })
    return badRequest(c, err.message)
  }
})

// GET /tasks/:id — 轮询任务状态
app.get('/:id', async (c) => {
  const id = Number(c.req.param('id'))
  const [row] = await db.select().from(schema.sysTask)
    .where(eq(schema.sysTask.id, id))
  if (!row) return success(c, null)
  if (!(await canAccessTask(c, row))) return success(c, null)
  return success(c, row)
})

// GET /tasks — 按 type / storyboard_id / drama_id 过滤（按用户隔离：COALESCE 回溯归属项目）
app.get('/', async (c) => {
  const type = c.req.query('type')
  const storyboardId = c.req.query('storyboard_id')
  const dramaId = c.req.query('drama_id')
  const characterId = c.req.query('character_id')
  const sceneId = c.req.query('scene_id')
  const propId = c.req.query('prop_id')
  const stateId = c.req.query('state_id')
  const scope = c.req.query('scope')
  const user = currentUser(c)

  let rows: (typeof schema.sysTask.$inferSelect)[]
  // 管理员进入创作端「我的任务」时也只看本人；单机免登录模式仍保留全量视图。
  if (canAccessAllProjects(user) && (scope !== 'mine' || user.bypass)) {
    rows = await db.select().from(schema.sysTask)
  } else {
    const ownerDrama = sql`(SELECT d.id FROM dramas d WHERE d.id = COALESCE(
      ${schema.sysTask.dramaId},
      (SELECT e.drama_id FROM storyboards sb JOIN episodes e ON e.id = sb.episode_id WHERE sb.id = ${schema.sysTask.storyboardId}),
      (SELECT s.drama_id FROM scenes s WHERE s.id = ${schema.sysTask.sceneId}),
      (SELECT ch.drama_id FROM characters ch WHERE ch.id = ${schema.sysTask.characterId}),
      (SELECT p.drama_id FROM props p WHERE p.id = ${schema.sysTask.propId})
    ) AND d.owner_id = ${user.id} AND d.deleted_at IS NULL)`
    // 无项目归属的自由任务（分镜生成台）按 owner_id 放行
    const ownFreeTask = sql`(${schema.sysTask.dramaId} IS NULL AND ${schema.sysTask.storyboardId} IS NULL
      AND ${schema.sysTask.sceneId} IS NULL AND ${schema.sysTask.characterId} IS NULL AND ${schema.sysTask.propId} IS NULL
      AND ${schema.sysTask.ownerId} = ${user.id})`
    rows = await db.select().from(schema.sysTask).where(sql`${ownerDrama} OR ${ownFreeTask}`)
  }

  // scope=standalone：只看无项目归属的自由任务（分镜生成台历史列表）
  if (scope === 'standalone') {
    rows = rows.filter(r => r.dramaId == null && r.storyboardId == null && r.sceneId == null && r.characterId == null && r.propId == null)
  }

  if (type) rows = rows.filter(r => r.type === type)
  if (storyboardId) rows = rows.filter(r => r.storyboardId === Number(storyboardId))
  if (dramaId) rows = rows.filter(r => r.dramaId === Number(dramaId))
  if (stateId) {
    rows = rows.filter(r => r.stateId === Number(stateId))
  } else if (characterId) {
    // 底图历史：排除状态图任务（它们按 state_id 查询），避免混进角色生成历史
    rows = rows.filter(r => r.characterId === Number(characterId) && r.stateId == null)
  }
  if (sceneId) rows = rows.filter(r => r.sceneId === Number(sceneId))
  if (propId) rows = rows.filter(r => r.propId === Number(propId))

  // 管理员视角附带发起人名称（精细制作「制作管理」与全自动任务队列同款能力）
  if (canAccessAllProjects(user)) {
    const nameRows = sqlite.prepare('SELECT id, COALESCE(display_name, username) AS name FROM users')
      .all() as Array<{ id: number; name: string }>
    const nameMap = new Map(nameRows.map(r => [r.id, r.name]))
    return success(c, rows.map(r => ({ ...r, ownerName: r.ownerId != null ? nameMap.get(r.ownerId) || null : null })))
  }

  return success(c, rows)
})

// POST /tasks/:id/recall-video — 复用原 job_id 召回豆包结果，不重新提交生成
app.post('/:id/recall-video', async (c) => {
  const id = Number(c.req.param('id'))
  const [row] = await db.select().from(schema.sysTask).where(eq(schema.sysTask.id, id)).limit(1)
  if (!row) return notFound(c, '任务不存在')
  if (!(await canAccessTask(c, row))) return notFound(c, '任务不存在')
  try {
    return success(c, await recallDoubaoVideoTask(id))
  } catch (err: any) {
    return badRequest(c, err?.message || '召回视频失败')
  }
})

// POST /tasks/:id/cancel — 仅取消 MiniMax H3 / ComfyUI 视频任务
app.post('/:id/cancel', async (c) => {
  const id = Number(c.req.param('id'))
  const [row] = await db.select().from(schema.sysTask).where(eq(schema.sysTask.id, id)).limit(1)
  if (!row) return notFound(c, '任务不存在')
  if (!(await canAccessTask(c, row))) return notFound(c, '任务不存在')
  try {
    return success(c, await cancelComfyVideoTask(id))
  } catch (err: any) {
    return badRequest(c, err?.message || '取消任务失败')
  }
})

// DELETE /tasks/:id
app.delete('/:id', async (c) => {
  const id = Number(c.req.param('id'))
  const [row] = await db.select().from(schema.sysTask).where(eq(schema.sysTask.id, id)).limit(1)
  if (!row) return notFound(c, '任务不存在')
  if (!(await canAccessTask(c, row))) return notFound(c, '任务不存在')
  if (row.status === 'pending' || row.status === 'processing' || row.status === 'canceling') {
    return badRequest(c, '进行中的任务不能删除，请先取消或等待任务结束')
  }
  await db.delete(schema.sysTask).where(eq(schema.sysTask.id, id))
  return success(c)
})

export default app
