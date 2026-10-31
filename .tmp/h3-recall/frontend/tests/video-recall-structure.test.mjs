import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'

const root = new URL('..', import.meta.url)
const read = path => readFileSync(new URL(path, root), 'utf8')

test('all user-facing video task lists expose Doubao recall', () => {
  const api = read('app/composables/useApi.ts')
  const taskCenter = read('app/creator/components/CreatorTasksPage.vue')
  const lab = read('app/pages/lab.vue')
  const episode = read('app/views/drama/episode.vue')

  assert.match(api, /recallVideo:.*\/recall-video/)
  for (const source of [taskCenter, lab, episode]) {
    assert.match(source, /召回视频|recallAction/)
    assert.match(source, /taskAPI\.recallVideo/)
    assert.match(source, /doubao_local/)
  }
})
