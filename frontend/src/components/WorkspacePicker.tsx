import { useState } from 'react'
import { createWorkspace } from '../api/endpoints'
import { useAuth } from '../context/AuthContext'
import { assertBrowserImportAllowed } from '../lib/browserImport'
import { isWorkspaceSupported, pickWorkspaceDirectory } from '../lib/localFs'
import type { Workspace } from '../types'
import { ServerFolderPicker } from './ServerFolderPicker'
import { SourceStatusPanel } from './SourceStatusPanel'
import { WorkspaceSharing } from './WorkspaceSharing'

const DEPARTMENTS = ['GENERAL', 'HR', 'FINANCE', 'TECH', 'EXEC']
const LEVELS = ['PUBLIC', 'INTERNAL', 'CONFIDENTIAL', 'SECRET']

/** Choose the chat's workspace, create one, and connect / disconnect the
 * local folder synced into it. The folder never leaves the browser: the
 * browser reads the files and sends them for indexing. */
export function WorkspacePicker({
  workspaces,
  currentId,
  connectedFolder,
  onSelect,
  onCreated,
  onFolderPicked,
  onDisconnectFolder,
  onClose,
  onWorkspaceChanged,
  onDeleteWorkspace,
}: {
  workspaces: Workspace[]
  currentId: string | null
  connectedFolder: string | null
  onSelect: (id: string) => void
  onCreated: (workspace: Workspace) => void
  onFolderPicked: (handle: FileSystemDirectoryHandle) => void
  onDisconnectFolder: () => void
  onClose: () => void
  onWorkspaceChanged?: () => void
  onDeleteWorkspace?: (id: string) => Promise<void>
}) {
  const [picking, setPicking] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)
  const [connectingServer, setConnectingServer] = useState(false)
  const [name, setName] = useState('')
  const [department, setDepartment] = useState('GENERAL')
  const [level, setLevel] = useState('INTERNAL')
  const { user } = useAuth()
  const supported = isWorkspaceSupported()
  const current = workspaces.find((w) => w.id === currentId) ?? null

  async function handlePick() {
    setError(null)
    setPicking(true)
    try {
      const handle = await pickWorkspaceDirectory()
      // Measured before anything is sent: a large folder is refused here.
      await assertBrowserImportAllowed(handle)
      onFolderPicked(handle)
    } catch (err) {
      // Cancelling the native picker throws an AbortError - not an error.
      if (!(err instanceof DOMException && err.name === 'AbortError')) {
        setError(err instanceof Error ? err.message : "Impossible d'accéder à ce dossier.")
      }
    } finally {
      setPicking(false)
    }
  }

  async function handleCreate() {
    setError(null)
    try {
      const workspace = await createWorkspace({
        name: name.trim(),
        default_department: department,
        default_confidentiality: level,
      })
      setCreating(false)
      setName('')
      onCreated(workspace)
    } catch (err) {
      setError(err instanceof Error ? err.message : "Impossible de créer l'espace de travail.")
    }
  }

  return (
    <div className="modal-overlay" onClick={picking ? undefined : onClose}>
      <div className="modal-card" style={{ maxWidth: 640 }} onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h3>Sélectionner un espace de travail</h3>
          {!picking && (
            <button className="modal-close" onClick={onClose}>
              ✕
            </button>
          )}
        </div>
        <div className="modal-body" style={{ whiteSpace: 'normal' }}>
          {error && <div className="error-banner">{error}</div>}

          <div className="workspace-list" role="radiogroup" aria-label="Espaces de travail">
            {workspaces.map((w) => (
              <label key={w.id} className={`workspace-option${w.id === currentId ? ' selected' : ''}`}>
                <input type="radio" name="workspace" checked={w.id === currentId} onChange={() => onSelect(w.id)} />
                <span className="workspace-option-name">
                  {w.source ? '🖥️' : '📁'} {w.name}
                </span>
                <span className="workspace-option-meta">
                  {w.document_count} document(s)
                  {w.is_default
                    ? ' · commun'
                    : w.is_owner
                      ? w.visibility === 'organization'
                        ? ' · 🏢 partagé'
                        : ' · 🔒 privé'
                      : ` · partagé par ${w.owner_name ?? 'un collègue'}`}
                </span>
              </label>
            ))}
          </div>

          {connectingServer ? (
            <ServerFolderPicker
              onCancel={() => setConnectingServer(false)}
              onCreated={(workspace) => {
                setConnectingServer(false)
                onCreated(workspace)
              }}
            />
          ) : !creating ? (
            <div style={{ display: 'flex', gap: 8, marginTop: 10, flexWrap: 'wrap' }}>
              {user?.role === 'DIRECTOR' && (
                <button type="button" className="btn btn-primary" onClick={() => setConnectingServer(true)}>
                  🖥️ Connecter un dossier de ce PC
                </button>
              )}
              <button type="button" className="btn" onClick={() => setCreating(true)}>
                + Nouvel espace de travail
              </button>
            </div>
          ) : (
            <div className="workspace-create">
              <input
                className="input"
                placeholder="Nom (ex. orbitaitest)"
                value={name}
                maxLength={100}
                onChange={(e) => setName(e.target.value)}
              />
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
              </div>
              <p className="workspace-hint">
                Les fichiers du dossier connecté recevront cette classification (modifiable ensuite document par
                document). Elle ne peut pas dépasser vos propres droits.
              </p>
              <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
                <button type="button" className="btn" onClick={() => setCreating(false)}>
                  Annuler
                </button>
                <button type="button" className="btn btn-primary" disabled={!name.trim()} onClick={handleCreate}>
                  Créer
                </button>
              </div>
            </div>
          )}

          {current && (
            <div className="workspace-folder-box">
              <div className="workspace-current">
                <span className={`workspace-dot${connectedFolder ? ' active' : ''}`} />
                Espace actuel : <strong>{current.name}</strong>
                {connectedFolder && <> — dossier 📂 {connectedFolder}</>}
              </div>
              {current.source ? (
                current.can_sync ? (
                  <SourceStatusPanel workspace={current} onChanged={() => onWorkspaceChanged?.()} />
                ) : (
                  <p className="workspace-hint">🖥️ Dossier du serveur, synchronisé automatiquement par ORBIT.</p>
                )
              ) : !current.can_sync ? (
                <p className="workspace-hint">
                  {current.is_default ? 'Espace commun : il ne se synchronise pas avec un dossier.' : 'Seul son propriétaire peut y connecter un dossier.'}{' '}
                  En choisissant un dossier, Orbitia utilise votre propre espace portant le nom du dossier (créé
                  automatiquement s'il n'existe pas) et le synchronise.
                </p>
              ) : (
                <p className="workspace-hint">
                  Les fichiers sont copiés dans ORBIT. La synchronisation n'a lieu que pendant que cette page est ouverte.
                </p>
              )}

              {current.is_owner && !current.is_default && (
                <WorkspaceSharing workspace={current} onChanged={() => onWorkspaceChanged?.()} />
              )}

              {current.is_owner && !current.is_default && onDeleteWorkspace && (
                <div className="workspace-danger">
                  <button
                    type="button"
                    className="btn"
                    style={{ color: 'var(--deny)' }}
                    disabled={picking}
                    onClick={async () => {
                      const ok = window.confirm(
                        `Retirer l'espace « ${current.name} » d'Orbitia ?\n\n` +
                          `• Ses ${current.document_count} document(s) disparaissent d'Orbitia pour tous les utilisateurs (recherche, liste, assistant IA).\n` +
                          (current.source
                            ? `• Le dossier ${current.source.host_path} n'est PAS touché (ORBIT le lit en lecture seule).\n`
                            : `• Votre dossier${current.folder_name ? ` « ${current.folder_name} »` : ''} sur votre ordinateur n'est PAS touché : aucun fichier n'est supprimé.\n`) +
                          `• L'historique reste dans le journal d'audit. Vous pourrez reconnecter ce dossier plus tard.`,
                      )
                      if (!ok) return
                      try {
                        await onDeleteWorkspace(current.id)
                      } catch (err) {
                        window.alert(err instanceof Error ? err.message : 'Suppression impossible.')
                      }
                    }}
                  >
                    🗑 Retirer cet espace d'Orbitia
                  </button>
                  <span className="workspace-hint">Le dossier sur votre ordinateur n'est pas supprimé.</span>
                </div>
              )}

              {!supported && !current.source && (
                <div className="error-banner" style={{ marginTop: 12 }}>
                  Votre navigateur ne permet pas de sélectionner un dossier local. Utilisez Chrome ou Edge.
                </div>
              )}

              <div style={{ display: 'flex', gap: 10, justifyContent: 'flex-end', marginTop: 14, flexWrap: 'wrap' }}>
                {connectedFolder && (
                  <button type="button" className="btn" style={{ color: 'var(--deny)' }} onClick={onDisconnectFolder} disabled={picking}>
                    Déconnecter le dossier
                  </button>
                )}
                <button type="button" className="btn" onClick={onClose} disabled={picking}>
                  Fermer
                </button>
                {!current.source && (
                  <button type="button" className="btn" onClick={handlePick} disabled={picking || !supported}>
                    {picking ? 'Sélection et vérification…' : '📂 Importer un petit dossier depuis le navigateur'}
                  </button>
                )}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
