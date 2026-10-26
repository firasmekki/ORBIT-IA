// Size gate for importing a folder through the browser (the folder is read
// client-side and each file is uploaded one by one). Only small folders are
// accepted: the folder is measured before anything is sent, and the walk
// stops as soon as a limit is exceeded, so a huge folder costs at most
// BROWSER_IMPORT_MAX_FILES + 1 metadata reads - never its content.
import { scanWorkspace } from './localFs.ts'

export const BROWSER_IMPORT_MAX_FILES = 200
export const BROWSER_IMPORT_MAX_BYTES = 500 * 1024 * 1024

export const BROWSER_IMPORT_TOO_LARGE_MESSAGE =
  'Ce dossier est trop volumineux pour un import depuis le navigateur. La liaison directe de dossiers volumineux arrive avec l’agent ORBIT.'

export class FolderTooLargeError extends Error {
  constructor() {
    super(BROWSER_IMPORT_TOO_LARGE_MESSAGE)
    this.name = 'FolderTooLargeError'
  }
}

export interface FolderMeasure {
  files: number
  bytes: number
  withinLimits: boolean
}

/** Counts every file of the folder (sub-folders included) and their total
 * size, stopping early once a limit is exceeded. */
export async function measureFolderForBrowserImport(root: FileSystemDirectoryHandle): Promise<FolderMeasure> {
  let files = 0
  let bytes = 0
  for await (const entry of scanWorkspace(root)) {
    files += 1
    bytes += entry.size
    if (files > BROWSER_IMPORT_MAX_FILES || bytes > BROWSER_IMPORT_MAX_BYTES) {
      return { files, bytes, withinLimits: false }
    }
  }
  return { files, bytes, withinLimits: true }
}

export async function assertBrowserImportAllowed(root: FileSystemDirectoryHandle): Promise<void> {
  if (!(await measureFolderForBrowserImport(root)).withinLimits) throw new FolderTooLargeError()
}
