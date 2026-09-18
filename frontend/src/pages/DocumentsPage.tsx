import { useEffect, useMemo, useState, type FormEvent } from 'react'
import { useSearchParams } from 'react-router-dom'
import { createDocument, downloadDocumentFile, getDocument, listDocuments, uploadDocument } from '../api/endpoints'
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
  const [showCreate, setShowCreate] = useState(false)
  const [probeId, setProbeId] = useState('')
  const [probeResult, setProbeResult] = useState<{ ok: boolean; message: string } | null>(null)
  const [query, setQuery] = useState(searchParams.get('q') ?? '')
  const [deptFilter, setDeptFilter] = useState('')
  const [levelFilter, setLevelFilter] = useState('')

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
    try {
      const doc = await getDocument(id)
      setSelected(doc)
    } catch (err) {
      setModalError(err instanceof Error ? err.message : "Impossible d'ouvrir ce document.")
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

      {showCreate && <CreateDocumentForm onCreated={() => { setShowCreate(false); loadDocuments() }} />}

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
                {selected && (
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
                {selected?.source_filename && (
                  <button className="btn" style={{ padding: '5px 12px', fontSize: 12 }} onClick={handleDownload} disabled={downloading}>
                    {downloading ? 'Téléchargement…' : 'Télécharger le fichier'}
                  </button>
                )}
                <button className="modal-close" onClick={() => { setSelected(null); setModalError(null) }}>
                  ✕
                </button>
              </div>
            </div>
            <div className="modal-body">
              {modalError ? (
                <div className="error-banner">{modalError}</div>
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

function CreateDocumentForm({ onCreated }: { onCreated: () => void }) {
  const [mode, setMode] = useState<'file' | 'text'>('file')
  const [title, setTitle] = useState('')
  const [department, setDepartment] = useState(DEPARTMENTS[3])
  const [confidentiality, setConfidentiality] = useState(LEVELS[1])
  const [content, setContent] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    setSaving(true)
    setError(null)
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
      <h3 style={{ marginBottom: 12 }}>Ajouter un document</h3>

      <div className="segmented">
        <button type="button" className={mode === 'file' ? 'active' : ''} onClick={() => setMode('file')}>
          Téléverser un fichier
        </button>
        <button type="button" className={mode === 'text' ? 'active' : ''} onClick={() => setMode('text')}>
          Coller du texte
        </button>
      </div>

      {error && <div className="error-banner" style={{ marginTop: 14 }}>{error}</div>}

      <form onSubmit={handleSubmit} style={{ marginTop: 14 }}>
        <div className="field">
          <label className="field-label">Titre</label>
          <input className="input" value={title} onChange={(e) => setTitle(e.target.value)} required />
        </div>
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

        {mode === 'file' ? (
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
        ) : (
          <div className="field">
            <label className="field-label">Contenu</label>
            <textarea className="input" rows={5} value={content} onChange={(e) => setContent(e.target.value)} required />
          </div>
        )}

        <button className="btn btn-primary" type="submit" disabled={saving}>
          {saving ? 'Indexation en cours…' : 'Créer et indexer'}
        </button>
      </form>
    </div>
  )
}
