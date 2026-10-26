import test from 'node:test'
import assert from 'node:assert/strict'
import { scanWorkspace } from '../src/lib/localFs.ts'

test('workspace scanner streams past the former 25,000-file ceiling', async () => {
  const expected = 66_336
  const root = {
    kind: 'directory',
    async *entries() {
      for (let i = 0; i < expected; i++) {
        yield [`f-${i}.js`, { kind: 'file', async getFile() { return { size: i, lastModified: 1 } } }]
      }
    },
  }
  let count = 0
  for await (const entry of scanWorkspace(root)) {
    count++
    assert.equal(entry.size_known, true)
    assert.equal(entry.extension, 'js')
  }
  assert.equal(count, expected)
})

test('scanner carries excluded directory paths without reading file contents', async () => {
  let getFileCalls = 0
  const file = {
    kind: 'file',
    async getFile() { getFileCalls++; return { size: 42, lastModified: 123 } },
  }
  const nodeModules = { kind: 'directory', async *entries() { yield ['package.json', file] } }
  const root = { kind: 'directory', async *entries() { yield ['node_modules', nodeModules] } }
  const entries = []
  for await (const entry of scanWorkspace(root)) entries.push(entry)
  assert.equal(entries.length, 1)
  assert.equal(entries[0].relative_path, 'node_modules/package.json')
  assert.equal(entries[0].excluded_directory, 'node_modules')
  assert.equal(getFileCalls, 1)
})
