/**
 * 豆包网页版本地管理台视频生成 Adapter。
 *
 * 协议：multipart 创建任务；完成后由本地管理台主动把 MP4 回推 H3。
 * H3 内部的 @图片N 标记在提交前转换为管理台支持的 @图N。
 */
import sharp from 'sharp'
import type {
  AIConfig,
  ProviderRequest,
  VideoGenResponse,
  VideoGenerationRecord,
  VideoPollResponse,
  VideoProviderAdapter,
} from './types'
import { joinProviderUrl } from './url'

const MAX_IMAGE_BYTES = 15 * 1024 * 1024
const MAX_REFERENCE_IMAGES = 9
const ALLOWED_IMAGE_TYPES = new Set(['image/png', 'image/jpeg', 'image/webp'])
const MODEL_NAMES = new Set([
  'Seedance 2.5',
  'Seedance 2.0',
  'Seedance 2.0 Fast',
  'Seedance 2.0 Mini',
])

function parseUrlArray(raw?: string | null): string[] {
  if (!raw) return []
  try {
    const values = JSON.parse(raw)
    return Array.isArray(values) ? values.map(value => String(value || '').trim()).filter(Boolean) : []
  } catch {
    return []
  }
}

function apiHeaders(apiKey: string): Record<string, string> {
  return apiKey ? { 'X-API-Key': apiKey } : {}
}

function imageExtension(mimeType: string): string {
  if (mimeType === 'image/png') return '.png'
  if (mimeType === 'image/webp') return '.webp'
  return '.jpg'
}

async function imageBytes(value: string, index: number): Promise<{ blob: Blob; filename: string }> {
  let bytes: Buffer
  let mimeType = ''

  const dataMatch = value.match(/^data:([^;,]+);base64,(.+)$/s)
  if (dataMatch) {
    mimeType = dataMatch[1].toLowerCase()
    bytes = Buffer.from(dataMatch[2], 'base64')
  } else if (/^https?:\/\//i.test(value)) {
    const response = await fetch(value, { signal: AbortSignal.timeout(60_000) })
    if (!response.ok) throw new Error(`豆包参考图下载失败（HTTP ${response.status}）：第 ${index} 张`)
    mimeType = String(response.headers.get('content-type') || '').split(';')[0].trim().toLowerCase()
    bytes = Buffer.from(await response.arrayBuffer())
  } else {
    throw new Error(`豆包参考图必须是 H3 本地图片、Data URL 或 HTTP(S) 地址：第 ${index} 张`)
  }

  if (!ALLOWED_IMAGE_TYPES.has(mimeType)) {
    throw new Error(`豆包参考图仅支持 PNG/JPEG/WEBP：第 ${index} 张为 ${mimeType || '未知格式'}`)
  }
  if (!bytes.length || bytes.length > MAX_IMAGE_BYTES) {
    throw new Error(`豆包参考图大小必须在 1B～15MB：第 ${index} 张为 ${(bytes.length / 1024 / 1024).toFixed(2)}MB`)
  }

  const metadata = await sharp(bytes).metadata()
  const width = Number(metadata.width || 0)
  const height = Number(metadata.height || 0)
  if (width < 128 || width > 8192 || height < 128 || height > 8192) {
    throw new Error(`豆包参考图尺寸必须为 128～8192 像素：第 ${index} 张为 ${width}×${height}`)
  }

  const blobBytes = new Uint8Array(bytes.length)
  blobBytes.set(bytes)
  return {
    blob: new Blob([blobBytes], { type: mimeType }),
    filename: `reference-${index}${imageExtension(mimeType)}`,
  }
}

function requiredText(value: unknown, label: string, max: number): string {
  const text = String(value || '').trim()
  if (!text) throw new Error(`${label}不能为空`)
  if (text.length > max) throw new Error(`${label}不能超过 ${max} 字`)
  return text
}

export class DoubaoLocalVideoAdapter implements VideoProviderAdapter {
  provider = 'doubao_local'
  async buildGenerateRequest(config: AIConfig, record: VideoGenerationRecord): Promise<ProviderRequest> {
    const prompt = requiredText(record.prompt, '视频提示词', 10_000)
      .replace(/@图片(\d+)/g, '@图$1')
    const model = String(record.model || config.model || '').trim()
    if (model && !MODEL_NAMES.has(model)) throw new Error(`豆包视频模型不存在：${model}`)

    const references = parseUrlArray(record.referenceImageUrls)
    if (references.length > MAX_REFERENCE_IMAGES) {
      throw new Error(`豆包参考生视频最多支持 ${MAX_REFERENCE_IMAGES} 张图片`)
    }

    const firstFrame = String(record.firstFrameUrl || record.imageUrl || '').trim()
    const lastFrame = String(record.lastFrameUrl || '').trim()
    let mode: 'text2video' | 'image2video' | 'first_last_frame' | 'reference_images'
    if (firstFrame && lastFrame) mode = 'first_last_frame'
    else if (firstFrame) mode = 'image2video'
    else if (references.length) mode = 'reference_images'
    else mode = 'text2video'

    const form = new FormData()
    form.append('prompt', prompt)
    form.append('mode', mode)
    form.append('ratio', String(record.aspectRatio || '16:9'))
    const requestedDuration = Math.round(Number(record.duration || 4))
    form.append('duration', String(Number.isFinite(requestedDuration) ? requestedDuration : 4))
    // H3 只提交任务内容；自动/人工模式由本地管理台的持久化设置决定。
    form.append('idempotency_key', `h3-video-task-${record.id}`)
    if (record.resultPushUrl) form.append('result_push_url', record.resultPushUrl)
    if (record.resultPushToken) form.append('result_push_token', record.resultPushToken)
    const names = [record.projectName, record.episodeName, record.shotName]
      .map(value => String(value || '').trim())
      .filter(Boolean)
    form.append('task_name', names.join(' / ') || '视频任务')
    if (model) form.append('model', model)
    if (record.projectName) form.append('project_name', record.projectName)
    if (record.episodeName) form.append('episode_name', record.episodeName)
    if (record.shotName) form.append('shot_name', record.shotName)

    if (mode === 'first_last_frame' || mode === 'image2video') {
      const first = await imageBytes(firstFrame, 1)
      form.append('first_frame', first.blob, first.filename)
    }
    if (mode === 'first_last_frame') {
      const last = await imageBytes(lastFrame, 2)
      form.append('last_frame', last.blob, last.filename)
    }
    if (mode === 'reference_images') {
      const files = await Promise.all(references.map((value, index) => imageBytes(value, index + 1)))
      for (const file of files) form.append('reference', file.blob, file.filename)
    }

    return {
      url: joinProviderUrl(config.baseUrl, '', '/api/v1/videos'),
      method: 'POST',
      headers: apiHeaders(config.apiKey),
      body: form,
    }
  }

  parseGenerateResponse(result: any): VideoGenResponse {
    const jobId = String(result?.job_id || '').trim()
    if (!jobId) throw new Error('豆包管理台未返回 job_id')
    return { isAsync: true, taskId: jobId }
  }

  buildPollRequest(config: AIConfig, taskId: string): ProviderRequest {
    return {
      url: joinProviderUrl(config.baseUrl, '', `/api/v1/videos/${encodeURIComponent(taskId)}`),
      method: 'GET',
      headers: apiHeaders(config.apiKey),
      body: undefined,
    }
  }

  buildRetryRequest(config: AIConfig, taskId: string): ProviderRequest {
    return {
      url: joinProviderUrl(config.baseUrl, '', `/api/v1/videos/generations/${encodeURIComponent(taskId)}/start`),
      method: 'POST',
      headers: apiHeaders(config.apiKey),
      body: undefined,
    }
  }

  buildRecallRequest(config: AIConfig, taskId: string): ProviderRequest {
    return {
      url: joinProviderUrl(config.baseUrl, '', `/api/v1/videos/generations/${encodeURIComponent(taskId)}/recall`),
      method: 'POST',
      headers: apiHeaders(config.apiKey),
      body: undefined,
    }
  }

  /**
   * 定向取消管理台中的同一个 job，不影响其他正在生成的视频。
   * 管理台没有 /cancel：豆包网页对已提交生成没有取消入口，管理台唯一
   * “停止等待该任务”的手段是人工判定失败（mark-failed?confirm_failed=true），
   * H3 借用它做定向取消，本地再把状态落为 canceled。
   * job 已被删除（404）视为取消成功，避免幽灵任务把 H3 卡死在“生成中”。
   */
  async cancelTask(config: AIConfig, taskId: string): Promise<'doubao'> {
    const response = await fetch(
      joinProviderUrl(config.baseUrl, '', `/api/v1/videos/generations/${encodeURIComponent(taskId)}/mark-failed?confirm_failed=true`),
      {
        method: 'POST',
        headers: apiHeaders(config.apiKey),
        signal: AbortSignal.timeout(15_000),
      },
    )
    if (!response.ok) {
      const detail = (await response.text()).trim()
      if (response.status === 404) return 'doubao'
      throw new Error(`豆包管理台取消任务失败（HTTP ${response.status}）${detail ? `：${detail}` : ''}`)
    }
    return 'doubao'
  }

  parsePollResponse(result: any): VideoPollResponse {
    const status = String(result?.status || '').trim().toLowerCase()
    if (status === 'downloaded') {
      const jobId = String(result?.job_id || '').trim()
      const videoUrl = String(result?.video_url || (jobId ? `/api/v1/videos/${encodeURIComponent(jobId)}/video` : '')).trim()
      return { status: 'completed', videoUrl, duration: Number(result?.duration) || undefined }
    }
    if (status === 'needs_review' || status === 'failed') {
      // 人工发送任务由本机操作者接管：管理台允许沿用原 job 重新发送，
      // H3 必须继续轮询同一个 taskId，才能在操作者重新发送并出片后自动收回视频。
      // 只有操作者在管理台明确“判定失败”时才终止；自动任务仍按上游失败处理。
      const manual = String(result?.submit_mode || '').trim().toLowerCase() === 'manual'
      const manualConfirmedFailure = String(result?.error_code || '').trim() === 'MANUAL_CONFIRMED_FAILED'
      if (manual && !manualConfirmedFailure) {
        return { status: 'processing' }
      }
      const code = String(result?.error_code || (status === 'needs_review' ? 'NEEDS_REVIEW' : 'DOUBAO_FAILED')).trim()
      const message = String(result?.error_message || (status === 'needs_review' ? '需要人工复核豆包网页' : '豆包视频生成失败')).trim()
      return { status: 'failed', error: `[${code}] ${message}` }
    }
    if (status === 'queued') return { status: 'pending' }
    // 人工发送阶段：页面已填好，等操作者在豆包网页点发送；对 H3 仍是“处理中”。
    if (status === 'awaiting_manual_send') return { status: 'processing' }
    if (['opening', 'configuring', 'uploading', 'submitting', 'generating', 'confirming', 'downloading'].includes(status)) {
      return { status: 'processing' }
    }
    return { status: 'processing' }
  }

  resolveVideoDownload(config: AIConfig, videoUrl: string) {
    return {
      url: /^https?:\/\//i.test(videoUrl) ? videoUrl : joinProviderUrl(config.baseUrl, '', videoUrl),
      headers: apiHeaders(config.apiKey),
      extension: '.mp4',
    }
  }

  extractVideoUrl(result: any): string | null {
    return result?.video_url || null
  }
}
