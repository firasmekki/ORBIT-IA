// The current workspace and its connected local folder, for the whole app
// (/chat, /documents, ...) - so the folder keeps being watched whatever page
// is open.
//
// Watching: browsers give no reliable "folder changed" event, so the folder
// is polled. Each check only lists it (paths, sizes, modification dates -
// no file is read); only when that listing differs from the last synced one
// does a real sync run (hash, plan, upload of the new/modified files,
// removal of the deleted ones - see lib/workspaceSync.ts). New sub-folders
// are included: the listing is recursive.
import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from 'react'
import {
  clearWorkspaceHandle,
  ensureReadPermission,
  hasReadPermission,
  loadWorkspaceHandle,
  saveWorkspaceHandle,
  scanWorkspace,
  workspaceHandleKey,
} from '../lib/localFs'
import { createWorkspace, deleteWorkspace } from '../api/endpoints'
import { assertBrowserImportAllowed, FolderTooLargeError } from '../lib/browserImport'
import { useWorkspaces } from '../lib/useWorkspaces'
import { useAuth } from './AuthContext'
import { syncWorkspaceFolder, type SyncProgress, type SyncReport } from '../lib/workspaceSync'
import type { Workspace } from '../types'

/** How often the connected folder is checked for changes. */
export const FOLDER_POLL_MS = 10_000

type SyncState = 'idle' | 'syncing' | 'synced' | 'error'

interface WorkspaceContextValue {
  workspaces: Workspace[]
  workspace: Workspace | null
  workspaceId: string | null
  selectWorkspace: (id: string) => void
  refreshWorkspaces: () => Promise<Workspace[]>
  folderName: string | null
  /** The browser forgot the folder permission (e.g. after a reload): a click is needed. */
  needsPermission: boolean
  syncState: SyncState
  syncProgress: SyncProgress | null
  syncReport: SyncReport | null
  lastSyncAt: Date | null
  /** Incremented after every sync that changed something - pages reload their lists on it. */
  syncVersion: number
  syncError: string | null
  connectFolder: (handle: FileSystemDirectoryHandle) => Promise<void>
  disconnectFolder: () => Promise<void>
  /** Removes a workspace from Orbitia (owner) and forgets its folder in this
   * browser. The folder on the computer is never touched. */
  removeWorkspace: (id: string) => Promise<void>
  reauthorizeFolder: () => Promise<void>
  syncNow: () => Promise<void>
}

const WorkspaceContext = createContext<WorkspaceContextValue | null>(null)

/** Cheap fingerprint of the folder: every file with size + date (an added
 * unsupported file must also refresh the "ignored" list). */
async function folderSnapshot(handle: FileSystemDirectoryHandle): Promise<string> {
  // Order-independent, streaming fingerprint of metadata. This keeps a
  // 100k-file watch from retaining a second full path list in memory.
  let count = 0
  let sum = 0
  let xor = 0
  for await (const entry of scanWorkspace(handle)) {
    const value = `${entry.relative_path}|${entry.size}|${entry.modified_at ?? ''}`
    let hash = 2166136261
    for (let i = 0; i < value.length; i++) hash = Math.imul(hash ^ value.charCodeAt(i), 16777619)
    hash >>>= 0
    count += 1
    sum = (sum + hash) >>> 0
    xor ^= hash
  }
  return `${count}:${sum}:${xor >>> 0}`
}

export function WorkspaceProvider({ children }: { children: ReactNode }) {
  const { user } = useAuth()
  const { workspaces, current: workspace, select, refresh: refreshWorkspaces } = useWorkspaces(user?.id ?? null)
  const workspaceId = workspace?.id ?? null
  const canSync = workspace?.can_sync ?? false

  const [folderHandle, setFolderHandle] = useState<FileSystemDirectoryHandle | null>(null)
  const [needsPermission, setNeedsPermission] = useState(false)
  const [syncState, setSyncState] = useState<SyncState>('idle')
  const [syncProgress, setSyncProgress] = useState<SyncProgress | null>(null)
  const [syncReport, setSyncReport] = useState<SyncReport | null>(null)
  const [lastSyncAt, setLastSyncAt] = useState<Date | null>(null)
  const [syncVersion, setSyncVersion] = useState(0)
  const [syncError, setSyncError] = useState<string | null>(null)

  // Read by the polling loop without restarting it on every render.
  const syncingRef = useRef(false)
  const snapshotRef = useRef<string | null>(null)

  const runSync = useCallback(
    async (id: string, handle: FileSystemDirectoryHandle) => {
      if (syncingRef.current) return
      syncingRef.current = true
      setSyncState('syncing')
      setSyncProgress(null)
      setSyncError(null)
      try {
        // Every path that uploads goes through here (new folder, folder
        // restored after a reload, watcher, "Synchroniser"): a folder that
        // grew past the browser-import limits is never uploaded, and is no
        // longer watched (the full rescans themselves are the cost).
        try {
          await assertBrowserImportAllowed(handle)
        } catch (err) {
          if (err instanceof FolderTooLargeError) setFolderHandle(null)
          throw err
        }
        // Taken BEFORE syncing: a file added while the sync runs differs
        // from it and is picked up by the next check.
        const snapshot = await folderSnapshot(handle)
        const report = await syncWorkspaceFolder(id, handle, setSyncProgress)
        // A rejected file is reported to the user, but must not make the
        // watcher replay the same full upload batch every 10 seconds. The
        // user can retry with "Synchroniser maintenant" or after changing
        // the folder; successful files are already tracked by their hashes.
        snapshotRef.current = snapshot
        setSyncReport(report)
        setLastSyncAt(new Date())
        setSyncState(report.failed.length > 0 ? 'error' : 'synced')
        if (report.uploaded.length > 0 || report.removed.length > 0) setSyncVersion((v) => v + 1)
      } catch (err) {
        setSyncState('error')
        setSyncError(err instanceof Error ? err.message : 'Synchronisation impossible.')
      } finally {
        syncingRef.current = false
        setSyncProgress(null)
        void refreshWorkspaces()
      }
    },
    [refreshWorkspaces],
  )

  // Restore this workspace's folder after a page load / workspace switch.
  useEffect(() => {
    if (!workspaceId || !canSync) return
    let cancelled = false
    ;(async () => {
      try {
        const handle = await loadWorkspaceHandle(workspaceHandleKey(workspaceId))
        if (!handle || cancelled) return
        setFolderHandle(handle)
        if (await hasReadPermission(handle)) {
          setNeedsPermission(false)
          await runSync(workspaceId, handle)
        } else {
          // Chrome/Edge may forget a folder permission between visits and
          // only grant it again on a user click - see reauthorizeFolder.
          setNeedsPermission(true)
        }
      } catch {
        // non-fatal - the folder can be connected again from the picker
      }
    })()
    return () => {
      cancelled = true
    }
  }, [workspaceId, canSync, runSync])

  // Watch the connected folder: poll + check again when the tab regains focus.
  useEffect(() => {
    if (!folderHandle || !workspaceId || !canSync || needsPermission) return
    let stopped = false
    const check = async () => {
      if (stopped || syncingRef.current) return
      try {
        if (!(await hasReadPermission(folderHandle))) {
          setNeedsPermission(true)
          return
        }
        if ((await folderSnapshot(folderHandle)) !== snapshotRef.current) {
          await runSync(workspaceId, folderHandle)
        }
      } catch {
        // folder temporarily unreadable (renamed, unplugged): retried next tick
      }
    }
    const timer = setInterval(check, FOLDER_POLL_MS)
    const onFocus = () => {
      if (!document.hidden) void check()
    }
    window.addEventListener('focus', onFocus)
    document.addEventListener('visibilitychange', onFocus)
    return () => {
      stopped = true
      clearInterval(timer)
      window.removeEventListener('focus', onFocus)
      document.removeEventListener('visibilitychange', onFocus)
    }
  }, [folderHandle, workspaceId, canSync, needsPermission, runSync])

  // While files are being embedded in the background, refresh the
  // workspace counts so "⟳ en cours" turns into "indexé" without a reload.
  // A server folder is scanned by the backend on its own: keep its state
  // fresh too (new files may appear at any time).
  const pendingCount = workspace?.pending_count ?? 0
  const serverScanning = workspace?.source?.status === 'scanning'
  const hasServerSource = Boolean(workspace?.source)
  useEffect(() => {
    if (pendingCount === 0 && !serverScanning && !hasServerSource) return
    const timer = setInterval(() => void refreshWorkspaces(), pendingCount > 0 || serverScanning ? 5000 : 15000)
    return () => clearInterval(timer)
  }, [pendingCount, serverScanning, hasServerSource, refreshWorkspaces])

  const selectWorkspace = useCallback(
    (id: string) => {
      if (id === workspaceId) return
      setFolderHandle(null)
      setNeedsPermission(false)
      setSyncState('idle')
      setSyncReport(null)
      setSyncError(null)
      setLastSyncAt(null)
      snapshotRef.current = null
      select(id)
    },
    [workspaceId, select],
  )

  /** Connects a folder. In a workspace this user may sync, that one; from
   * "Général" (never synced) or someone else's workspace, the user's own
   * workspace named after the folder - reused if it exists, created
   * otherwise - so choosing a folder always ends in a synced workspace. */
  const connectFolder = useCallback(
    async (handle: FileSystemDirectoryHandle) => {
      // Before anything else - in particular before creating a workspace
      // named after a folder that will not be imported.
      await assertBrowserImportAllowed(handle)
      let targetId = workspace?.can_sync ? workspace.id : null
      if (!targetId) {
        const list = await refreshWorkspaces()
        const mine = list.find(
          (w) => w.is_owner && !w.is_default && (w.folder_name === handle.name || w.name === handle.name),
        )
        if (mine) {
          targetId = mine.id
        } else {
          const created = await createWorkspace({
            name: handle.name,
            default_department: 'GENERAL',
            default_confidentiality: 'INTERNAL',
            // The company folder: every user finds the synced copy (each
            // one filtered by their own rights, server-side).
            visibility: 'organization',
          })
          await refreshWorkspaces()
          targetId = created.id
        }
        selectWorkspace(targetId)
      }
      setFolderHandle(handle)
      setNeedsPermission(false)
      snapshotRef.current = null
      try {
        await saveWorkspaceHandle(handle, workspaceHandleKey(targetId))
      } catch {
        // non-fatal: synced now, just not remembered after a reload
      }
      await runSync(targetId, handle)
    },
    [workspace, refreshWorkspaces, selectWorkspace, runSync],
  )

  const disconnectFolder = useCallback(async () => {
    setFolderHandle(null)
    setNeedsPermission(false)
    setSyncState('idle')
    setSyncReport(null)
    snapshotRef.current = null
    if (!workspaceId) return
    try {
      await clearWorkspaceHandle(workspaceHandleKey(workspaceId))
    } catch {
      // non-fatal
    }
  }, [workspaceId])

  const removeWorkspace = useCallback(
    async (id: string) => {
      await deleteWorkspace(id)
      try {
        // Only this browser's permission to read the folder - not the folder.
        await clearWorkspaceHandle(workspaceHandleKey(id))
      } catch {
        // non-fatal
      }
      if (id === workspaceId) {
        setFolderHandle(null)
        setNeedsPermission(false)
        setSyncState('idle')
        setSyncReport(null)
        snapshotRef.current = null
      }
      // The removed id is no longer listed: the selection falls back to the
      // latest synced folder, or "Général".
      await refreshWorkspaces()
    },
    [workspaceId, refreshWorkspaces],
  )

  const reauthorizeFolder = useCallback(async () => {
    if (!folderHandle || !workspaceId) return
    if (await ensureReadPermission(folderHandle)) {
      setNeedsPermission(false)
      await runSync(workspaceId, folderHandle)
    }
  }, [folderHandle, workspaceId, runSync])

  const syncNow = useCallback(async () => {
    if (folderHandle && workspaceId) await runSync(workspaceId, folderHandle)
  }, [folderHandle, workspaceId, runSync])

  return (
    <WorkspaceContext.Provider
      value={{
        workspaces,
        workspace,
        workspaceId,
        selectWorkspace,
        refreshWorkspaces,
        folderName: folderHandle?.name ?? null,
        needsPermission,
        syncState,
        syncProgress,
        syncReport,
        lastSyncAt,
        syncVersion,
        syncError,
        connectFolder,
        disconnectFolder,
        removeWorkspace,
        reauthorizeFolder,
        syncNow,
      }}
    >
      {children}
    </WorkspaceContext.Provider>
  )
}

export function useWorkspaceContext(): WorkspaceContextValue {
  const value = useContext(WorkspaceContext)
  if (!value) throw new Error('useWorkspaceContext must be used inside <WorkspaceProvider>')
  return value
}
