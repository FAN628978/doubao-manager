import assert from 'node:assert/strict'
import { test } from 'node:test'
import sharp from 'sharp'
import { DoubaoLocalVideoAdapter } from '../src/services/adapters/doubao-local-video.ts'

async function referenceDataUrl() {
  const png = await sharp({
    create: { width: 128, height: 128, channels: 3, background: '#ff0000' },
  }).png().toBuffer()
  return `data:image/png;base64,${png.toString('base64')}`
}

const config = {
  provider: 'doubao_local',
  baseUrl: 'http://127.0.0.1:8765/',
  apiKey: 'test-secret',
  model: 'Seedance 2.0 Mini',
  doubaoSubmitMode: 'manual' as const,
}

const autoConfig = { ...config, doubaoSubmitMode: 'auto' as const }

test('Doubao local adapter builds multipart reference image jobs with stable ordering', async () => {
  const adapter = new DoubaoLocalVideoAdapter()
  const dataUrl = await referenceDataUrl()
  const request = await adapter.buildGenerateRequest(config, {
    id: 42,
    dramaId: 7,
    storyboardId: 9,
    projectName: '江城旧梦',
    episodeName: '第一集 初遇',
    shotName: '雨夜重逢',
    prompt: '@图片1红色纸船缓缓漂动',
    model: 'Seedance 2.0 Fast',
    referenceImageUrls: JSON.stringify([dataUrl, dataUrl]),
    aspectRatio: '16:9',
    duration: 10,
  })

  assert.equal(request.url, 'http://127.0.0.1:8765/api/v1/videos')
  assert.deepEqual(request.headers, { 'X-API-Key': 'test-secret' })
  assert.ok(request.body instanceof FormData)
  const form = request.body as FormData
  assert.equal(form.has('account_id'), false)
  assert.equal(form.get('prompt'), '@图1红色纸船缓缓漂动')
  assert.equal(form.get('mode'), 'reference_images')
  assert.equal(form.get('model'), 'Seedance 2.0 Fast')
  assert.equal(form.get('duration'), '10')
  assert.equal(form.get('project_name'), '江城旧梦')
  assert.equal(form.get('episode_name'), '第一集 初遇')
  assert.equal(form.get('shot_name'), '雨夜重逢')
  assert.equal(form.get('task_name'), '江城旧梦 / 第一集 初遇 / 雨夜重逢')
  assert.equal(form.get('project_id'), null)
  assert.equal(form.get('episode_id'), null)
  assert.equal(form.get('shot_id'), null)
  assert.equal(form.get('idempotency_key'), 'h3-video-task-42')
  assert.equal(form.get('submit_mode'), 'manual')
  assert.equal(form.get('auto_start'), 'false')
  assert.equal(form.getAll('reference').length, 2)
})

test('Doubao local adapter can opt back into fully automatic submission per config', async () => {
  const adapter = new DoubaoLocalVideoAdapter()
  const request = await adapter.buildGenerateRequest(autoConfig, { id: 4, prompt: '自动提交', duration: 4 })
  const form = request.body as FormData
  assert.equal(form.get('submit_mode'), 'auto')
  assert.equal(form.get('auto_start'), 'true')
})

test('Doubao local adapter defaults to auto when the config has no submit mode', async () => {
  const adapter = new DoubaoLocalVideoAdapter()
  const unsetConfig = { ...config, doubaoSubmitMode: null }
  const request = await adapter.buildGenerateRequest(unsetConfig, { id: 5, prompt: '默认自动', duration: 4 })
  const form = request.body as FormData
  assert.equal(form.get('submit_mode'), 'auto')
  assert.equal(form.get('auto_start'), 'true')
})

test('Doubao local adapter allows the env var to choose manual for unset configs', async () => {
  const previous = process.env.DOUBAO_LOCAL_SUBMIT_MODE
  process.env.DOUBAO_LOCAL_SUBMIT_MODE = 'manual'
  try {
    const adapter = new DoubaoLocalVideoAdapter()
    const unsetConfig = { ...config, doubaoSubmitMode: null }
    const request = await adapter.buildGenerateRequest(unsetConfig, { id: 6, prompt: '人工发送', duration: 4 })
    const form = request.body as FormData
    assert.equal(form.get('submit_mode'), 'manual')
    assert.equal(form.get('auto_start'), 'false')
  } finally {
    if (previous === undefined) delete process.env.DOUBAO_LOCAL_SUBMIT_MODE
    else process.env.DOUBAO_LOCAL_SUBMIT_MODE = previous
  }
})

test('Doubao local adapter uses text2video without references', async () => {
  const adapter = new DoubaoLocalVideoAdapter()
  const request = await adapter.buildGenerateRequest(config, {
    id: 3,
    prompt: '电影感城市夜景',
    duration: 4,
  })
  assert.equal((request.body as FormData).get('mode'), 'text2video')
  assert.equal((request.body as FormData).get('submit_mode'), 'manual')
  assert.equal((request.body as FormData).get('auto_start'), 'false')
  assert.equal((request.body as FormData).getAll('reference').length, 0)
})

test('Doubao local adapter keeps manual jobs polling through needs_review and failed', () => {
  const adapter = new DoubaoLocalVideoAdapter()
  // 人工发送任务：本地可能等操作者重新发送，H3 不能把 needs_review/failed 当终态，
  // 否则操作者在原 job 上重新发送并归档后，H3 已停止轮询，收不到视频。
  assert.deepEqual(adapter.parsePollResponse({
    status: 'needs_review', submit_mode: 'manual', error_code: 'MANUAL_SEND_INTERRUPTED', error_message: '服务重启',
  }), { status: 'processing' })
  assert.deepEqual(adapter.parsePollResponse({
    status: 'failed', submit_mode: 'manual', error_code: 'GENERATION_FAILED', error_message: '生成失败',
  }), { status: 'processing' })
  // 本机操作者明确判定失败后，H3 必须看到失败并停止等待。
  assert.deepEqual(adapter.parsePollResponse({
    status: 'failed', submit_mode: 'manual', error_code: 'MANUAL_CONFIRMED_FAILED', error_message: '人工判定失败',
  }), { status: 'failed', error: '[MANUAL_CONFIRMED_FAILED] 人工判定失败' })
  // 自动任务保持原有的失败语义。
  assert.deepEqual(adapter.parsePollResponse({
    status: 'needs_review', submit_mode: 'auto', error_code: 'LOGIN_REQUIRED', error_message: '请登录',
  }), { status: 'failed', error: '[LOGIN_REQUIRED] 请登录' })
})

test('Doubao local adapter maps terminal states and authenticated download URLs', () => {
  const adapter = new DoubaoLocalVideoAdapter()
  assert.deepEqual(adapter.parseGenerateResponse({ job_id: 'job_1', status: 'queued' }), {
    isAsync: true,
    taskId: 'job_1',
  })
  assert.deepEqual(adapter.parsePollResponse({ job_id: 'job_1', status: 'queued' }), { status: 'pending' })
  assert.deepEqual(adapter.parsePollResponse({ job_id: 'job_1', status: 'awaiting_manual_send' }), { status: 'processing' })
  assert.deepEqual(adapter.parsePollResponse({ job_id: 'job_1', status: 'generating' }), { status: 'processing' })
  assert.deepEqual(adapter.parsePollResponse({
    job_id: 'job_1', status: 'downloaded', duration: 10, video_url: '/api/v1/videos/job_1/video',
  }), {
    status: 'completed', videoUrl: '/api/v1/videos/job_1/video', duration: 10,
  })
  assert.deepEqual(adapter.parsePollResponse({
    status: 'needs_review', error_code: 'LOGIN_REQUIRED', error_message: '请登录',
  }), {
    status: 'failed', error: '[LOGIN_REQUIRED] 请登录',
  })
  assert.deepEqual(adapter.resolveVideoDownload(config, '/api/v1/videos/job_1/video'), {
    url: 'http://127.0.0.1:8765/api/v1/videos/job_1/video',
    headers: { 'X-API-Key': 'test-secret' },
    extension: '.mp4',
  })
  assert.deepEqual(adapter.buildRetryRequest(config, 'job_1'), {
    url: 'http://127.0.0.1:8765/api/v1/videos/generations/job_1/start',
    method: 'POST',
    headers: { 'X-API-Key': 'test-secret' },
    body: undefined,
  })
  assert.deepEqual(adapter.buildRecallRequest(config, 'job_1'), {
    url: 'http://127.0.0.1:8765/api/v1/videos/generations/job_1/recall',
    method: 'POST',
    headers: { 'X-API-Key': 'test-secret' },
    body: undefined,
  })
})

test('Doubao local adapter rejects unknown models', async () => {
  const adapter = new DoubaoLocalVideoAdapter()
  await assert.rejects(
    adapter.buildGenerateRequest(config, { id: 1, prompt: 'test', model: 'unknown-model' }),
    /模型不存在/,
  )
})
