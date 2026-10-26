// Thin wrapper around the File System Access API (Chrome/Edge only - see
// src/types/file-system-access.d.ts for the ambient types, not yet part
// of TypeScript's bundled DOM lib). The whole point of this module: the
// FileSystemDirectoryHandle the user grants NEVER leaves the browser.
// Only a lightweight metadata index (walkWorkspace) and, on explicit
// agent request, one file's bytes at a time (readWorkspaceFile) are ever
// sent to the backend - see app/agent/local_files.py's docstring for the
// matching backend-side half of this contract.
import type { LocalFileEntry } from '../types'
import { isIgnoredDirectory } from './workspaceSyncPolicy.js'

const DB_NAME = 'orbitia-workspace'
const STORE_NAME = 'handles'
const HANDLE_KEY = 'active'

export function isWorkspaceSupported(): boolean {
  return typeof window !== 'undefined' && typeof window.showDirectoryPicker === 'function'
}

export async function pickWorkspaceDirectory(): Promise<FileSystemDirectoryHandle> {
  if (!window.showDirectoryPicker) {
    throw new Error(
      "Votre navigateur ne permet pas de sélectionner un dossier local. Utilisez Chrome ou Edge.",
    )
  }
  return window.showDirectoryPicker({ mode: 'read' })
}

/** Without prompting: is read access still granted (e.g. after a reload)? */
export async function hasReadPermission(handle: FileSystemDirectoryHandle): Promise<boolean> {
  try {
    return (await handle.queryPermission({ mode: 'read' })) === 'granted'
  } catch {
    return false
  }
}

export async function ensureReadPermission(handle: FileSystemDirectoryHandle): Promise<boolean> {
  const current = await handle.queryPermission({ mode: 'read' })
  if (current === 'granted') return true
  const requested = await handle.requestPermission({ mode: 'read' })
  return requested === 'granted'
}

function openDb(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, 1)
    request.onupgradeneeded = () => {
      request.result.createObjectStore(STORE_NAME)
    }
    request.onsuccess = () => resolve(request.result)
    request.onerror = () => reject(request.error)
  })
}

/** One remembered folder per workspace: key = `ws:<workspace id>`. */
export function workspaceHandleKey(workspaceId: string): string {
  return `ws:${workspaceId}`
}

export async function saveWorkspaceHandle(handle: FileSystemDirectoryHandle, key: string = HANDLE_KEY): Promise<void> {
  const db = await openDb()
  try {
    await new Promise<void>((resolve, reject) => {
      const tx = db.transaction(STORE_NAME, 'readwrite')
      tx.objectStore(STORE_NAME).put(handle, key)
      tx.oncomplete = () => resolve()
      tx.onerror = () => reject(tx.error)
    })
  } finally {
    db.close()
  }
}

export async function loadWorkspaceHandle(key: string = HANDLE_KEY): Promise<FileSystemDirectoryHandle | null> {
  const db = await openDb()
  try {
    return await new Promise<FileSystemDirectoryHandle | null>((resolve, reject) => {
      const tx = db.transaction(STORE_NAME, 'readonly')
      const req = tx.objectStore(STORE_NAME).get(key)
      req.onsuccess = () => resolve((req.result as FileSystemDirectoryHandle | undefined) ?? null)
      req.onerror = () => reject(req.error)
    })
  } finally {
    db.close()
  }
}

export async function clearWorkspaceHandle(key: string = HANDLE_KEY): Promise<void> {
  const db = await openDb()
  try {
    await new Promise<void>((resolve, reject) => {
      const tx = db.transaction(STORE_NAME, 'readwrite')
      tx.objectStore(STORE_NAME).delete(key)
      tx.oncomplete = () => resolve()
      tx.onerror = () => reject(tx.error)
    })
  } finally {
    db.close()
  }
}

/** Streams every file entry recursively. Only one metadata record is held
 * by the traversal at a time; file bytes are never read. */
export async function* scanWorkspace(root: FileSystemDirectoryHandle): AsyncGenerator<LocalFileEntry> {
  async function* walk(
    dir: FileSystemDirectoryHandle,
    relativePrefix: string,
    parentFolder: string | null,
    excludedDirectory: string | null,
  ): AsyncGenerator<LocalFileEntry> {
    for await (const [name, handle] of dir.entries()) {
      const relativePath = relativePrefix ? `${relativePrefix}/${name}` : name
      if (handle.kind === 'directory') {
        const excluded = excludedDirectory ?? (isIgnoredDirectory(name) ? relativePath : null)
        yield* walk(handle as FileSystemDirectoryHandle, relativePath, relativePath, excluded)
      } else {
        const fileHandle = handle as FileSystemFileHandle
        const dotIndex = name.lastIndexOf('.')
        const extension = dotIndex > 0 ? name.slice(dotIndex + 1).toLowerCase() : ''
        let size = 0
        let sizeKnown = false
        let modifiedAt: string | null = null
        try {
          const file = await fileHandle.getFile()
          size = file.size
          sizeKnown = true
          modifiedAt = new Date(file.lastModified).toISOString()
        } catch {
          // unreadable right now - keep the entry, just without size/date
        }
        yield {
          name,
          relative_path: relativePath,
          extension,
          size,
          modified_at: modifiedAt,
          parent_folder: parentFolder,
          excluded_directory: excludedDirectory,
          size_known: sizeKnown,
        }
      }
    }
  }

  yield* walk(root, '', null, null)
}

/** Compatibility helper for callers that need a materialized list. */
export async function walkWorkspace(root: FileSystemDirectoryHandle): Promise<LocalFileEntry[]> {
  const entries: LocalFileEntry[] = []
  for await (const entry of scanWorkspace(root)) entries.push(entry)
  return entries
}

/** Reads one file's bytes by its relative path within `root` - called
 * only when the agent has explicitly asked for this exact file (see
 * ChatPage.tsx's pending_client_action handling), never eagerly. */
/** The File at `relativePath` (lazy - no bytes read until asked). */
export async function getWorkspaceFile(root: FileSystemDirectoryHandle, relativePath: string): Promise<File> {
  const segments = relativePath.split('/').filter(Boolean)
  let dir = root
  for (let i = 0; i < segments.length - 1; i++) {
    dir = await dir.getDirectoryHandle(segments[i])
  }
  return (await dir.getFileHandle(segments[segments.length - 1])).getFile()
}

export async function readWorkspaceFile(
  root: FileSystemDirectoryHandle,
  relativePath: string,
): Promise<{ file: File; base64: string }> {
  const segments = relativePath.split('/').filter(Boolean)
  let dir = root
  for (let i = 0; i < segments.length - 1; i++) {
    dir = await dir.getDirectoryHandle(segments[i])
  }
  const fileHandle = await dir.getFileHandle(segments[segments.length - 1])
  const file = await fileHandle.getFile()
  const buffer = await file.arrayBuffer()
  return { file, base64: arrayBufferToBase64(buffer) }
}

function arrayBufferToBase64(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer)
  let binary = ''
  const chunkSize = 0x8000
  for (let i = 0; i < bytes.length; i += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(i, i + chunkSize))
  }
  return btoa(binary)
}
