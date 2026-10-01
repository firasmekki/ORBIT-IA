import { useEffect, useState, type ComponentType } from 'react'
import { useNavigate } from 'react-router-dom'
import { adminListAlerts, adminMarkAlertRead, adminMarkAllAlertsRead } from '../api/endpoints'
import { RoleBadge } from '../components/Badges'
import { IconChat, IconDocument, IconTrash } from '../components/Icons'
import type { AlertOut, AlertType } from '../types'

const TYPE_LABELS: Record<AlertType, string> = {
  CHAT_ACCESS_DENIED: 'Assistant IA — accès refusé',
  DOCUMENT_ACCESS_DENIED: 'Document — accès refusé',
  HISTORY_CONVERSATION_DELETED: 'Conversation supprimée',
  HISTORY_ALL_DELETED: 'Historique supprimé',
}

const TYPE_ICONS: Record<AlertType, ComponentType<{ size?: number }>> = {
  CHAT_ACCESS_DENIED: IconChat,
  DOCUMENT_ACCESS_DENIED: IconDocument,
  HISTORY_CONVERSATION_DELETED: IconTrash,
  HISTORY_ALL_DELETED: IconTrash,
}

// CHAT_ACCESS_DENIED/DOCUMENT_ACCESS_DENIED are genuine policy refusals
// (deny-red, by design); the two HISTORY_* types are informational/
// traceability events, not security denials, so they get the neutral
// "system" tone instead of red - a Director shouldn't read every entry in
// this list as an intrusion attempt.
const TYPE_ICON_TONE: Record<AlertType, string> = {
  CHAT_ACCESS_DENIED: 'chat',
  DOCUMENT_ACCESS_DENIED: 'doc',
  HISTORY_CONVERSATION_DELETED: '',
  HISTORY_ALL_DELETED: '',
}

const TYPE_BADGE_CLASS: Record<AlertType, string> = {
  CHAT_ACCESS_DENIED: 'badge-deny',
  DOCUMENT_ACCESS_DENIED: 'badge-deny',
  HISTORY_CONVERSATION_DELETED: 'badge-system',
  HISTORY_ALL_DELETED: 'badge-system',
}

const HISTORY_ALERT_TYPES = new Set<AlertType>(['HISTORY_CONVERSATION_DELETED', 'HISTORY_ALL_DELETED'])

const PAGE_SIZE = 30

export function AlertsPage() {
  const navigate = useNavigate()
  const [items, setItems] = useState<AlertOut[]>([])
  const [total, setTotal] = useState(0)
  const [unreadCount, setUnreadCount] = useState(0)
  const [unreadOnly, setUnreadOnly] = useState(false)
  const [loading, setLoading] = useState(true)
  const [offset, setOffset] = useState(0)

  useEffect(() => {
    load(0, unreadOnly)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [unreadOnly])

  async function load(nextOffset: number, currentUnreadOnly: boolean) {
    setLoading(true)
    try {
      const page = await adminListAlerts({ limit: PAGE_SIZE, offset: nextOffset, unread_only: currentUnreadOnly })
      if (nextOffset === 0) setItems(page.items)
      else setItems((prev) => [...prev, ...page.items])
      setTotal(page.total)
      setUnreadCount(page.unread_count)
      setOffset(nextOffset)
    } finally {
      setLoading(false)
    }
  }

  async function markRead(alert: AlertOut) {
    if (alert.is_read) return
    setItems((prev) => prev.map((a) => (a.id === alert.id ? { ...a, is_read: true } : a)))
    setUnreadCount((c) => Math.max(0, c - 1))
    try {
      await adminMarkAlertRead(alert.id)
    } catch {
      load(0, unreadOnly)
    }
  }

  async function markAllRead() {
    await adminMarkAllAlertsRead()
    load(0, unreadOnly)
  }

  return (
    <div className="page">
      <div className="page-header toolbar">
        <div>
          <h1>Alertes</h1>
          <p>
            Vous êtes notifié chaque fois qu'un employé tente d'accéder à une information ou un document hors de
            son périmètre — via l'assistant IA ou directement. Réservé au rôle Directeur.
          </p>
        </div>
        {unreadCount > 0 && (
          <button className="btn btn-primary" onClick={markAllRead}>
            Tout marquer comme lu ({unreadCount})
          </button>
        )}
      </div>

      <div className="filter-bar">
        <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13 }}>
          <input type="checkbox" checked={unreadOnly} onChange={(e) => setUnreadOnly(e.target.checked)} />
          Non lues uniquement
        </label>
        <span style={{ color: 'var(--text-muted)', fontSize: 13 }}>
          {total} alerte{total > 1 ? 's' : ''} · {unreadCount} non lue{unreadCount > 1 ? 's' : ''}
        </span>
      </div>

      <div className="alert-list">
        {items.map((alert) => {
          const Icon = TYPE_ICONS[alert.alert_type] ?? IconDocument
          const tone = TYPE_ICON_TONE[alert.alert_type] ?? 'doc'
          const badgeClass = TYPE_BADGE_CLASS[alert.alert_type] ?? 'badge-deny'
          return (
            <div key={alert.id} className={`alert-item${alert.is_read ? '' : ' unread'}`} onClick={() => markRead(alert)}>
              <div className={`alert-item-icon${tone ? ` ${tone}` : ''}`}>
                <Icon size={16} />
              </div>
              <div style={{ minWidth: 0, flex: 1 }}>
                <div className="alert-item-top">
                  <span className={`badge ${badgeClass}`}>{TYPE_LABELS[alert.alert_type] ?? alert.alert_type}</span>
                  {alert.role && <RoleBadge role={alert.role} />}
                  {!alert.is_read && <span className="unread-dot" />}
                </div>
                <div className="alert-item-title">{alert.title}</div>
                <div className="alert-item-desc">{alert.description}</div>
                <div className="alert-item-time">{new Date(alert.created_at).toLocaleString('fr-FR')}</div>
                {HISTORY_ALERT_TYPES.has(alert.alert_type) && alert.audit_log_id && (
                  <button
                    type="button"
                    className="btn"
                    style={{ marginTop: 8, padding: '4px 10px', fontSize: 11.5 }}
                    onClick={(e) => {
                      e.stopPropagation()
                      navigate('/audit')
                    }}
                  >
                    Voir la traçabilité
                  </button>
                )}
              </div>
            </div>
          )
        })}
        {!loading && items.length === 0 && <div className="empty-state">Aucune alerte pour le moment.</div>}
        {loading && (
          <div className="centered-spinner">
            <div className="spinner" />
          </div>
        )}
      </div>

      {items.length < total && !loading && (
        <div style={{ textAlign: 'center', marginTop: 16 }}>
          <button className="btn" onClick={() => load(offset + PAGE_SIZE, unreadOnly)}>
            Charger plus
          </button>
        </div>
      )}
    </div>
  )
}
