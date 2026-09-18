import type { ReactNode } from 'react'
import { Navigate } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'

export function RequireAuth({ children }: { children: ReactNode }) {
  const { user, loading } = useAuth()

  if (loading) {
    return (
      <div className="centered-spinner">
        <div className="spinner" />
      </div>
    )
  }
  if (!user) return <Navigate to="/login" replace />
  return <>{children}</>
}

export function RequireRole({ role, children }: { role: string; children: ReactNode }) {
  const { user } = useAuth()
  if (!user) return null
  if (user.role !== role) {
    return (
      <div className="page">
        <div className="denied-panel">
          <strong>Accès refusé.</strong> Cette page est réservée au rôle Directeur. Votre rôle actuel
          ({user.role}) ne dispose pas de cette permission. Cette restriction est appliquée côté backend sur
          chaque appel API correspondant, pas seulement ici.
        </div>
      </div>
    )
  }
  return <>{children}</>
}
