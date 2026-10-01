import { useState } from 'react'
import { isWorkspaceSupported, pickWorkspaceDirectory } from '../lib/localFs'

export function WorkspacePicker({
  currentWorkspaceName,
  onClose,
  onPicked,
  onDisconnect,
}: {
  currentWorkspaceName: string | null
  onClose: () => void
  onPicked: (handle: FileSystemDirectoryHandle) => void
  onDisconnect: () => void
}) {
  const [picking, setPicking] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const supported = isWorkspaceSupported()

  async function handlePick() {
    setError(null)
    setPicking(true)
    try {
      const handle = await pickWorkspaceDirectory()
      onPicked(handle)
    } catch (err) {
      // The user cancelling the native picker throws an AbortError - not
      // a real error, just close silently like clicking Annuler would.
      if (err instanceof DOMException && err.name === 'AbortError') {
        setPicking(false)
        return
      }
      setError(err instanceof Error ? err.message : "Impossible d'accéder à ce dossier.")
      setPicking(false)
    }
  }

  return (
    <div className="modal-overlay" onClick={picking ? undefined : onClose}>
      <div className="modal-card" style={{ maxWidth: 460 }} onClick={(e) => e.stopPropagation()}>
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

          <p style={{ fontSize: 13.5, color: 'var(--text)' }}>
            Choisissez un dossier de votre ordinateur pour qu'Orbitia puisse y lire des fichiers à votre demande.
            Le dossier reste sur votre poste — seuls son nom et la liste de ses fichiers (jamais leur contenu) sont
            transmis, et uniquement lorsque vous le demandez.
          </p>

          {currentWorkspaceName && (
            <div className="workspace-current">
              <span className="workspace-dot active" />
              Espace actuel : <strong>{currentWorkspaceName}</strong>
            </div>
          )}

          {!supported && (
            <div className="error-banner" style={{ marginTop: 12 }}>
              Votre navigateur ne permet pas de sélectionner un dossier local. Utilisez Chrome ou Edge.
            </div>
          )}

          <div style={{ display: 'flex', gap: 10, justifyContent: 'flex-end', marginTop: 20, flexWrap: 'wrap' }}>
            {currentWorkspaceName && (
              <button
                type="button"
                className="btn"
                style={{ color: 'var(--deny)' }}
                onClick={onDisconnect}
                disabled={picking}
              >
                Déconnecter
              </button>
            )}
            <button type="button" className="btn" onClick={onClose} disabled={picking}>
              Annuler
            </button>
            <button type="button" className="btn btn-primary" onClick={handlePick} disabled={picking || !supported}>
              {picking ? 'Sélection…' : '📂 Choisir un dossier'}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
