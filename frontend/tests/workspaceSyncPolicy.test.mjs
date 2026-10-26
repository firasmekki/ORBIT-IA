import test from 'node:test'
import assert from 'node:assert/strict'
import { ignoreReason, isIgnoredDirectory, SYNC_EXTENSIONS } from '../src/lib/workspaceSyncPolicy.js'

test('all requested document and image formats are indexable', () => {
  for (const ext of ['pdf', 'docx', 'xlsx', 'xls', 'csv', 'txt', 'md', 'jpg', 'jpeg', 'png', 'webp']) {
    assert.equal(SYNC_EXTENSIONS.has(ext), true, ext)
    assert.equal(ignoreReason(ext, 100), null, ext)
  }
})

test('JSON and known code/archive extensions are ignored before upload', () => {
  for (const ext of ['json', 'js', 'ts', 'jsx', 'tsx', 'py', 'exe', 'dll', 'zip', 'rar', '7z', 'iso', 'bin', 'map', 'lock', 'bak', 'tmp']) {
    assert.equal(ignoreReason(ext, 100)?.code, 'unsupported_extension', ext)
  }
})

test('all configured generated and cache directories are excluded case-insensitively', () => {
  for (const name of ['node_modules', '.git', 'dist', 'build', '__pycache__', '.venv', 'venv', '.next', 'coverage', 'local-models', 'logs', 'tmp', 'cache']) {
    assert.equal(isIgnoredDirectory(name), true, name)
    assert.equal(ignoreReason('md', 100, `${name}/README.md`)?.code, 'ignored_directory', name)
  }
  assert.equal(isIgnoredDirectory('my-pycache-copy'), true)
})

test('empty and oversized supported files are ignored rather than uploaded', () => {
  assert.equal(ignoreReason('pdf', 0)?.code, 'empty_file')
  assert.equal(ignoreReason('csv', 50 * 1024 * 1024 + 1)?.code, 'file_too_large')
})
