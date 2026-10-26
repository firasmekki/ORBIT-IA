// The chat scope (sub-folders the assistant searches in) remembered per
// workspace, per browser - a convenience, never a permission: the server
// applies it on top of every access rule.

function storageKey(workspaceId: string) {
  return `orbitia-scope:${workspaceId}`
}

export function loadScope(workspaceId: string | null): string[] {
  if (!workspaceId) return []
  try {
    const raw = localStorage.getItem(storageKey(workspaceId))
    const parsed = raw ? JSON.parse(raw) : []
    return Array.isArray(parsed) ? parsed.filter((p): p is string => typeof p === 'string') : []
  } catch {
    return []
  }
}

export function saveScope(workspaceId: string, scope: string[]) {
  try {
    localStorage.setItem(storageKey(workspaceId), JSON.stringify(scope))
  } catch {
    // storage unavailable: the scope just isn't remembered
  }
}
