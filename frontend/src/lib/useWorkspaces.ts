// The current workspace, shared by /chat and /documents: the list comes
// from the backend (only workspaces this user may use), the selection is
// remembered per browser AND per account (two people taking turns on one
// PC never inherit each other's choice). The backend re-checks the
// workspace on every request - this is a UI preference, never a permission.
import { useCallback, useEffect, useState } from 'react'
import { listWorkspaces } from '../api/endpoints'
import type { Workspace } from '../types'

const STORAGE_KEY = 'orbitia_workspace_id'

function storageKey(userId: string | null) {
  return userId ? `${STORAGE_KEY}:${userId}` : STORAGE_KEY
}

function storedId(userId: string | null): string | null {
  try {
    return localStorage.getItem(storageKey(userId))
  } catch {
    return null
  }
}

export function useWorkspaces(userId: string | null) {
  const [workspaces, setWorkspaces] = useState<Workspace[]>([])
  // Choices made in this tab, per account; otherwise what this browser remembers.
  const [chosen, setChosen] = useState<Record<string, string>>({})
  const currentId = chosen[userId ?? ''] ?? storedId(userId)
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    try {
      const list = await listWorkspaces()
      setWorkspaces(list)
      setError(null)
      return list
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Impossible de charger les espaces de travail.')
      return []
    }
  }, [])

  useEffect(() => {
    let cancelled = false
    listWorkspaces()
      .then((list) => {
        if (!cancelled) setWorkspaces(list)
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Impossible de charger les espaces de travail.')
      })
    return () => {
      cancelled = true
    }
  }, [])

  const select = useCallback(
    (id: string) => {
      setChosen((prev) => ({ ...prev, [userId ?? '']: id }))
      try {
        localStorage.setItem(storageKey(userId), id)
      } catch {
        // per-browser convenience only
      }
    },
    [userId],
  )

  // Nothing remembered for this account in this browser (first visit, other
  // PC, other address such as :5173 vs :8081, cleared data), or a
  // remembered workspace this user can no longer use: the most recently
  // synced folder they can see - their own or one shared with them - so
  // everyone lands on the up-to-date synced copy, not on "Général".
  const lastSynced = workspaces
    .filter((w) => !w.is_default && w.last_synced_at)
    .sort((a, b) => (b.last_synced_at ?? '').localeCompare(a.last_synced_at ?? ''))[0]
  const current =
    workspaces.find((w) => w.id === currentId) ?? lastSynced ?? workspaces.find((w) => w.is_default) ?? null

  return { workspaces, current, select, refresh, error }
}
