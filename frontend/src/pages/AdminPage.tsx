import { useEffect, useState, type FormEvent } from 'react'
import {
  adminCreateUser,
  adminDeleteUser,
  adminListUsers,
  adminStats,
  adminUpdateUser,
  adminUpdateUserAccess,
} from '../api/endpoints'
import { useAuth } from '../context/AuthContext'
import { RoleBadge } from '../components/Badges'
import { toolLabel } from '../lib/tools'
import type { AdminStats, Role, UserOut } from '../types'

const ROLES: Role[] = ['DIRECTOR', 'HR', 'ACCOUNTANT', 'DEVELOPER', 'EMPLOYEE']
const DEPARTMENTS = ['HR', 'FINANCE', 'TECH', 'GENERAL', 'EXEC']
const LEVELS = ['PUBLIC', 'INTERNAL', 'CONFIDENTIAL', 'SECRET']
const TOOLS = ['search_documents', 'get_document', 'search_database', 'get_company_information']

export function AdminPage() {
  const { user: currentUser } = useAuth()
  const [stats, setStats] = useState<AdminStats | null>(null)
  const [users, setUsers] = useState<UserOut[]>([])
  const [loading, setLoading] = useState(true)
  const [showCreate, setShowCreate] = useState(false)
  const [editingUser, setEditingUser] = useState<UserOut | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)

  useEffect(() => {
    loadAll()
  }, [])

  function loadAll() {
    setLoading(true)
    return Promise.all([adminStats(), adminListUsers()])
      .then(([s, u]) => {
        setStats(s)
        setUsers(u)
      })
      .finally(() => setLoading(false))
  }

  async function toggleActive(u: UserOut) {
    setActionError(null)
    try {
      await adminUpdateUser(u.id, { is_active: !u.is_active })
      loadAll()
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Impossible de modifier le statut de l'utilisateur.")
    }
  }

  async function handleDelete(u: UserOut) {
    if (!confirm(`Supprimer définitivement le compte de ${u.full_name} (${u.username}) ?`)) return
    setActionError(null)
    try {
      await adminDeleteUser(u.id)
      loadAll()
    } catch (err) {
      setActionError(err instanceof Error ? err.message : 'Impossible de supprimer cet utilisateur.')
    }
  }

  if (loading) {
    return (
      <div className="centered-spinner">
        <div className="spinner" />
      </div>
    )
  }

  return (
    <div className="page">
      <div className="page-header">
        <h1>Utilisateurs</h1>
        <p>Page réservée au rôle Directeur — création, modification, rôles et accès personnalisés des comptes employés.</p>
      </div>

      {stats && (
        <div className="stat-grid">
          <div className="card stat-tile">
            <div className="stat-value">{stats.total_users}</div>
            <div className="stat-label">Utilisateurs</div>
          </div>
          <div className="card stat-tile">
            <div className="stat-value">{stats.total_documents}</div>
            <div className="stat-label">Documents indexés</div>
          </div>
          <div className="card stat-tile">
            <div className="stat-value">{stats.total_audit_events}</div>
            <div className="stat-label">Événements journalisés</div>
          </div>
          <div className="card stat-tile">
            <div className="stat-value" style={{ color: 'var(--deny)' }}>
              {stats.total_denied_events}
            </div>
            <div className="stat-label">Accès refusés</div>
          </div>
        </div>
      )}

      <div className="toolbar" style={{ margin: '24px 0 10px' }}>
        <h3>Comptes employés</h3>
        <button className="btn btn-primary" onClick={() => setShowCreate((v) => !v)}>
          {showCreate ? 'Fermer' : '+ Ajouter un employé'}
        </button>
      </div>

      {actionError && <div className="error-banner">{actionError}</div>}

      {showCreate && (
        <CreateUserForm
          onCreated={() => {
            setShowCreate(false)
            loadAll()
          }}
        />
      )}

      <div className="card" style={{ marginBottom: 28 }}>
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                <th>Nom</th>
                <th>Identifiant</th>
                <th>Rôle</th>
                <th>Accès effectif</th>
                <th>Statut</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {users.map((u) => {
                const hasOverride =
                  u.extra_departments.length > 0 || u.confidentiality_override || u.extra_tools.length > 0
                return (
                <tr key={u.id}>
                  <td>{u.full_name}</td>
                  <td className="mono">{u.username}</td>
                  <td>
                    <RoleBadge role={u.role} />
                  </td>
                  <td>
                    <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap', maxWidth: 220 }}>
                      {u.effective_departments.map((d) => (
                        <span key={d} className="dept-tag">
                          {d}
                        </span>
                      ))}
                      {hasOverride && (
                        <span className="badge" style={{ background: 'var(--accent-soft)', color: 'var(--accent-strong)' }}>
                          accès étendu
                        </span>
                      )}
                    </div>
                  </td>
                  <td>
                    <span className={u.is_active ? 'badge badge-allow' : 'badge badge-deny'}>
                      {u.is_active ? 'Actif' : 'Désactivé'}
                    </span>
                  </td>
                  <td>
                    <div style={{ display: 'flex', gap: 6 }}>
                      <button className="btn" style={{ padding: '4px 10px', fontSize: 12 }} onClick={() => setEditingUser(u)}>
                        Modifier
                      </button>
                      <button
                        className="btn"
                        style={{ padding: '4px 10px', fontSize: 12 }}
                        onClick={() => toggleActive(u)}
                        disabled={u.id === currentUser?.id}
                        title={u.id === currentUser?.id ? 'Vous ne pouvez pas vous désactiver vous-même' : undefined}
                      >
                        {u.is_active ? 'Désactiver' : 'Activer'}
                      </button>
                      <button
                        className="btn"
                        style={{ padding: '4px 10px', fontSize: 12, color: 'var(--deny)' }}
                        onClick={() => handleDelete(u)}
                        disabled={u.id === currentUser?.id}
                        title={u.id === currentUser?.id ? 'Vous ne pouvez pas supprimer votre propre compte' : undefined}
                      >
                        Supprimer
                      </button>
                    </div>
                  </td>
                </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      </div>

      {editingUser && (
        <EditUserModal
          user={editingUser}
          onClose={() => setEditingUser(null)}
          onSaved={() => {
            setEditingUser(null)
            loadAll()
          }}
        />
      )}
    </div>
  )
}

function CreateUserForm({ onCreated }: { onCreated: () => void }) {
  const [username, setUsername] = useState('')
  const [fullName, setFullName] = useState('')
  const [email, setEmail] = useState('')
  const [role, setRole] = useState<Role>('EMPLOYEE')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    setSaving(true)
    setError(null)
    try {
      await adminCreateUser({ username, full_name: fullName, email, role, password })
      onCreated()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Impossible de créer ce compte.')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="card card-pad" style={{ marginBottom: 20 }}>
      <h3 style={{ marginBottom: 12 }}>Ajouter un employé</h3>
      {error && <div className="error-banner">{error}</div>}
      <form onSubmit={handleSubmit}>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14 }}>
          <div className="field">
            <label className="field-label">Nom complet</label>
            <input className="input" value={fullName} onChange={(e) => setFullName(e.target.value)} required />
          </div>
          <div className="field">
            <label className="field-label">Identifiant de connexion</label>
            <input className="input" value={username} onChange={(e) => setUsername(e.target.value)} required />
          </div>
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14 }}>
          <div className="field">
            <label className="field-label">Email</label>
            <input type="email" className="input" value={email} onChange={(e) => setEmail(e.target.value)} required />
          </div>
          <div className="field">
            <label className="field-label">Rôle</label>
            <select className="input" value={role} onChange={(e) => setRole(e.target.value as Role)}>
              {ROLES.map((r) => (
                <option key={r} value={r}>
                  {r}
                </option>
              ))}
            </select>
          </div>
        </div>
        <div className="field">
          <label className="field-label">Mot de passe initial</label>
          <input
            type="password"
            className="input"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            minLength={8}
            required
          />
        </div>
        <button className="btn btn-primary" type="submit" disabled={saving}>
          {saving ? 'Création…' : 'Créer le compte'}
        </button>
      </form>
    </div>
  )
}

function EditUserModal({ user, onClose, onSaved }: { user: UserOut; onClose: () => void; onSaved: () => void }) {
  const { user: currentUser } = useAuth()
  const isSelf = user.id === currentUser?.id
  const [fullName, setFullName] = useState(user.full_name)
  const [email, setEmail] = useState(user.email)
  const [role, setRole] = useState<Role>(user.role)
  const [password, setPassword] = useState('')
  const [extraDepartments, setExtraDepartments] = useState<string[]>(user.extra_departments)
  const [confidentialityOverride, setConfidentialityOverride] = useState<string>(user.confidentiality_override ?? '')
  const [extraTools, setExtraTools] = useState<string[]>(user.extra_tools)
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  const roleDepartments = new Set(PolicyDepartmentsFor(role))

  function toggle(list: string[], setList: (v: string[]) => void, value: string) {
    setList(list.includes(value) ? list.filter((v) => v !== value) : [...list, value])
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    setSaving(true)
    setError(null)
    try {
      const payload: Record<string, unknown> = { full_name: fullName, email, role }
      if (password) payload.password = password
      await Promise.all([
        adminUpdateUser(user.id, payload),
        adminUpdateUserAccess(user.id, {
          extra_departments: extraDepartments,
          confidentiality_override: confidentialityOverride || null,
          extra_tools: extraTools,
        }),
      ])
      onSaved()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Impossible de modifier ce compte.')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-card" style={{ maxWidth: 560 }} onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <div>
            <h3>Modifier {user.full_name}</h3>
            <div style={{ color: 'var(--text-muted)', fontSize: 12.5, marginTop: 4 }}>@{user.username}</div>
          </div>
          <button className="modal-close" onClick={onClose}>
            ✕
          </button>
        </div>
        <div className="modal-body" style={{ whiteSpace: 'normal' }}>
          {error && <div className="error-banner">{error}</div>}
          {isSelf && (
            <div className="hint-banner" style={{ marginBottom: 14 }}>
              Vous modifiez votre propre compte : le rôle ne peut pas être changé depuis cet écran.
            </div>
          )}
          <form onSubmit={handleSubmit}>
            <div className="field">
              <label className="field-label">Nom complet</label>
              <input className="input" value={fullName} onChange={(e) => setFullName(e.target.value)} required />
            </div>
            <div className="field">
              <label className="field-label">Email</label>
              <input type="email" className="input" value={email} onChange={(e) => setEmail(e.target.value)} required />
            </div>
            <div className="field">
              <label className="field-label">Rôle</label>
              <select
                className="input"
                value={role}
                onChange={(e) => setRole(e.target.value as Role)}
                disabled={isSelf}
              >
                {ROLES.map((r) => (
                  <option key={r} value={r}>
                    {r}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label className="field-label">Nouveau mot de passe (laisser vide pour ne pas changer)</label>
              <input
                type="password"
                className="input"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                minLength={8}
              />
            </div>

            <div style={{ borderTop: '1px solid var(--border)', margin: '18px 0 14px', paddingTop: 14 }}>
              <div className="field-label" style={{ marginBottom: 2 }}>
                Accès personnalisé (en plus du rôle)
              </div>
              <p style={{ color: 'var(--text-muted)', fontSize: 12.5, margin: '0 0 12px' }}>
                Pour un besoin ponctuel — par ex. un Développeur qui doit voir un document FINANCE pour résoudre
                un incident. Ces accès s'ajoutent au rôle, ils ne le remplacent pas.
              </p>

              <label className="field-label">Départements supplémentaires</label>
              <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 12 }}>
                {DEPARTMENTS.map((d) => {
                  const impliedByRole = roleDepartments.has(d)
                  return (
                    <label
                      key={d}
                      className="dept-tag"
                      style={{
                        display: 'flex',
                        alignItems: 'center',
                        gap: 5,
                        border: '1px solid var(--border-strong)',
                        borderRadius: 100,
                        padding: '3px 10px',
                        cursor: impliedByRole ? 'default' : 'pointer',
                        opacity: impliedByRole ? 0.5 : 1,
                      }}
                    >
                      <input
                        type="checkbox"
                        checked={impliedByRole || extraDepartments.includes(d)}
                        disabled={impliedByRole}
                        onChange={() => toggle(extraDepartments, setExtraDepartments, d)}
                      />
                      {d}
                    </label>
                  )
                })}
              </div>

              <div className="field">
                <label className="field-label">Niveau de confidentialité supplémentaire</label>
                <select
                  className="input"
                  value={confidentialityOverride}
                  onChange={(e) => setConfidentialityOverride(e.target.value)}
                >
                  <option value="">Aucun (niveau du rôle uniquement)</option>
                  {LEVELS.map((l) => (
                    <option key={l} value={l}>
                      jusqu'à {l}
                    </option>
                  ))}
                </select>
              </div>

              <label className="field-label">Fonctionnalités supplémentaires de l'assistant</label>
              <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                {TOOLS.map((t) => (
                  <label
                    key={t}
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      gap: 5,
                      border: '1px solid var(--border-strong)',
                      borderRadius: 100,
                      padding: '3px 10px',
                      fontSize: 12,
                      cursor: 'pointer',
                    }}
                  >
                    <input type="checkbox" checked={extraTools.includes(t)} onChange={() => toggle(extraTools, setExtraTools, t)} />
                    {toolLabel(t)}
                  </label>
                ))}
              </div>
            </div>

            <div style={{ display: 'flex', gap: 10, justifyContent: 'flex-end' }}>
              <button type="button" className="btn" onClick={onClose}>
                Annuler
              </button>
              <button type="submit" className="btn btn-primary" disabled={saving}>
                {saving ? 'Enregistrement…' : 'Enregistrer'}
              </button>
            </div>
          </form>
        </div>
      </div>
    </div>
  )
}

// Mirrors app/policy/rules.py ROLE_ACCESS[role]["departments"] so the modal
// can grey out departments the role already grants by default.
const ROLE_DEPARTMENTS: Record<Role, string[]> = {
  DIRECTOR: ['HR', 'FINANCE', 'TECH', 'GENERAL', 'EXEC'],
  HR: ['HR', 'GENERAL'],
  ACCOUNTANT: ['FINANCE', 'GENERAL'],
  DEVELOPER: ['TECH', 'GENERAL'],
  EMPLOYEE: ['GENERAL'],
}

function PolicyDepartmentsFor(role: Role): string[] {
  return ROLE_DEPARTMENTS[role] ?? []
}
