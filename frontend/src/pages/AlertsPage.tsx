import { useEffect, useState, type ComponentType } from 'react'
import { adminListAlerts, adminMarkAlertRead, adminMarkAllAlertsRead } from '../api/endpoints'
import { RoleBadge } from '../components/Badges'
import { IconChat, IconDocument } from '../components/Icons'
import type { AlertOut, AlertType } from '../types'

const TYPE_LABELS: Record<AlertType, string> = {
  CHAT_ACCESS_DENIED: 'Assistant IA — accès refusé',
  DOCUMENT_ACCESS_DENIED: 'Document — accès refusé',
}

const TYPE_ICONS: Record<AlertType, ComponentType<{ size?: number }>> = {
  CHAT_ACCESS_DENIED: IconChat,
  DOCUMENT_ACCESS_DENIED: IconDocument,
}

const PAGE_SIZE = 30

export function AlertsPage() {
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
          return (
            <div key={alert.id} className={`alert-item${alert.is_read ? '' : ' unread'}`} onClick={() => markRead(alert)}>
              <div className={`alert-item-icon ${alert.alert_type === 'CHAT_ACCESS_DENIED' ? 'chat' : 'doc'}`}>
                <Icon size={16} />
              </div>
              <div style={{ minWidth: 0, flex: 1 }}>
                <div className="alert-item-top">
                  <span className="badge badge-deny">{TYPE_LABELS[alert.alert_type] ?? alert.alert_type}</span>
                  {alert.role && <RoleBadge role={alert.role} />}
                  {!alert.is_read && <span className="unread-dot" />}
                </div>
                <div className="alert-item-title">{alert.title}</div>
                <div className="alert-item-desc">{alert.description}</div>
                <div className="alert-item-time">{new Date(alert.created_at).toLocaleString('fr-FR')}</div>
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
