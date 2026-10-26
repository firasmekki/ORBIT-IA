import type { EmailResults } from '../types'
import { formatBytes, formatEmailDate, safeExternalLink, senderLabel } from '../lib/emailAccounts'

/** Mailbox answer cards (app/agent/email_turn.py). Facts come from the
 * mailbox as returned by the provider; the local model's output is shown
 * apart, labelled, with each deadline's exact quote. */
export function EmailResultsBlock({ results }: { results: EmailResults }) {
  if (results.expired) {
    return <div className="email-results email-results-expired">Résultats d’emails expirés (politique de rétention).</div>
  }
  const { account, query, items, report, notices } = results
  const timeZone = query?.timezone
  return (
    <section className="email-results" aria-label="Emails">
      <header className="email-results-header">
        <span className="email-results-account">{account?.provider_label} · {account?.email_address}</span>
        <span className="email-results-scope">
          {query?.period ? `${query.period} · ` : ''}{query?.unread_only ? 'non lus · ' : ''}
          {items.length} affiché(s) sur {results.total_examined} · limite {query?.limit}{query?.limit_is_default ? ' (par défaut)' : ''}
        </span>
        <span className="email-results-readonly">Lecture seule · IA locale</span>
      </header>

      {report && (
        <div className="email-report">
          <div className="email-report-block">
            <h4>Faits extraits</h4>
            <p>{report.facts.examined} email(s) examiné(s) · {report.facts.unread} non lu(s) · {report.facts.with_attachments} avec pièce(s) jointe(s)</p>
          </div>
          <div className="email-report-block email-report-ai">
            <h4>Synthèse de l’IA locale</h4>
            <p>{report.ai.summarized} résumé(s) · {report.ai.needs_action} demandent une action</p>
            {report.ai.deadlines.length > 0 && <ul>{report.ai.deadlines.map((d, i) => (
              <li key={i}><strong>{d.text}</strong> — {d.subject || '(sans objet)'} <q>{d.evidence}</q></li>
            ))}</ul>}
          </div>
          {report.suggestions.length > 0 && <div className="email-report-block email-report-suggestions">
            <h4>Suggestions</h4>
            <ul>{report.suggestions.map((s, i) => <li key={i}>{s}</li>)}</ul>
          </div>}
        </div>
      )}

      <ol className="email-cards">
        {items.map((card) => {
          const link = safeExternalLink(card.web_link)
          const facts = card.facts
          return (
            <li className={`email-card email-card-${card.status}`} key={card.ref}>
              <div className="email-card-facts">
                <div className="email-card-subject">
                  <strong>{facts.subject || '(sans objet)'}</strong>
                  {facts.is_unread && <span className="email-badge">non lu</span>}
                </div>
                <div className="email-card-meta">
                  <span title={facts.sender?.address}>{senderLabel(facts.sender)}</span>
                  <time dateTime={facts.received_at ?? undefined}>{formatEmailDate(facts.received_at, timeZone)}</time>
                </div>
                {facts.attachments && facts.attachments.length > 0 && (
                  <div className="email-card-attachments">
                    Pièces jointes : {facts.attachments.map((a) => `${a.filename ?? 'sans nom'}${a.size_bytes != null ? ` (${formatBytes(a.size_bytes)})` : ''}`).join(', ')}
                  </div>
                )}
                {!card.ai && facts.snippet && <p className="email-card-snippet">{facts.snippet}</p>}
                {link && <a className="email-card-link" href={link} target="_blank" rel="noopener noreferrer">Ouvrir l’original</a>}
              </div>
              {card.ai && (
                <div className="email-card-ai">
                  <span className="email-card-ai-label">Résumé IA (local)</span>
                  {card.ai.summary && <p>{card.ai.summary}</p>}
                  {card.ai.requests.length > 0 && <><h5>Demandes</h5><ul>{card.ai.requests.map((r, i) => <li key={i}>{r}</li>)}</ul></>}
                  {card.ai.decisions.length > 0 && <><h5>Décisions</h5><ul>{card.ai.decisions.map((r, i) => <li key={i}>{r}</li>)}</ul></>}
                  {card.deadlines.length > 0 && <><h5>Échéances</h5><ul>{card.deadlines.map((d, i) => <li key={i}><strong>{d.text}</strong> <q>{d.evidence}</q></li>)}</ul></>}
                  {card.suggestions.length > 0 && <><h5>Actions suggérées</h5><ul>{card.suggestions.map((s, i) => <li key={i}>{s}</li>)}</ul></>}
                </div>
              )}
              {card.note && <p className="email-card-note">{card.note}</p>}
              {card.error && <p className="email-card-note">{card.error}</p>}
            </li>
          )
        })}
      </ol>
      {notices?.map((n, i) => <p className="email-results-notice" key={i}>{n}</p>)}
    </section>
  )
}
