import { useEffect, useState } from 'react'
import { addWorkspaceMember, listWorkspaceMembers, removeWorkspaceMember, updateWorkspace } from '../api/endpoints'
import type { Workspace, WorkspaceMember } from '../types'

/** Owner-only: who may use this workspace - only the members, or the whole
 * organization. Documents stay filtered by each reader's role either way. */
export function WorkspaceSharing({ workspace, onChanged }: { workspace: Workspace; onChanged: () => void }) {
  const [members, setMembers] = useState<WorkspaceMember[]>([])
  const [username, setUsername] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [reload, setReload] = useState(0)

  useEffect(() => {
    let cancelled = false
    listWorkspaceMembers(workspace.id)
      .then((m) => {
        if (!cancelled) setMembers(m)
      })
      .catch(() => {})
    return () => {
      cancelled = true
    }
  }, [workspace.id, reload])

  async function act(action: () => Promise<unknown>) {
    setBusy(true)
    setError(null)
    try {
      await action()
      setReload((n) => n + 1)
      onChanged()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Action impossible.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="workspace-sharing">
      <div className="workspace-sharing-title">Partage de l'espace</div>
      <label className="workspace-sharing-option">
        <input
          type="radio"
          name={`visibility-${workspace.id}`}
          checked={workspace.visibility === 'private'}
          disabled={busy}
          onChange={() => act(() => updateWorkspace(workspace.id, { visibility: 'private' }))}
        />
        🔒 Privé — vous et les membres ci-dessous
      </label>
      <label className="workspace-sharing-option">
        <input
          type="radio"
          name={`visibility-${workspace.id}`}
          checked={workspace.visibility === 'organization'}
          disabled={busy}
          onChange={() => act(() => updateWorkspace(workspace.id, { visibility: 'organization' }))}
        />
        🏢 Toute l'entreprise
      </label>
      <p className="workspace-hint">
        Dans les deux cas, chacun ne voit que les documents autorisés pour son département et son niveau de
        confidentialité.
      </p>

      {workspace.visibility === 'private' && (
        <>
          <div className="workspace-members">
            {members.length === 0 && <span className="workspace-hint">Aucun membre pour l'instant.</span>}
            {members.map((m) => (
              <span key={m.user_id} className="access-person ok">
                {m.full_name} <em>{m.username}</em>
                <button type="button" disabled={busy} onClick={() => act(() => removeWorkspaceMember(workspace.id, m.user_id))}>
                  Retirer
                </button>
              </span>
            ))}
          </div>
          <form
            className="workspace-member-add"
            onSubmit={(e) => {
              e.preventDefault()
              const name = username.trim()
              if (name) act(() => addWorkspaceMember(workspace.id, name).then(() => setUsername('')))
            }}
          >
            <input className="input" placeholder="Identifiant (ex. rh)" value={username} onChange={(e) => setUsername(e.target.value)} />
            <button type="submit" className="btn" disabled={busy || !username.trim()}>
              + Ajouter un membre
            </button>
          </form>
        </>
      )}
      {error && <div className="error-banner">{error}</div>}
    </div>
  )
}
