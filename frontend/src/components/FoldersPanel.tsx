import { useEffect, useState } from 'react'
import { adminListUsers, folderTree, setFolderRestriction, shareFolder, unshareFolder } from '../api/endpoints'
import { countsLabel, displayPath, parentPath } from '../lib/folderTree'
import type { FolderListing, FolderTreeEntry, WorkspaceFolder, WorkspaceFolders } from '../types'
import { FolderTree } from './FolderTree'

/** The workspace's folders as a real hierarchy (FolderTree): open a folder
 * to see its own sub-folders; select one to list its documents - directly
 * inside it, or with its sub-folders. Sharing is an action of the selected
 * folder ("⋯" / "Actions"), not a button on every line. The server computes
 * everything shown here and enforces it on every read. */
export function FoldersPanel({
  workspaceId,
  workspaceName,
  selected,
  includeSubfolders,
  onSelect,
  onIncludeSubfoldersChange,
  onChanged,
}: {
  workspaceId: string
  workspaceName: string
  /** Selected folder path ("" = the whole workspace). */
  selected: string
  includeSubfolders: boolean
  onSelect: (path: string) => void
  onIncludeSubfoldersChange: (value: boolean) => void
  onChanged: () => void
}) {
  const [listing, setListing] = useState<FolderListing | null>(null)
  const [sharing, setSharing] = useState(false)
  const [menuOpen, setMenuOpen] = useState(false)
  const [refreshKey, setRefreshKey] = useState(0)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    folderTree(workspaceId, selected)
      .then((l) => {
        if (!cancelled) {
          setListing(l)
          setError(null)
        }
      })
      .catch((err) => {
        if (cancelled) return
        setError(err instanceof Error ? err.message : 'Dossier introuvable.')
        if (selected) onSelect('')
      })
    return () => {
      cancelled = true
    }
  }, [workspaceId, selected, refreshKey, onSelect])

  function select(path: string) {
    setSharing(false)
    setMenuOpen(false)
    onSelect(path)
  }

  const current = listing?.folder
  const canManage = Boolean(listing?.can_manage)

  return (
    <div className="folders-panel card">
      <div className="explorer">
        <div className="explorer-tree">
          <FolderTree
            workspaceId={workspaceId}
            workspaceName={workspaceName}
            variant="navigate"
            selected={[selected]}
            onSelect={(entry) => select(entry.path)}
            refreshKey={refreshKey}
            rowActions={
              canManage
                ? (entry) =>
                    entry.path ? (
                      <button
                        type="button"
                        className="tree-more"
                        aria-label={`Actions sur ${entry.name}`}
                        onClick={() => {
                          select(entry.path)
                          setMenuOpen(true)
                        }}
                      >
                        ⋯
                      </button>
                    ) : null
                : undefined
            }
          />
        </div>

        <div className="explorer-current" aria-live="polite">
          {error && <div className="error-banner">{error}</div>}
          {current && listing && (
            <>
              <nav className="breadcrumb" aria-label="Chemin du dossier">
                {listing.breadcrumb.map((crumb, i) => (
                  <span key={crumb.path || '__root'}>
                    {i > 0 && <span className="breadcrumb-sep"> / </span>}
                    {i === listing.breadcrumb.length - 1 ? (
                      <strong>{i === 0 ? `📁 ${crumb.name}` : crumb.name}</strong>
                    ) : (
                      <button type="button" className="breadcrumb-link" onClick={() => select(crumb.path)}>
                        {i === 0 ? `📁 ${crumb.name}` : crumb.name}
                      </button>
                    )}
                  </span>
                ))}
              </nav>
              <div className="explorer-current-head">
                <div>
                  <div className="explorer-title">
                    {current.path ? current.name : workspaceName}
                    {(current.restricted || current.restricted_by) && <span title="Accès restreint"> 🔒</span>}
                  </div>
                  <div className="workspace-hint">{countsLabel(current)}</div>
                  {current.path && <div className="explorer-path mono">{displayPath(workspaceName, current.path)}</div>}
                </div>
                <div className="explorer-actions">
                  {current.path && (
                    <button type="button" className="btn" onClick={() => select(parentPath(current.path))}>
                      ↑ Dossier parent
                    </button>
                  )}
                  {canManage && current.path && (
                    <span className="explorer-menu">
                      <button
                        type="button"
                        className="btn"
                        aria-haspopup="menu"
                        aria-expanded={menuOpen}
                        onClick={() => setMenuOpen((o) => !o)}
                      >
                        Actions ▾
                      </button>
                      {menuOpen && (
                        <div className="explorer-menu-popover" role="menu">
                          <button
                            type="button"
                            role="menuitem"
                            onClick={() => {
                              setSharing(true)
                              setMenuOpen(false)
                            }}
                          >
                            Partager / restreindre l'accès…
                          </button>
                        </div>
                      )}
                    </span>
                  )}
                </div>
              </div>
              <label className="workspace-sharing-option">
                <input
                  type="checkbox"
                  checked={includeSubfolders}
                  onChange={(e) => onIncludeSubfoldersChange(e.target.checked)}
                />
                Afficher aussi les documents des sous-dossiers ({current.total_documents} au total, dont{' '}
                {current.direct_documents} directement dans ce dossier)
              </label>
              {sharing && current.path && (
                <FolderShareEditor
                  workspaceId={workspaceId}
                  folder={current}
                  onChanged={() => {
                    setRefreshKey((k) => k + 1)
                    onChanged()
                  }}
                  onClose={() => setSharing(false)}
                />
              )}
            </>
          )}
        </div>
      </div>
    </div>
  )
}

type ShareableFolder = Pick<WorkspaceFolder, 'path' | 'restricted' | 'restricted_by' | 'shares'> | FolderTreeEntry

function FolderShareEditor({
  workspaceId,
  folder,
  onChanged,
  onClose,
}: {
  workspaceId: string
  folder: ShareableFolder
  onChanged: (next: WorkspaceFolders) => void
  onClose: () => void
}) {
  const [users, setUsers] = useState<{ username: string; label: string }[] | null>(null)
  const [username, setUsername] = useState('')
  const [effect, setEffect] = useState<'allow' | 'deny'>('allow')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    // The Director picks from the directory; a workspace owner types an identifiant.
    adminListUsers()
      .then((list) =>
        setUsers(list.filter((u) => u.is_active).map((u) => ({ username: u.username, label: `${u.full_name} (${u.role})` }))),
      )
      .catch(() => setUsers([]))
  }, [])

  async function act(action: () => Promise<WorkspaceFolders>) {
    setBusy(true)
    setError(null)
    try {
      onChanged(await action())
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Action impossible.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="folder-share-editor">
      <div className="folder-share-editor-head">
        <strong>Partage de « {folder.path.replace(/\/$/, '')} »</strong>
        <button type="button" className="btn" onClick={onClose}>
          Fermer
        </button>
      </div>
      <label className="workspace-sharing-option">
        <input
          type="checkbox"
          checked={folder.restricted || Boolean(folder.restricted_by)}
          disabled={busy || Boolean(folder.restricted_by)}
          onChange={(e) => act(() => setFolderRestriction(workspaceId, folder.path, e.target.checked))}
        />
        🔒 Accès restreint : seules les personnes ci-dessous (et vous) voient ce dossier
      </label>
      {folder.restricted_by && (
        <p className="workspace-hint">Restreint parce que le dossier parent « {folder.restricted_by} » l'est.</p>
      )}
      {!folder.restricted && !folder.restricted_by && (
        <p className="workspace-hint">
          Non restreint : chacun voit les fichiers que son rôle autorise. Vous pouvez quand même ouvrir ce dossier à une
          personne précise, ou le lui interdire.
        </p>
      )}
      {folder.shares.length > 0 && (
        <ul className="access-rules-list">
          {folder.shares.map((s) => (
            <li key={s.id}>
              <span className={s.effect === 'allow' ? 'rule-allow' : 'rule-deny'}>
                {s.effect === 'allow' ? '✓ Partagé avec' : '⛔ Interdit à'}
              </span>{' '}
              <strong>{s.full_name}</strong>
              <button type="button" disabled={busy} onClick={() => act(() => unshareFolder(workspaceId, s.id))}>
                Retirer
              </button>
            </li>
          ))}
        </ul>
      )}
      <form
        className="access-rules-form"
        onSubmit={(e) => {
          e.preventDefault()
          const name = username.trim()
          if (name) act(() => shareFolder(workspaceId, folder.path, name, effect).then((r) => (setUsername(''), r)))
        }}
      >
        <select className="input" value={effect} onChange={(e) => setEffect(e.target.value as 'allow' | 'deny')}>
          <option value="allow">Partager avec</option>
          <option value="deny">Interdire à</option>
        </select>
        {users && users.length > 0 ? (
          <select className="input" value={username} onChange={(e) => setUsername(e.target.value)}>
            <option value="">— utilisateur —</option>
            {users.map((u) => (
              <option key={u.username} value={u.username}>
                {u.label}
              </option>
            ))}
          </select>
        ) : (
          <input className="input" placeholder="Identifiant (ex. rh)" value={username} onChange={(e) => setUsername(e.target.value)} />
        )}
        <button type="submit" className="btn" disabled={busy || !username.trim()}>
          Appliquer
        </button>
      </form>
      <p className="workspace-hint">
        « Partager avec » ouvre tout le dossier et ses sous-dossiers à cette personne, quel que soit son rôle.
      </p>
      {error && <div className="error-banner">{error}</div>}
    </div>
  )
}
