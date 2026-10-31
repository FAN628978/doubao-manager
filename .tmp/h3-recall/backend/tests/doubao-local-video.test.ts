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
}

test('Doubao local adapter builds multipart reference image jobs with stable ordering', async () => {
  const adapter = new DoubaoLocalVideoAdapter()
  const dataUrl = await referenceDataUrl()
  const request = await adapter.buildGenerateRequest(config, {
    id: 42,
    dramaId: 7,
    storyboardId: 9,
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
  assert.equal(form.get('project_id'), '7')
  assert.equal(form.get('shot_id'), '9')
  assert.equal(form.get('idempotency_key'), 'h3-video-task-42')
  assert.equal(form.getAll('reference').length, 2)
})

test('Doubao local adapter uses text2video without references', async () => {
  const adapter = new DoubaoLocalVideoAdapter()
  const request = await adapter.buildGenerateRequest(config, {
    id: 3,
    prompt: '电影感城市夜景',
    duration: 4,
  })
  assert.equal((request.body as FormData).get('mode'), 'text2video')
  assert.equal((request.body as FormData).getAll('reference').length, 0)
})

test('Doubao local adapter maps terminal states and authenticated download URLs', () => {
  const adapter = new DoubaoLocalVideoAdapter()
  assert.deepEqual(adapter.parseGenerateResponse({ job_id: 'job_1', status: 'queued' }), {
    isAsync: true,
    taskId: 'job_1',
  })
  assert.deepEqual(adapter.parsePollResponse({ job_id: 'job_1', status: 'queued' }), { status: 'pending' })
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
    url: 'http://127.0.0.1:8765/api/v1/videos/generations/job_1/download-result?confirm_review=true',
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
