// Thin wrapper around the File System Access API (Chrome/Edge only - see
// src/types/file-system-access.d.ts for the ambient types, not yet part
// of TypeScript's bundled DOM lib). The whole point of this module: the
// FileSystemDirectoryHandle the user grants NEVER leaves the browser.
// Only a lightweight metadata index (walkWorkspace) and, on explicit
// agent request, one file's bytes at a time (readWorkspaceFile) are ever
// sent to the backend - see app/agent/local_files.py's docstring for the
// matching backend-side half of this contract.
import type { LocalFileEntry } from '../types'

const DB_NAME = 'orbitia-workspace'
const STORE_NAME = 'handles'
const HANDLE_KEY = 'active'

// Guards against a pathological selection (e.g. the user's whole home
// folder) making the index-build hang the tab - matches this feature's
// own test plan ("1000+ fichiers").
const MAX_WORKSPACE_FILES = 5000

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

export async function saveWorkspaceHandle(handle: FileSystemDirectoryHandle): Promise<void> {
  const db = await openDb()
  try {
    await new Promise<void>((resolve, reject) => {
      const tx = db.transaction(STORE_NAME, 'readwrite')
      tx.objectStore(STORE_NAME).put(handle, HANDLE_KEY)
      tx.oncomplete = () => resolve()
      tx.onerror = () => reject(tx.error)
    })
  } finally {
    db.close()
  }
}

export async function loadWorkspaceHandle(): Promise<FileSystemDirectoryHandle | null> {
  const db = await openDb()
  try {
    return await new Promise<FileSystemDirectoryHandle | null>((resolve, reject) => {
      const tx = db.transaction(STORE_NAME, 'readonly')
      const req = tx.objectStore(STORE_NAME).get(HANDLE_KEY)
      req.onsuccess = () => resolve((req.result as FileSystemDirectoryHandle | undefined) ?? null)
      req.onerror = () => reject(req.error)
    })
  } finally {
    db.close()
  }
}

export async function clearWorkspaceHandle(): Promise<void> {
  const db = await openDb()
  try {
    await new Promise<void>((resolve, reject) => {
      const tx = db.transaction(STORE_NAME, 'readwrite')
      tx.objectStore(STORE_NAME).delete(HANDLE_KEY)
      tx.oncomplete = () => resolve()
      tx.onerror = () => reject(tx.error)
    })
  } finally {
    db.close()
  }
}

/** Recursively walks every sub-folder of `root`, building metadata-only
 * entries - no file content is ever read here. A file that can't be
 * opened (permission/lock) is still indexed by name; size/date are left
 * unknown rather than guessed. */
export async function walkWorkspace(root: FileSystemDirectoryHandle): Promise<LocalFileEntry[]> {
  const entries: LocalFileEntry[] = []

  async function walk(dir: FileSystemDirectoryHandle, relativePrefix: string, parentFolder: string | null) {
    for await (const [name, handle] of dir.entries()) {
      if (entries.length >= MAX_WORKSPACE_FILES) return
      const relativePath = relativePrefix ? `${relativePrefix}/${name}` : name
      if (handle.kind === 'directory') {
        await walk(handle as FileSystemDirectoryHandle, relativePath, relativePath)
      } else {
        const fileHandle = handle as FileSystemFileHandle
        let size = 0
        let modifiedAt: string | null = null
        try {
          const file = await fileHandle.getFile()
          size = file.size
          modifiedAt = new Date(file.lastModified).toISOString()
        } catch {
          // unreadable right now - keep the entry, just without size/date
        }
        const dotIndex = name.lastIndexOf('.')
        const extension = dotIndex > 0 ? name.slice(dotIndex + 1).toLowerCase() : ''
        entries.push({
          name,
          relative_path: relativePath,
          extension,
          size,
          modified_at: modifiedAt,
          parent_folder: parentFolder,
        })
      }
    }
  }

  await walk(root, '', null)
  return entries
}

/** Reads one file's bytes by its relative path within `root` - called
 * only when the agent has explicitly asked for this exact file (see
 * ChatPage.tsx's pending_client_action handling), never eagerly. */
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
