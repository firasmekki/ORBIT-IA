import { useCallback, useEffect, useState, type ReactNode } from 'react'
import { folderTree, searchFolders } from '../api/endpoints'
import { countsLabel, displayPath, folderDepth } from '../lib/folderTree'
import type { FolderTreeEntry } from '../types'

// The workspace's real folder hierarchy, built by the server from the
// stored paths (app/services/folder_tree.py): open / close a folder to load
// its sub-folders (one level per request - fine for thousands of folders),
// search a folder by name anywhere. Two folders with the same name are
// told apart by their full path. Counts are what this user may read.
//
// variant "navigate": a click selects one folder (Documents page);
// variant "pick": checkboxes, several folders (chat scope).

const nf = new Intl.NumberFormat('fr-FR')

function Counters({ entry }: { entry: FolderTreeEntry }) {
  return (
    <span className="tree-counts" title={countsLabel(entry)}>
      <span>{nf.format(entry.direct_documents)} direct{entry.direct_documents > 1 ? 's' : ''}</span>
      <span>{nf.format(entry.subfolder_count)} sous-dossier{entry.subfolder_count > 1 ? 's' : ''}</span>
      <span>{nf.format(entry.total_documents)} au total</span>
    </span>
  )
}

export function FolderTree({
  workspaceId,
  workspaceName,
  variant,
  selected,
  onSelect,
  onToggle,
  rowActions,
  refreshKey = 0,
}: {
  workspaceId: string
  workspaceName: string
  variant: 'navigate' | 'pick'
  /** Folder paths ending with "/" ("" = the workspace root). */
  selected: string[]
  onSelect?: (entry: FolderTreeEntry) => void
  onToggle?: (entry: FolderTreeEntry) => void
  rowActions?: (entry: FolderTreeEntry) => ReactNode
  refreshKey?: number
}) {
  const [root, setRoot] = useState<FolderTreeEntry | null>(null)
  const [children, setChildren] = useState<Record<string, FolderTreeEntry[]>>({})
  const [truncated, setTruncated] = useState<Record<string, boolean>>({})
  const [expanded, setExpanded] = useState<Set<string>>(new Set(['']))
  const [loading, setLoading] = useState<Set<string>>(new Set())
  const [error, setError] = useState<string | null>(null)
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<{ entries: FolderTreeEntry[]; total: number } | null>(null)

  const load = useCallback(
    async (path: string) => {
      setLoading((s) => new Set(s).add(path))
      try {
        const listing = await folderTree(workspaceId, path)
        if (path === '') setRoot(listing.folder)
        setChildren((c) => ({ ...c, [path]: listing.folders }))
        setTruncated((t) => ({ ...t, [path]: listing.folders_truncated }))
        setError(null)
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Impossible de charger les dossiers.')
      } finally {
        setLoading((s) => {
          const next = new Set(s)
          next.delete(path)
          return next
        })
      }
    },
    [workspaceId],
  )

  // Workspace changed or data changed (share, sync): reload the open levels.
  useEffect(() => {
    setChildren({})
    setRoot(null)
    for (const path of expanded) void load(path)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspaceId, refreshKey, load])

  useEffect(() => {
    const needle = query.trim()
    if (!needle) {
      setResults(null)
      return
    }
    const timer = setTimeout(() => {
      searchFolders(workspaceId, needle)
        .then((res) => setResults({ entries: res.results, total: res.result_count }))
        .catch(() => setResults({ entries: [], total: 0 }))
    }, 300)
    return () => clearTimeout(timer)
  }, [query, workspaceId])

  function toggleOpen(path: string) {
    setExpanded((s) => {
      const next = new Set(s)
      if (next.has(path)) next.delete(path)
      else {
        next.add(path)
        if (!children[path]) void load(path)
      }
      return next
    })
  }

  function activate(entry: FolderTreeEntry) {
    if (variant === 'navigate') onSelect?.(entry)
    else onToggle?.(entry)
  }

  function row(entry: FolderTreeEntry, isRoot = false): ReactNode {
    const open = expanded.has(entry.path)
    const isSelected = selected.includes(entry.path)
    const depth = isRoot ? 0 : folderDepth(entry.path) + 1
    return (
      <li
        key={entry.path || '__root'}
        role="treeitem"
        aria-level={depth + 1}
        aria-expanded={entry.has_children ? open : undefined}
        aria-selected={isSelected}
      >
        <div className={`tree-row${isSelected ? ' selected' : ''}`} style={{ paddingLeft: 6 + depth * 18 }}>
          {entry.has_children ? (
            <button
              type="button"
              className="tree-toggle"
              aria-label={`${open ? 'Fermer' : 'Ouvrir'} ${entry.name || workspaceName}`}
              onClick={() => toggleOpen(entry.path)}
            >
              {loading.has(entry.path) ? '…' : open ? '▾' : '▸'}
            </button>
          ) : (
            <span className="tree-toggle" aria-hidden="true" />
          )}
          {variant === 'pick' && (
            <input
              type="checkbox"
              checked={isSelected}
              onChange={() => onToggle?.(entry)}
              aria-label={`Inclure ${displayPath(workspaceName, entry.path)}`}
            />
          )}
          <button type="button" className="tree-name" title={displayPath(workspaceName, entry.path)} onClick={() => activate(entry)}>
            <span aria-hidden="true">{isRoot ? '🗂️' : entry.restricted || entry.restricted_by ? '🔒' : '📁'}</span>{' '}
            {isRoot ? workspaceName : entry.name}
          </button>
          <Counters entry={entry} />
          {rowActions?.(entry)}
        </div>
        {open && children[entry.path] && (
          <ul role="group" className="tree-group">
            {children[entry.path].map((child) => row(child))}
            {truncated[entry.path] && (
              <li className="tree-note" style={{ paddingLeft: 6 + (depth + 1) * 18 }}>
                Liste limitée aux 500 premiers sous-dossiers : utilisez la recherche.
              </li>
            )}
            {children[entry.path].length === 0 && (
              <li className="tree-note" style={{ paddingLeft: 6 + (depth + 1) * 18 }}>
                Aucun sous-dossier.
              </li>
            )}
          </ul>
        )}
      </li>
    )
  }

  return (
    <div className="folder-tree">
      <input
        className="input tree-search"
        type="search"
        placeholder="Rechercher un dossier par son nom…"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        aria-label="Rechercher un dossier"
      />
      {error && <div className="error-banner">{error}</div>}
      {results ? (
        <ul className="tree-results" aria-label="Dossiers trouvés">
          {results.entries.map((entry) => (
            <li key={entry.path}>
              <div className={`tree-row${selected.includes(entry.path) ? ' selected' : ''}`}>
                {variant === 'pick' && (
                  <input
                    type="checkbox"
                    checked={selected.includes(entry.path)}
                    onChange={() => onToggle?.(entry)}
                    aria-label={`Inclure ${displayPath(workspaceName, entry.path)}`}
                  />
                )}
                <button type="button" className="tree-name" onClick={() => activate(entry)}>
                  📁 {displayPath(workspaceName, entry.path)}
                </button>
                <Counters entry={entry} />
                {rowActions?.(entry)}
              </div>
            </li>
          ))}
          {results.entries.length === 0 && <li className="tree-note">Aucun dossier ne porte ce nom.</li>}
          {results.total > results.entries.length && (
            <li className="tree-note">
              {nf.format(results.total)} dossiers trouvés : précisez la recherche pour voir les autres.
            </li>
          )}
        </ul>
      ) : root ? (
        <ul role="tree" className="tree-root" aria-label={`Dossiers de ${workspaceName}`}>
          {row(root, true)}
        </ul>
      ) : (
        !error && <div className="tree-note">Chargement des dossiers…</div>
      )}
    </div>
  )
}
