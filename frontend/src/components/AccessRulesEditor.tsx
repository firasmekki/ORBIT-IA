import { useEffect, useState } from 'react'
import { adminListUsers, deleteAccessRule, listAccessRules, setAccessRule } from '../api/endpoints'
import type { AccessRules, UserOut } from '../types'

/** Director only: explicit exceptions for one person on this document or on
 * one of its folders. "Interdire" always wins - over the role, over a
 * personal document, over an "Autoriser". Enforced server-side in the same
 * SQL filter as every read. */
export function AccessRulesEditor({ documentId, onChanged }: { documentId: string; onChanged: () => void }) {
  const [rules, setRules] = useState<AccessRules | null>(null)
  const [users, setUsers] = useState<UserOut[]>([])
  const [username, setUsername] = useState('')
  const [effect, setEffect] = useState<'allow' | 'deny'>('allow')
  const [folder, setFolder] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [reload, setReload] = useState(0)

  useEffect(() => {
    let cancelled = false
    listAccessRules(documentId)
      .then((r) => {
        if (!cancelled) setRules(r)
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Règles indisponibles.')
      })
    return () => {
      cancelled = true
    }
  }, [documentId, reload])

  useEffect(() => {
    adminListUsers()
      .then((list) => setUsers(list.filter((u) => u.is_active)))
      .catch(() => {})
  }, [])

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

  if (!rules) return error ? <div className="error-banner">{error}</div> : null

  return (
    <div className="access-rules">
      <div className="workspace-sharing-title">🔑 Accès particuliers (directeur)</div>
      {rules.rules.length === 0 ? (
        <p className="workspace-hint">Aucune exception : seuls les rôles et les documents personnels s'appliquent.</p>
      ) : (
        <ul className="access-rules-list">
          {rules.rules.map((r) => (
            <li key={r.id}>
              <span className={r.effect === 'allow' ? 'rule-allow' : 'rule-deny'}>
                {r.effect === 'allow' ? '✓ Autorisé' : '⛔ Interdit'}
              </span>{' '}
              <strong>{r.full_name}</strong> — {r.folder_path ? <>tout le dossier « {r.folder_path} »</> : 'ce document'}
              <button type="button" disabled={busy} onClick={() => act(() => deleteAccessRule(documentId, r.id))}>
                Supprimer
              </button>
            </li>
          ))}
        </ul>
      )}
      <form
        className="access-rules-form"
        onSubmit={(e) => {
          e.preventDefault()
          if (!username) return
          act(() => setAccessRule(documentId, { username, effect, folder_path: folder || null }))
        }}
      >
        <select className="input" value={effect} onChange={(e) => setEffect(e.target.value as 'allow' | 'deny')}>
          <option value="allow">Autoriser</option>
          <option value="deny">Interdire</option>
        </select>
        <select className="input" value={username} onChange={(e) => setUsername(e.target.value)}>
          <option value="">— utilisateur —</option>
          {users.map((u) => (
            <option key={u.id} value={u.username}>
              {u.full_name} ({u.role})
            </option>
          ))}
        </select>
        <select className="input" value={folder} onChange={(e) => setFolder(e.target.value)}>
          <option value="">ce document</option>
          {rules.folders.map((f) => (
            <option key={f} value={f}>
              tout le dossier « {f} »
            </option>
          ))}
        </select>
        <button type="submit" className="btn" disabled={busy || !username}>
          Appliquer
        </button>
      </form>
      {error && <div className="error-banner">{error}</div>}
    </div>
  )
}
