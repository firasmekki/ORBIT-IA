import { Fragment, useEffect, useState } from 'react'
import { listAuditLogs } from '../api/endpoints'
import { DecisionBadge, RoleBadge } from '../components/Badges'
import type { AuditLogOut } from '../types'

const PAGE_SIZE = 40

function formatMetadataKey(key: string): string {
  return key.replace(/_/g, ' ')
}

export function AuditLogsPage() {
  const [items, setItems] = useState<AuditLogOut[]>([])
  const [total, setTotal] = useState(0)
  const [decision, setDecision] = useState<'' | 'ALLOW' | 'DENY'>('')
  const [offset, setOffset] = useState(0)
  const [loading, setLoading] = useState(true)
  const [expandedId, setExpandedId] = useState<string | null>(null)

  useEffect(() => {
    setOffset(0)
    load(0, decision)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [decision])

  async function load(nextOffset: number, currentDecision: '' | 'ALLOW' | 'DENY') {
    setLoading(true)
    try {
      const page = await listAuditLogs({
        limit: PAGE_SIZE,
        offset: nextOffset,
        decision: currentDecision || undefined,
      })
      if (nextOffset === 0) setItems(page.items)
      else setItems((prev) => [...prev, ...page.items])
      setTotal(page.total)
      setOffset(nextOffset)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="page">
      <div className="page-header">
        <h1>Journal d'audit</h1>
        <p>
          Trace immuable de chaque accès et refus : connexions, lectures de documents, appels d'outils MCP et
          messages envoyés à l'assistant. Réservé au rôle Directeur.
        </p>
      </div>

      <div className="filter-bar">
        <select className="input" style={{ width: 200 }} value={decision} onChange={(e) => setDecision(e.target.value as '' | 'ALLOW' | 'DENY')}>
          <option value="">Toutes les décisions</option>
          <option value="ALLOW">Autorisé uniquement</option>
          <option value="DENY">Refusé uniquement</option>
        </select>
        <span style={{ color: 'var(--text-muted)', fontSize: 13 }}>
          {total} événement{total > 1 ? 's' : ''}
        </span>
      </div>

      <div className="card">
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                <th>Horodatage</th>
                <th>Utilisateur</th>
                <th>Rôle</th>
                <th>Action</th>
                <th>Ressource</th>
                <th>Décision</th>
                <th>Motif</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {items.map((log) => {
                const hasDetail = Boolean(log.extra && Object.keys(log.extra).length > 0)
                const expanded = expandedId === log.id
                return (
                  <Fragment key={log.id}>
                    <tr
                      style={hasDetail ? { cursor: 'pointer' } : undefined}
                      onClick={() => hasDetail && setExpandedId(expanded ? null : log.id)}
                    >
                      <td className="mono" style={{ whiteSpace: 'nowrap', fontSize: 12 }}>
                        {new Date(log.created_at).toLocaleString('fr-FR')}
                      </td>
                      <td>{log.username ?? '—'}</td>
                      <td>{log.role ? <RoleBadge role={log.role} /> : '—'}</td>
                      <td className="mono" style={{ fontSize: 12 }}>
                        {log.action}
                      </td>
                      <td className="mono" style={{ fontSize: 11.5, color: 'var(--text-muted)' }}>
                        {log.resource_type ? `${log.resource_type}${log.resource_id ? `#${log.resource_id.slice(0, 8)}` : ''}` : '—'}
                      </td>
                      <td>
                        <DecisionBadge decision={log.decision} />
                      </td>
                      <td style={{ fontSize: 12.5, color: 'var(--text-muted)', maxWidth: 280 }}>{log.reason ?? '—'}</td>
                      <td style={{ fontSize: 11.5, color: 'var(--accent-strong)', whiteSpace: 'nowrap' }}>
                        {hasDetail ? (expanded ? 'Masquer ▲' : 'Détails ▼') : ''}
                      </td>
                    </tr>
                    {expanded && (
                      <tr>
                        <td colSpan={8} style={{ background: 'var(--surface-alt)', padding: '10px 14px' }}>
                          <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px 24px', fontSize: 12 }}>
                            <div>
                              <strong style={{ color: 'var(--text-muted)' }}>Audit ID : </strong>
                              <span className="mono">{log.id}</span>
                            </div>
                            {log.extra &&
                              Object.entries(log.extra).map(([key, value]) => (
                                <div key={key}>
                                  <strong style={{ color: 'var(--text-muted)' }}>{formatMetadataKey(key)} : </strong>
                                  <span className="mono">
                                    {typeof value === 'object' ? JSON.stringify(value) : String(value)}
                                  </span>
                                </div>
                              ))}
                          </div>
                        </td>
                      </tr>
                    )}
                  </Fragment>
                )
              })}
            </tbody>
          </table>
        </div>
        {items.length === 0 && !loading && <div className="empty-state">Aucun événement pour ce filtre.</div>}
        {loading && (
          <div className="centered-spinner">
            <div className="spinner" />
          </div>
        )}
      </div>

      {items.length < total && !loading && (
        <div style={{ textAlign: 'center', marginTop: 16 }}>
          <button className="btn" onClick={() => load(offset + PAGE_SIZE, decision)}>
            Charger plus
          </button>
        </div>
      )}
    </div>
  )
}
