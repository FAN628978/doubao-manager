const BASE = '/api/v1'

export const TOKEN_KEY = 'h3-token'

export function getToken(): string {
  if (import.meta.server) return ''
  try { return localStorage.getItem(TOKEN_KEY) || '' } catch { return '' }
}

export function setToken(token: string) {
  if (import.meta.server) return
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token)
    else localStorage.removeItem(TOKEN_KEY)
  } catch {}
}

// 会话类端点 401 不触发跳转（避免登录页/守卫自锁死循环）
const AUTH_EXEMPT_PATHS = ['/auth/login', '/auth/status', '/auth/me', '/auth/setup']
let redirecting401 = false

function authHeaders(): Record<string, string> {
  const token = getToken()
  return token ? { authorization: `Bearer ${token}` } : {}
}

function handleUnauthorized(path: string) {
  if (redirecting401 || AUTH_EXEMPT_PATHS.some(p => path.startsWith(p))) return
  // 已在登录/初始化页时不再整页跳转：页面级插件（如 tour hydrate）的后台 401
  // 会在 login 上再次触发本函数，location.href 指向自身 = 无限刷新循环
  const page = window.location.pathname
  if (page === '/login' || page === '/setup') return
  redirecting401 = true
  setToken('')
  window.location.href = '/login'
}

async function req<T = any>(method: string, path: string, body?: any): Promise<T> {
  const opts: RequestInit = {
    method,
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    // 工作台刷新必须命中服务端最新状态，不能复用浏览器缓存的 GET 响应。
    ...(method === 'GET' ? { cache: 'no-store' as RequestCache } : {}),
  }
  if (body) opts.body = JSON.stringify(body)

  const start = performance.now()
  console.log(`%c[API] %c${method} %c${path}`, 'color:#888', 'color:#4fc3f7;font-weight:bold', 'color:#ccc', body || '')

  try {
    const resp = await fetch(`${BASE}${path}`, opts)
    const json = await resp.json()
    const ms = Math.round(performance.now() - start)

    if (resp.status === 401) handleUnauthorized(path)

    if (!resp.ok || (json.code && json.code >= 400)) {
      console.log(`%c[API] %c${method} ${path} %c${resp.status} %c${ms}ms`, 'color:#888', 'color:#ef5350', 'color:#ef5350;font-weight:bold', 'color:#888', json.message || '')
      throw new Error(json.message || `${resp.status}`)
    }

    console.log(`%c[API] %c${method} ${path} %c${resp.status} %c${ms}ms`, 'color:#888', 'color:#79b8ef', 'color:#79b8ef;font-weight:bold', 'color:#888')
    return json.data ?? json
  } catch (err: any) {
    if (!err.message?.match(/^\d{3}$/)) {
      const ms = Math.round(performance.now() - start)
      console.log(`%c[API] %c${method} ${path} %cERROR %c${ms}ms`, 'color:#888', 'color:#ef5350', 'color:#ef5350;font-weight:bold', 'color:#888', err.message)
    }
    throw err
  }
}

// 二进制文件下载（POST）：Blob 接收并触发保存；后端返回 JSON 错误时抛出原始 message；
// 文件名优先取响应 Content-Disposition 的 filename*=UTF-8''，缺失时用 fallbackName
async function downloadFile(path: string, body: any, fallbackName: string) {
  const start = performance.now()
  console.log(`%c[API] %cPOST %c${path}`, 'color:#888', 'color:#4fc3f7;font-weight:bold', 'color:#ccc', 'download')
  const resp = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify(body || {}),
  })
  if (resp.status === 401) { handleUnauthorized(path); throw new Error('401') }
  if (!resp.ok) {
    let msg = `${resp.status}`
    if ((resp.headers.get('content-type') || '').includes('application/json')) {
      try { msg = (await resp.json()).message || msg } catch { /* keep status */ }
    }
    throw new Error(msg)
  }
  let filename = fallbackName
  const m = (resp.headers.get('content-disposition') || '').match(/filename\*=UTF-8''([^;]+)/i)
  if (m) { try { filename = decodeURIComponent(m[1]) } catch { /* keep fallback */ } }
  const blob = await resp.blob()
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 10_000)
  console.log(`%c[API] %cPOST ${path} %cdownloaded %c${Math.round(performance.now() - start)}ms`, 'color:#888', 'color:#79b8ef', 'color:#ccc', 'color:#ccc', filename)
}

export const api = {
  get: <T = any>(p: string) => req<T>('GET', p),
  post: <T = any>(p: string, b?: any) => req<T>('POST', p, b),
  put: <T = any>(p: string, b?: any) => req<T>('PUT', p, b),
  del: <T = any>(p: string) => req<T>('DELETE', p),
  download: (p: string, b?: any, filename?: string) => downloadFile(p, b, filename || 'download'),
}

export type ToolboxKind = 'prompts' | 'commands'

export interface ToolboxItem {
  id: number
  title: string
  description: string | null
  content?: string
  command?: string
  category: string | null
  tags: string[]
  is_favorite: boolean
  sort_order: number
  usage_count: number
  created_at: string
  updated_at: string
}

export interface ToolboxListResponse {
  items: ToolboxItem[]
  total: number
  page: number
  page_size: number
}

export interface ToolboxListParams {
  keyword?: string
  category?: string
  favorite?: boolean
  page?: number
  page_size?: number
}

export const toolboxAPI = {
  list(kind: ToolboxKind, params: ToolboxListParams = {}) {
    const query = new URLSearchParams()
    if (params.keyword) query.set('keyword', params.keyword)
    if (params.category) query.set('category', params.category)
    if (params.favorite) query.set('favorite', '1')
    if (params.page) query.set('page', String(params.page))
    if (params.page_size) query.set('page_size', String(params.page_size))
    return api.get<ToolboxListResponse>(`/toolbox/${kind}${query.size ? `?${query.toString()}` : ''}`)
  },
  get: (kind: ToolboxKind, id: number) => api.get<ToolboxItem>(`/toolbox/${kind}/${id}`),
  create: (kind: ToolboxKind, body: Record<string, any>) => api.post<ToolboxItem>(`/toolbox/${kind}`, body),
  update: (kind: ToolboxKind, id: number, body: Record<string, any>) => api.put<ToolboxItem>(`/toolbox/${kind}/${id}`, body),
  remove: (kind: ToolboxKind, id: number) => api.del(`/toolbox/${kind}/${id}`),
  markUsed: (kind: ToolboxKind, id: number) => api.post<ToolboxItem>(`/toolbox/${kind}/${id}/use`, {}),
}

export const promptEditAPI = {
  get: (dramaId: number, targetType: string, targetId: number, episodeId?: number, fresh?: boolean) => api.get(`/prompt-edits?drama_id=${dramaId}&target_type=${encodeURIComponent(targetType)}&target_id=${targetId}${episodeId ? `&episode_id=${episodeId}` : ''}${fresh ? '&fresh=1' : ''}`),
  message: (sessionId: number, message: string, model?: string, configId?: number) => api.post('/prompt-edits/messages', { session_id: sessionId, message, model: model || undefined, config_id: configId || undefined }),
  apply: (candidateId: number, prompt?: string) => api.post(`/prompt-edits/candidates/${candidateId}/apply`, prompt === undefined ? {} : { prompt }),
  reject: (candidateId: number) => api.post(`/prompt-edits/candidates/${candidateId}/reject`),
}

export const dramaAPI = {
  list: (params?: { page?: number; page_size?: number; owner_id?: number }) => {
    const query = new URLSearchParams()
    if (params?.page) query.set('page', String(params.page))
    if (params?.page_size) query.set('page_size', String(params.page_size))
    if (params?.owner_id) query.set('owner_id', String(params.owner_id))
    return api.get<{ items: any[] }>(`/dramas${query.size ? `?${query}` : ''}`)
  },
  get: (id: number) => api.get(`/dramas/${id}`),
  create: (data: any) => api.post('/dramas', data),
  update: (id: number, data: any) => api.put(`/dramas/${id}`, data),
  transferOwner: (id: number, ownerId: number) => api.put(`/dramas/${id}/owner`, { owner_id: ownerId }),
  del: (id: number) => api.del(`/dramas/${id}`),
  ownerPresence: () => api.get<{ id: number; username: string; remark: string | null; is_online: boolean; last_seen_at: string | null }[]>('/dramas/owner-presence'),
}

export const episodeAPI = {
  get: (id: number) => api.get(`/episodes/${id}`),
  byNumber: (dramaId: number, episodeNumber: number) => api.get(`/episodes/by-number?drama_id=${dramaId}&episode_number=${episodeNumber}`),
  create: (data: any) => api.post('/episodes', data),
  update: (id: number, data: any) => api.put(`/episodes/${id}`, data),
  del: (id: number) => api.del(`/episodes/${id}`),
  characters: (id: number) => api.get(`/episodes/${id}/characters`),
  scenes: (id: number) => api.get(`/episodes/${id}/scenes`),
  props: (id: number) => api.get(`/episodes/${id}/props`),
  importAssets: (id: number, data: { character_ids?: number[]; scene_ids?: number[]; prop_ids?: number[] }) => api.post(`/episodes/${id}/assets/import`, data),
  storyboards: (id: number) => api.get(`/episodes/${id}/storyboards`),
  pipelineStatus: (id: number) => api.get(`/episodes/${id}/pipeline-status`),
  extract: (id: number, target: string, model?: string, configId?: number, executionEngine: 'claude_cli' = 'claude_cli') => api.post(`/episodes/${id}/extract`, { target, model: model || undefined, config_id: configId || undefined, execution_engine: executionEngine }),
  extractStatus: (id: number) => api.get(`/episodes/${id}/extract-status`),
  generateVideoPrompts: (id: number, model?: string, configId?: number, storyboardIds?: number[], videoModel?: string, videoConfigId?: number) => api.post(`/episodes/${id}/generate-video-prompts`, {
    model: model || undefined,
    config_id: configId || undefined,
    video_model: videoModel || undefined,
    video_config_id: videoConfigId || undefined,
    storyboard_ids: storyboardIds?.length ? storyboardIds : undefined,
  }),
  videoPromptsStatus: (id: number) => api.get(`/episodes/${id}/video-prompts-status`),
  // 该集在飞的 Agent 任务类型（刷新页面后恢复按钮锁，防止重复发起并发拆镜）
  agentRunning: (id: number) => api.get(`/agent/running?episode_id=${id}`),
  // 分镜生成中的隔离草稿；校验通过前正式 storyboards 不会被替换
  storyboardDraft: (id: number) => api.get(`/agent/storyboard-draft?episode_id=${id}`),
}

export const storyboardAPI = {
  create: (data: any) => api.post('/storyboards', data),
  update: (id: number, data: any) => api.put(`/storyboards/${id}`, data),
  del: (id: number) => api.del(`/storyboards/${id}`),
}

export const storyboardReviewAPI = {
  autofix: (dramaId: number, episodeId: number, model?: string, configId?: number, executionEngine: 'claude_cli' = 'claude_cli') => api.post('/agent/storyboard_v8_reviewer/chat', {
    message: '按 V8 母版检查整集并直接修复',
    review_mode: 'autofix',
    drama_id: dramaId,
    episode_id: episodeId,
    model: model || undefined,
    config_id: configId || undefined,
    execution_engine: executionEngine || undefined,
  }),
  // 旧接口保留给已部署客户端；当前工作台只使用 autofix。
  check: (dramaId: number, episodeId: number, model?: string, configId?: number) => api.post('/agent/storyboard_v8_reviewer/chat', {
    message: '检查当前正式 V8 分镜',
    review_mode: 'check',
    drama_id: dramaId,
    episode_id: episodeId,
    model: model || undefined,
    config_id: configId || undefined,
  }),
  repair: (dramaId: number, episodeId: number, issues: any[], model?: string, configId?: number) => api.post('/agent/storyboard_v8_reviewer/chat', {
    message: '修复用户确认的 V8 分镜问题',
    review_mode: 'repair',
    issues,
    drama_id: dramaId,
    episode_id: episodeId,
    model: model || undefined,
    config_id: configId || undefined,
  }),
}

export const agentRuntimeAPI = {
  claude: (refresh = false) => api.get(`/agent/runtimes/claude${refresh ? '?refresh=1' : ''}`),
}

export const characterAPI = {
  create: (data: any) => api.post('/characters', data),
  update: (id: number, data: any) => api.put(`/characters/${id}`, data),
  del: (id: number) => api.del(`/characters/${id}`),
  generatePrompt: (id: number, episodeId: number, force = false, textModel?: string, textConfigId?: number) => api.post(`/characters/${id}/generate-prompt`, { episode_id: episodeId, force, text_model: textModel || undefined, text_config_id: textConfigId || undefined }),
  generateImage: (id: number, episodeId: number, model?: string, configId?: number, textModel?: string, textConfigId?: number) => api.post(`/characters/${id}/generate-image`, { episode_id: episodeId, model: model || undefined, config_id: configId || undefined, text_model: textModel || undefined, text_config_id: textConfigId || undefined }),
  batchImages: (ids: number[], episodeId: number, model?: string, configId?: number, textModel?: string, textConfigId?: number) => api.post('/characters/batch-generate-images', { character_ids: ids, episode_id: episodeId, model: model || undefined, config_id: configId || undefined, text_model: textModel || undefined, text_config_id: textConfigId || undefined }),
}

// 角色服装/造型状态（底图之外的状态图；分镜按 storyboard.character_state_ids 选择）
export const characterStateAPI = {
  list: (characterId: number) => api.get(`/characters/${characterId}/states`),
  create: (characterId: number, data: { name: string; description?: string }) => api.post(`/characters/${characterId}/states`, data),
  update: (characterId: number, stateId: number, data: any) => api.put(`/characters/${characterId}/states/${stateId}`, data),
  del: (characterId: number, stateId: number) => api.del(`/characters/${characterId}/states/${stateId}`),
  generateImage: (characterId: number, stateId: number, episodeId: number, model?: string, configId?: number, textModel?: string, textConfigId?: number) => api.post(`/characters/${characterId}/states/${stateId}/generate-image`, { episode_id: episodeId, model: model || undefined, config_id: configId || undefined, text_model: textModel || undefined, text_config_id: textConfigId || undefined }),
}

export const sceneAPI = {
  create: (data: any) => api.post('/scenes', data),
  update: (id: number, data: any) => api.put(`/scenes/${id}`, data),
  del: (id: number) => api.del(`/scenes/${id}`),
  generatePrompt: (id: number, episodeId: number, force = false, textModel?: string, textConfigId?: number) => api.post(`/scenes/${id}/generate-prompt`, { episode_id: episodeId, force, text_model: textModel || undefined, text_config_id: textConfigId || undefined }),
  generateImage: (id: number, episodeId: number, model?: string, configId?: number, textModel?: string, textConfigId?: number) => api.post(`/scenes/${id}/generate-image`, { episode_id: episodeId, model: model || undefined, config_id: configId || undefined, text_model: textModel || undefined, text_config_id: textConfigId || undefined }),
}

export const propAPI = {
  create: (data: any) => api.post('/props', data),
  update: (id: number, data: any) => api.put(`/props/${id}`, data),
  del: (id: number) => api.del(`/props/${id}`),
  generatePrompt: (id: number, episodeId: number, force = false, textModel?: string, textConfigId?: number) => api.post(`/props/${id}/generate-prompt`, { episode_id: episodeId, force, text_model: textModel || undefined, text_config_id: textConfigId || undefined }),
  generateImage: (id: number, episodeId: number, model?: string, configId?: number, textModel?: string, textConfigId?: number) => api.post(`/props/${id}/generate-image`, { episode_id: episodeId, model: model || undefined, config_id: configId || undefined, text_model: textModel || undefined, text_config_id: textConfigId || undefined }),
}

// 统一生成任务（图片/视频）：POST 带 type 字段，列表按 type 过滤
export const taskAPI = {
  generate: (d: any) => api.post('/tasks', d),
  get: (id: number) => api.get(`/tasks/${id}`),
  cancel: (id: number) => api.post(`/tasks/${id}/cancel`, {}),
  recallVideo: (id: number) => api.post(`/tasks/${id}/recall-video`, {}),
  del: (id: number) => api.del(`/tasks/${id}`),
  list: (params?: { type?: 'image' | 'video'; drama_id?: number; storyboard_id?: number; character_id?: number; scene_id?: number; prop_id?: number; state_id?: number; scope?: 'standalone' | 'mine' }) => {
    const query = new URLSearchParams()
    if (params?.type) query.set('type', params.type)
    if (params?.scope) query.set('scope', params.scope)
    if (params?.drama_id) query.set('drama_id', String(params.drama_id))
    if (params?.storyboard_id) query.set('storyboard_id', String(params.storyboard_id))
    if (params?.character_id) query.set('character_id', String(params.character_id))
    if (params?.scene_id) query.set('scene_id', String(params.scene_id))
    if (params?.prop_id) query.set('prop_id', String(params.prop_id))
    if (params?.state_id) query.set('state_id', String(params.state_id))
    return api.get(`/tasks${query.size ? `?${query.toString()}` : ''}`)
  },
  myQueue: (params?: { status?: '' | 'active' | 'completed' | 'failed'; page?: number; page_size?: number }) => {
    const query = new URLSearchParams()
    if (params?.status) query.set('status', params.status)
    if (params?.page) query.set('page', String(params.page))
    if (params?.page_size) query.set('page_size', String(params.page_size))
    return api.get<{ items: any[]; total: number; page: number; pageSize: number; counts: Record<string, number> }>(`/tasks/mine/queue?${query.toString()}`)
  },
  adminQueue: (params?: { status?: string; type?: string; page?: number; page_size?: number }) => {
    const query = new URLSearchParams()
    if (params?.status) query.set('status', params.status)
    if (params?.type) query.set('type', params.type)
    if (params?.page) query.set('page', String(params.page))
    if (params?.page_size) query.set('page_size', String(params.page_size))
    return api.get<{ items: any[]; total: number; page: number; pageSize: number; counts: Record<string, number> }>(`/tasks/admin/queue?${query.toString()}`)
  },
  // 按集聚合生成任务（sys_task + video_merges）
  listByEpisode: (episodeId: number) => api.get<{ tasks: any[]; merges: any[] }>(`/episodes/${episodeId}/generation-tasks`),
}

async function uploadReq<T = any>(path: string, file: File, fields?: Record<string, string>): Promise<T> {
  const fd = new FormData()
  fd.append('file', file)
  for (const [key, value] of Object.entries(fields || {})) fd.append(key, value)
  console.log(`%c[API] %cPOST %c${path} %c${file.name}`, 'color:#888', 'color:#4fc3f7;font-weight:bold', 'color:#ccc', 'color:#888')
  const resp = await fetch(`${BASE}${path}`, { method: 'POST', body: fd, headers: authHeaders() })
  const json = await resp.json()
  if (resp.status === 401) handleUnauthorized(path)
  if (!resp.ok || (json.code && json.code >= 400)) {
    console.log(`%c[API] %cPOST ${path} %c${resp.status}`, 'color:#888', 'color:#ef5350', 'color:#ef5350;font-weight:bold')
    throw new Error(json.message || `${resp.status}`)
  }
  return json.data ?? json
}

export const uploadAPI = {
  image: (f: File) => uploadReq<{ url: string; path: string }>('/upload/image', f),
  video: (f: File) => uploadReq<{ url: string; path: string }>('/upload/video', f),
  audio: (f: File) => uploadReq<{ url: string; path: string }>('/upload/audio', f),
}

export const viralBreakdownAPI = {
  list: () => api.get<any[]>('/viral/breakdowns'),
  get: (id: number) => api.get<any>(`/viral/breakdowns/${id}`),
  create: (file: File, fields: {
    title: string
    platform: string
    license_type: string
    copyright_holder?: string
    note?: string
  }) => uploadReq<any>('/viral/breakdowns', file, {
    title: fields.title,
    platform: fields.platform,
    license_type: fields.license_type,
    copyright_holder: fields.copyright_holder || '',
    note: fields.note || '',
  }),
  generateStrategy: (id: number) => api.post<any>(`/viral/breakdowns/${id}/strategy/generate`, {}),
  saveStrategy: (id: number, strategy: Record<string, any>) => api.put<any>(`/viral/breakdowns/${id}/strategy`, strategy),
  generateScript: (id: number) => api.post<any>(`/viral/breakdowns/${id}/script/generate`, {}),
  saveScript: (id: number, script: { title: string; logline: string; content: string }) => api.put<any>(`/viral/breakdowns/${id}/script`, script),
}

// 小说导入与逐集改编工作台（后端 routes/novels.ts）
export const novelAPI = {
  import: (f: File, title?: string) => uploadReq<{ id: number; chapters: any[] }>('/novels/import', f, title ? { title } : undefined),
  list: (params?: { drama_id?: number }) => api.get<{ items: any[]; total: number }>(`/novels${params?.drama_id ? `?drama_id=${params.drama_id}` : ''}`),
  get: (id: number) => api.get<any>(`/novels/${id}`),
  source: (id: number, start: number, end: number) => api.get<{ text: string; truncated: boolean; length: number }>(`/novels/${id}/source?start=${start}&end=${end}`),
  updateSettings: (id: number, data: { rules?: string; bible?: string }) => api.put(`/novels/${id}/settings`, data),
  attach: (id: number, dramaId: number) => api.post(`/novels/${id}/attach`, { drama_id: dramaId }),
  adaptation: (id: number) => api.get<any>(`/novels/${id}/adaptation`),
  generate: (id: number, options: { start_chapter: number; end_chapter: number; duration: number; mode: string; notes?: string }) => api.post(`/novels/${id}/generate`, options),
  generateStatus: (id: number) => api.get<any>(`/novels/${id}/generate-status`),
  cancel: (id: number) => api.post(`/novels/${id}/generate/cancel`, {}),
  importEpisodeSource: (id: number, number: number, data: { start_chapter: number; end_chapter: number }) => api.post<{ text: string; length: number; start_chapter: number; end_chapter: number }>(`/novels/${id}/episodes/${number}/source`, data),
  saveEpisode: (id: number, number: number, data: Record<string, string>) => api.put(`/novels/${id}/episodes/${number}`, data),
  reviseEpisode: (id: number, number: number, data: Record<string, string>) => api.put(`/novels/${id}/episodes/${number}/rework`, data),
  confirmEpisode: (id: number, number: number) => api.post(`/novels/${id}/episodes/${number}/confirm`, {}),
}

export const assetAPI = {
  list: () => api.get<any[]>('/assets'),
  create: (data: any) => api.post('/assets', data),
  update: (id: number, data: { name?: string; description?: string; category?: string; is_favorite?: boolean }) => api.put(`/assets/${id}`, data),
  markViewed: (id: number) => api.post(`/assets/${id}/view`, {}),
  del: (id: number) => api.del(`/assets/${id}`),
}
export const mergeAPI = {
  merge: (epId: number, storyboardIds?: number[]) => api.post(`/merge/episodes/${epId}/merge`, storyboardIds?.length ? { storyboard_ids: storyboardIds } : {}),
  status: (epId: number) => api.get(`/merge/episodes/${epId}/merge`),
  list: (epId: number) => api.get<any[]>(`/merge/episodes/${epId}/merges`),
  del: (mergeId: number) => api.del(`/merge/merges/${mergeId}`),
}
export const labAPI = {
  // 自由镜头视频提示词：正文直出，不落库
  generateVideoPrompt: (d: { description: string; duration?: number; aspect_ratio?: string; resolution?: string; model?: string; config_id?: number; reference_count?: number; references?: Array<{ index: number; name: string }>; style_prefix?: string }) =>
    api.post<{ video_prompt: string }>('/lab/video-prompt', d),
}

export const aiConfigAPI = {
  list: (t?: string) => api.get(`/ai-configs${t ? `?service_type=${t}` : ''}`),
  serviceSwitches: () => api.get<{ text: boolean; image: boolean; video: boolean }>('/ai-configs/service-switches'),
  setServiceSwitch: (type: 'text' | 'image' | 'video', enabled: boolean) =>
    api.put<{ type: string; enabled: boolean }>(`/ai-configs/service-switches/${type}`, { enabled }),
  create: (d: any) => api.post('/ai-configs', d),
  update: (id: number, d: any) => api.put(`/ai-configs/${id}`, d),
  del: (id: number) => api.del(`/ai-configs/${id}`),
  test: (d: any) => api.post('/ai-configs/test', d),
}

export const promptAPI = {
  list: () => api.get('/prompts'),
  get: (type: string) => api.get(`/prompts/${type}`),
  update: (type: string, d: any) => api.put(`/prompts/${type}`, d),
  setDefault: (type: string, d: any) => api.post(`/prompts/${type}/default`, d),
}

export const skillsAPI = {
  list: () => api.get('/skills'),
  get: (id: string) => api.get(`/skills/${id}`),
  create: (data: { id: string; name: string; description?: string }) => api.post('/skills', data),
  update: (id: string, content: string) => api.put(`/skills/${id}`, { content }),
  setDefault: (id: string, content: string) => api.post(`/skills/default/${id}`, { content }),
  del: (id: string) => api.del(`/skills/${id}`),
}

export const stylePresetAPI = {
  list: (all = false) => api.get(`/style-presets${all ? '?all=1' : ''}`),
  create: (d: any) => api.post('/style-presets', d),
  update: (id: number, d: any) => api.put(`/style-presets/${id}`, d),
  del: (id: number) => api.del(`/style-presets/${id}`),
}

export const storageAPI = {
  info: () => api.get('/storage'),
}

// 服务器/Docker 部署的版本检查与更新
export const serverUpdateAPI = {
  state: () => api.get('/server-update/state'),
  check: () => api.post('/server-update/check'),
  apply: () => api.post('/server-update/apply'),
}

export interface AuthStatus { enabled: boolean; needsSetup: boolean; registrationApprovalRequired: boolean }
export interface AuthUserInfo { id: number; username: string; displayName: string | null; isAdmin: boolean; isSubAdmin?: boolean; bypass?: boolean }
export interface LoginResult { token: string; expiresAt: string; user: AuthUserInfo }
export interface RegisterResult { id: number; username: string; displayName: string | null; approvalStatus: 'pending' | 'approved' }

export const authAPI = {
  status: () => api.get<AuthStatus>('/auth/status'),
  setup: (data: { username: string; password: string; display_name?: string }) => api.post<LoginResult>('/auth/setup', data),
  register: (data: { username: string; password: string }) => api.post<RegisterResult>('/auth/register', data),
  login: (data: { username: string; password: string }) => api.post<LoginResult>('/auth/login', data),
  logout: () => api.post('/auth/logout'),
  me: () => api.get<AuthUserInfo>('/auth/me'),
  heartbeat: () => api.post('/auth/heartbeat'),
}

export interface UserRow { id: number; username: string; displayName: string | null; remark: string | null; isAdmin: boolean; isSubAdmin: boolean; isActive: boolean; approvalStatus: 'pending' | 'approved' | 'rejected'; videoQueuePriority: -10 | 0 | 10; isOnline: boolean; createdAt: string; lastLoginAt: string | null; lastSeenAt: string | null }

export const userAPI = {
  list: () => api.get<UserRow[]>('/users'),
  registrationPolicy: () => api.get<{ approvalRequired: boolean }>('/users/registration-policy'),
  setRegistrationPolicy: (approvalRequired: boolean) => api.put<{ approvalRequired: boolean }>('/users/registration-policy', { approval_required: approvalRequired }),
  setVideoQueuePriority: (id: number, priority: -10 | 0 | 10) => api.put<{ userId: number; videoQueuePriority: number }>(`/users/${id}/video-queue-priority`, { priority }),
  create: (data: { username: string; password: string; display_name?: string; remark?: string; is_admin?: boolean; is_sub_admin?: boolean }) => api.post('/users', data),
  update: (id: number, data: { username?: string; display_name?: string; remark?: string; is_admin?: boolean; is_sub_admin?: boolean }) => api.put(`/users/${id}`, data),
  resetPassword: (id: number, password: string) => api.post(`/users/${id}/reset-password`, { password }),
  review: (id: number, status: 'approved' | 'rejected') => api.put(`/users/${id}/approval`, { status }),
  setStatus: (id: number, isActive: boolean) => api.put(`/users/${id}/status`, { is_active: isActive }),
  del: (id: number) => api.del(`/users/${id}`),
}

export interface MembershipPlan {
  id: number
  code: string
  name: string
  description: string | null
  includedCredits: number
}

export interface MembershipSummary {
  id: number
  userId: number
  creditsBalance: number
  creditsUsed: number
  unlimited: boolean
  plan: MembershipPlan
}

export const membershipAPI = {
  me: () => api.get<MembershipSummary>('/memberships/me'),
  plans: () => api.get<MembershipPlan[]>('/memberships/plans'),
  ledger: (limit = 50) => api.get<any[]>(`/memberships/ledger?limit=${limit}`),
  adminUsers: () => api.get<any[]>('/memberships/admin/users'),
  adminBillingMode: () => api.get<{ freeAll: boolean }>('/memberships/admin/billing-mode'),
  setAdminBillingMode: (freeAll: boolean) =>
    api.put<{ freeAll: boolean }>('/memberships/admin/billing-mode', { free_all: freeAll }),
  assign: (userId: number, data: { plan_id: number; reset_credits?: boolean }) =>
    api.put<MembershipSummary>(`/memberships/admin/users/${userId}`, data),
  adjustCredits: (userId: number, delta: number, reason: string) =>
    api.post<MembershipSummary>(`/memberships/admin/users/${userId}/credits`, { delta, reason }),
}

export interface TrialContactConfig { text: string }

export const settingsAPI = {
  /** 体验账号顶栏联系文案（全局一条，管理员维护） */
  trialContact: () => api.get<TrialContactConfig>('/settings/trial-contact'),
  saveTrialContact: (text: string) => api.put<TrialContactConfig>('/settings/trial-contact', { text }),
  /** 停用账号登录提示文案（全局一条，管理员维护） */
  disabledLoginText: () => api.get<TrialContactConfig>('/settings/disabled-login-text'),
  saveDisabledLoginText: (text: string) => api.put<TrialContactConfig>('/settings/disabled-login-text', { text }),
}
