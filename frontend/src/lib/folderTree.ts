// Pure helpers of the folder hierarchy (components/FolderTree and the
// chat scope picker). Paths are the stored relative folder paths ending
// with "/" ("noveau consultation/Attijari/"); "" is the workspace root.
import type { FolderTreeEntry, ScopeMode } from '../types'

const nf = new Intl.NumberFormat('fr-FR')

export function folderDepth(path: string): number {
  return path ? path.replace(/\/$/, '').split('/').length - 1 : -1
}

export function parentPath(path: string): string {
  const parts = path.replace(/\/$/, '').split('/')
  return parts.length > 1 ? parts.slice(0, -1).join('/') + '/' : ''
}

/** "projets / noveau consultation / Attijari" */
export function displayPath(workspaceName: string, path: string): string {
  return [workspaceName, ...path.replace(/\/$/, '').split('/').filter(Boolean)].join(' / ')
}

function plural(n: number, one: string, many: string) {
  return `${nf.format(n)} ${n > 1 ? many : one}`
}

/** The three counters, never mixed: "1 document direct · 3 sous-dossiers · 6 au total" */
export function countsLabel(entry: Pick<FolderTreeEntry, 'direct_documents' | 'subfolder_count' | 'total_documents'>): string {
  return [
    plural(entry.direct_documents, 'document direct', 'documents directs'),
    plural(entry.subfolder_count, 'sous-dossier', 'sous-dossiers'),
    `${nf.format(entry.total_documents)} au total`,
  ].join(' · ')
}

export const SCOPE_MODES: { value: ScopeMode; label: string; hint: string }[] = [
  { value: 'folder', label: 'Dossier courant uniquement', hint: 'Seulement les documents placés directement dans le dossier choisi.' },
  { value: 'recursive', label: 'Dossier et sous-dossiers', hint: 'Tout le contenu du dossier choisi (recommandé).' },
  { value: 'all', label: 'Tous les espaces autorisés', hint: 'Recherche de documents dans tous vos espaces de travail.' },
]

/** What the scope button says: where and how deep the assistant searches. */
export function scopeLabel(workspaceName: string, scope: string[], mode: ScopeMode): string {
  if (mode === 'all') return 'Tous les espaces autorisés'
  const where = scope.length === 0 ? workspaceName : scope.length <= 2 ? scope.join(', ') : `${scope.length} dossiers`
  return mode === 'folder' ? `${where} (dossier seul)` : `${where} (avec sous-dossiers)`
}

function modeKey(workspaceId: string) {
  return `orbitia-scope-mode:${workspaceId}`
}

export function loadScopeMode(workspaceId: string | null): ScopeMode {
  if (!workspaceId) return 'recursive'
  try {
    const raw = localStorage.getItem(modeKey(workspaceId))
    return raw === 'folder' || raw === 'all' ? raw : 'recursive'
  } catch {
    return 'recursive'
  }
}

export function saveScopeMode(workspaceId: string, mode: ScopeMode) {
  try {
    localStorage.setItem(modeKey(workspaceId), mode)
  } catch {
    // storage unavailable: the mode just isn't remembered
  }
}
