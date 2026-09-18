import { useAuth } from '../context/AuthContext'
import { ConfidentialityBadge, RoleBadge } from '../components/Badges'
import { toolLabel } from '../lib/tools'

export function ProfilePage() {
  const { user } = useAuth()
  if (!user) return null

  return (
    <div className="page">
      <div className="page-header">
        <h1>Mon profil</h1>
        <p>Identité et permissions telles que résolues par le backend à chaque requête.</p>
      </div>

      <div className="card card-pad" style={{ marginBottom: 20 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 16, marginBottom: 18 }}>
          <div className="avatar" style={{ width: 52, height: 52, fontSize: 18 }}>
            {user.full_name
              .split(' ')
              .map((p) => p[0])
              .join('')}
          </div>
          <div>
            <h2>{user.full_name}</h2>
            <div style={{ color: 'var(--text-muted)', fontSize: 13 }}>{user.email}</div>
          </div>
          <div style={{ marginLeft: 'auto' }}>
            <RoleBadge role={user.role} />
          </div>
        </div>

        <div className="table-wrap">
          <table className="data-table">
            <tbody>
              <tr>
                <td style={{ width: 220, color: 'var(--text-muted)' }}>Identifiant</td>
                <td className="mono">{user.username}</td>
              </tr>
              <tr>
                <td style={{ color: 'var(--text-muted)' }}>Rôle</td>
                <td>
                  <RoleBadge role={user.role} />
                </td>
              </tr>
              <tr>
                <td style={{ color: 'var(--text-muted)' }}>Départements accessibles</td>
                <td>
                  <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                    {user.departments.map((d) => (
                      <span key={d} className="dept-tag">
                        {d}
                      </span>
                    ))}
                  </div>
                </td>
              </tr>
              <tr>
                <td style={{ color: 'var(--text-muted)' }}>Niveau de confidentialité maximal</td>
                <td>
                  <ConfidentialityBadge level={user.max_confidentiality} />
                </td>
              </tr>
              <tr>
                <td style={{ color: 'var(--text-muted)' }}>Fonctionnalités de l'assistant autorisées</td>
                <td>
                  <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                    {user.allowed_tools.map((t) => (
                      <span key={t} className="dept-tag" style={{ border: '1px solid var(--border)', borderRadius: 100, padding: '3px 10px' }}>
                        {toolLabel(t)}
                      </span>
                    ))}
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <div className="card card-pad">
        <h3 style={{ marginBottom: 8 }}>D'où viennent ces permissions ?</h3>
        <p style={{ color: 'var(--text-muted)', fontSize: 13 }}>
          Ces valeurs sont calculées par le moteur de policy du backend à partir de votre rôle (RBAC), puis
          vérifiées à nouveau à chaque appel — sur l'API documents, sur le pipeline RAG et dans chaque outil du
          serveur MCP. Elles ne sont jamais déclarées ou modifiables depuis le frontend.
        </p>
      </div>
    </div>
  )
}
