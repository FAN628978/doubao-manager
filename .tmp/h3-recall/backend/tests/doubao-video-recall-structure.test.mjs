import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'

const root = new URL('..', import.meta.url)
const read = path => readFileSync(new URL(path, root), 'utf8')

test('Doubao recall reuses the persisted upstream job without resubmitting generation', () => {
  const generation = read('src/services/generation.ts')
  const recall = generation.slice(generation.indexOf('export async function recallDoubaoVideoTask'))

  assert.match(recall, /record\.taskId/)
  assert.match(recall, /adapter\.buildPollRequest\(config, record\.taskId\)/)
  assert.match(recall, /adapter\.buildRecallRequest\(config, record\.taskId\)/)
  assert.doesNotMatch(recall, /buildGenerateRequest|processTask\(/)
})

test('task API exposes the authenticated recall action', () => {
  const routes = read('src/routes/tasks.ts')
  assert.match(routes, /app\.post\('\/:id\/recall-video'/)
  assert.match(routes, /canAccessTask\(c, row\)/)
  assert.match(routes, /recallDoubaoVideoTask\(id\)/)
})

test('temporary Doubao connection loss remains recoverable', () => {
  const generation = read('src/services/generation.ts')
  const adapter = read('src/services/adapters/doubao-local-video.ts')
  assert.match(generation, /syncDoubaoConnectionWarning/)
  assert.match(adapter, /7 \* 24 \* 60 \* 60_000/)
})
