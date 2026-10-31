import { Hono } from 'hono'
import { eq } from 'drizzle-orm'
import { db, getInsertId, schema } from '../db/index.js'
import { success, notFound, created, badRequest, now } from '../utils/response.js'
import { toSnakeCase } from '../utils/transform.js'
import { joinProviderUrl } from '../services/adapters/url.js'
import { isOfficialProvider, parseConfigAllowedUserIds, parseConfigTemperature } from '../services/ai.js'
import { redactUrl, logTaskError, logTaskProgress, logTaskSuccess } from '../utils/task-logger.js'
import { currentUser, requireAdmin } from '../middleware/auth.js'
import { getAIServiceSwitches, setAIServiceEnabled, type AIServiceType } from '../services/app-settings.js'

const app = new Hono()

// AI 服务配置由管理员全局维护；普通用户可读可选，但永不返回完整密钥。
const canRead = (
  row: { ownerId: number | null; provider?: string | null; settings?: string | null },
  me: { id: number; bypass: boolean; isAdmin: boolean },
) => me.bypass || me.isAdmin || (
  row.ownerId === null
  && ((row.provider || '').toLowerCase() !== 'doubao_local'
    || parseConfigAllowedUserIds(row.settings).includes(me.id))
)

/** 归一化 temperature 入参：null=未设置；合法值 0~2；非法抛错 */
function normalizeTemperature(v: any): number | null {
  if (v === null || v === undefined || v === '') return null
  const n = Number(v)
  if (!Number.isFinite(n) || n < 0 || n > 2) throw new Error('invalid temperature')
  return n
}

function parseSettings(raw: string | null | undefined): Record<string, any> {
  try {
    const parsed = raw ? JSON.parse(raw) : {}
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {}
  } catch {
    return {}
  }
}

function normalizeModels(value: unknown): string[] {
  const values = Array.isArray(value) ? value : value == null ? [] : [value]
  return [...new Set(values.map(model => String(model || '').trim()).filter(Boolean))]
}

async function normalizeAllowedUserIds(value: unknown): Promise<number[]> {
  if (!Array.isArray(value)) throw new Error('allowed_user_ids 必须是数组')
  const ids = [...new Set(value.map(Number).filter(id => Number.isInteger(id) && id > 0))]
  if (!ids.length) return []
  const users = await db.select({ id: schema.users.id }).from(schema.users)
  const existing = new Set(users.map(user => user.id))
  if (ids.some(id => !existing.has(id))) throw new Error('允许用户中包含不存在的账号')
  return ids
}

/** 把 settings JSON 中的 temperature 透出为顶层字段，便于前端直接读写 */
function withParsedFields(r: any, includeSecret = false) {
  const parsed: Record<string, any> = {
    ...toSnakeCase(r),
    model: normalizeModels(r.model ? JSON.parse(r.model) : []),
    temperature: parseConfigTemperature(r.settings),
    allowed_user_ids: (r.provider || '').toLowerCase() === 'doubao_local'
      ? parseConfigAllowedUserIds(r.settings)
      : [],
    has_api_key: !!String(r.apiKey || '').trim(),
  }
  if (!includeSecret) {
    delete parsed.api_key
    delete parsed.base_url
    delete parsed.endpoint
    delete parsed.query_endpoint
    delete parsed.settings
    delete parsed.owner_id
    delete parsed.allowed_user_ids
  }
  return parsed
}

function bearerHeaders(apiKey?: string, withJson = false) {
  const headers: Record<string, string> = {}
  if (apiKey) headers.Authorization = `Bearer ${apiKey}`
  if (withJson) headers['Content-Type'] = 'application/json'
  return headers
}

function geminiHeaders(apiKey?: string, withJson = false) {
  const headers: Record<string, string> = {}
  if (apiKey) {
    headers['x-goog-api-key'] = apiKey
  }
  if (withJson) headers['Content-Type'] = 'application/json'
  return headers
}

function comfyClusterKey(serviceType: string, provider: string, baseUrl: string): string | null {
  if (serviceType !== 'video' || provider.toLowerCase() !== 'minimax') return null
  try {
    const url = new URL(baseUrl)
    if (url.port !== '8188' && url.port !== '8189') return null
    return `${url.protocol}//${url.hostname}${url.pathname.replace(/\/+$/, '')}`.toLowerCase()
  } catch {
    return null
  }
}

function buildProbe(serviceType: string, provider: string, baseUrl: string, model?: string, apiKey?: string) {
  const p = provider.toLowerCase()
  const m = model || ''

  if (p === 'gemini') {
    // 探针统一走 generateContent:文本运行时(AI SDK)走的就是它,官方与中转站都支持;
    // interactions 端点很多中转站未配置,探它会误报 500。
    // 用最小合法请求体而非空体——空体在部分中转站会触发上游认证失败的误报
    const modelName = m || 'gemini-3.1-pro-preview'
    const url = new URL(joinProviderUrl(baseUrl, '/v1beta', `/models/${modelName}:generateContent`))
    if (apiKey) url.searchParams.set('key', apiKey)
    return {
      method: 'POST',
      url: url.toString(),
      headers: geminiHeaders(apiKey, true),
      body: { contents: [{ parts: [{ text: 'hi' }] }] },
    }
  }

  if (p === 'openai') {
    return {
      method: 'GET',
      url: joinProviderUrl(baseUrl, '/v1', '/models'),
      headers: bearerHeaders(apiKey),
      body: undefined,
    }
  }

  if (p === 'volcengine') {
    const path = serviceType === 'video'
      ? '/contents/generations/tasks'
      : serviceType === 'text'
        ? '/chat/completions'
        : '/images/generations'
    return {
      method: 'POST',
      url: joinProviderUrl(baseUrl, '/api/v3', path),
      headers: bearerHeaders(apiKey, true),
      body: {},
    }
  }

  if (p === 'minimax') {
    // H3 的 MiniMax H3 Provider 走本地 ComfyUI，不触发真实生成任务。
    return {
      method: 'GET',
      url: joinProviderUrl(baseUrl, '', '/system_stats'),
      headers: bearerHeaders(apiKey),
      body: undefined,
    }
  }

  if (p === 'aliyun') {
    // Wan 3.0 仅支持异步提交；空请求体不会创建计费任务，仅用于验证地域端点与鉴权是否可达。
    return {
      method: 'POST',
      url: joinProviderUrl(baseUrl, '/api/v1', '/services/aigc/video-generation/video-synthesis'),
      headers: {
        ...bearerHeaders(apiKey, true),
        'X-DashScope-Async': 'enable',
      },
      body: {},
    }
  }

  if (p === 'doubao_local') {
    return {
      method: 'GET',
      url: joinProviderUrl(baseUrl, '', '/docs'),
      headers: apiKey ? { 'X-API-Key': apiKey } : {},
      body: undefined,
    }
  }

  return {
    method: 'GET',
    url: joinProviderUrl(baseUrl, '', m ? `/${m}` : '/'),
    headers: bearerHeaders(apiKey),
    body: undefined,
  }
}

// GET /ai-configs?service_type=text — 全局配置；非管理员只返回可选元数据
app.get('/', async (c) => {
  const serviceType = c.req.query('service_type')
  let rows = await db.select().from(schema.aiServiceConfigs)
  if (serviceType) rows = rows.filter(r => r.serviceType === serviceType)
  const me = currentUser(c)
  rows = rows.filter(r => canRead(r, me) && isOfficialProvider(r.serviceType, r.provider))

  const switches = getAIServiceSwitches()
  const parsed = rows.map(row => ({
    ...withParsedFields(row, me.bypass || me.isAdmin),
    service_enabled: switches[row.serviceType as AIServiceType] ?? true,
  }))
  return success(c, parsed)
})

// GET /ai-configs/service-switches — 文本、图片、视频能力的全局总开关
app.get('/service-switches', async (c) => success(c, getAIServiceSwitches()))

// PUT /ai-configs/service-switches/:type — 仅管理员可切换，不改变各配置自身的启停状态
app.put('/service-switches/:type', requireAdmin, async (c) => {
  const type = c.req.param('type') as AIServiceType
  if (!['text', 'image', 'video'].includes(type)) return badRequest(c, '不支持的服务类型')
  const body = await c.req.json().catch(() => null)
  if (typeof body?.enabled !== 'boolean') return badRequest(c, 'enabled 必须是布尔值')
  return success(c, { type, enabled: setAIServiceEnabled(type, body.enabled) })
})

// POST /ai-configs
app.post('/', requireAdmin, async (c) => {
  const body = await c.req.json()
  const ts = now()

  // 验证必填字段
  if (!body.service_type || !body.provider) {
    return badRequest(c, '需要 service_type 与 provider')
  }
  if (!isOfficialProvider(body.service_type, body.provider)) {
    return badRequest(c, '不支持的 service_type/provider')
  }
  const newClusterKey = comfyClusterKey(body.service_type, body.provider, body.base_url || '')
  if (newClusterKey) {
    const existingConfigs = await db.select().from(schema.aiServiceConfigs)
    if (existingConfigs.some(config => comfyClusterKey(config.serviceType, config.provider || '', config.baseUrl) === newClusterKey)) {
      return badRequest(c, 'MiniMax H3 的 8188/8189 共用一条 AI 服务配置，请编辑已有的 ComfyUI 集群配置')
    }
  }

  let temperature: number | null = null
  if ('temperature' in body) {
    try {
      temperature = normalizeTemperature(body.temperature)
    } catch {
      return badRequest(c, 'temperature 须为 0 到 2 之间的数字')
    }
  }
  if (String(body.provider).toLowerCase() === 'doubao_local' && !String(body.base_url || '').trim()) {
    return badRequest(c, '豆包本地管理台需要 base_url')
  }
  const settings: Record<string, any> = {}
  if (temperature !== null) settings.temperature = temperature
  if (String(body.provider).toLowerCase() === 'doubao_local') {
    try {
      settings.allowed_user_ids = await normalizeAllowedUserIds(body.allowed_user_ids ?? [])
    } catch (error: any) {
      return badRequest(c, error.message || '允许用户不合法')
    }
  }

  const res = await db.insert(schema.aiServiceConfigs).values({
    serviceType: body.service_type,
    provider: body.provider,
    name: body.name || `${body.provider}-${body.service_type}`,
    baseUrl: body.base_url || '',
    apiKey: body.api_key || '',
    model: JSON.stringify(normalizeModels(body.model)),
    priority: body.priority || 0,
    isActive: true,
    settings: Object.keys(settings).length ? JSON.stringify(settings) : null,
    createdAt: ts,
    updatedAt: ts,
    ownerId: null,
  })

  const [row] = await db.select().from(schema.aiServiceConfigs)
    .where(eq(schema.aiServiceConfigs.id, getInsertId(res)))

  return created(c, withParsedFields(row, true))
})

// POST /ai-configs/test
app.post('/test', requireAdmin, async (c) => {
  const body = await c.req.json()
  if (!body.service_type || !body.provider || !body.base_url) {
    return badRequest(c, '需要 service_type、provider 与 base_url')
  }
  if (!isOfficialProvider(body.service_type, body.provider)) {
    return badRequest(c, '不支持的 service_type/provider')
  }

  const model = Array.isArray(body.model) ? body.model[0] : body.model
  const probe = buildProbe(body.service_type, body.provider, body.base_url, model, body.api_key)
  const probeUrl = redactUrl(probe.url)

  logTaskProgress('AIConfig', 'probe-start', {
    serviceType: body.service_type,
    provider: body.provider,
    method: probe.method,
    url: probeUrl,
  })

  try {
    const resp = await fetch(probe.url, {
      method: probe.method,
      headers: probe.headers,
      body: probe.body ? JSON.stringify(probe.body) : undefined,
    })
    const text = await resp.text()
    const reachable = [200, 204, 400, 401, 403].includes(resp.status)
    const payload = {
      ok: resp.ok,
      reachable,
      status: resp.status,
      status_text: resp.statusText,
      method: probe.method,
      url: probeUrl,
      message: reachable
        ? (resp.ok ? '端点可访问，认证与路径基本正常' : '端点已响应，请根据状态码判断认证或路径是否正确')
        : '端点未按预期响应，请检查 Base URL 和代理前缀',
      response_preview: text.slice(0, 240),
    }
    if (reachable) {
      logTaskSuccess('AIConfig', 'probe-done', {
        provider: body.provider,
        status: resp.status,
        url: probeUrl,
      })
    } else {
      logTaskError('AIConfig', 'probe-unexpected', {
        provider: body.provider,
        status: resp.status,
        url: probeUrl,
      })
    }
    return success(c, payload)
  } catch (error: any) {
    logTaskError('AIConfig', 'probe-failed', {
      provider: body.provider,
      url: probeUrl,
      error: error.message,
    })
    return success(c, {
      ok: false,
      reachable: false,
      method: probe.method,
      url: probeUrl,
      message: error.message || '请求失败',
      response_preview: '',
    })
  }
})

// GET /ai-configs/:id — 越权与不存在统一 404，不泄露资源存在性
app.get('/:id', async (c) => {
  const id = Number(c.req.param('id'))
  const [row] = await db.select().from(schema.aiServiceConfigs).where(eq(schema.aiServiceConfigs.id, id))
  const me = currentUser(c)
  if (!row || !canRead(row, me)) return notFound(c)
  return success(c, {
    ...withParsedFields(row, me.bypass || me.isAdmin),
    service_enabled: getAIServiceSwitches()[row.serviceType as AIServiceType] ?? true,
  })
})

// PUT /ai-configs/:id
app.put('/:id', requireAdmin, async (c) => {
  const id = Number(c.req.param('id'))
  const body = await c.req.json()
  const [existing] = await db.select().from(schema.aiServiceConfigs).where(eq(schema.aiServiceConfigs.id, id))
  if (!existing) return notFound(c)

  const serviceType = 'service_type' in body ? body.service_type : existing.serviceType
  const provider = 'provider' in body ? body.provider : existing.provider
  if (!isOfficialProvider(serviceType, provider)) {
    return badRequest(c, '不支持的 service_type/provider')
  }
  const currentSettings = parseSettings(existing.settings)
  const effectiveBaseUrl = 'base_url' in body ? String(body.base_url || '').trim() : String(existing.baseUrl || '').trim()
  if (String(provider).toLowerCase() === 'doubao_local' && !effectiveBaseUrl) {
    return badRequest(c, '豆包本地管理台需要 base_url')
  }
  const updatedBaseUrl = 'base_url' in body ? body.base_url : existing.baseUrl
  const updatedClusterKey = comfyClusterKey(serviceType, provider, updatedBaseUrl || '')
  if (updatedClusterKey) {
    const existingConfigs = await db.select().from(schema.aiServiceConfigs)
    if (existingConfigs.some(config => config.id !== id
      && comfyClusterKey(config.serviceType, config.provider || '', config.baseUrl) === updatedClusterKey)) {
      return badRequest(c, 'MiniMax H3 的 8188/8189 共用一条 AI 服务配置，请编辑已有的 ComfyUI 集群配置')
    }
  }

  const updates: Record<string, any> = { updatedAt: now() }

  if ('service_type' in body) updates.serviceType = body.service_type
  if ('provider' in body) updates.provider = body.provider
  if ('name' in body) updates.name = body.name
  if ('base_url' in body) updates.baseUrl = body.base_url
  if ('api_key' in body) updates.apiKey = body.api_key
  if ('model' in body) updates.model = JSON.stringify(normalizeModels(body.model))
  if ('priority' in body) updates.priority = body.priority
  if ('is_active' in body) updates.isActive = body.is_active
  const nextSettings = { ...currentSettings }
  let settingsChanged = false
  if ('temperature' in body) {
    let temperature: number | null
    try {
      temperature = normalizeTemperature(body.temperature)
    } catch {
      return badRequest(c, 'temperature 须为 0 到 2 之间的数字')
    }
    // 与已有 settings 合并，清空的 temperature 从 JSON 中移除
    if (temperature === null) delete nextSettings.temperature
    else nextSettings.temperature = temperature
    settingsChanged = true
  }
  if (String(provider).toLowerCase() === 'doubao_local') {
    if ('allowed_user_ids' in body || String(existing.provider || '').toLowerCase() !== 'doubao_local') {
      try {
        nextSettings.allowed_user_ids = await normalizeAllowedUserIds(body.allowed_user_ids ?? [])
      } catch (error: any) {
        return badRequest(c, error.message || '允许用户不合法')
      }
      settingsChanged = true
    }
  } else if ('allowed_user_ids' in nextSettings) {
    delete nextSettings.allowed_user_ids
    settingsChanged = true
  }
  if (settingsChanged) updates.settings = Object.keys(nextSettings).length ? JSON.stringify(nextSettings) : null

  await db.update(schema.aiServiceConfigs).set(updates).where(eq(schema.aiServiceConfigs.id, id))
  return success(c)
})

// DELETE /ai-configs/:id
app.delete('/:id', requireAdmin, async (c) => {
  const id = Number(c.req.param('id'))
  const [row] = await db.select().from(schema.aiServiceConfigs).where(eq(schema.aiServiceConfigs.id, id))
  if (!row) return notFound(c)
  await db.delete(schema.aiServiceConfigs).where(eq(schema.aiServiceConfigs.id, id))
  return success(c)
})

// GET /ai-providers
export const aiProviders = new Hono()
aiProviders.get('/', async (c) => {
  const rows = await db.select().from(schema.aiServiceProviders)
  const parsed = rows.map(r => ({
    ...toSnakeCase(r),
    preset_models: r.presetModels ? JSON.parse(r.presetModels) : [],
  }))
  return success(c, parsed)
})

export default app
