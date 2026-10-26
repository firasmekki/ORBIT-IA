// Folder hierarchy helpers (lib/folderTree): paths, breadcrumb text, the
// three counters never mixed, the scope label and the 3 search modes.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { countsLabel, displayPath, folderDepth, parentPath, SCOPE_MODES, scopeLabel } from '../src/lib/folderTree.ts'

const source = (path) => readFileSync(new URL(path, import.meta.url), 'utf8')

test('paths keep accents and spaces, and climb to their real parent', () => {
  assert.equal(displayPath('projets', 'noveau consultation/Attijari/CCTP/'), 'projets / noveau consultation / Attijari / CCTP')
  assert.equal(displayPath('projets', 'Délice CLC/Études thermiques/'), 'projets / Délice CLC / Études thermiques')
  assert.equal(parentPath('noveau consultation/Attijari/CCTP/'), 'noveau consultation/Attijari/')
  assert.equal(parentPath('noveau consultation/'), '')
  assert.equal(folderDepth(''), -1)
  assert.equal(folderDepth('a/b/'), 1)
})

test('the three counters are shown separately', () => {
  assert.equal(
    countsLabel({ direct_documents: 1, subfolder_count: 3, total_documents: 6 }).replace(/\s/g, ' '),
    '1 document direct · 3 sous-dossiers · 6 au total',
  )
  assert.equal(
    countsLabel({ direct_documents: 0, subfolder_count: 1, total_documents: 1250 }).replace(/\s/g, ' '),
    '0 document direct · 1 sous-dossier · 1 250 au total',
  )
})

test('the scope label always says how deep the assistant searches', () => {
  assert.deepEqual(SCOPE_MODES.map((m) => m.value), ['folder', 'recursive', 'all'])
  assert.equal(scopeLabel('projets', [], 'recursive'), 'projets (avec sous-dossiers)')
  assert.equal(scopeLabel('projets', ['noveau consultation'], 'folder'), 'noveau consultation (dossier seul)')
  assert.equal(scopeLabel('projets', ['BIC'], 'all'), 'Tous les espaces autorisés')
})

test('sharing is a contextual action, not a button on every line', () => {
  const panel = source('../src/components/FoldersPanel.tsx')
  assert.ok(!panel.includes("'Partager…'"), 'no permanent "Partager…" button per row')
  assert.match(panel, /aria-haspopup="menu"/)
  assert.match(panel, /className="tree-more"/)
  const tree = source('../src/components/FolderTree.tsx')
  assert.match(tree, /role="tree"/)
  assert.match(tree, /aria-expanded/)
  assert.match(tree, /folderTree\(workspaceId, path\)/) // one level loaded per opened folder
})

test('the chat sends the selected depth to the server, which applies it', () => {
  const endpoints = source('../src/api/endpoints.ts')
  assert.match(endpoints, /scope_mode: scopeMode/)
  const page = source('../src/pages/ChatPage.tsx')
  assert.match(page, /folderScope,\n\s+scopeMode,\n/)
})
