// How an answer was produced, and the model status banner (lib/answerMode).
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { LLM_STATUS_CLASS, answerModeLabel } from '../src/lib/answerMode.ts'

const source = (path) => readFileSync(new URL(path, import.meta.url), 'utf8')

test('each answer says how it was produced; nothing is guessed for older messages', () => {
  assert.equal(answerModeLabel('direct'), 'Réponse locale')
  assert.equal(answerModeLabel('documents'), 'Documents du projet')
  assert.equal(answerModeLabel('web'), 'Recherche Web')
  assert.equal(answerModeLabel('hybrid'), 'Documents + Web')
  assert.match(answerModeLabel('unavailable'), /Modèle indisponible/)
  assert.equal(answerModeLabel(null), null)
  assert.equal(answerModeLabel('anything-else'), null)
})

test('the banner follows the state the server reports, including errors', () => {
  assert.equal(LLM_STATUS_CLASS.ready, 'llm-status-ready')
  assert.equal(LLM_STATUS_CLASS.loading, 'llm-status-loading')
  for (const bad of ['unavailable', 'error', 'misconfigured']) assert.equal(LLM_STATUS_CLASS[bad], 'llm-status-unavailable')
  const page = source('../src/pages/ChatPage.tsx')
  assert.match(page, /llmStatus\.detail/)
  assert.match(page, /msg-unavailable/)
})
