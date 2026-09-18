import { useState, type FormEvent } from 'react'
import { NavLink, Outlet, useNavigate } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'
import { RoleBadge } from '../components/Badges'
import { NotificationBell } from '../components/NotificationBell'
import {
  IconAudit,
  IconBell,
  IconChat,
  IconDashboard,
  IconDocument,
  IconLogout,
  IconMenu,
  IconSearch,
  IconSettings,
  IconShield,
  IconUsers,
} from '../components/Icons'

const SIDEBAR_COLLAPSED_KEY = 'orbitia_sidebar_collapsed'

const NAV_ITEMS = [
  { to: '/', label: 'Tableau de bord', icon: IconDashboard },
  { to: '/chat', label: 'Assistant IA', icon: IconChat },
  { to: '/documents', label: 'Documents', icon: IconDocument },
]

const DIRECTOR_ITEMS = [
  { to: '/admin', label: 'Utilisateurs', icon: IconUsers },
  { to: '/permissions', label: 'Permissions', icon: IconShield },
  { to: '/alerts', label: 'Alertes', icon: IconBell },
  { to: '/audit', label: "Journal d'audit", icon: IconAudit },
]

const SETTINGS_ITEM = { to: '/settings', label: 'Paramètres', icon: IconSettings }

function initials(name: string): string {
  return name
    .split(' ')
    .map((p) => p[0])
    .slice(0, 2)
    .join('')
    .toUpperCase()
}

export function AppLayout() {
  const { user, logout } = useAuth()
  const navigate = useNavigate()
  const [search, setSearch] = useState('')
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === '1')

  if (!user) return null

  const isDirector = user.role === 'DIRECTOR'

  function handleLogout() {
    logout()
    navigate('/login')
  }

  function toggleSidebar() {
    setCollapsed((prev) => {
      const next = !prev
      localStorage.setItem(SIDEBAR_COLLAPSED_KEY, next ? '1' : '0')
      return next
    })
  }

  function handleSearch(e: FormEvent) {
    e.preventDefault()
    const q = search.trim()
    navigate(q ? `/documents?q=${encodeURIComponent(q)}` : '/documents')
  }

  return (
    <div className="app-shell">
      <aside className={`sidebar${collapsed ? ' collapsed' : ''}`}>
        <div className="brand">
          <div className="brand-logo-plate">
            <img src="/logo-orbit.png" alt="Orbit" />
          </div>
          <div className="brand-mark-mini">O</div>
        </div>

        <nav className="nav-group">
          <div className="nav-label">Espace de travail</div>
          {NAV_ITEMS.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === '/'}
              title={item.label}
              className={({ isActive }) => `nav-link${isActive ? ' active' : ''}`}
            >
              <span className="nav-icon">
                <item.icon size={17} />
              </span>
              <span className="nav-link-label">{item.label}</span>
            </NavLink>
          ))}
        </nav>

        {isDirector && (
          <nav className="nav-group">
            <div className="nav-label">Direction</div>
            {DIRECTOR_ITEMS.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                title={item.label}
                className={({ isActive }) => `nav-link${isActive ? ' active' : ''}`}
              >
                <span className="nav-icon">
                  <item.icon size={17} />
                </span>
                <span className="nav-link-label">{item.label}</span>
              </NavLink>
            ))}
          </nav>
        )}

        <nav className="nav-group" style={{ marginBottom: 0 }}>
          <NavLink to={SETTINGS_ITEM.to} title={SETTINGS_ITEM.label} className={({ isActive }) => `nav-link${isActive ? ' active' : ''}`}>
            <span className="nav-icon">
              <SETTINGS_ITEM.icon size={17} />
            </span>
            <span className="nav-link-label">{SETTINGS_ITEM.label}</span>
          </NavLink>
        </nav>

        <div className="sidebar-footer">
          <div className="user-chip" title={user.full_name}>
            <div className="avatar">{initials(user.full_name)}</div>
            <div className="nav-link-label" style={{ minWidth: 0 }}>
              <div className="user-chip-name">{user.full_name}</div>
              <div className="user-chip-role">
                <RoleBadge role={user.role} />
              </div>
            </div>
          </div>
          <button className="logout-btn" onClick={handleLogout} title="Se déconnecter">
            <IconLogout size={15} />
            <span className="nav-link-label">Se déconnecter</span>
          </button>
        </div>
      </aside>

      <div className="main">
        <header className="topbar">
          <button className="icon-btn" type="button" title="Afficher/masquer le menu" onClick={toggleSidebar}>
            <IconMenu size={17} />
          </button>
          <form className="topbar-search" onSubmit={handleSearch}>
            <IconSearch size={15} />
            <input
              placeholder="Rechercher un document, une information…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </form>
          <div className="topbar-actions">
            {isDirector && <NotificationBell />}
            <NavLink to="/profile" className="topbar-user">
              <div className="avatar" style={{ width: 30, height: 30, fontSize: 11.5 }}>
                {initials(user.full_name)}
              </div>
              <div style={{ textAlign: 'left' }}>
                <div style={{ fontSize: 12.5, fontWeight: 600, color: 'var(--text)' }}>{user.full_name}</div>
                <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>{user.role}</div>
              </div>
            </NavLink>
          </div>
        </header>
        <Outlet />
      </div>
    </div>
  )
}
