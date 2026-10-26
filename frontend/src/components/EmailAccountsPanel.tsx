import { useEffect, useState } from 'react'
import {
  disconnectEmailAccount, emailAccounts, readEmailMessage, refreshEmailAccountStatus, searchEmailAccount,
  startEmailOAuth, testEmailAccount, updateEmailAccount,
  type EmailAccount, type EmailListItem, type EmailProviderId, type EmailProviderStatus,
} from '../api/endpoints'
import {
  EMAIL_STATUS_LABEL, PROVIDER_LABEL, TOKEN_STATE_LABEL, formatBytes, formatEmailDate, oauthOutcomeMessage,
  safeExternalLink, senderLabel,
} from '../lib/emailAccounts'

type Reader = Awaited<ReturnType<typeof readEmailMessage>>['message']

/** "Comptes email connectés": each user's own Gmail / Outlook mailboxes,
 * read-only. Yahoo is listed but stays disabled until Yahoo approves access. */
export function EmailAccountsPanel() {
  const [accounts, setAccounts] = useState<EmailAccount[]>([])
  const [providers, setProviders] = useState<EmailProviderStatus[]>([])
  const [busy, setBusy] = useState(false)
  // ?email_oauth=<code>&provider=<p>: where the provider's redirect lands (a code, never a provider message)
  const [returned] = useState(() => {
    const params = new URLSearchParams(window.location.search)
    const provider = params.get('provider')
    return oauthOutcomeMessage(params.get('email_oauth'), provider ? PROVIDER_LABEL[provider] : undefined)
  })
  const [error, setError] = useState<string | null>(returned && !returned.ok ? returned.text : null)
  const [notice, setNotice] = useState<string | null>(returned?.ok ? returned.text : null)
  const [selected, setSelected] = useState('')
  const [filters, setFilters] = useState({ text: '', from: '', since: '', unread_only: false })
  const [hits, setHits] = useState<EmailListItem[]>([])
  const [cursor, setCursor] = useState<string | null>(null)
  const [reader, setReader] = useState<Reader | null>(null)

  async function reload() {
    const result = await emailAccounts()
    setAccounts(result.accounts)
    setProviders(result.providers)
    setSelected((current) => current && result.accounts.some((a) => a.id === current)
      ? current : result.accounts.find((a) => a.is_primary)?.id ?? result.accounts[0]?.id ?? '')
  }

  useEffect(() => {
    if (new URLSearchParams(window.location.search).has('email_oauth')) {
      window.history.replaceState({}, '', window.location.pathname)
    }
    void reload().catch((e) => setError(e instanceof Error ? e.message : 'Comptes email indisponibles.'))
  }, [])

  async function run<T>(action: () => Promise<T>, after?: (value: T) => void) {
    setBusy(true); setError(null); setNotice(null)
    try { after?.(await action()) }
    catch (e) { setError(e instanceof Error ? e.message : 'Action impossible.') }
    finally { setBusy(false) }
  }

  function connect(provider: EmailProviderId, reconnect?: string) {
    void run(async () => { window.location.assign((await startEmailOAuth(provider, reconnect)).url) })
  }

  function replace(account: EmailAccount) {
    setAccounts((list) => list.map((a) => (a.id === account.id ? account : a)))
  }

  function search(next?: string | null) {
    if (!selected) return
    void run(async () => {
      const result = await searchEmailAccount(selected, {
        text: filters.text || undefined, from: filters.from || undefined, since: filters.since || undefined,
        unread_only: filters.unread_only, cursor: next ?? undefined,
      })
      setHits(next ? [...hits, ...result.items] : result.items)
      setCursor(result.next_cursor)
      if (!next) setReader(null)
    })
  }

  const usable = accounts.filter((a) => a.is_enabled && a.read_permission_granted)

  return (
    <section className="card email-accounts" aria-labelledby="email-accounts-title">
      <header className="email-accounts-header">
        <div>
          <h2 id="email-accounts-title">Comptes email connectés</h2>
          <p>Vos boîtes personnelles, en <strong>lecture seule</strong> : ORBIT recherche, affiche et résume vos emails
            avec le modèle IA local, mais n’envoie, ne modifie et ne supprime jamais rien. Chaque compte n’est visible que par vous.</p>
        </div>
        <button className="btn" disabled={busy} onClick={() => void run(reload)}>Actualiser</button>
      </header>
      {error && <div className="error-banner" role="alert">{error}</div>}
      {notice && <div className="success-banner" role="status">{notice}</div>}

      <div className="email-provider-buttons">
        {providers.map((p) => (
          <div className="email-provider" key={p.provider}>
            <button className="btn btn-primary" disabled={busy || !p.enabled || !p.configured} onClick={() => connect(p.provider)}>
              Connecter {p.label}
            </button>
            <small className={`connector-state connector-state-${p.configured ? 'configured' : 'not_configured'}`}>
              {EMAIL_STATUS_LABEL[p.status] ?? p.status}{p.pending ? ' · autorisation en cours' : ''}
            </small>
            {!p.configured && p.issues.length > 0 && <small className="connector-error">{p.issues.join(' · ')}</small>}
            <small className="muted">{p.permission}</small>
          </div>
        ))}
      </div>

      {accounts.length === 0
        ? <p className="muted">Aucun compte connecté. L’autorisation est demandée par Google ou Microsoft : ORBIT ne demande jamais votre mot de passe.</p>
        : <div className="email-account-list">
          {accounts.map((a) => (
            <article className="email-account-row" key={a.id}>
              <div className="email-account-main">
                <strong>{a.provider_label} · {a.email_address}</strong>
                {a.display_name && <span>{a.display_name}</span>}
                <span className={`connector-state connector-state-${a.status === 'connected' ? 'connected' : 'connection_error'}`}>
                  {a.status_label ?? EMAIL_STATUS_LABEL[a.status] ?? a.status}{a.is_primary ? ' · Principal' : ''}{a.is_enabled ? '' : ' · Désactivé'}
                </span>
                <small>Permissions : {a.permission} · {TOKEN_STATE_LABEL[a.token_state] ?? a.token_state}</small>
                <small>Dernière vérification : {a.last_checked_at ? formatEmailDate(a.last_checked_at) : 'jamais'}
                  {a.account_kind === 'work_school' ? ' · compte professionnel' : a.account_kind === 'personal' ? ' · compte personnel' : ''}</small>
                {a.error && <small className="connector-error">{a.error.category} : {a.error.message}</small>}
              </div>
              <div className="email-account-actions">
                <button className="btn" disabled={busy} onClick={() => void run(() => testEmailAccount(a.id), (r) => replace(r.account))}>Tester</button>
                <button className="btn" disabled={busy} onClick={() => void run(() => refreshEmailAccountStatus(a.id), (r) => replace(r.account))}>Actualiser le statut</button>
                {['authorization_required', 'token_expired_or_revoked', 'policy_blocked'].includes(a.status)
                  && <button className="btn btn-primary" disabled={busy} onClick={() => connect(a.provider, a.id)}>Reconnecter</button>}
                {!a.is_primary && <button className="btn" disabled={busy} onClick={() => void run(() => updateEmailAccount(a.id, { is_primary: true }), (r) => setAccounts(r.accounts))}>Définir principal</button>}
                <button className="btn" disabled={busy} onClick={() => void run(() => updateEmailAccount(a.id, { is_enabled: !a.is_enabled }), (r) => setAccounts(r.accounts))}>{a.is_enabled ? 'Désactiver' : 'Activer'}</button>
                <button className="btn" disabled={busy} onClick={() => void run(() => disconnectEmailAccount(a.id), (r) => {
                  setAccounts(r.accounts)
                  setNotice(r.revocation === 'revoked'
                    ? 'Compte déconnecté : l’autorisation a été révoquée chez le fournisseur et les jetons locaux supprimés.'
                    : `Compte déconnecté et jetons locaux supprimés. Retirez aussi l’accès d’ORBIT chez le fournisseur${r.manual_revoke_url ? ` : ${r.manual_revoke_url}` : '.'}`)
                })}>Déconnecter</button>
              </div>
            </article>
          ))}
        </div>}

      {usable.length > 0 && <>
        <p className="email-accounts-help">Astuce : demandez directement dans le chat « Résume mes cinq derniers emails » ou
          « Quels emails demandent une action de ma part ? ». Sans précision, le compte principal est utilisé.</p>
        <form className="connector-search-form" onSubmit={(e) => { e.preventDefault(); search() }}>
          <select className="input" aria-label="Compte email" value={selected} onChange={(e) => { setSelected(e.target.value); setHits([]); setReader(null) }}>
            {usable.map((a) => <option key={a.id} value={a.id}>{a.provider_label} · {a.email_address}</option>)}
          </select>
          <input className="input" placeholder="Mots-clés" maxLength={200} value={filters.text} onChange={(e) => setFilters({ ...filters, text: e.target.value })} />
          <input className="input" placeholder="Expéditeur" maxLength={254} value={filters.from} onChange={(e) => setFilters({ ...filters, from: e.target.value })} />
          <input className="input" type="date" aria-label="Depuis le" value={filters.since} onChange={(e) => setFilters({ ...filters, since: e.target.value })} />
          <label className="connector-toggle-inline"><input type="checkbox" checked={filters.unread_only} onChange={(e) => setFilters({ ...filters, unread_only: e.target.checked })} /> Non lus</label>
          <button className="btn btn-primary" disabled={busy}>Rechercher</button>
        </form>
        {hits.length > 0 && <div className="connector-mail-list">{hits.map((hit) => (
          <button className="connector-mail-row" key={hit.ref} disabled={busy} onClick={() => void run(() => readEmailMessage(selected, hit.ref), (r) => setReader(r.message))}>
            <strong>{hit.subject || '(sans objet)'}{hit.is_unread ? ' · non lu' : ''}</strong>
            <span>{senderLabel(hit.sender)} · {formatEmailDate(hit.received_at)}</span>
            {hit.snippet && <span>{hit.snippet}</span>}
          </button>
        ))}</div>}
        {cursor && <button className="btn" disabled={busy} onClick={() => search(cursor)}>Plus de résultats</button>}
        {reader && <article className="connector-mail-reader">
          <h3>{reader.subject || '(sans objet)'}</h3>
          <p>{senderLabel(reader.sender)} · {formatEmailDate(reader.received_at)}
            {safeExternalLink(reader.web_link) && <> · <a href={safeExternalLink(reader.web_link)!} target="_blank" rel="noopener noreferrer">Ouvrir l’original</a></>}</p>
          {reader.attachments.length > 0 && <p>Pièces jointes (non téléchargées) : {reader.attachments.map((f) => `${f.filename ?? 'sans nom'}${f.size_bytes != null ? ` (${formatBytes(f.size_bytes)})` : ''}`).join(', ')}</p>}
          <pre>{reader.body_text || '(aucun corps lisible)'}</pre>
          {reader.body_truncated && <small className="muted">Contenu tronqué : ouvrez l’original pour le lire en entier.</small>}
        </article>}
        <p className="muted">Lecture à la demande : le contenu des emails n’est ni indexé ni conservé, les pièces jointes ne sont pas téléchargées.</p>
      </>}
    </section>
  )
}
