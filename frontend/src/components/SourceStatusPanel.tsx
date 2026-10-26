import { useCallback, useEffect, useState } from 'react'
import {
  getSourceStatus,
  listInventory,
  requestSourceScan,
  requestVisualAnalysis,
  retryFailedDocuments,
  setSourceEnabled,
} from '../api/endpoints'
import type { InventoryItem, SourceStatus, Workspace } from '../types'

const nf = new Intl.NumberFormat('fr-FR')

const ISSUE_LABELS: Record<string, string> = {
  no_text_image: 'Images sans texte détectable',
  no_text_scan: 'PDF scannés sans texte',
  ocr_failed: 'OCR échoué',
  corrupt: 'Fichiers endommagés ou format inattendu',
  empty: 'Documents vides',
  embedding: 'Index sémantique impossible (texte consultable)',
  unsupported: 'Contenu non pris en charge',
  inconnu: 'Cause non enregistrée',
}

/** Real state of a server folder source: every counter comes from the
 * inventory and the documents table (nothing simulated). Owner only. */
export function SourceStatusPanel({ workspace, onChanged }: { workspace: Workspace; onChanged?: () => void }) {
  const [status, setStatus] = useState<SourceStatus | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [detailState, setDetailState] = useState<InventoryItem['state'] | null>(null)
  const [items, setItems] = useState<InventoryItem[]>([])

  const refresh = useCallback(async () => {
    try {
      setStatus(await getSourceStatus(workspace.id))
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'État indisponible.')
    }
  }, [workspace.id])

  useEffect(() => {
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | undefined
    async function poll() {
      await refresh()
      if (!cancelled) timer = setTimeout(poll, 4000)
    }
    void poll()
    return () => {
      cancelled = true
      if (timer) clearTimeout(timer)
    }
  }, [refresh])

  async function act(action: () => Promise<unknown>) {
    setBusy(true)
    try {
      await action()
      await refresh()
      onChanged?.()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Action impossible.')
    } finally {
      setBusy(false)
    }
  }

  async function showDetails(state: InventoryItem['state']) {
    if (detailState === state) {
      setDetailState(null)
      return
    }
    setDetailState(state)
    setItems(await listInventory(workspace.id, state, 0, 200).catch(() => []))
  }

  async function loadMore() {
    if (!detailState) return
    const more = await listInventory(workspace.id, detailState, items.length, 200).catch(() => [])
    setItems((prev) => [...prev, ...more])
  }

  const source = status?.source ?? workspace.source
  if (!source) return null
  const docs = status?.documents
  const totalDocs = docs
    ? docs.queued + docs.extracting + docs.indexing + docs.indexed + docs.failed + docs.no_text + docs.visual_queued + docs.visual_running
    : 0
  // Done = indexed, or settled without text (an image/scan is not pending).
  const percent = totalDocs ? Math.floor((((docs?.indexed ?? 0) + (docs?.no_text ?? 0) + (docs?.failed ?? 0)) * 100) / totalDocs) : 0
  const issues = status?.issues ?? {}
  const imagesWithoutText = issues.no_text_image ?? 0

  let headline: string
  if (!source.enabled) headline = '⏸ En pause — les documents déjà indexés restent consultables'
  else if (source.status === 'scanning') headline = `🟡 Analyse du dossier… ${nf.format(source.scan_seen ?? 0)} élément(s) parcouru(s)`
  else if (source.status === 'missing') headline = `🔴 ${source.error ?? 'Dossier introuvable'} — rien n'a été retiré de l'index`
  else if (source.status === 'error') headline = `🔴 Dernière analyse en erreur : ${source.error ?? 'inconnue'}`
  else if (status && !status.complete) headline = `🟡 Lecture des documents en cours (${nf.format(status.in_progress)} restant(s))`
  else headline = '🟢 À jour'

  return (
    <div className="source-panel">
      <div className="source-panel-path" title={source.host_path}>
        🖥️ {source.host_path}
      </div>
      <div className="source-panel-headline">
        {headline}
        {status?.last_synced_at && (
          <span className="workspace-count"> · dernière analyse complète {new Date(status.last_synced_at).toLocaleString('fr-FR')}</span>
        )}
      </div>
      {error && <div className="error-banner">{error}</div>}

      {status && docs && (
        <>
          <div className="source-progress" aria-label={`${percent} % indexé`}>
            <div className="source-progress-bar" style={{ width: `${percent}%` }} />
          </div>
          {/* Disk -> inventory -> documents: every file is in exactly one box. */}
          <div className="source-breakdown">
            <strong>Sur le disque : {nf.format(status.detected_files + status.excluded_files)} fichiers</strong>
            {' = '}
            {nf.format(status.detected_files)} inventoriés
            {status.excluded_directories > 0 && (
              <>
                {' + '}
                {nf.format(status.excluded_files)} dans {nf.format(status.excluded_directories)} dossier(s) technique(s) exclu(s)
                (node_modules, .git…), comptés mais non lus
              </>
            )}
            <br />
            {nf.format(status.detected_files)} inventoriés = {nf.format(totalDocs)} documents à lire + {nf.format(status.ignored)} non pris
            en charge + {nf.format(status.unreadable)} illisible(s)
          </div>
          <div className="source-grid">
            <Stat label="Indexés complètement" value={status.fully_indexed} tone="ok" />
            <Stat label="Texte extrait, index en cours" value={docs.indexing} />
            <Stat label="En attente de lecture" value={docs.queued} />
            <Stat label="Lecture / OCR en cours" value={docs.extracting} />
            <Stat label="Sans texte (images, scans)" value={docs.no_text} onClick={() => showDetails('no_text')} />
            <Stat label="En erreur" value={docs.failed} tone={docs.failed ? 'bad' : undefined} onClick={() => showDetails('failed')} />
            <Stat label="Non pris en charge" value={status.ignored} onClick={() => showDetails('ignored')} />
            <Stat label="Illisibles (réessayés)" value={status.unreadable} tone={status.unreadable ? 'bad' : undefined} onClick={() => showDetails('unreadable')} />
            <Stat label="Images décrites (vision)" value={status.visual_described} />
          </div>
          {(docs.no_text > 0 || docs.failed > 0) && (
            <div className="workspace-hint">
              {Object.entries(issues)
                .sort(([, a], [, b]) => b - a)
                .map(([kind, count]) => `${ISSUE_LABELS[kind] ?? kind} : ${nf.format(count)}`)
                .join(' · ')}
            </div>
          )}
          {status.visual_pending > 0 && (
            <div className="workspace-hint">
              👁 Analyse visuelle en cours : {nf.format(status.visual_pending)} image(s) restante(s), une à la fois.
            </div>
          )}
          {status.reasons.length > 0 && (
            <details className="workspace-ignored">
              <summary>Pourquoi certains fichiers ne sont pas indexés</summary>
              <ul>
                {status.reasons.map((r) => (
                  <li key={r.reason}>
                    {r.reason} : {nf.format(r.count)}
                  </li>
                ))}
              </ul>
            </details>
          )}
          {detailState && (
            <div className="source-details">
              <strong>
                {{ failed: 'Documents en erreur', no_text: 'Documents sans texte (images, scans)', ignored: 'Fichiers non pris en charge', unreadable: 'Fichiers illisibles' }[detailState]}
              </strong>
              <ul>
                {items.map((item) => (
                  <li key={item.relative_path}>
                    <code>{item.relative_path}</code> — {item.reason ?? 'raison inconnue'}
                  </li>
                ))}
              </ul>
              {items.length > 0 && items.length % 200 === 0 && (
                <button type="button" className="btn" onClick={loadMore}>
                  Afficher plus
                </button>
              )}
              {items.length === 0 && <div className="workspace-hint">Aucun.</div>}
            </div>
          )}
        </>
      )}

      <div className="source-actions">
        <button type="button" className="btn" disabled={busy || source.status === 'scanning'} onClick={() => act(() => requestSourceScan(workspace.id))}>
          ⟳ Analyser maintenant
        </button>
        <button type="button" className="btn" disabled={busy} onClick={() => act(() => setSourceEnabled(workspace.id, !source.enabled))}>
          {source.enabled ? '⏸ Mettre en pause' : '▶ Reprendre'}
        </button>
        {imagesWithoutText > 0 && (status?.visual_pending ?? 0) === 0 && (
          <button
            type="button"
            className="btn"
            disabled={busy}
            title="Le modèle vision décrit chaque image sans texte (photo, schéma…), une à la fois, en arrière-plan. La description devient cherchable."
            onClick={() => act(() => requestVisualAnalysis(workspace.id))}
          >
            👁 Analyser visuellement les {nf.format(imagesWithoutText)} image(s) sans texte
          </button>
        )}
        {docs && docs.failed > 0 && (
          <button type="button" className="btn" disabled={busy} onClick={() => act(() => retryFailedDocuments(workspace.id))}>
            ↻ Relancer les {nf.format(docs.failed)} échec(s)
          </button>
        )}
      </div>
    </div>
  )
}

function Stat({ label, value, tone, onClick }: { label: string; value: number; tone?: 'ok' | 'bad'; onClick?: () => void }) {
  return (
    <button
      type="button"
      className={`source-stat${tone ? ` source-stat-${tone}` : ''}`}
      onClick={onClick}
      disabled={!onClick}
      title={onClick ? 'Voir la liste' : undefined}
    >
      <span className="source-stat-value">{nf.format(value)}</span>
      <span className="source-stat-label">{label}</span>
    </button>
  )
}
