import { useEffect, useMemo, useState, type ChangeEvent, type FormEvent } from 'react'
import { useSearchParams } from 'react-router-dom'
import {
  createDocument,
  downloadDocumentFile,
  getDocument,
  getWatchStatus,
  listDocuments,
  updateDocument,
  uploadDocument,
} from '../api/endpoints'
import { ApiError } from '../api/client'
import { useAuth } from '../context/AuthContext'
import { ConfidentialityBadge } from '../components/Badges'
import { IconDocument, IconSearch } from '../components/Icons'
import type { DocumentDetail, DocumentSummary } from '../types'

const DEPARTMENTS = ['HR', 'FINANCE', 'TECH', 'GENERAL', 'EXEC']
const LEVELS = ['PUBLIC', 'INTERNAL', 'CONFIDENTIAL', 'SECRET']
const ACCEPTED_EXTENSIONS = ['.txt', '.md', '.pdf', '.xlsx', '.docx']

export function DocumentsPage() {
  const { user } = useAuth()
  const [searchParams, setSearchParams] = useSearchParams()
  const [documents, setDocuments] = useState<DocumentSummary[]>([])
  const [loading, setLoading] = useState(true)
  const [selected, setSelected] = useState<DocumentDetail | null>(null)
  const [modalError, setModalError] = useState<string | null>(null)
  const [downloading, setDownloading] = useState(false)
  const [editing, setEditing] = useState(false)
  const [editDept, setEditDept] = useState('')
  const [editLevel, setEditLevel] = useState('')
  const [editSaving, setEditSaving] = useState(false)
  const [editError, setEditError] = useState<string | null>(null)
  const [showCreate, setShowCreate] = useState(false)
  const [probeId, setProbeId] = useState('')
  const [probeResult, setProbeResult] = useState<{ ok: boolean; message: string } | null>(null)
  const [query, setQuery] = useState(searchParams.get('q') ?? '')
  const [deptFilter, setDeptFilter] = useState('')
  const [levelFilter, setLevelFilter] = useState('')
  const [watchStatus, setWatchStatus] = useState<{ enabled: boolean; department: string; confidentiality: string } | null>(null)

  useEffect(() => {
    getWatchStatus()
      .then(setWatchStatus)
      .catch(() => {})
  }, [])

  // Auto-imported files land silently in the background - poll the list
  // periodically so they show up without the user needing to refresh.
  useEffect(() => {
    if (!watchStatus?.enabled) return
    const interval = setInterval(loadDocuments, 15000)
    return () => clearInterval(interval)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [watchStatus?.enabled])

  useEffect(() => {
    const urlQuery = searchParams.get('q') ?? ''
    if (urlQuery !== query) setQuery(urlQuery)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams])

  useEffect(() => {
    setSearchParams(query ? { q: query } : {}, { replace: true })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query])

  const filteredDocuments = useMemo(() => {
    return documents.filter((d) => {
      if (query && !d.title.toLowerCase().includes(query.toLowerCase())) return false
      if (deptFilter && d.department !== deptFilter) return false
      if (levelFilter && d.confidentiality !== levelFilter) return false
      return true
    })
  }, [documents, query, deptFilter, levelFilter])

  useEffect(() => {
    loadDocuments()
  }, [])

  async function loadDocuments() {
    setLoading(true)
    try {
      const docs = await listDocuments()
      setDocuments(docs)
    } finally {
      setLoading(false)
    }
  }

  async function openDocument(id: string) {
    setModalError(null)
    setSelected(null)
    setEditing(false)
    try {
      const doc = await getDocument(id)
      setSelected(doc)
    } catch (err) {
      setModalError(err instanceof Error ? err.message : "Impossible d'ouvrir ce document.")
    }
  }

  function startEdit() {
    if (!selected) return
    setEditDept(selected.department)
    setEditLevel(selected.confidentiality)
    setEditError(null)
    setEditing(true)
  }

  async function saveEdit() {
    if (!selected) return
    setEditSaving(true)
    setEditError(null)
    try {
      const updated = await updateDocument(selected.id, { department: editDept, confidentiality: editLevel })
      setSelected(updated)
      setEditing(false)
      loadDocuments()
    } catch (err) {
      setEditError(err instanceof Error ? err.message : 'Impossible de modifier ce document.')
    } finally {
      setEditSaving(false)
    }
  }

  async function handleDownload() {
    if (!selected) return
    setDownloading(true)
    try {
      const { blob, filename } = await downloadDocumentFile(selected.id)
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = filename || selected.source_filename || selected.title
      document.body.appendChild(a)
      a.click()
      a.remove()
      URL.revokeObjectURL(url)
    } catch (err) {
      setModalError(err instanceof Error ? err.message : 'Téléchargement impossible.')
    } finally {
      setDownloading(false)
    }
  }

  async function probeAccess(e: FormEvent) {
    e.preventDefault()
    if (!probeId.trim()) return
    try {
      const doc = await getDocument(probeId.trim())
      setProbeResult({ ok: true, message: `Accès autorisé : "${doc.title}" (${doc.department} / ${doc.confidentiality}).` })
    } catch (err) {
      const message = err instanceof ApiError ? err.message : 'Erreur inconnue.'
      setProbeResult({ ok: false, message })
    }
  }

  return (
    <div className="page">
      <div className="page-header toolbar">
        <div>
          <h1>Documents</h1>
          <p>
            Cette liste est déjà filtrée côté serveur : elle ne contient que les documents que votre rôle («
            {user?.role}») est autorisé à consulter.
          </p>
        </div>
        <button className="btn" onClick={() => setShowCreate((v) => !v)}>
          {showCreate ? 'Fermer' : '+ Ajouter un document'}
        </button>
      </div>

      {watchStatus?.enabled && (
        <div className="hint-banner" style={{ marginBottom: 16, display: 'flex', alignItems: 'center', gap: 8 }}>
          <span className="badge badge-allow">Auto</span>
          Surveillance de dossier active — tout fichier déposé dans le dossier configuré est importé
          automatiquement (département {watchStatus.department}, confidentialité {watchStatus.confidentiality}),
          sans action manuelle.
        </div>
      )}

      {showCreate && (
        <CreateDocumentForm
          onCreated={() => { setShowCreate(false); loadDocuments() }}
          onRefresh={loadDocuments}
        />
      )}

      <div className="filter-bar">
        <div className="topbar-search" style={{ margin: 0, maxWidth: 320 }}>
          <IconSearch size={15} />
          <input placeholder="Filtrer par titre…" value={query} onChange={(e) => setQuery(e.target.value)} />
        </div>
        <select className="input" style={{ width: 160 }} value={deptFilter} onChange={(e) => setDeptFilter(e.target.value)}>
          <option value="">Tous les départements</option>
          {DEPARTMENTS.map((d) => (
            <option key={d} value={d}>
              {d}
            </option>
          ))}
        </select>
        <select className="input" style={{ width: 180 }} value={levelFilter} onChange={(e) => setLevelFilter(e.target.value)}>
          <option value="">Toute confidentialité</option>
          {LEVELS.map((l) => (
            <option key={l} value={l}>
              {l}
            </option>
          ))}
        </select>
      </div>

      <div className="card" style={{ marginBottom: 24 }}>
        {loading ? (
          <div className="centered-spinner">
            <div className="spinner" />
          </div>
        ) : documents.length === 0 ? (
          <div className="empty-state">Aucun document accessible pour votre rôle.</div>
        ) : filteredDocuments.length === 0 ? (
          <div className="empty-state">Aucun document ne correspond à ces filtres.</div>
        ) : (
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Titre</th>
                  <th>Département</th>
                  <th>Confidentialité</th>
                  <th>Ajouté le</th>
                </tr>
              </thead>
              <tbody>
                {filteredDocuments.map((doc) => (
                  <tr key={doc.id} className="row-clickable" onClick={() => openDocument(doc.id)}>
                    <td>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                        {doc.source_filename && (
                          <span className="file-type-tag">{fileExt(doc.source_filename)}</span>
                        )}
                        {doc.title}
                      </div>
                    </td>
                    <td>
                      <span className="dept-tag">{doc.department}</span>
                    </td>
                    <td>
                      <ConfidentialityBadge level={doc.confidentiality} />
                    </td>
                    <td>{new Date(doc.created_at).toLocaleDateString('fr-FR')}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="card card-pad">
        <h3 style={{ marginBottom: 6 }}>Tester un accès direct par identifiant</h3>
        <p style={{ color: 'var(--text-muted)', fontSize: 13, marginBottom: 12 }}>
          Même en connaissant l'identifiant exact d'un document, l'API refuse la lecture si votre rôle n'y a pas
          droit — la protection ne dépend pas de ce que montre l'interface.
        </p>
        <form onSubmit={probeAccess} style={{ display: 'flex', gap: 10 }}>
          <input
            className="input"
            placeholder="Identifiant (UUID) du document"
            value={probeId}
            onChange={(e) => setProbeId(e.target.value)}
          />
          <button className="btn" type="submit">
            Tester
          </button>
        </form>
        {probeResult && (
          <div className={probeResult.ok ? 'hint-banner' : 'error-banner'} style={{ marginTop: 12 }}>
            {probeResult.message}
          </div>
        )}
      </div>

      {(selected || modalError) && (
        <div className="modal-overlay" onClick={() => { setSelected(null); setModalError(null) }}>
          <div className="modal-card" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <div>
                <h3>{selected?.title ?? 'Accès refusé'}</h3>
                {selected && !editing && (
                  <div style={{ marginTop: 6, display: 'flex', gap: 8, alignItems: 'center' }}>
                    <span className="dept-tag">{selected.department}</span>
                    <ConfidentialityBadge level={selected.confidentiality} />
                    {selected.source_filename && (
                      <span style={{ fontSize: 11.5, color: 'var(--text-muted)' }}>{selected.source_filename}</span>
                    )}
                  </div>
                )}
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                {selected && !editing && (
                  <button className="btn" style={{ padding: '5px 12px', fontSize: 12 }} onClick={startEdit}>
                    Modifier
                  </button>
                )}
                {selected?.source_filename && !editing && (
                  <button className="btn" style={{ padding: '5px 12px', fontSize: 12 }} onClick={handleDownload} disabled={downloading}>
                    {downloading ? 'Téléchargement…' : 'Télécharger le fichier'}
                  </button>
                )}
                <button className="modal-close" onClick={() => { setSelected(null); setModalError(null); setEditing(false) }}>
                  ✕
                </button>
              </div>
            </div>
            <div className="modal-body">
              {modalError ? (
                <div className="error-banner">{modalError}</div>
              ) : editing && selected ? (
                <div style={{ whiteSpace: 'normal' }}>
                  {editError && <div className="error-banner">{editError}</div>}
                  <p style={{ fontSize: 13, color: 'var(--text-muted)', marginBottom: 14 }}>
                    Corrigez le département et/ou le niveau de confidentialité si ce document a été mal classé à
                    l'ajout. Vous ne pouvez le déplacer que vers un niveau que vous êtes vous-même autorisé à
                    consulter.
                  </p>
                  <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14, marginBottom: 16 }}>
                    <div className="field" style={{ marginBottom: 0 }}>
                      <label className="field-label">Département</label>
                      <select className="input" value={editDept} onChange={(e) => setEditDept(e.target.value)}>
                        {DEPARTMENTS.map((d) => (
                          <option key={d} value={d}>
                            {d}
                          </option>
                        ))}
                      </select>
                    </div>
                    <div className="field" style={{ marginBottom: 0 }}>
                      <label className="field-label">Confidentialité</label>
                      <select className="input" value={editLevel} onChange={(e) => setEditLevel(e.target.value)}>
                        {LEVELS.map((l) => (
                          <option key={l} value={l}>
                            {l}
                          </option>
                        ))}
                      </select>
                    </div>
                  </div>
                  <div style={{ display: 'flex', gap: 10, justifyContent: 'flex-end' }}>
                    <button type="button" className="btn" onClick={() => setEditing(false)}>
                      Annuler
                    </button>
                    <button type="button" className="btn btn-primary" onClick={saveEdit} disabled={editSaving}>
                      {editSaving ? 'Enregistrement…' : 'Enregistrer'}
                    </button>
                  </div>
                </div>
              ) : (
                <>
                  {selected?.source_filename && (
                    <div className="hint-banner" style={{ marginBottom: 14 }}>
                      Texte extrait automatiquement de « {selected.source_filename} » pour la recherche et
                      l'assistant IA. Utilisez « Télécharger le fichier » pour le document original.
                    </div>
                  )}
                  {selected?.content}
                </>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

function fileExt(filename: string): string {
  const parts = filename.split('.')
  return parts.length > 1 ? parts[parts.length - 1].toUpperCase() : 'FILE'
}

function stripExtension(filename: string): string {
  const base = filename.split('/').pop() ?? filename
  const idx = base.lastIndexOf('.')
  return idx > 0 ? base.slice(0, idx) : base
}

function isSupported(filename: string): boolean {
  const lower = filename.toLowerCase()
  return ACCEPTED_EXTENSIONS.some((ext) => lower.endsWith(ext))
}

function CreateDocumentForm({ onCreated, onRefresh }: { onCreated: () => void; onRefresh: () => void }) {
  const [mode, setMode] = useState<'file' | 'folder' | 'text'>('file')
  const [title, setTitle] = useState('')
  const [department, setDepartment] = useState(DEPARTMENTS[3])
  const [confidentiality, setConfidentiality] = useState(LEVELS[1])
  const [content, setContent] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const [folderFiles, setFolderFiles] = useState<File[]>([])
  const [skippedCount, setSkippedCount] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [batchProgress, setBatchProgress] = useState<{ done: number; total: number } | null>(null)
  const [batchFailures, setBatchFailures] = useState<{ name: string; error: string }[]>([])
  const [batchDoneSummary, setBatchDoneSummary] = useState<{ ok: number; failed: number } | null>(null)

  function handleFolderPick(e: ChangeEvent<HTMLInputElement>) {
    const all = Array.from(e.target.files ?? [])
    const supported = all.filter((f) => isSupported(f.name))
    setFolderFiles(supported)
    setSkippedCount(all.length - supported.length)
    setBatchDoneSummary(null)
    setBatchFailures([])
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    setError(null)

    if (mode === 'folder') {
      if (folderFiles.length === 0) {
        setError('Sélectionnez un dossier contenant au moins un fichier supporté.')
        return
      }
      setSaving(true)
      setBatchFailures([])
      setBatchDoneSummary(null)
      const failures: { name: string; error: string }[] = []
      for (let i = 0; i < folderFiles.length; i++) {
        const f = folderFiles[i]
        setBatchProgress({ done: i, total: folderFiles.length })
        try {
          await uploadDocument({ title: stripExtension(f.name), department, confidentiality, file: f })
        } catch (err) {
          failures.push({ name: f.name, error: err instanceof Error ? err.message : 'Erreur inconnue' })
        }
        onRefresh()
      }
      setBatchProgress({ done: folderFiles.length, total: folderFiles.length })
      setBatchFailures(failures)
      setBatchDoneSummary({ ok: folderFiles.length - failures.length, failed: failures.length })
      setSaving(false)
      if (failures.length === 0) onCreated()
      return
    }

    setSaving(true)
    try {
      if (mode === 'file') {
        if (!file) {
          setError('Sélectionnez un fichier.')
          setSaving(false)
          return
        }
        await uploadDocument({ title, department, confidentiality, file })
      } else {
        await createDocument({ title, department, confidentiality, content })
      }
      onCreated()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Impossible de créer ce document.')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="card card-pad" style={{ marginBottom: 20 }}>
      <h3 style={{ marginBottom: 12 }}>Ajouter des documents</h3>

      <div className="segmented">
        <button type="button" className={mode === 'file' ? 'active' : ''} onClick={() => setMode('file')}>
          Un fichier
        </button>
        <button type="button" className={mode === 'folder' ? 'active' : ''} onClick={() => setMode('folder')}>
          Un dossier entier
        </button>
        <button type="button" className={mode === 'text' ? 'active' : ''} onClick={() => setMode('text')}>
          Coller du texte
        </button>
      </div>

      {error && <div className="error-banner" style={{ marginTop: 14 }}>{error}</div>}

      <form onSubmit={handleSubmit} style={{ marginTop: 14 }}>
        {mode !== 'folder' && (
          <div className="field">
            <label className="field-label">Titre</label>
            <input className="input" value={title} onChange={(e) => setTitle(e.target.value)} required />
          </div>
        )}
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14 }}>
          <div className="field">
            <label className="field-label">Département</label>
            <select className="input" value={department} onChange={(e) => setDepartment(e.target.value)}>
              {DEPARTMENTS.map((d) => (
                <option key={d} value={d}>
                  {d}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label className="field-label">Confidentialité</label>
            <select className="input" value={confidentiality} onChange={(e) => setConfidentiality(e.target.value)}>
              {LEVELS.map((l) => (
                <option key={l} value={l}>
                  {l}
                </option>
              ))}
            </select>
          </div>
        </div>
        {mode === 'folder' && (
          <p style={{ fontSize: 11.5, color: 'var(--text-muted)', marginTop: -6, marginBottom: 14 }}>
            Ce département et ce niveau de confidentialité s'appliquent à tous les fichiers du dossier — pour un
            lot mélangeant plusieurs niveaux, importez-les en plusieurs fois.
          </p>
        )}

        {mode === 'file' && (
          <div className="field">
            <label className="field-label">Fichier ({ACCEPTED_EXTENSIONS.join(', ')})</label>
            <label className="file-drop">
              <IconDocument size={22} />
              <span>{file ? file.name : 'Cliquez pour choisir un fichier, ou glissez-le ici'}</span>
              <input
                type="file"
                accept={ACCEPTED_EXTENSIONS.join(',')}
                onChange={(e) => setFile(e.target.files?.[0] ?? null)}
                style={{ display: 'none' }}
                required
              />
            </label>
            <p style={{ fontSize: 11.5, color: 'var(--text-muted)', marginTop: 6 }}>
              Le texte est extrait automatiquement pour la recherche ; le fichier original reste téléchargeable
              tel quel. Taille max. 20 Mo.
            </p>
          </div>
        )}

        {mode === 'folder' && (
          <div className="field">
            <label className="field-label">Dossier ({ACCEPTED_EXTENSIONS.join(', ')})</label>
            <label className="file-drop">
              <IconDocument size={22} />
              <span>
                {folderFiles.length > 0
                  ? `${folderFiles.length} fichier(s) prêt(s)${skippedCount > 0 ? ` — ${skippedCount} ignoré(s) (format non supporté)` : ''}`
                  : 'Cliquez pour choisir un dossier'}
              </span>
              <input
                type="file"
                multiple
                ref={(el) => {
                  if (el) {
                    el.setAttribute('webkitdirectory', 'true')
                    el.setAttribute('directory', 'true')
                  }
                }}
                onChange={handleFolderPick}
                style={{ display: 'none' }}
              />
            </label>
            {folderFiles.length > 0 && (
              <ul style={{ fontSize: 12, color: 'var(--text-muted)', margin: '8px 0 0', paddingLeft: 18, maxHeight: 120, overflowY: 'auto' }}>
                {folderFiles.map((f) => (
                  <li key={f.webkitRelativePath || f.name}>
                    {stripExtension(f.name)} <span className="file-type-tag">{fileExt(f.name)}</span>
                  </li>
                ))}
              </ul>
            )}
            <p style={{ fontSize: 11.5, color: 'var(--text-muted)', marginTop: 6 }}>
              Chaque fichier est importé et indexé séparément (titre = nom de fichier) ; ils apparaîtront tous
              dans la liste et seront immédiatement utilisables par l'assistant IA, selon les permissions de
              chacun.
            </p>
            {batchProgress && (
              <div style={{ marginTop: 10 }}>
                <div style={{ height: 6, borderRadius: 100, background: 'var(--surface-alt)', overflow: 'hidden' }}>
                  <div
                    style={{
                      height: '100%',
                      background: 'var(--accent)',
                      width: `${(batchProgress.done / Math.max(batchProgress.total, 1)) * 100}%`,
                      transition: 'width 0.2s ease',
                    }}
                  />
                </div>
                <div style={{ fontSize: 11.5, color: 'var(--text-muted)', marginTop: 4 }}>
                  {batchProgress.done} / {batchProgress.total} importés
                </div>
              </div>
            )}
            {batchDoneSummary && (
              <div className={batchDoneSummary.failed === 0 ? 'hint-banner' : 'error-banner'} style={{ marginTop: 10 }}>
                {batchDoneSummary.ok} document(s) importé(s) avec succès
                {batchDoneSummary.failed > 0 && `, ${batchDoneSummary.failed} en échec :`}
                {batchFailures.length > 0 && (
                  <ul style={{ margin: '6px 0 0', paddingLeft: 18 }}>
                    {batchFailures.map((f) => (
                      <li key={f.name}>
                        {f.name} — {f.error}
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            )}
          </div>
        )}

        {mode === 'text' && (
          <div className="field">
            <label className="field-label">Contenu</label>
            <textarea className="input" rows={5} value={content} onChange={(e) => setContent(e.target.value)} required />
          </div>
        )}

        <div style={{ display: 'flex', gap: 10 }}>
          <button className="btn btn-primary" type="submit" disabled={saving}>
            {saving
              ? mode === 'folder' && batchProgress
                ? `Import ${batchProgress.done}/${batchProgress.total}…`
                : 'Indexation en cours…'
              : mode === 'folder'
                ? 'Importer le dossier'
                : 'Créer et indexer'}
          </button>
          {mode === 'folder' && batchDoneSummary && batchDoneSummary.failed > 0 && (
            <button type="button" className="btn" onClick={onCreated}>
              Terminer
            </button>
          )}
        </div>
      </form>
    </div>
  )
}
