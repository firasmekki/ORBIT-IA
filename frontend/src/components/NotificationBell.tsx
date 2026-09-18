import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { adminListAlerts, adminMarkAlertRead, adminUnreadAlertCount } from '../api/endpoints'
import { IconBell } from './Icons'
import type { AlertOut } from '../types'

const POLL_INTERVAL_MS = 20000

export function NotificationBell() {
  const navigate = useNavigate()
  const [unreadCount, setUnreadCount] = useState(0)
  const [open, setOpen] = useState(false)
  const [recent, setRecent] = useState<AlertOut[]>([])
  const wrapRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    refreshCount()
    const interval = setInterval(refreshCount, POLL_INTERVAL_MS)
    return () => clearInterval(interval)
  }, [])

  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [])

  async function refreshCount() {
    try {
      const { unread_count } = await adminUnreadAlertCount()
      setUnreadCount(unread_count)
    } catch {
      // non-fatal (e.g. session expiring)
    }
  }

  async function toggleOpen() {
    const next = !open
    setOpen(next)
    if (next) {
      const page = await adminListAlerts({ limit: 6 })
      setRecent(page.items)
    }
  }

  async function handleItemClick(alert: AlertOut) {
    if (!alert.is_read) {
      await adminMarkAlertRead(alert.id)
      setUnreadCount((c) => Math.max(0, c - 1))
      setRecent((prev) => prev.map((a) => (a.id === alert.id ? { ...a, is_read: true } : a)))
    }
    setOpen(false)
    navigate('/alerts')
  }

  return (
    <div className="bell-wrap" ref={wrapRef}>
      <button className="icon-btn" type="button" title="Notifications" onClick={toggleOpen}>
        <IconBell size={17} />
      </button>
      {unreadCount > 0 && <span className="bell-badge">{unreadCount > 9 ? '9+' : unreadCount}</span>}

      {open && (
        <div className="bell-dropdown">
          <div className="bell-dropdown-header">
            <span>Alertes</span>
            <span style={{ color: 'var(--text-muted)', fontWeight: 500 }}>{unreadCount} non lue(s)</span>
          </div>
          {recent.length === 0 ? (
            <div className="empty-state" style={{ padding: 24 }}>
              Aucune alerte.
            </div>
          ) : (
            recent.map((alert) => (
              <div
                key={alert.id}
                className={`bell-dropdown-item${alert.is_read ? '' : ' unread'}`}
                onClick={() => handleItemClick(alert)}
              >
                <div style={{ minWidth: 0 }}>
                  <div style={{ fontSize: 12.5, fontWeight: 600 }}>{alert.title}</div>
                  <div
                    style={{
                      fontSize: 11.5,
                      color: 'var(--text-muted)',
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                      whiteSpace: 'nowrap',
                    }}
                  >
                    {alert.description}
                  </div>
                  <div style={{ fontSize: 10.5, color: 'var(--text-muted)', marginTop: 3 }}>
                    {new Date(alert.created_at).toLocaleString('fr-FR')}
                  </div>
                </div>
              </div>
            ))
          )}
          <div className="bell-dropdown-footer">
            <button
              className="btn"
              style={{ width: '100%', justifyContent: 'center', padding: '6px 10px', fontSize: 12 }}
              onClick={() => {
                setOpen(false)
                navigate('/alerts')
              }}
            >
              Voir tout l'historique
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
