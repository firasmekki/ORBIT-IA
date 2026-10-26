import { useEffect, useState } from 'react'
import { addWorkspaceMember, getDocumentAccess, updateWorkspace } from '../api/endpoints'
import type { DocumentAccess, DocumentAccessPerson } from '../types'
import { AccessRulesEditor } from './AccessRulesEditor'

const BASIS_LABEL: Record<string, string> = {
  personal: 'son document personnel',
  granted: 'accès accordé par le directeur',
}

function basisLabel(p: DocumentAccessPerson) {
  return p.basis ? BASIS_LABEL[p.basis] : undefined
}

/** "Qui peut voir ce document ?" - computed by the server with the same rules
 * as every read: access to the document's workspace AND a role allowed for
 * its department/confidentiality. Explains when the workspace, not the
 * classification, is what hides it - and lets the owner share. */
export function DocumentAccessPanel({
  documentId,
  refreshKey,
  onWorkspaceChanged,
}: {
  documentId: string
  /** Changes after a reclassification, to recompute. */
  refreshKey: string
  onWorkspaceChanged?: () => void
}) {
  const [access, setAccess] = useState<DocumentAccess | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [reload, setReload] = useState(0)

  useEffect(() => {
    let cancelled = false
    getDocumentAccess(documentId)
      .then((a) => {
        if (!cancelled) {
          setAccess(a)
          setError(null)
        }
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Impossible de calculer les accès.')
      })
    return () => {
      cancelled = true
    }
  }, [documentId, refreshKey, reload])

  async function act(action: () => Promise<unknown>) {
    setBusy(true)
    setError(null)
    try {
      await action()
      setReload((n) => n + 1)
      onWorkspaceChanged?.()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Action impossible.')
    } finally {
      setBusy(false)
    }
  }

  if (!access) return error ? <div className="error-banner">{error}</div> : null
  const ws = access.workspace
  const blocked = access.blocked_by_workspace

  return (
    <div className="access-panel">
      <div className="access-panel-title">
        👥 Qui peut voir ce document ?
        <span className="access-panel-ws">
          {ws.is_default ? '📁 Général (espace commun)' : `📁 ${ws.name} · ${ws.visibility === 'organization' ? '🏢 partagé avec toute l’entreprise' : '🔒 espace privé'}`}
        </span>
      </div>

      <div className="access-people">
        {access.visible_to.map((p) => (
          <span key={p.username} className="access-person ok" title={p.username}>
            ✓ {p.full_name} <em>{p.role}</em>
            {basisLabel(p) && <small className="access-basis">· {basisLabel(p)}</small>}
          </span>
        ))}
      </div>

      {blocked.length > 0 && (
        <div className="access-warning">
          <p>
            ⚠ {blocked.length === 1 ? 'Cet utilisateur a' : `Ces ${blocked.length} utilisateurs ont`} le rôle requis pour
            ce département et ce niveau, mais <strong>pas accès à l'espace privé « {ws.name} »</strong> : la
            classification seule ne suffit pas.
          </p>
          <div className="access-people">
            {blocked.map((p) => (
              <span key={p.username} className="access-person blocked">
                {p.full_name} <em>{p.role}</em>
                {ws.is_owner && (
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => act(() => addWorkspaceMember(ws.id, p.username))}
                    title="Ajouter comme membre de l'espace"
                  >
                    + Ajouter
                  </button>
                )}
              </span>
            ))}
          </div>
          {ws.is_owner ? (
            <button
              type="button"
              className="btn btn-primary"
              style={{ marginTop: 8, padding: '5px 12px', fontSize: 12 }}
              disabled={busy}
              onClick={() => act(() => updateWorkspace(ws.id, { visibility: 'organization' }))}
            >
              🏢 Partager l'espace « {ws.name} » avec toute l'entreprise
            </button>
          ) : (
            <p className="workspace-hint">Seul le propriétaire de l'espace peut le partager.</p>
          )}
          <p className="workspace-hint" style={{ marginTop: 6 }}>
            Partager l'espace ne contourne pas les droits : chaque document reste filtré par le département et le niveau
            de confidentialité de chacun.
          </p>
        </div>
      )}

      {access.denied_by_rule.length > 0 && (
        <div className="access-people" style={{ marginTop: 8 }}>
          {access.denied_by_rule.map((p) => (
            <span key={p.username} className="access-person denied">
              ⛔ {p.full_name} <em>{p.role}</em> <small className="access-basis">· interdit par le directeur</small>
            </span>
          ))}
        </div>
      )}

      {access.blocked_by_role_count > 0 && (
        <p className="workspace-hint">
          {access.blocked_by_role_count} autre{access.blocked_by_role_count > 1 ? 's' : ''} utilisateur
          {access.blocked_by_role_count > 1 ? 's' : ''} n'y {access.blocked_by_role_count > 1 ? 'ont' : 'a'} pas accès à
          cause de son rôle (département, niveau de confidentialité) ou parce que c'est le document personnel
          d'un collègue.
        </p>
      )}
      {access.can_manage_rules && (
        <AccessRulesEditor documentId={documentId} onChanged={() => setReload((n) => n + 1)} />
      )}
      {error && <div className="error-banner">{error}</div>}
    </div>
  )
}
