// Web search in the chat: no mode for the user to pick (the server decides
// documents / web / both for each question); web pages and documents are
// listed apart; only http(s) links clickable; a search snippet is never
// shown as a read page. Admin key field is write-only.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fetchLabel, safeHttpUrl, splitSources } from '../src/lib/webSearch.ts'

const source = (path) => readFileSync(new URL(path, import.meta.url), 'utf8')

test('the chat no longer renders a search mode selector nor sends a mode', () => {
  const page = source('../src/pages/ChatPage.tsx')
  for (const gone of ['search-mode', 'Recherche :', 'Où chercher', 'Documents uniquement', 'Hybride', 'web-status', 'searchMode', 'webSearchStatus']) {
    assert.ok(!page.includes(gone), `still in ChatPage: ${gone}`)
  }
  const endpoints = source('../src/api/endpoints.ts')
  assert.ok(!endpoints.includes('search_mode'))
  assert.ok(!source('../src/index.css').includes('.search-mode'))
  // the workspace and folder controls are still there
  assert.match(page, /<FolderScopePicker/)
  assert.match(page, /workspace-button/)
  assert.match(endpoints, /scope_mode: scopeMode/)
})

test('web pages and workspace documents are listed apart', () => {
  const { local, web } = splitSources([
    { kind: 'local', document_id: 'd1', title: 'CCTP', department: '', confidentiality: '', excerpt: '' },
    { kind: 'web', document_id: null, title: 'Loi', url: 'https://energie.gov.tn/loi', department: '', confidentiality: '', excerpt: '' },
    { document_id: 'd2', title: 'Ancien message', department: 'GENERAL', confidentiality: 'INTERNAL', excerpt: '' },
  ])
  assert.deepEqual(local.map((s) => s.title), ['CCTP', 'Ancien message']) // old messages have no kind: local
  assert.deepEqual(web.map((s) => s.title), ['Loi'])
})

test('only http(s) links become clickable', () => {
  assert.equal(safeHttpUrl('https://www.anme.tn/exigences'), 'https://www.anme.tn/exigences')
  assert.equal(safeHttpUrl('javascript:alert(1)'), null)
  assert.equal(safeHttpUrl('data:text/html,x'), null)
  assert.equal(safeHttpUrl('pas une url'), null)
})

test('a search snippet is never presented as a read page', () => {
  assert.equal(fetchLabel({ fetch_status: 'fetched' }), 'page lue')
  assert.equal(fetchLabel({ fetch_status: 'http_error', fetch_note: 'page non lue : erreur du site' }),
    'extrait du moteur seulement — page non lue : erreur du site')
})

test('web settings stay in the administration, the API key is write-only', () => {
  const panel = source('../src/components/WebSearchPanel.tsx')
  assert.match(panel, /type="password"/)
  assert.match(panel, /setKeyInput\(''\)/) // cleared as soon as it is sent
  assert.match(source('../src/pages/AdminPage.tsx'), /<WebSearchPanel \/>/)
  const page = source('../src/pages/ChatPage.tsx')
  assert.match(page, /rel="noopener noreferrer"/)
  assert.match(page, /Sources Web/)
})

test('the image analysis action has a readable name', () => {
  const tools = source('../src/lib/tools.ts')
  assert.match(tools, /analyze_image: 'Analyse de l’image \(modèle vision\)'/)
})

test('the connection test is reported with an explicit status', async () => {
  const { connectionLabel } = await import('../src/lib/webSearch.ts')
  assert.equal(connectionLabel('connected'), 'Connexion fonctionnelle')
  assert.equal(connectionLabel('not_configured'), 'Clé API absente')
  assert.match(connectionLabel('invalid_key'), /Clé API invalide/)
  assert.match(connectionLabel('rate_limited'), /Quota/)
  assert.equal(connectionLabel('timeout'), 'Délai dépassé')
  assert.match(connectionLabel('network_error'), /réseau/)
  assert.match(connectionLabel('provider_error'), /Fournisseur/)
  const panel = source('../src/components/WebSearchPanel.tsx')
  assert.match(panel, /connectionLabel\(test\.connection\)/)
  assert.match(panel, /safeHttpUrl\(s\.url\)/) // sample links shown only if http(s)
})
