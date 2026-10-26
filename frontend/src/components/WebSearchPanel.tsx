import { useEffect, useState, type FormEvent } from 'react'
import {
  adminDeleteWebSearchKey,
  adminSetWebSearchKey,
  adminTestWebSearch,
  adminUpdateWebSearch,
  adminWebSearch,
} from '../api/endpoints'
import type { WebSearchAdmin, WebSearchTestResult } from '../types'
import { connectionLabel, safeHttpUrl } from '../lib/webSearch'

// Director only. Settings of the web search; API keys are WRITE-ONLY: typed
// once, sent to the server (stored encrypted, or read from .env), never shown
// again - the page only knows "configured / not configured".

const ERROR_KIND_LABEL: Record<string, string> = {
  config: 'configuration',
  auth: 'authentification (clé refusée)',
  quota: 'quota / limite de débit',
  timeout: 'délai dépassé',
  network: 'réseau',
  provider: 'erreur du fournisseur',
  bad_response: 'réponse illisible',
}

export function WebSearchPanel() {
  const [state, setState] = useState<WebSearchAdmin | null>(null)
  const [form, setForm] = useState<Partial<WebSearchAdmin>>({})
  const [keyInput, setKeyInput] = useState('')
  const [test, setTest] = useState<WebSearchTestResult | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    adminWebSearch()
      .then((s) => {
        setState(s)
        setForm(s)
      })
      .catch((err) => setError(err instanceof Error ? err.message : 'Paramètres indisponibles.'))
  }, [])

  async function run<T>(action: () => Promise<T>, after?: (value: T) => void) {
    setBusy(true)
    setError(null)
    setSaved(false)
    try {
      const value = await action()
      after?.(value)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Action impossible.')
    } finally {
      setBusy(false)
    }
  }

  function save(e: FormEvent) {
    e.preventDefault()
    void run(
      () =>
        adminUpdateWebSearch({
          enabled: form.enabled,
          provider: form.provider,
          trigger: form.trigger,
          max_results: Number(form.max_results),
          fetch_pages: Number(form.fetch_pages),
          timeout_seconds: Number(form.timeout_seconds),
          language: form.language,
          country: (form.country ?? '').toUpperCase(),
          cache_ttl_seconds: Number(form.cache_ttl_seconds),
          max_per_hour: Number(form.max_per_hour),
          google_engine_id: form.google_engine_id,
          searxng_url: form.searxng_url,
        }),
      (s) => {
        setState(s)
        setForm(s)
        setSaved(true)
      },
    )
  }

  if (!state) {
    return (
      <section className="card engine-panel">
        <h2>Recherche Web</h2>
        {error ? <div className="error-banner">{error}</div> : <p className="muted">Chargement…</p>}
      </section>
    )
  }

  const provider = form.provider ?? state.provider
  const key = state.keys[provider]
  const note = state.providers.find((p) => p.id === provider)?.note
  const last = state.last_status

  return (
    <section className="card engine-panel websearch-panel" aria-label="Recherche Web">
      <div className="engine-panel-head">
        <h2>Recherche Web</h2>
        <span className={`engine-state ${state.enabled ? 'engine-state-ready' : 'engine-state-sleeping'}`}>
          {state.enabled ? 'Activée' : 'Désactivée'}
        </span>
      </div>
      <p className="muted">
        La réponse reste rédigée par le modèle local. Seule une requête courte, tirée de la question (jamais vos
        documents ni la conversation), est envoyée au fournisseur.
      </p>
      {error && <div className="error-banner">{error}</div>}

      <form className="websearch-form" onSubmit={save}>
        <label className="workspace-sharing-option">
          <input type="checkbox" checked={Boolean(form.enabled)} onChange={(e) => setForm({ ...form, enabled: e.target.checked })} />
          Activer la recherche Web
        </label>
        <div className="websearch-grid">
          <label>
            Fournisseur
            <select className="input" value={provider} onChange={(e) => setForm({ ...form, provider: e.target.value })}>
              {state.providers.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            Déclenchement
            <select className="input" value={form.trigger} onChange={(e) => setForm({ ...form, trigger: e.target.value as 'auto' | 'manual' })}>
              <option value="auto">Automatique (quand la question l’exige : actualité, réglementation, comparaison)</option>
              <option value="manual">Sur demande explicite seulement (« cherche sur internet… »)</option>
            </select>
          </label>
          <label>
            Résultats par recherche
            <input className="input" type="number" min={1} max={10} value={form.max_results ?? 5} onChange={(e) => setForm({ ...form, max_results: Number(e.target.value) })} />
          </label>
          <label>
            Pages lues par recherche
            <input className="input" type="number" min={0} max={5} value={form.fetch_pages ?? 3} onChange={(e) => setForm({ ...form, fetch_pages: Number(e.target.value) })} />
          </label>
          <label>
            Délai maximal (s)
            <input className="input" type="number" min={2} max={60} value={form.timeout_seconds ?? 10} onChange={(e) => setForm({ ...form, timeout_seconds: Number(e.target.value) })} />
          </label>
          <label>
            Langue
            <select className="input" value={form.language ?? 'fr'} onChange={(e) => setForm({ ...form, language: e.target.value })}>
              <option value="fr">Français</option>
              <option value="en">Anglais</option>
              <option value="ar">Arabe</option>
            </select>
          </label>
          <label>
            Pays (code Brave, vide ou ALL = tous ; TN non proposé par Brave)
            <input className="input" maxLength={3} value={form.country ?? ''} onChange={(e) => setForm({ ...form, country: e.target.value })} placeholder="FR" />
          </label>
          <label>
            Cache (secondes)
            <input className="input" type="number" min={0} max={86400} value={form.cache_ttl_seconds ?? 3600} onChange={(e) => setForm({ ...form, cache_ttl_seconds: Number(e.target.value) })} />
          </label>
          <label>
            Recherches max. par heure
            <input className="input" type="number" min={1} max={1000} value={form.max_per_hour ?? 60} onChange={(e) => setForm({ ...form, max_per_hour: Number(e.target.value) })} />
          </label>
          {provider === 'google' && (
            <label>
              Identifiant du moteur Google (cx)
              <input className="input" value={form.google_engine_id ?? ''} onChange={(e) => setForm({ ...form, google_engine_id: e.target.value })} />
            </label>
          )}
          {provider === 'searxng' && (
            <label>
              URL de l’instance SearXNG
              <input className="input" type="url" value={form.searxng_url ?? ''} onChange={(e) => setForm({ ...form, searxng_url: e.target.value })} placeholder="http://searxng:8080" />
            </label>
          )}
        </div>
        <div className="engine-actions">
          <button type="submit" className="btn btn-primary" disabled={busy}>
            Enregistrer
          </button>
          {saved && <span className="muted">Enregistré.</span>}
        </div>
      </form>

      {note && <p className="workspace-hint">{note}</p>}

      {provider !== 'searxng' && <div className="websearch-key">
        <strong>Clé API {state.providers.find((p) => p.id === provider)?.label}</strong> :{' '}
        {key?.configured ? (
          <span className="rule-allow">configurée {key.source === 'env' ? '(fichier .env du serveur)' : '(saisie ici, chiffrée)'}</span>
        ) : (
          <span className="rule-deny">absente</span>
        )}
        <form
          className="access-rules-form"
          onSubmit={(e) => {
            e.preventDefault()
            const value = keyInput
            setKeyInput('')
            void run(() => adminSetWebSearchKey(provider, value), setState)
          }}
        >
          <input
            className="input"
            type="password"
            autoComplete="new-password"
            placeholder={key?.configured ? 'Remplacer la clé…' : 'Coller la clé API…'}
            value={keyInput}
            onChange={(e) => setKeyInput(e.target.value)}
            aria-label="Clé API (jamais réaffichée)"
          />
          <button type="submit" className="btn" disabled={busy || keyInput.trim().length < 8}>
            Enregistrer la clé
          </button>
          {key?.source === 'admin' && (
            <button type="button" className="btn engine-danger" disabled={busy} onClick={() => void run(() => adminDeleteWebSearchKey(provider), setState)}>
              Supprimer la clé
            </button>
          )}
        </form>
        <p className="workspace-hint">
          La clé n’est jamais réaffichée ni écrite dans les journaux. Une clé définie dans .env est prioritaire.
        </p>
      </div>}

      <div className="engine-actions">
        <button type="button" className="btn" disabled={busy} onClick={() => void run(adminTestWebSearch, (t) => setTest(t))}>
          Tester la connexion
        </button>
        {test && (
          <span className={test.connection === 'connected' ? 'rule-allow' : 'rule-deny'} role="status">
            {connectionLabel(test.connection)} ({test.provider_label})
            {test.connection === 'connected'
              ? ` : ${test.results} résultat(s) pour la requête de test.`
              : test.error
                ? ` : ${test.error}`
                : ''}
          </span>
        )}
      </div>
      {test && test.sample.length > 0 && (
        <ul className="workspace-hint">
          {test.sample.map((s) =>
            safeHttpUrl(s.url) ? (
              <li key={s.url}>
                <a href={safeHttpUrl(s.url) ?? undefined} target="_blank" rel="noopener noreferrer">
                  {s.title}
                </a>
              </li>
            ) : null,
          )}
        </ul>
      )}
      {test?.unresponsive_engines && test.unresponsive_engines.length > 0 && (
        <p className="workspace-hint" role="status">Moteurs externes indisponibles : {test.unresponsive_engines.join(', ')}</p>
      )}
      {last?.status && (
        <p className="workspace-hint">
          Dernier appel réel : {last.provider} — {last.connection ? connectionLabel(last.connection) : last.status}
          {last.error_kind ? ` (${ERROR_KIND_LABEL[last.error_kind] ?? last.error_kind})` : ''} — {last.at?.replace('T', ' ')}
          {last.test ? ' (test)' : ''}
        </p>
      )}
    </section>
  )
}
