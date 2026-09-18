import { useState, type FormEvent } from 'react'
import { updateMe } from '../api/endpoints'
import { useAuth } from '../context/AuthContext'
import { RoleBadge } from '../components/Badges'
import { IconLock } from '../components/Icons'

export function SettingsPage() {
  const { user, refreshUser } = useAuth()
  const [fullName, setFullName] = useState(user?.full_name ?? '')
  const [email, setEmail] = useState(user?.email ?? '')
  const [profileSaving, setProfileSaving] = useState(false)
  const [profileError, setProfileError] = useState<string | null>(null)
  const [profileSaved, setProfileSaved] = useState(false)

  const [currentPassword, setCurrentPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [passwordSaving, setPasswordSaving] = useState(false)
  const [passwordError, setPasswordError] = useState<string | null>(null)
  const [passwordSaved, setPasswordSaved] = useState(false)

  if (!user) return null

  async function saveProfile(e: FormEvent) {
    e.preventDefault()
    setProfileSaving(true)
    setProfileError(null)
    setProfileSaved(false)
    try {
      await updateMe({ full_name: fullName, email })
      await refreshUser()
      setProfileSaved(true)
    } catch (err) {
      setProfileError(err instanceof Error ? err.message : 'Impossible de mettre à jour le profil.')
    } finally {
      setProfileSaving(false)
    }
  }

  async function savePassword(e: FormEvent) {
    e.preventDefault()
    setPasswordSaving(true)
    setPasswordError(null)
    setPasswordSaved(false)
    try {
      await updateMe({ current_password: currentPassword, new_password: newPassword })
      setCurrentPassword('')
      setNewPassword('')
      setPasswordSaved(true)
    } catch (err) {
      setPasswordError(err instanceof Error ? err.message : 'Impossible de changer le mot de passe.')
    } finally {
      setPasswordSaving(false)
    }
  }

  return (
    <div className="page">
      <div className="page-header">
        <h1>Paramètres</h1>
        <p>Gérez vos informations personnelles et votre mot de passe. Le rôle et les accès sont gérés par le Directeur.</p>
      </div>

      <div className="card card-pad" style={{ marginBottom: 20, maxWidth: 480 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 16 }}>
          <h3 style={{ margin: 0 }}>Profil</h3>
          <RoleBadge role={user.role} />
        </div>
        {profileError && <div className="error-banner">{profileError}</div>}
        {profileSaved && <div className="hint-banner">Profil mis à jour.</div>}
        <form onSubmit={saveProfile}>
          <div className="field">
            <label className="field-label">Nom complet</label>
            <input className="input" value={fullName} onChange={(e) => setFullName(e.target.value)} required />
          </div>
          <div className="field">
            <label className="field-label">Email</label>
            <input type="email" className="input" value={email} onChange={(e) => setEmail(e.target.value)} required />
          </div>
          <button className="btn btn-primary" type="submit" disabled={profileSaving}>
            {profileSaving ? 'Enregistrement…' : 'Enregistrer'}
          </button>
        </form>
      </div>

      <div className="card card-pad" style={{ maxWidth: 480 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 16 }}>
          <IconLock size={17} style={{ color: 'var(--accent)' }} />
          <h3 style={{ margin: 0 }}>Mot de passe</h3>
        </div>
        {passwordError && <div className="error-banner">{passwordError}</div>}
        {passwordSaved && <div className="hint-banner">Mot de passe changé avec succès.</div>}
        <form onSubmit={savePassword}>
          <div className="field">
            <label className="field-label">Mot de passe actuel</label>
            <input
              type="password"
              className="input"
              value={currentPassword}
              onChange={(e) => setCurrentPassword(e.target.value)}
              required
            />
          </div>
          <div className="field">
            <label className="field-label">Nouveau mot de passe</label>
            <input
              type="password"
              className="input"
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
              minLength={8}
              required
            />
          </div>
          <button className="btn btn-primary" type="submit" disabled={passwordSaving}>
            {passwordSaving ? 'Changement…' : 'Changer le mot de passe'}
          </button>
        </form>
      </div>
    </div>
  )
}
