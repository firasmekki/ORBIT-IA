import { useEffect, useState } from 'react'
import { adminPolicyMatrix } from '../api/endpoints'
import { ConfidentialityBadge, RoleBadge } from '../components/Badges'
import { IconShield } from '../components/Icons'
import { toolLabel } from '../lib/tools'
import type { PolicyMatrix } from '../types'

export function PermissionsPage() {
  const [policy, setPolicy] = useState<PolicyMatrix>({})
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    adminPolicyMatrix()
      .then(setPolicy)
      .finally(() => setLoading(false))
  }, [])

  if (loading) {
    return (
      <div className="centered-spinner">
        <div className="spinner" />
      </div>
    )
  }

  const roles = Object.keys(policy)
  const departments = Array.from(new Set(roles.flatMap((r) => policy[r].departments))).sort()

  return (
    <div className="page">
      <div className="page-header">
        <h1>Permissions</h1>
        <p>
          La matrice RBAC/ABAC réellement appliquée par le backend à chaque requête — pas une documentation à
          part, ce sont les règles exactes évaluées par <code className="mono">app/policy/rules.py</code>. Un
          Directeur peut en plus accorder des accès ponctuels par employé depuis la page Utilisateurs.
        </p>
      </div>

      <div className="card" style={{ marginBottom: 24 }}>
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                <th>Rôle</th>
                {departments.map((d) => (
                  <th key={d} style={{ textAlign: 'center' }}>
                    {d}
                  </th>
                ))}
                <th>Niveau max.</th>
              </tr>
            </thead>
            <tbody>
              {roles.map((role) => (
                <tr key={role}>
                  <td>
                    <RoleBadge role={role} />
                  </td>
                  {departments.map((d) => (
                    <td key={d} style={{ textAlign: 'center' }}>
                      {policy[role].departments.includes(d) ? (
                        <span style={{ color: 'var(--allow)', fontWeight: 700 }}>✓</span>
                      ) : (
                        <span style={{ color: 'var(--border-strong)' }}>—</span>
                      )}
                    </td>
                  ))}
                  <td>
                    <ConfidentialityBadge level={policy[role].max_level} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <h3 style={{ marginBottom: 10 }}>Outils MCP par rôle</h3>
      <p style={{ color: 'var(--text-muted)', fontSize: 13, marginBottom: 14, maxWidth: '70ch' }}>
        Un outil absent de cette liste n'est même pas proposé à l'assistant IA pour ce rôle — il n'y a rien à
        refuser, l'option n'existe pas dans sa session.
      </p>
      <div className="tool-grid">
        {roles.map((role) => (
          <div key={role} className="card card-pad tool-role-card">
            <RoleBadge role={role} />
            <div className="tool-pill-list">
              {policy[role].tools.map((t) => (
                <span key={t} className="tool-pill">
                  {toolLabel(t)}
                </span>
              ))}
              {policy[role].tools.length === 0 && (
                <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>Aucun outil</span>
              )}
            </div>
          </div>
        ))}
      </div>

      <div className="card card-pad" style={{ marginTop: 24, display: 'flex', gap: 12, alignItems: 'flex-start' }}>
        <IconShield size={20} style={{ color: 'var(--accent)', flexShrink: 0, marginTop: 2 }} />
        <p style={{ margin: 0, fontSize: 13, color: 'var(--text-muted)' }}>
          Ces règles sont vérifiées indépendamment à trois niveaux — l'API documents, le retrieval RAG et chaque
          outil du serveur MCP — pour qu'une faille dans une couche ne devienne pas un contournement complet
          (défense en profondeur).
        </p>
      </div>
    </div>
  )
}
