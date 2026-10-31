/**
 * AI 服务抽象层 — 从数据库配置中获取 provider 和 API key
 */
import { db, schema } from '../db/index.js'
import { eq } from 'drizzle-orm'
import { logTaskProgress, logTaskWarn } from '../utils/task-logger.js'
import { joinProviderUrl } from './adapters/url.js'
import { getAIServiceDisabledMessage, getAIServiceEnabled } from './app-settings.js'

export type ServiceType = 'text' | 'image' | 'video'

export interface AIConfig {
  id?: number
  provider: string
  baseUrl: string
  apiKey: string
  model: string
  /** 采样温度，null 表示不设置（跟随服务商默认）。存于 ai_service_configs.settings JSON */
  temperature?: number | null
}

function effectiveApiKey(provider: string | null | undefined, storedKey: string | null | undefined): string {
  const key = (storedKey || '').trim()
  if (provider?.toLowerCase() !== 'minimax') return key
  // 本地 ComfyUI 的环境变量是机器端连接的权威密钥。
  return (process.env.COMFYUI_H3_API_KEY || process.env.H3_API_KEY || key).trim()
}

/** 从 settings JSON 解析 temperature；非法值一律视为未设置 */
export function parseConfigTemperature(settingsRaw: string | null | undefined): number | null {
  if (!settingsRaw) return null
  try {
    const t = JSON.parse(settingsRaw)?.temperature
    return typeof t === 'number' && Number.isFinite(t) ? t : null
  } catch {
    return null
  }
}

/** 豆包本地配置的显式用户白名单；非整数值一律忽略。 */
export function parseConfigAllowedUserIds(settingsRaw: string | null | undefined): number[] {
  if (!settingsRaw) return []
  try {
    const ids = JSON.parse(settingsRaw)?.allowed_user_ids
    if (!Array.isArray(ids)) return []
    return [...new Set(ids.map(Number).filter(id => Number.isInteger(id) && id > 0))]
  } catch {
    return []
  }
}

export const officialProviders: Record<ServiceType, readonly string[]> = {
  text: ['openai', 'gemini', 'volcengine'],
  image: ['openai', 'gemini', 'volcengine'],
  video: ['volcengine', 'minimax', 'aliyun', 'doubao_local'],
}

export function isOfficialProvider(serviceType?: string | null, provider?: string | null): boolean {
  const providers = officialProviders[serviceType as ServiceType]
  return !!providers && providers.includes((provider || '').toLowerCase())
}

export function getTextProviderBaseUrl(config: AIConfig) {
  const provider = config.provider.toLowerCase()

  if (provider === 'openai') {
    return joinProviderUrl(config.baseUrl, '/v1', '')
  }

  if (provider === 'gemini') {
    return joinProviderUrl(config.baseUrl, '/v1beta', '')
  }

  if (provider === 'volcengine') {
    return joinProviderUrl(config.baseUrl, '/api/v3', '')
  }

  return config.baseUrl
}

function sortActiveConfigs<T extends { priority: number | null; provider: string | null }>(rows: T[]) {
  rows.sort((a, b) => (b.priority || 0) - (a.priority || 0))
  return rows
}

// Agent 多步循环会逐步重复解析同一配置，相同配置只打一次日志避免刷屏
const lastLoggedActiveConfigKey = new Map<string, string>()
const lastLoggedConfigByIdKey = new Map<number, string>()

/**
 * 普通全局配置对所有用户可见；豆包本地配置仅对白名单用户可见。
 * ownerId 为空时是关闭鉴权的本地模式，保留原有不做用户过滤的行为。
 */
export function isConfigVisibleToOwner(
  row: { ownerId: number | null; provider?: string | null; settings?: string | null },
  ownerId?: number,
): boolean {
  if (ownerId === undefined) return true
  if ((row.provider || '').toLowerCase() === 'doubao_local') {
    return parseConfigAllowedUserIds(row.settings).includes(ownerId)
  }
  return row.ownerId === null || row.ownerId === ownerId
}

export async function getActiveConfig(serviceType: ServiceType, ownerId?: number): Promise<AIConfig | null> {
  if (!getAIServiceEnabled(serviceType)) {
    logTaskWarn('AIConfig', 'service-type-disabled', { serviceType })
    return null
  }
  const rows = (await db.select().from(schema.aiServiceConfigs)
    .where(eq(schema.aiServiceConfigs.serviceType, serviceType))
  )
    .filter(r => r.isActive && isOfficialProvider(serviceType, r.provider) && isConfigVisibleToOwner(r, ownerId))
  sortActiveConfigs(rows)

  const active = rows[0]
  if (!active) {
    logTaskWarn('AIConfig', 'active-config-missing', { serviceType })
    return null
  }

  const models = active.model ? JSON.parse(active.model) : []
  const logKey = `${active.id}:${models[0] || ''}`
  if (lastLoggedActiveConfigKey.get(serviceType) !== logKey) {
    lastLoggedActiveConfigKey.set(serviceType, logKey)
    logTaskProgress('AIConfig', 'active-config-selected', {
      serviceType,
      configId: active.id,
      provider: active.provider,
      model: models[0] || '',
      priority: active.priority,
    })
  }
  return {
    id: active.id,
    provider: active.provider || '',
    baseUrl: active.baseUrl,
    apiKey: effectiveApiKey(active.provider, active.apiKey),
    model: models[0] || '',
    temperature: parseConfigTemperature(active.settings),
  }
}

export async function getTextConfig(ownerId?: number): Promise<AIConfig> {
  const config = await getActiveConfig('text', ownerId)
  if (!config) {
    const disabledMessage = getAIServiceDisabledMessage('text')
    if (disabledMessage) throw new Error(disabledMessage)
    throw new Error('未配置文本模型，请先到「设置」页添加并启用 AI 服务')
  }
  return config
}

/**
 * 取某服务类型当前启用且优先级最高的官方配置 ID（创建集时自动锁定用）
 */
export async function getActiveConfigId(serviceType: ServiceType, ownerId?: number): Promise<number | null> {
  if (!getAIServiceEnabled(serviceType)) return null
  const rows = (await db.select().from(schema.aiServiceConfigs)
    .where(eq(schema.aiServiceConfigs.serviceType, serviceType))
  )
    .filter(r => r.isActive && isOfficialProvider(serviceType, r.provider) && isConfigVisibleToOwner(r, ownerId))
  sortActiveConfigs(rows) // 高优先级优先；同优先级 ComfyUI 优先
  return rows[0]?.id ?? null
}

/**
 * 按服务类型 + provider 取启用配置（历史任务没有 config_id 时的恢复回退）。
 */
export async function getConfigForProvider(serviceType: ServiceType, provider: string | null | undefined, ownerId?: number): Promise<AIConfig | null> {
  if (!getAIServiceEnabled(serviceType)) return null
  const rows = (await db.select().from(schema.aiServiceConfigs)
    .where(eq(schema.aiServiceConfigs.serviceType, serviceType))
  ).filter(r => r.isActive && (r.provider || '').toLowerCase() === (provider || '').toLowerCase() && isOfficialProvider(serviceType, r.provider) && isConfigVisibleToOwner(r, ownerId))
  sortActiveConfigs(rows)

  const active = rows[0]
  if (!active) {
    logTaskWarn('AIConfig', 'recovery-config-missing', { serviceType, provider })
    return null
  }
  const models = active.model ? JSON.parse(active.model) : []
  return {
    id: active.id,
    provider: active.provider || '',
    baseUrl: active.baseUrl,
    apiKey: effectiveApiKey(active.provider, active.apiKey),
    model: models[0] || '',
    temperature: parseConfigTemperature(active.settings),
  }
}

export async function getConfigById(id: number, ownerId?: number): Promise<AIConfig | null> {
  const [row] = await db.select().from(schema.aiServiceConfigs)
    .where(eq(schema.aiServiceConfigs.id, id))
  if (!row || !row.isActive || !isConfigVisibleToOwner(row, ownerId)) {
    logTaskWarn('AIConfig', 'config-by-id-missing', { configId: id })
    return null
  }
  if (!getAIServiceEnabled(row.serviceType as ServiceType)) {
    logTaskWarn('AIConfig', 'service-type-disabled', { configId: id, serviceType: row.serviceType })
    return null
  }
  if (!isOfficialProvider(row.serviceType as ServiceType, row.provider)) {
    logTaskWarn('AIConfig', 'config-by-id-unsupported-provider', {
      configId: id,
      serviceType: row.serviceType,
      provider: row.provider,
    })
    return null
  }
  const models = row.model ? JSON.parse(row.model) : []
  const logKey = `${row.provider}:${models[0] || ''}:${row.serviceType}`
  if (lastLoggedConfigByIdKey.get(id) !== logKey) {
    lastLoggedConfigByIdKey.set(id, logKey)
    logTaskProgress('AIConfig', 'config-by-id-selected', {
      configId: id,
      provider: row.provider,
      model: models[0] || '',
      serviceType: row.serviceType,
    })
  }
  return {
    id: row.id,
    provider: row.provider || '',
    baseUrl: row.baseUrl,
    apiKey: effectiveApiKey(row.provider, row.apiKey),
    model: models[0] || '',
    temperature: parseConfigTemperature(row.settings),
  }
}
