/**
 * 图片生成 Provider Adapter 接口
 */
export interface ImageProviderAdapter {
  /** 厂商标识 */
  provider: string

  /**
   * 构建图片生成请求
   * @param config AI 配置 { baseUrl, apiKey, model }
   * @param record 图片生成记录
   */
  buildGenerateRequest(config: AIConfig, record: ImageGenerationRecord): ProviderRequest

  /**
   * 解析生成响应，判断是同步还是异步
   */
  parseGenerateResponse(result: any): ImageGenResponse

  /**
   * 构建轮询请求
   * @param config AI 配置
   * @param taskId 任务 ID
   */
  buildPollRequest(config: AIConfig, taskId: string): ProviderRequest

  /**
   * 解析轮询响应
   */
  parsePollResponse(result: any): ImagePollResponse

  /**
   * 从响应中提取图片 URL（用于直接下载）
   * 返回 null 表示图片数据是 base64 格式，需要用 extractImageBase64 处理
   */
  extractImageUrl(result: any): string | null

  /**
   * 从响应中提取 base64 图片数据
   * 仅用于 Gemini 等只返回 base64 的厂商
   */
  extractImageBase64(result: any): { data: string; mimeType: string } | null
}

/**
 * 视频生成 Provider Adapter 接口
 */
export interface VideoProviderAdapter {
  provider: string

  /** Provider-specific polling cadence. Defaults to generation.ts' shared video profile. */
  pollProfile?: { intervalMs: number; maxDurationMs: number }

  buildGenerateRequest(config: AIConfig, record: VideoGenerationRecord): ProviderRequest | Promise<ProviderRequest>

  parseGenerateResponse(result: any, request?: ProviderRequest): VideoGenResponse

  /** Release any short-lived local capacity reserved while building a request. */
  releaseGenerateReservation?(request: ProviderRequest): void

  buildPollRequest(config: AIConfig, taskId: string): ProviderRequest

  parsePollResponse(result: any): VideoPollResponse

  /** Retry the same upstream job after a provider-declared transient failure. */
  buildRetryRequest?(config: AIConfig, taskId: string): ProviderRequest

  /** Recover an existing upstream result without creating or resubmitting a job. */
  buildRecallRequest?(config: AIConfig, taskId: string): ProviderRequest

  /** Resolve an authenticated provider download endpoint from a completed task response. */
  resolveVideoDownload?(config: AIConfig, videoUrl: string): { url: string; headers?: Record<string, string>; extension?: string }

  /**
   * 部分异步服务（原生 ComfyUI）将已完成任务与排队/执行中任务放在不同接口。
   * 当主轮询响应返回 missing 时，生成服务用这组可选方法补查队列状态。
   */
  buildQueueRequest?(config: AIConfig, taskId: string): ProviderRequest

  parseQueueResponse?(result: any, taskId: string): VideoPollResponse

  /** Resolve the concrete provider instance that owns an async task. */
  resolveTaskBaseUrl?(config: AIConfig, taskId: string): string

  extractVideoUrl(result: any): string | null
}

// ============ 通用类型 ============

export interface ProviderRequest {
  url: string
  method: string
  headers: Record<string, string>
  /** Concrete worker selected by a load-balancing adapter. */
  providerBaseUrl?: string
  /**
   * 普通请求为可 JSON 序列化的对象（generation.ts 统一 JSON.stringify）；
   * multipart 上传（如 OpenAI /v1/images/edits）直接返回 FormData，
   * 此时 headers 不要设置 Content-Type，边界由 fetch 自动生成。
   */
  body: any
}

export interface AIConfig {
  id?: number
  provider: string
  baseUrl: string
  apiKey: string
  model: string
  /** Concrete worker selected by the H3-side MiniMax dispatcher. */
  dispatchBaseUrl?: string
  /** 豆包本地管理台：auto=脚本自动发送；manual=只填好页面，由操作者亲自发送。 */
  doubaoSubmitMode?: 'auto' | 'manual' | null
}

export interface ImageGenerationRecord {
  id: number
  model?: string | null
  prompt?: string | null
  size?: string | null
  frameType?: string | null
  referenceImages?: string | null
  // ... 其他字段
}

export interface VideoGenerationRecord {
  id: number
  dramaId?: number | null
  episodeId?: number | null
  storyboardId?: number | null
  projectName?: string | null
  episodeName?: string | null
  shotName?: string | null
  model?: string | null
  prompt?: string | null
  referenceMode?: string | null
  imageUrl?: string | null
  firstFrameUrl?: string | null
  lastFrameUrl?: string | null
  referenceImageUrls?: string | null
  referenceVideoUrls?: string | null
  referenceAudioUrls?: string | null
  referenceFileUrl?: string | null
  referenceLinkUrl?: string | null
  generateAudio?: number | boolean | null
  duration?: number | null
  aspectRatio?: string | null
  resolution?: string | null
  seed?: number | null
  promptExtend?: number | boolean | null
  watermark?: number | boolean | null
  // ... 其他字段
}

export interface ImageGenResponse {
  isAsync: boolean
  taskId?: string
  /** 同步模式下直接返回的图片 URL */
  imageUrl?: string
}

export interface ImagePollResponse {
  status: 'pending' | 'processing' | 'completed' | 'failed'
  imageUrl?: string
  error?: string
}

export interface VideoGenResponse {
  isAsync: boolean
  taskId?: string
  videoUrl?: string
}

export interface VideoPollResponse {
  status: 'missing' | 'pending' | 'processing' | 'completed' | 'failed'
  /** Number of running or queued jobs ahead of this task, when exposed by the provider. */
  queueAhead?: number
  videoUrl?: string
  duration?: number
  error?: string
}
