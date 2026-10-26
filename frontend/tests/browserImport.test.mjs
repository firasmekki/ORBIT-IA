import test from 'node:test'
import assert from 'node:assert/strict'
import {
  BROWSER_IMPORT_MAX_FILES,
  BROWSER_IMPORT_TOO_LARGE_MESSAGE,
  FolderTooLargeError,
  assertBrowserImportAllowed,
  measureFolderForBrowserImport,
} from '../src/lib/browserImport.ts'

const MB = 1024 * 1024

function fakeFile(size, counter) {
  return {
    kind: 'file',
    async getFile() {
      counter.calls++
      return { size, lastModified: 1 }
    },
  }
}

function fakeDir(entries) {
  return {
    kind: 'directory',
    async *entries() {
      yield* entries
    },
  }
}

function flatFolder(count, size, counter) {
  return fakeDir(Array.from({ length: count }, (_, i) => [`f-${i}.pdf`, fakeFile(size, counter)]))
}

test('a folder of exactly 200 files under 500 MB is accepted', async () => {
  const counter = { calls: 0 }
  const measure = await measureFolderForBrowserImport(flatFolder(BROWSER_IMPORT_MAX_FILES, MB, counter))
  assert.deepEqual(measure, { files: 200, bytes: 200 * MB, withinLimits: true })
  await assert.doesNotReject(assertBrowserImportAllowed(flatFolder(200, MB, counter)))
})

test('201 files are refused and the walk stops right after the limit', async () => {
  const counter = { calls: 0 }
  const measure = await measureFolderForBrowserImport(flatFolder(66_336, 1, counter))
  assert.equal(measure.withinLimits, false)
  assert.equal(measure.files, 201)
  assert.equal(counter.calls, 201)
})

test('more than 500 MB is refused even with few files', async () => {
  const counter = { calls: 0 }
  const measure = await measureFolderForBrowserImport(flatFolder(2, 300 * MB, counter))
  assert.equal(measure.withinLimits, false)
})

test('files in sub-folders are counted', async () => {
  const counter = { calls: 0 }
  const sub = flatFolder(150, 1, counter)
  const root = fakeDir([['Client C', sub], ...Array.from({ length: 60 }, (_, i) => [`r-${i}.txt`, fakeFile(1, counter)])])
  const measure = await measureFolderForBrowserImport(root)
  assert.equal(measure.withinLimits, false)
  assert.equal(measure.files, 201)
})

test('a refused folder raises the user-facing message', async () => {
  const counter = { calls: 0 }
  await assert.rejects(assertBrowserImportAllowed(flatFolder(500, 1, counter)), (err) => {
    assert.ok(err instanceof FolderTooLargeError)
    assert.equal(err.message, BROWSER_IMPORT_TOO_LARGE_MESSAGE)
    return true
  })
})

// A connected folder that grew past the limits (e.g. thousands of files) must
// stop being watched WITHOUT touching what is already indexed: the only
// browser paths that retire documents server-side are planWorkspaceSync
// (inside syncWorkspaceFolder) and deleteWorkspace. The size gate has to run
// before either can be reached, and its failure branch must call neither.
test('runSync checks the limits before any request that could retire documents', async () => {
  const { readFile } = await import('node:fs/promises')
  const source = await readFile(new URL('../src/context/WorkspaceContext.tsx', import.meta.url), 'utf8')
  const runSync = source.slice(source.indexOf('const runSync = useCallback('), source.indexOf('// Restore this workspace'))
  const gate = runSync.indexOf('await assertBrowserImportAllowed(handle)')
  assert.ok(gate > 0, 'size gate present in runSync')
  assert.ok(gate < runSync.indexOf('folderSnapshot(handle)'), 'gate runs before the folder is rescanned')
  assert.ok(gate < runSync.indexOf('syncWorkspaceFolder('), 'gate runs before plan/upload (the only retire path)')
  const failureBranch = runSync.slice(gate, runSync.indexOf('throw err'))
  for (const forbidden of ['planWorkspaceSync', 'deleteWorkspace', 'clearWorkspaceHandle', 'syncWorkspaceFolder']) {
    assert.ok(!failureBranch.includes(forbidden), `failure branch does not call ${forbidden}`)
  }
  assert.ok(failureBranch.includes('setFolderHandle(null)'), 'watching stops (local state only)')
})

test('connectFolder checks the limits before creating a workspace or syncing', async () => {
  const { readFile } = await import('node:fs/promises')
  const source = await readFile(new URL('../src/context/WorkspaceContext.tsx', import.meta.url), 'utf8')
  const connect = source.slice(source.indexOf('const connectFolder = useCallback('))
  const gate = connect.indexOf('await assertBrowserImportAllowed(handle)')
  assert.ok(gate > 0 && gate < connect.indexOf('createWorkspace(') && gate < connect.indexOf('runSync('))
})
