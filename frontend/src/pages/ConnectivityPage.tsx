import { useEffect, useState, type FormEvent } from 'react'
import {
  connectivityOverview, adminDeleteEmailPassword, adminSetEmailPassword, adminTestEmailConnector,
  adminUpdateEmailConnector, prepareConnectorEmail, readConnectorEmail, searchConnectorEmails,
  sendConnectorEmail, type ConnectivitySnapshot, type ConnectorCard, type EmailHit,
  whatsappContacts, addWhatsAppContact, deleteWhatsAppContact, prepareWhatsApp, type WhatsAppContact,
} from '../api/endpoints'
import { EmailAccountsPanel } from '../components/EmailAccountsPanel'
import { WebSearchPanel } from '../components/WebSearchPanel'
import { IconMail } from '../components/Icons'
import { useAuth } from '../context/AuthContext'
import { isInternationalPhone } from '../lib/whatsappPhone'

const STATE_LABEL: Record<string, string> = {
  not_configured: 'Non configuré', connected: 'Connecté', configured: 'Configuré, à tester',
  disabled: 'Désactivé', connection_error: 'Erreur de connexion', auth_expired: 'Authentification expirée',
  development: 'À développer',
}
const ROLES = ['DIRECTOR', 'HR', 'ACCOUNTANT', 'DEVELOPER', 'EMPLOYEE']
const TOOL_LABELS: Record<string, string> = {
  search_emails: 'Rechercher des emails', read_email: 'Lire un email', draft_email: 'Préparer un brouillon',
}
const EMAIL_PERMISSION_GROUPS = [
  { key: 'search_emails', title: 'Rechercher des emails', description: 'Autorise la recherche et le filtrage des messages.' },
  { key: 'read_email', title: 'Lire un email', description: 'Autorise la consultation du contenu d’un message sélectionné.' },
  { key: 'draft_email', title: 'Préparer un brouillon', description: 'Autorise la préparation d’un message sans l’envoyer.' },
  { key: 'send_email', title: 'Envoyer après confirmation explicite', description: 'Autorise l’envoi uniquement après confirmation du destinataire et du contenu exact.' },
] as const
type Draft = { to: string; subject: string; body: string }

export function ConnectivityPage() {
  const { user } = useAuth()
  const [data, setData] = useState<ConnectivitySnapshot | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [configOpen, setConfigOpen] = useState(false)
  const [webOpen, setWebOpen] = useState(false)
  const [password, setPassword] = useState('')
  const [filters, setFilters] = useState({ from: '', to: '', subject: '', since: '', before: '' })
  const [hits, setHits] = useState<EmailHit[]>([])
  const [selected, setSelected] = useState<Record<string, unknown> | null>(null)
  const [draft, setDraft] = useState<Draft>({ to: '', subject: '', body: '' })
  const [pending, setPending] = useState<{ confirmation: string; draft: Draft } | null>(null)
  const [success, setSuccess] = useState<string | null>(null)
  const [contacts, setContacts] = useState<Record<string, WhatsAppContact>>({})
  const [contact, setContact] = useState<WhatsAppContact>({ name: '', phone: '', category: '' })
  const [waTarget, setWaTarget] = useState('')
  const [waPhone, setWaPhone] = useState('')
  const [waMessage, setWaMessage] = useState('')
  const [waPreview, setWaPreview] = useState<{ url: string; phone: string; message: string; sent: false } | null>(null)
  const [waContactErrors, setWaContactErrors] = useState<{ name?: string; phone?: string }>({})
  const [waErrors, setWaErrors] = useState<{ phone?: string; message?: string }>({})

  async function refresh() { setData(await connectivityOverview()) }
  useEffect(() => { void Promise.all([refresh(), whatsappContacts()]).then(([, w]) => setContacts(w.contacts)).catch((e) => setError(e instanceof Error ? e.message : 'Connectivité indisponible.')) }, [])

  async function run<T>(action: () => Promise<T>, after?: (value: T) => void) {
    setBusy(true); setError(null); setSuccess(null)
    try { const result = await action(); after?.(result) }
    catch (e) { setError(e instanceof Error ? e.message : 'Action impossible.') }
    finally { setBusy(false) }
  }

  if (!data) return <main className="page"><div className="page-header"><h1>Connectivité</h1></div>{error ? <div className="error-banner">{error}</div> : <p>Chargement…</p>}</main>

  const email = data.email
  const isDirector = user?.role === 'DIRECTOR'
  const emailCard = data.connectors.find((item) => item.id === 'email_imap')
  const webCard = data.connectors.find((item) => item.id === 'web_search')
  // Personal mailboxes (Gmail, Outlook, Yahoo) have their own section below.
  const future = data.connectors.filter((item) => !['email_imap', 'google_gmail', 'microsoft_outlook', 'yahoo_mail', 'web_search'].includes(item.id))
  const emailReady = email.enabled && ['configured', 'connected'].includes(email.state)
  const canUseEmail = emailReady && Boolean(user && email.tool_roles.search_emails?.includes(user.role))
  const canReadEmail = emailReady && Boolean(user && email.tool_roles.read_email?.includes(user.role))
  const canSendEmail = emailReady && email.tools.draft_email
    && Boolean(user && email.tool_roles.draft_email?.includes(user.role) && email.send_roles.includes(user.role))

  function saveEmail(e: FormEvent) {
    e.preventDefault()
    void run(async () => {
      await adminUpdateEmailConnector({
        enabled: email.enabled, imap_host: email.imap_host, imap_port: email.imap_port, imap_ssl: email.imap_ssl,
        smtp_host: email.smtp_host, smtp_port: email.smtp_port, smtp_ssl: email.smtp_ssl,
        username: email.username, mailbox: email.mailbox, allowed_roles: email.allowed_roles,
        tool_roles: email.tool_roles, send_roles: email.send_roles, tools: email.tools,
      })
      if (password.trim()) await adminSetEmailPassword(password)
      setPassword('')
      setConfigOpen(false)
      await refresh()
      setSuccess('Configuration Email enregistrée.')
    })
  }

  function saveWhatsAppContact(e: FormEvent) {
    e.preventDefault()
    const next: typeof waContactErrors = {}
    if (!contact.name.trim()) next.name = 'Saisissez un nom.'
    if (!isInternationalPhone(contact.phone)) next.phone = 'Utilisez le format international, par exemple +2348012345678.'
    setWaContactErrors(next)
    if (Object.keys(next).length) return
    void run(async () => {
      const result = await addWhatsAppContact({ ...contact, name: contact.name.trim(), phone: contact.phone.trim(), category: contact.category?.trim() || null })
      setContacts(result.contacts)
      setContact({ name: '', phone: '', category: '' })
      setWaContactErrors({})
    })
  }

  function prepareWhatsAppMessage(e: FormEvent) {
    e.preventDefault()
    const next: typeof waErrors = {}
    if (!waTarget && !isInternationalPhone(waPhone)) next.phone = 'Saisissez un numéro au format + indicatif pays, par exemple +2348012345678.'
    if (!waMessage.trim()) next.message = 'Saisissez le message à prévisualiser.'
    setWaErrors(next)
    if (Object.keys(next).length) return
    void run(async () => setWaPreview(await prepareWhatsApp({ ...(waTarget ? { contact_id: waTarget } : { phone: waPhone.trim() }), message: waMessage.trim() })))
  }

  return (
    <main className="page connectivity-page">
      <div className="page-header"><h1>Connectivité</h1><p>Connexions externes, outils accessibles à l’assistant et autorisations accordées.</p></div>
      {error && <div className="error-banner" role="alert">{error}</div>}
      {success && <div className="success-banner" role="status">{success}</div>}
      <EmailAccountsPanel />
      <div className="connector-grid connector-grid-active">
        {[emailCard, webCard].filter((x): x is ConnectorCard => Boolean(x)).map((connector) => (
          <section className="card connector-card" key={connector.id}>
            <div className="connector-card-heading"><span className="connector-icon" aria-hidden>{connector.id === 'email_imap' ? '✉' : '⌕'}</span>
              <div><h2>{connector.name}</h2><span className={'connector-state connector-state-' + connector.state}>{STATE_LABEL[connector.state] ?? connector.state}</span></div>
            </div>
            <p>{connector.description}</p>
            <dl><div><dt>Protocole</dt><dd>{connector.protocol}</dd></div>
              <div><dt>Tools disponibles</dt><dd>{connector.tools.length} — {connector.tools.length ? connector.tools.join(', ') : 'Aucun'}</dd></div>
              <div><dt>Permissions</dt><dd>{connector.permissions.length ? connector.permissions.join(', ') : 'Aucune'}</dd></div></dl>
            {connector.error && <p className="connector-error">{connector.error}</p>}
            {Boolean(connector.last_status?.error) && <p className="connector-error">{String(connector.last_status?.error)}</p>}
            <div className="connector-actions">
              {connector.id === 'email_imap' ? <>
                <button className="btn btn-primary" onClick={() => setConfigOpen((v) => !v)}>Configurer</button>
                <button className="btn" disabled={busy} onClick={() => void run(async () => { await adminTestEmailConnector(); await refresh() })}>Tester la connexion</button>
                <button className="btn" disabled={busy || (!email.credentials_configured && !email.enabled)} onClick={() => void run(async () => { await adminUpdateEmailConnector({ enabled: !email.enabled }); await refresh() })}>{email.enabled ? 'Désactiver' : 'Activer'}</button>
                <button className="btn" disabled={busy || !email.credentials_configured} onClick={() => void run(adminDeleteEmailPassword, () => { void refresh() })}>Révoquer</button>
              </> : <button className="btn btn-primary" onClick={() => setWebOpen((v) => !v)}>Configurer</button>}
            </div>
            {connector.id === 'email_imap' && email.last_status?.checked_at && <small>Dernière vérification : {new Date(email.last_status.checked_at).toLocaleString()}</small>}
          </section>
        ))}
      </div>
      {isDirector && configOpen && <section className="card connector-config">
        <form onSubmit={saveEmail}>
          <header className="connector-config-header">
            <div className="connector-config-title">
              <span className="connector-config-icon" aria-hidden="true"><IconMail size={22} /></span>
              <div><h2>Compte Email générique IMAP / SMTP</h2><p>Configurez la réception, l’envoi et les accès de l’assistant.</p></div>
            </div>
            <div className="connector-config-header-actions">
              <span className={'connector-state connector-state-' + email.state}>{STATE_LABEL[email.state] ?? email.state}</span>
              <button className="btn connector-close" type="button" aria-label="Fermer la configuration Email" onClick={() => setConfigOpen(false)}>Fermer</button>
            </div>
          </header>
          <p className="connector-config-intro">Boîte partagée IMAP/SMTP gérée par l’administrateur, distincte des comptes personnels. Pour un compte Gmail ou Outlook personnel, utilisez la section « Comptes email connectés » (OAuth, lecture seule) ; aucun mot de passe Gmail ou Microsoft n’est demandé ici.</p>

          <section className="connector-form-section" aria-labelledby="connector-connection-title">
            <div className="connector-section-heading"><div><h3 id="connector-connection-title">Paramètres de connexion</h3><p>Renseignez les serveurs fournis par votre service de messagerie.</p></div></div>
            <div className="connector-settings-grid">
              <section className="connector-settings-group" aria-labelledby="connector-imap-title">
                <div className="connector-settings-heading"><span className="connector-settings-kicker">RÉCEPTION</span><h4 id="connector-imap-title">IMAP</h4><p>Accès aux messages de la boîte.</p></div>
                <div className="connector-fields-grid">
                  <label>Serveur IMAP<input className="input" required value={email.imap_host} onChange={(e) => setData({ ...data, email: { ...email, imap_host: e.target.value } })} placeholder="imap.example.com" autoComplete="off" /></label>
                  <label>Port IMAP<input className="input" type="number" min={1} max={65535} required value={email.imap_port} onChange={(e) => setData({ ...data, email: { ...email, imap_port: Number(e.target.value) } })} /></label>
                  <label>Adresse du compte<input className="input" type="email" required value={email.username} onChange={(e) => setData({ ...data, email: { ...email, username: e.target.value } })} placeholder="nom@entreprise.com" autoComplete="username" /></label>
                  <label>Dossier IMAP<input className="input" value={email.mailbox} onChange={(e) => setData({ ...data, email: { ...email, mailbox: e.target.value } })} placeholder="INBOX" /></label>
                </div>
                <label className="connector-toggle"><input type="checkbox" checked={email.imap_ssl} onChange={(e) => setData({ ...data, email: { ...email, imap_ssl: e.target.checked } })} /><span><strong>Utiliser SSL/TLS</strong><small>Connexion chiffrée dès l’ouverture de la session IMAP.</small></span></label>
              </section>

              <section className="connector-settings-group" aria-labelledby="connector-smtp-title">
                <div className="connector-settings-heading"><span className="connector-settings-kicker">ENVOI</span><h4 id="connector-smtp-title">SMTP</h4><p>Connexion au serveur de messagerie sortant.</p></div>
                <div className="connector-fields-grid">
                  <label>Serveur SMTP<input className="input" required value={email.smtp_host} onChange={(e) => setData({ ...data, email: { ...email, smtp_host: e.target.value } })} placeholder="smtp.example.com" autoComplete="off" /></label>
                  <label>Port SMTP<input className="input" type="number" min={1} max={65535} required value={email.smtp_port} onChange={(e) => setData({ ...data, email: { ...email, smtp_port: Number(e.target.value) } })} /></label>
                  <label className="connector-field-wide">Mot de passe ou mot de passe d’application<input className="input" type="password" autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} placeholder={email.credentials_configured ? 'Déjà enregistré (non affiché)' : 'Saisir le secret'} /><small>Le secret existant reste masqué. Laissez ce champ vide pour le conserver.</small></label>
                </div>
                <label className="connector-toggle"><input type="checkbox" checked={email.smtp_ssl} onChange={(e) => setData({ ...data, email: { ...email, smtp_ssl: e.target.checked } })} /><span><strong>Utiliser SSL/TLS</strong><small>Désactivez cette option pour utiliser STARTTLS, généralement sur le port 587.</small></span></label>
              </section>
            </div>
            <label className="connector-enabled-toggle"><input type="checkbox" checked={email.enabled} onChange={(e) => setData({ ...data, email: { ...email, enabled: e.target.checked } })} /><span><strong>Activer le connecteur Email</strong><small>Les outils restent soumis aux autorisations ci-dessous.</small></span></label>
          </section>

          <section className="connector-form-section" aria-labelledby="connector-permissions-title">
            <div className="connector-section-heading"><div><h3 id="connector-permissions-title">Permissions de l’assistant</h3><p>Attribuez séparément les droits de recherche, lecture, rédaction et envoi.</p></div></div>
            <div className="connector-permission-grid">
              {EMAIL_PERMISSION_GROUPS.map((group) => {
                const allowed = group.key === 'send_email' ? email.send_roles : (email.tool_roles[group.key] ?? [])
                return <fieldset className="connector-permission-card" key={group.key}>
                  <legend>{group.title}</legend><p>{group.description}</p>
                  <div className="connector-role-list" role="group" aria-label={`Rôles autorisés : ${group.title}`}>
                    {ROLES.map((role) => <label className="connector-role-option" key={role}><input type="checkbox" checked={allowed.includes(role)} onChange={(e) => {
                      const next = e.target.checked ? [...allowed, role] : allowed.filter((item) => item !== role)
                      if (group.key === 'send_email') setData({ ...data, email: { ...email, send_roles: next } })
                      else setData({ ...data, email: { ...email, tool_roles: { ...email.tool_roles, [group.key]: next } } })
                    }} /><span>{role}</span></label>)}
                  </div>
                </fieldset>
              })}
            </div>
          </section>

          <section className="connector-form-section" aria-labelledby="connector-tools-title">
            <div className="connector-section-heading"><div><h3 id="connector-tools-title">Outils accessibles au modèle local</h3><p>Choisissez les actions que le modèle peut proposer pendant une conversation.</p></div></div>
            <div className="connector-tool-list">{Object.entries(TOOL_LABELS).map(([name, label]) => <label className="connector-tool-option" key={name}><input type="checkbox" checked={email.tools[name] ?? false} onChange={(e) => setData({ ...data, email: { ...email, tools: { ...email.tools, [name]: e.target.checked } } })} /><span>{label}</span></label>)}</div>
            <p className="connector-security-note">L’envoi n’est pas un outil autonome : il exige une confirmation distincte du destinataire et du contenu exact.</p>
          </section>

          <footer className="connector-form-footer">
            <span>Les modifications ne sont enregistrées qu’après validation.</span>
            <button className="btn btn-primary" disabled={busy} type="submit">{busy ? 'Enregistrement…' : 'Enregistrer la configuration'}</button>
          </footer>
        </form>
      </section>}
      {isDirector && webOpen && <section className="connector-config"><WebSearchPanel /></section>}

      <section className="card connector-workbench whatsapp-workbench">
        <header className="whatsapp-header"><div><h2>WhatsApp Click to Chat</h2><p>Gérez vos contacts, puis préparez un lien WhatsApp avec le message à vérifier.</p></div></header>

        <section className="whatsapp-section" aria-labelledby="wa-contacts-title">
          <h3 id="wa-contacts-title">Contacts</h3>
          <form className="whatsapp-contact-form" onSubmit={saveWhatsAppContact} noValidate>
            <div className="whatsapp-contact-fields">
              <label className="whatsapp-field">Nom<input className="input" required maxLength={120} value={contact.name} aria-invalid={Boolean(waContactErrors.name)} aria-describedby={waContactErrors.name ? 'wa-contact-name-error' : undefined} onChange={(e) => { setContact({ ...contact, name: e.target.value }); setWaContactErrors({ ...waContactErrors, name: undefined }) }} />{waContactErrors.name && <small className="whatsapp-field-error" id="wa-contact-name-error">{waContactErrors.name}</small>}</label>
              <label className="whatsapp-field">Numéro international<input className="input" type="tel" required inputMode="tel" placeholder="+2348012345678" maxLength={16} pattern={"^\\+[1-9]\\d{7,14}$"} value={contact.phone} aria-invalid={Boolean(waContactErrors.phone)} aria-describedby={waContactErrors.phone ? 'wa-contact-phone-error' : undefined} onChange={(e) => { setContact({ ...contact, phone: e.target.value }); setWaContactErrors({ ...waContactErrors, phone: undefined }) }} />{waContactErrors.phone && <small className="whatsapp-field-error" id="wa-contact-phone-error">{waContactErrors.phone}</small>}</label>
              <label className="whatsapp-field"><span>Catégorie <small className="whatsapp-optional">(facultatif)</small></span><input className="input" maxLength={80} value={contact.category ?? ''} onChange={(e) => setContact({ ...contact, category: e.target.value })} /></label>
            </div>
            <div className="whatsapp-form-actions"><button className="btn btn-primary" type="submit" disabled={busy}>Ajouter le contact</button></div>
          </form>
          <div className="whatsapp-contact-list" aria-label="Contacts enregistrés">
            {Object.entries(contacts).length ? Object.entries(contacts).map(([id, item]) => <div className="whatsapp-contact-row" key={id}>
              <div className="whatsapp-contact-details"><strong>{item.name}</strong><span>{item.phone}</span>{item.category && <small>{item.category}</small>}</div>
              <div className="whatsapp-row-actions"><button className="btn" type="button" onClick={() => { setWaTarget(id); setWaPreview(null); setWaErrors({}) }}>Choisir</button><button className="btn" type="button" disabled={busy} onClick={() => void run(async () => { setContacts((await deleteWhatsAppContact(id)).contacts); setWaPreview(null); if (waTarget === id) setWaTarget('') })}>Supprimer</button></div>
            </div>) : <p className="whatsapp-empty">Aucun contact enregistré pour le moment.</p>}
          </div>
        </section>

        <section className="whatsapp-section whatsapp-compose" aria-labelledby="wa-compose-title">
          <h3 id="wa-compose-title">Préparer un message</h3>
          <form className="whatsapp-message-form" onSubmit={prepareWhatsAppMessage} noValidate>
            <label className="whatsapp-field">Destinataire<select className="input" value={waTarget} onChange={(e) => { setWaTarget(e.target.value); setWaPreview(null); setWaErrors({}) }}><option value="">Saisir un numéro</option>{Object.entries(contacts).map(([id, item]) => <option key={id} value={id}>{item.name} · {item.phone}</option>)}</select></label>
            {!waTarget && <label className="whatsapp-field">Numéro international<input className="input" type="tel" inputMode="tel" placeholder="+2348012345678" maxLength={16} pattern={"^\\+[1-9]\\d{7,14}$"} value={waPhone} aria-invalid={Boolean(waErrors.phone)} aria-describedby={waErrors.phone ? 'wa-message-phone-error' : undefined} onChange={(e) => { setWaPhone(e.target.value); setWaPreview(null); setWaErrors({ ...waErrors, phone: undefined }) }} />{waErrors.phone && <small className="whatsapp-field-error" id="wa-message-phone-error">{waErrors.phone}</small>}</label>}
            <label className="whatsapp-field">Message<textarea className="input" rows={5} required maxLength={4000} value={waMessage} aria-invalid={Boolean(waErrors.message)} aria-describedby={waErrors.message ? 'wa-message-error' : undefined} onChange={(e) => { setWaMessage(e.target.value); setWaPreview(null); setWaErrors({ ...waErrors, message: undefined }) }} /></label>
            {waErrors.message && <small className="whatsapp-field-error" id="wa-message-error">{waErrors.message}</small>}
            <div className="whatsapp-form-actions"><button className="btn btn-primary" type="submit" disabled={busy}>Préparer l'aperçu</button></div>
          </form>
        </section>

        {waPreview && <section className="whatsapp-preview" aria-labelledby="wa-preview-title" aria-live="polite">
          <div className="whatsapp-preview-heading"><div><h3 id="wa-preview-title">Aperçu du message</h3><p>Vérifiez le destinataire et le contenu avant de continuer.</p></div></div>
          <dl className="whatsapp-preview-details"><div><dt>Destinataire</dt><dd>{waTarget ? contacts[waTarget]?.name ?? 'Contact choisi' : 'Numéro manuel'}</dd></div><div><dt>Numéro</dt><dd>{waPreview.phone}</dd></div></dl>
          <div className="whatsapp-preview-content"><span>Contenu exact</span><pre>{waPreview.message}</pre></div>
          <div className="whatsapp-preview-actions"><button className="btn btn-primary" type="button" onClick={() => window.open(waPreview.url, '_blank', 'noopener,noreferrer')}>Ouvrir WhatsApp</button><button className="btn" type="button" onClick={() => setWaPreview(null)}>Annuler</button></div>
          <p className="whatsapp-send-reminder">WhatsApp s’ouvrira avec le texte prérempli. Vous devez vérifier et envoyer le message dans WhatsApp; ORBIT AI ne l’envoie pas.</p>
        </section>}
      </section>

      {future.length > 0 && <><h2 className="connectivity-section-title">Connecteurs à venir</h2><div className="connector-grid connector-grid-future">{future.map((connector) => <section className="card connector-card connector-card-future" key={connector.id}><div className="connector-card-heading"><span className="connector-icon" aria-hidden>◇</span><div><h2>{connector.name}</h2><span className="connector-state connector-state-development">À développer</span></div></div><p>{connector.description}</p><dl><div><dt>Protocole</dt><dd>{connector.protocol}</dd></div><div><dt>Tools disponibles</dt><dd>Aucun — intégration non implémentée</dd></div></dl></section>)}</div></>}

      {isDirector && canUseEmail && <section className="card connector-workbench">
        <h2>Boîte de réception</h2>
        <form className="connector-search-form" onSubmit={(e) => { e.preventDefault(); void run(async () => { const result = await searchConnectorEmails(filters); setHits(result.messages); setSelected(null) }) }}>
          <input className="input" aria-label="Expéditeur" placeholder="Expéditeur" value={filters.from} onChange={(e) => setFilters({ ...filters, from: e.target.value })} />
          <input className="input" aria-label="Destinataire" placeholder="Destinataire" value={filters.to} onChange={(e) => setFilters({ ...filters, to: e.target.value })} />
          <input className="input" aria-label="Sujet" placeholder="Sujet" value={filters.subject} onChange={(e) => setFilters({ ...filters, subject: e.target.value })} />
          <input className="input" aria-label="Depuis le" type="date" value={filters.since} onChange={(e) => setFilters({ ...filters, since: e.target.value })} />
          <input className="input" aria-label="Jusqu’au" type="date" value={filters.before} onChange={(e) => setFilters({ ...filters, before: e.target.value })} />
          <button className="btn btn-primary" disabled={busy}>Rechercher</button>
        </form>
        {hits.length > 0 && <div className="connector-mail-list">{hits.map((hit) => <button className="connector-mail-row" key={hit.uid} disabled={!canReadEmail} title={canReadEmail ? 'Lire cet email' : 'Votre rôle ne permet pas la lecture du contenu'} onClick={() => void run(async () => setSelected(await readConnectorEmail(hit.uid)))}><strong>{hit.subject || '(sans objet)'}</strong><span>{hit.from} → {hit.to}</span><time>{hit.date}</time></button>)}</div>}
        {selected && <article className="connector-mail-reader"><h3>{String(selected.subject ?? '')}</h3><p>{String(selected.from ?? '')} → {String(selected.to ?? '')} · {String(selected.date ?? '')}</p><pre>{String(selected.body ?? '')}</pre>{Array.isArray(selected.attachments) && <p>Pièces jointes : {selected.attachments.join(', ') || 'aucune'}</p>}</article>}
      </section>}

      {isDirector && canSendEmail && <section className="card connector-workbench">
        <h2>Préparer un email</h2>
        <form className="connector-draft-form" onSubmit={(e) => { e.preventDefault(); void run(async () => { const result = await prepareConnectorEmail(draft); setPending({ confirmation: result.confirmation, draft: result.draft }) }) }}>
          <label>Destinataire<input className="input" type="email" required value={draft.to} onChange={(e) => setDraft({ ...draft, to: e.target.value })} /></label>
          <label>Objet<input className="input" required value={draft.subject} onChange={(e) => setDraft({ ...draft, subject: e.target.value })} /></label>
          <label>Message<textarea className="input" rows={6} required value={draft.body} onChange={(e) => setDraft({ ...draft, body: e.target.value })} /></label>
          <button className="btn" disabled={busy}>Préparer et vérifier</button>
        </form>
        {pending && <div className="connector-confirm"><h3>Confirmez l’envoi de ce contenu exact</h3><p><strong>À :</strong> {pending.draft.to}<br /><strong>Objet :</strong> {pending.draft.subject}</p><pre>{pending.draft.body}</pre>
          <button className="btn btn-primary" disabled={busy} onClick={() => void run(async () => { const result = await sendConnectorEmail({ ...pending.draft, confirmation: pending.confirmation }); setPending(null); setError('Email envoyé à ' + result.to + '.') })}>Confirmer et envoyer</button>
          <button className="btn" onClick={() => setPending(null)}>Annuler</button></div>}
      </section>}

      {isDirector && <section className="card connector-history"><div className="toolbar"><h2>Historique des exécutions et erreurs</h2><button className="btn" disabled={busy} onClick={() => void run(refresh)}>Actualiser</button></div>
        {data.history.length ? <div className="table-wrap"><table className="data-table"><thead><tr><th>Date</th><th>Utilisateur</th><th>Connecteur / action</th><th>État</th><th>Erreur</th><th>Référence</th></tr></thead>
          <tbody>{data.history.map((entry) => <tr key={entry.correlation_id}><td>{entry.at ? new Date(entry.at).toLocaleString() : '—'}</td><td>{entry.user ?? '—'}</td><td>{entry.connector} · {entry.action}</td><td>{entry.status}</td><td>{entry.error ?? '—'}</td><td>{entry.correlation_id.slice(0, 8)}</td></tr>)}</tbody></table></div>
          : <p className="muted">Aucune exécution journalisée.</p>}
      </section>}
    </main>
  )
}
