export const SYNC_EXTENSIONS = new Set(['pdf', 'docx', 'pptx', 'xlsx', 'xls', 'csv', 'txt', 'md', 'jpg', 'jpeg', 'png', 'webp'])
export const MAX_SYNC_FILE_BYTES = 50 * 1024 * 1024

const IGNORED_DIRECTORY_NAMES = new Set([
  'node_modules', '.git', 'dist', 'build', '__pycache__', '.venv', 'venv', '.next',
  'coverage', 'local-models', 'logs', 'tmp', 'cache',
])

export function isIgnoredDirectory(name) {
  const normalized = name.toLowerCase()
  return IGNORED_DIRECTORY_NAMES.has(normalized) || normalized.includes('pycache')
}

export function ignoreReason(extension, size, excludedDirectory = null, sizeKnown = true) {
  if (excludedDirectory) return { code: 'ignored_directory', reason: 'dossier_exclu' }
  if (!SYNC_EXTENSIONS.has(extension)) return { code: 'unsupported_extension', reason: 'unsupported_extension' }
  if (sizeKnown && size === 0) return { code: 'empty_file', reason: 'empty_file' }
  if (size > MAX_SYNC_FILE_BYTES) return { code: 'file_too_large', reason: 'file_too_large' }
  return null
}
