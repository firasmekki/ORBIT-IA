// Local policy gate for workspace folder indexing. Only indexable files are
// hashed and uploaded; unsupported entries never reach /sync/file.
import { planWorkspaceSync, uploadWorkspaceFile } from '../api/endpoints'
import { getWorkspaceFile, scanWorkspace } from './localFs'
import { ignoreReason } from './workspaceSyncPolicy.js'
import type { LocalFileEntry } from '../types'

export interface SyncProgress {
  phase: 'scanning' | 'hashing' | 'uploading' | 'done'
  done: number
  total: number
  current?: string
}

export interface SyncReport {
  totalFiles: number
  localFiles: number
  ignored: { relative_path: string; reason: string; code: string; status: 'ignored' }[]
  formatCounts: Record<string, number>
  uploaded: string[]
  unchanged: number
  removed: string[]
  rejected: { relative_path: string; reason: string }[]
  failed: { relative_path: string; reason: string }[]
}

async function sha256Hex(file: File): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', await file.arrayBuffer())
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, '0')).join('')
}

async function forEachConcurrent<T>(items: T[], limit: number, worker: (item: T, index: number) => Promise<void>) {
  let next = 0
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, async () => {
    while (next < items.length) {
      const index = next++
      await worker(items[index], index)
    }
  }))
}

export async function syncWorkspaceFolder(
  workspaceId: string,
  handle: FileSystemDirectoryHandle,
  onProgress: (progress: SyncProgress) => void,
): Promise<SyncReport> {
  onProgress({ phase: 'scanning', done: 0, total: 0 })
  const ignored: SyncReport['ignored'] = []
  const formatCounts: Record<string, number> = {}
  const excludedDirectories = new Set<string>()
  const manifest: { relative_path: string; sha256: string; size: number; last_modified: string | null }[] = []
  const failed: SyncReport['failed'] = []
  const hashBatch: LocalFileEntry[] = []
  let totalFiles = 0
  let hashDone = 0

  const hashPendingBatch = async () => {
    if (!hashBatch.length) return
    const batch = hashBatch.splice(0, hashBatch.length)
    await forEachConcurrent(batch, 4, async (entry) => {
      onProgress({ phase: 'hashing', done: hashDone, total: 0, current: entry.relative_path })
      try {
        const file = await getWorkspaceFile(handle, entry.relative_path)
        manifest.push({
          relative_path: entry.relative_path,
          sha256: await sha256Hex(file),
          size: file.size,
          last_modified: new Date(file.lastModified).toISOString(),
        })
      } catch {
        failed.push({ relative_path: entry.relative_path, reason: 'illisible depuis le navigateur' })
      } finally {
        hashDone += 1
      }
    })
  }

  for await (const entry of scanWorkspace(handle)) {
    totalFiles += 1
    if (entry.excluded_directory) excludedDirectories.add(entry.excluded_directory)
    const format = entry.extension.toUpperCase() || '(sans extension)'
    formatCounts[format] = (formatCounts[format] ?? 0) + 1
    const reason = ignoreReason(entry.extension, entry.size, entry.excluded_directory, entry.size_known)
    if (reason) ignored.push({ relative_path: entry.relative_path, ...reason, status: 'ignored' })
    if (!reason) hashBatch.push(entry)
    if (hashBatch.length >= 128) await hashPendingBatch()
    if (totalFiles % 250 === 0) {
      onProgress({ phase: 'scanning', done: totalFiles, total: 0, current: entry.relative_path })
    }
  }
  await hashPendingBatch()

  // One metadata batch makes the incremental plan. Only changed, locally
  // supported files are uploaded below.
  const plan = await planWorkspaceSync(workspaceId, {
    folder_name: handle.name,
    files: manifest,
    excluded_directories: [...excludedDirectories],
  })

  const uploaded: string[] = []
  await forEachConcurrent(plan.to_upload, 3, async (path, i) => {
    onProgress({ phase: 'uploading', done: i, total: plan.to_upload.length, current: path })
    try {
      const file = await getWorkspaceFile(handle, path)
      await uploadWorkspaceFile(workspaceId, path, file)
      uploaded.push(path)
    } catch (err) {
      failed.push({ relative_path: path, reason: err instanceof Error ? err.message : 'échec de l’indexation' })
    }
  })

  onProgress({ phase: 'done', done: plan.to_upload.length, total: plan.to_upload.length })
  return {
    totalFiles,
    localFiles: manifest.length,
    ignored: [...ignored, ...plan.rejected.map((entry) => ({ ...entry, code: 'server_rejected', status: 'ignored' as const }))],
    formatCounts,
    uploaded,
    unchanged: plan.unchanged.length,
    removed: plan.removed,
    rejected: plan.rejected,
    failed,
  }
}
