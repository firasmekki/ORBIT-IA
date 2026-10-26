import { useEffect, useState } from 'react'
import { browseSource, createWorkspace, listSourceRoots, resolveSourcePath } from '../api/endpoints'
import type { SourceListing, SourceRoot, Workspace } from '../types'

const DEPARTMENTS = ['GENERAL', 'HR', 'FINANCE', 'TECH', 'EXEC']
const LEVELS = ['PUBLIC', 'INTERNAL', 'CONFIDENTIAL', 'SECRET']

/** Connect a folder of the server PC as a workspace. The backend reads it
 * itself (read-only), continuously - no browser tab, no size limit beyond
 * 50 MB per file. Browsing never reads a file: one directory listing at a
 * time. Director only (the backend refuses everyone else). */
export function ServerFolderPicker({
  onCreated,
  onCancel,
}: {
  onCreated: (workspace: Workspace) => void
  onCancel: () => void
}) {
  const [roots, setRoots] = useState<SourceRoot[] | null>(null)
  const [listing, setListing] = useState<SourceListing | null>(null)
  const [pasted, setPasted] = useState('')
  const [name, setName] = useState('')
  const [department, setDepartment] = useState('GENERAL')
  const [level, setLevel] = useState('INTERNAL')
  const [visibility, setVisibility] = useState<'private' | 'organization'>('organization')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    listSourceRoots()
      .then((list) => {
        setRoots(list)
        const first = list.find((r) => r.available)
        if (first) void open(first.id, '')
      })
      .catch((err) => setError(err instanceof Error ? err.message : 'Emplacements indisponibles.'))
    // open() only depends on setters
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  async function open(rootId: string, path: string) {
    setError(null)
    try {
      const next = await browseSource(rootId, path)
      setListing(next)
      setName(next.path ? next.path.split('/').pop() ?? '' : '')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Dossier inaccessible.')
    }
  }

  async function goToPasted() {
    if (!pasted.trim()) return
    setError(null)
    try {
      const target = await resolveSourcePath(pasted)
      await open(target.root_id, target.path)
      setPasted('')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Chemin invalide.')
    }
  }

  async function create() {
    if (!listing) return
    setBusy(true)
    setError(null)
    try {
      const workspace = await createWorkspace({
        name: name.trim(),
        default_department: department,
        default_confidentiality: level,
        visibility,
        source_root: listing.root_id,
        source_path: listing.path,
      })
      onCreated(workspace)
    } catch (err) {
      setError(err instanceof Error ? err.message : "Impossible de connecter ce dossier.")
    } finally {
      setBusy(false)
    }
  }

  const crumbs = listing?.path ? listing.path.split('/') : []

  return (
    <div className="workspace-create server-picker">
      <strong>🖥️ Connecter un dossier de ce PC</strong>
      <p className="workspace-hint">
        ORBIT lit le dossier directement (lecture seule), en continu, même page fermée. Tous les sous-dossiers sont
        inclus ; dans le chat, vous pourrez ensuite limiter la recherche à un client (ex. BIC) sans rien réindexer.
      </p>
      {error && <div className="error-banner">{error}</div>}

      {roots && roots.length === 0 && (
        <div className="error-banner">
          Aucun emplacement autorisé. Ajoutez ORBIT_SOURCE_ROOT_1=C:\Users\…\Desktop dans .env puis relancez
          start-orbit-ai.ps1.
        </div>
      )}

      {roots && roots.length > 1 && (
        <div className="server-picker-roots">
          {roots.map((root) => (
            <button
              key={root.id}
              type="button"
              className={`btn${listing?.root_id === root.id ? ' btn-primary' : ''}`}
              disabled={!root.available}
              title={root.available ? root.host_path : `${root.host_path} n'est pas monté`}
              onClick={() => open(root.id, '')}
            >
              {root.label}
            </button>
          ))}
        </div>
      )}

      <div className="server-picker-paste">
        <input
          className="input"
          placeholder="…ou collez un chemin : C:\Users\ORBIT\Desktop\projets"
          value={pasted}
          onChange={(e) => setPasted(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') void goToPasted()
          }}
        />
        <button type="button" className="btn" onClick={goToPasted} disabled={!pasted.trim()}>
          Aller
        </button>
      </div>

      {listing && (
        <>
          <div className="server-picker-crumbs">
            <button type="button" className="link-button" onClick={() => open(listing.root_id, '')}>
              {roots?.find((r) => r.id === listing.root_id)?.label ?? 'Racine'}
            </button>
            {crumbs.map((part, i) => (
              <span key={i}>
                {' › '}
                <button type="button" className="link-button" onClick={() => open(listing.root_id, crumbs.slice(0, i + 1).join('/'))}>
                  {part}
                </button>
              </span>
            ))}
          </div>
          <div className="server-picker-list">
            {listing.parent !== null && (
              <button type="button" className="server-picker-folder" onClick={() => open(listing.root_id, listing.parent ?? '')}>
                ⬆ Dossier parent
              </button>
            )}
            {listing.folders.length === 0 && <div className="workspace-hint">Aucun sous-dossier.</div>}
            {listing.folders.map((folder) => (
              <button
                key={folder}
                type="button"
                className="server-picker-folder"
                onClick={() => open(listing.root_id, listing.path ? `${listing.path}/${folder}` : folder)}
              >
                📁 {folder}
              </button>
            ))}
          </div>
          <div className="workspace-hint">
            Dossier sélectionné : <strong>{listing.host_path}</strong> · {listing.folders.length} sous-dossier(s),{' '}
            {listing.file_count} fichier(s) à ce niveau
          </div>

          <input className="input" placeholder="Nom de l'espace" value={name} maxLength={100} onChange={(e) => setName(e.target.value)} />
          <div className="workspace-create-row">
            <label>
              Département par défaut
              <select className="input" value={department} onChange={(e) => setDepartment(e.target.value)}>
                {DEPARTMENTS.map((d) => (
                  <option key={d}>{d}</option>
                ))}
              </select>
            </label>
            <label>
              Confidentialité par défaut
              <select className="input" value={level} onChange={(e) => setLevel(e.target.value)}>
                {LEVELS.map((l) => (
                  <option key={l}>{l}</option>
                ))}
              </select>
            </label>
            <label>
              Partage
              <select className="input" value={visibility} onChange={(e) => setVisibility(e.target.value as 'private' | 'organization')}>
                <option value="organization">Toute l'entreprise</option>
                <option value="private">Privé</option>
              </select>
            </label>
          </div>
        </>
      )}

      <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
        <button type="button" className="btn" onClick={onCancel} disabled={busy}>
          Annuler
        </button>
        <button type="button" className="btn btn-primary" disabled={!listing || !name.trim() || busy} onClick={create}>
          {busy ? 'Connexion…' : 'Connecter ce dossier'}
        </button>
      </div>
    </div>
  )
}
