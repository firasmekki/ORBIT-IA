import { useEffect, useState, type ReactNode } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'
import { listConversations, listDocuments } from '../api/endpoints'
import { ConfidentialityBadge } from '../components/Badges'
import { IconArrowRight, IconChat, IconCheck, IconClock, IconDatabase, IconDocument, IconSearch } from '../components/Icons'
import { RobotMascot } from '../components/RobotMascot'
import type { ConversationSummary, DocumentSummary } from '../types'

const TODAY = new Date().toLocaleDateString('fr-FR', { weekday: 'long', day: 'numeric', month: 'long' })

export function DashboardPage() {
  const { user } = useAuth()
  const navigate = useNavigate()
  const [documents, setDocuments] = useState<DocumentSummary[]>([])
  const [conversations, setConversations] = useState<ConversationSummary[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    Promise.all([listDocuments(), listConversations()])
      .then(([docs, convs]) => {
        setDocuments(docs)
        setConversations(convs)
      })
      .finally(() => setLoading(false))
  }, [])

  if (!user) return null

  const canSearchDatabase = user.allowed_tools.includes('search_database')

  return (
    <div className="page dashboard-page">
      <div className="page-header toolbar">
        <div>
          <h1>Bonjour, {user.full_name.split(' ')[0]} 👋</h1>
          <p>Bienvenue sur Orbitia, votre assistant IA interne.</p>
        </div>
        <div style={{ color: 'var(--text-muted)', fontSize: 13, textTransform: 'capitalize' }}>{TODAY}</div>
      </div>

      <div className="stat-strip" style={{ marginBottom: 24 }}>
        <StatStripItem
          icon={<IconDocument size={18} />}
          tone="blue"
          value={loading ? '—' : documents.length}
          label="Documents accessibles"
          to="/documents"
        />
        <StatStripItem
          icon={<IconChat size={18} />}
          tone="yellow"
          value={loading ? '—' : conversations.length}
          label="Conversations"
          to="/chat"
        />
        <StatStripItem
          icon={<IconSearch size={18} />}
          tone="purple"
          value={user.allowed_tools.length}
          label="Outils IA disponibles"
          to={user.role === 'DIRECTOR' ? '/permissions' : '/chat'}
        />
        <Link to="/profile" className="stat-strip-item">
          <div className="stat-strip-icon tone-green">
            <IconClock size={18} />
          </div>
          <div style={{ minWidth: 0 }}>
            <div style={{ lineHeight: 1.3 }}>
              <ConfidentialityBadge level={user.max_confidentiality} />
            </div>
            <div className="stat-strip-label">Niveau d'accès maximum</div>
          </div>
        </Link>
      </div>

      <h3 style={{ marginBottom: 12 }}>Que souhaitez-vous faire ?</h3>
      <div className="quick-actions" style={{ marginBottom: 28 }}>
        <QuickAction
          icon={<IconChat size={20} />}
          title="Poser une question"
          description="Interrogez Orbitia sur vos documents et données internes."
          onClick={() => navigate('/chat')}
        />
        <QuickAction
          icon={<IconDocument size={20} />}
          title="Rechercher un document"
          description="Trouvez rapidement les documents de votre département."
          onClick={() => navigate('/documents')}
        />
        {canSearchDatabase ? (
          <QuickAction
            icon={<IconDatabase size={20} />}
            title="Rechercher dans les données"
            description="Obtenez des informations depuis les bases de données internes."
            onClick={() => navigate('/chat')}
          />
        ) : (
          <QuickAction
            icon={<IconDatabase size={20} />}
            title="Informations générales"
            description="Politiques, onboarding et organisation accessibles à tous."
            onClick={() => navigate('/chat')}
          />
        )}
      </div>

      <div className="dashboard-split">
        <div>
          <div className="toolbar" style={{ marginBottom: 12 }}>
            <h3 style={{ margin: 0 }}>Activité récente</h3>
            <Link to="/chat" style={{ fontSize: 12.5, textDecoration: 'none' }}>
              Voir tout →
            </Link>
          </div>
          <div className="card">
            {loading ? (
              <div className="centered-spinner">
                <div className="spinner" />
              </div>
            ) : conversations.length === 0 ? (
              <div className="empty-state">Aucune activité pour le moment — posez votre première question.</div>
            ) : (
              conversations.slice(0, 5).map((c) => (
                <div key={c.id} className="activity-row" onClick={() => navigate('/chat')}>
                  <div className="activity-icon">
                    <IconChat size={15} />
                  </div>
                  <div style={{ minWidth: 0 }}>
                    <div className="activity-title">Vous avez posé une question</div>
                    <div className="activity-sub">"{c.title}"</div>
                  </div>
                  <div className="activity-time">{new Date(c.created_at).toLocaleDateString('fr-FR')}</div>
                </div>
              ))
            )}
          </div>
        </div>

        <div>
          <div className="ai-card">
            <div className="ai-card-body">
              <h3 className="ai-card-title">Orbitia AI</h3>
              <div className="ai-card-subtitle">Votre assistant intelligent</div>
              <p className="ai-card-desc">Accédez aux connaissances de votre entreprise en toute sécurité.</p>
              <button className="ai-card-cta" onClick={() => navigate('/chat')}>
                <IconChat size={15} /> Commencer une conversation
              </button>
              <ul className="ai-card-checks">
                <li>
                  <span className="ai-card-check-dot">
                    <IconCheck size={10} />
                  </span>
                  Données sécurisées
                </li>
                <li>
                  <span className="ai-card-check-dot">
                    <IconCheck size={10} />
                  </span>
                  Accès selon vos permissions
                </li>
                <li>
                  <span className="ai-card-check-dot">
                    <IconCheck size={10} />
                  </span>
                  Réponses fiables
                </li>
              </ul>
            </div>
            <div className="ai-card-mascot">
              <RobotMascot size={100} />
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}

function StatStripItem({
  icon,
  value,
  label,
  to,
  tone,
}: {
  icon: ReactNode
  value: number | string
  label: string
  to: string
  tone: 'blue' | 'yellow' | 'purple' | 'green'
}) {
  return (
    <Link to={to} className="stat-strip-item">
      <div className={`stat-strip-icon tone-${tone}`}>{icon}</div>
      <div style={{ minWidth: 0 }}>
        <div className="stat-strip-value">{value}</div>
        <div className="stat-strip-label">{label}</div>
      </div>
    </Link>
  )
}

function QuickAction({
  icon,
  title,
  description,
  onClick,
}: {
  icon: ReactNode
  title: string
  description: string
  onClick: () => void
}) {
  return (
    <button className="quick-action-card" onClick={onClick}>
      <div className="quick-action-icon">{icon}</div>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div className="quick-action-title">{title}</div>
        <div className="quick-action-desc">{description}</div>
      </div>
      <IconArrowRight size={16} style={{ color: 'var(--text-muted)', flexShrink: 0 }} />
    </button>
  )
}
