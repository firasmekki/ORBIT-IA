import { useEffect, useMemo, useState, type ChangeEvent, type FormEvent } from 'react'
import { useWorkspaceContext } from '../context/WorkspaceContext'
import { DocumentAccessPanel } from '../components/DocumentAccessPanel'
import { FoldersPanel } from '../components/FoldersPanel'
import { ImagePreview } from '../components/DocumentPreview'
import { isImageFile } from '../lib/fileTypes'
import { useSearchParams } from 'react-router-dom'
import {
  adminListUsers,
  createDocument,
  downloadDocumentFile,
  getDocument,
  getWatchStatus,
  listDocuments,
  listFolders,
  updateDocument,
  uploadDocument,
} from '../api/endpoints'
import { ApiError } from '../api/client'
import { useAuth } from '../context/AuthContext'
import { ConfidentialityBadge } from '../components/Badges'
import { IconDocument, IconSearch } from '../components/Icons'
import type { DocumentDetail, DocumentSummary, UserOut, WorkspaceFolders } from '../types'

const DEPARTMENTS = ['HR', 'FINANCE', 'TECH', 'GENERAL', 'EXEC']
const LEVELS = ['PUBLIC', 'INTERNAL', 'CONFIDENTIAL', 'SECRET']
const ACCEPTED_EXTENSIONS = ['.txt', '.md', '.csv', '.pdf', '.xlsx', '.docx', '.png', '.jpg', '.jpeg', '.webp']

export function DocumentsPage() {
  const { user } = useAuth()
  const [searchParams, setSearchParams] = useSearchParams()
  const [documents, setDocuments] = useState<DocumentSummary[]>([])
  // Same current workspace as /chat; the backend filters the list by it
  // (and refuses a workspace this user may not use).
  const {
    workspaces,
    workspace,
    workspaceId,
    selectWorkspace,
    refreshWorkspaces,
    folderName,
    needsPermission,
    syncState,
    syncVersion,
    lastSyncAt,
    reauthorizeFolder,
  } = useWorkspaceContext()
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
  const [folderFilter, setFolderFilter] = useState('')
  // Documents of the selected folder: with its sub-folders (default) or only
  // those directly inside it.
  const [includeSubfolders, setIncludeSubfolders] = useState(true)
  const [folders, setFolders] = useState<WorkspaceFolders>({ can_manage: false, folders: [] })
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
      if (query && !`${d.title} ${d.relative_path ?? ''}`.toLowerCase().includes(query.toLowerCase())) return false
      if (deptFilter && d.department !== deptFilter) return false
      if (levelFilter && d.confidentiality !== levelFilter) return false
      const path = d.relative_path ?? ''
      if (folderFilter && !path.startsWith(folderFilter)) return false
      if (!includeSubfolders && path.slice(folderFilter.length).includes('/')) return false
      return true
    })
  }, [documents, query, deptFilter, levelFilter, folderFilter, includeSubfolders])

  // While some documents are still being read or embedded, refresh their progress.
  const indexing = documents.some((d) => ['queued', 'extracting', 'indexing'].includes(d.status))
  useEffect(() => {
    if (!indexing) return
    const timer = setInterval(loadDocuments, 5000)
    return () => clearInterval(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [indexing, workspaceId])

  // Reloaded when the workspace changes AND after every automatic sync that
  // added/updated/removed files from the connected folder.
  useEffect(() => {
    if (workspaceId) loadDocuments()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspaceId, syncVersion])

  async function loadDocuments() {
    if (!workspaceId) return
    setLoading(true)
    try {
      const [docs, tree] = await Promise.all([
        listDocuments(workspaceId),
        listFolders(workspaceId).catch(() => ({ can_manage: false, folders: [] })),
      ])
      setDocuments(docs)
      setFolders(tree)
      if (folderFilter && !tree.folders.some((f) => f.path === folderFilter)) setFolderFilter('')
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

  const isDirector = user?.role === 'DIRECTOR'
  const [editSubject, setEditSubject] = useState('')
  const [people, setPeople] = useState<UserOut[]>([])

  useEffect(() => {
    if (!editing || !isDirector || people.length) return
    adminListUsers()
      .then((list) => setPeople(list.filter((u) => u.is_active)))
      .catch(() => {})
  }, [editing, isDirector, people.length])

  function startEdit() {
    if (!selected) return
    setEditDept(selected.department)
    setEditLevel(selected.confidentiality)
    setEditSubject(selected.subject_user_id ?? '')
    setEditError(null)
    setEditing(true)
  }

  async function saveEdit() {
    if (!selected) return
    setEditSaving(true)
    setEditError(null)
    try {
      const subjectChanged = isDirector && editSubject !== (selected.subject_user_id ?? '')
      const updated = await updateDocument(selected.id, {
        department: editDept,
        confidentiality: editLevel,
        ...(subjectChanged ? { subject_user_id: editSubject || null } : {}),
      })
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
            Cette liste est déjà filtrée côté serveur : elle ne contient que les documents de l'espace de travail
            sélectionné que votre rôle («{user?.role}») est autorisé à consulter.
          </p>
          <label className="workspace-select">
            📁 Workspace :
            <select className="input" value={workspaceId ?? ''} onChange={(e) => selectWorkspace(e.target.value)}>
              {workspaces.map((w) => (
                <option key={w.id} value={w.id}>
                  {w.name}
                  {w.folder_name ? ` (dossier : ${w.folder_name})` : ''}
                </option>
              ))}
            </select>
          </label>
          {folderName && (
            <p className="workspace-hint" style={{ marginTop: 6 }}>
              {needsPermission ? (
                <button type="button" className="btn btn-primary" style={{ padding: '3px 10px', fontSize: 12 }} onClick={reauthorizeFolder}>
                  🔓 Réactiver la synchronisation de « {folderName} »
                </button>
              ) : syncState === 'syncing' ? (
                <>🟡 Synchronisation du dossier « {folderName} »…</>
              ) : (
                <>
                  🟢 Dossier « {folderName} » synchronisé automatiquement (nouveaux fichiers et sous-dossiers inclus)
                  {lastSyncAt && <> — dernière synchronisation : {lastSyncAt.toLocaleTimeString('fr-FR')}</>}
                </>
              )}
            </p>
          )}
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
          workspaceId={workspaceId}
          workspaceName={workspace?.name ?? ''}
          isDefaultWorkspace={workspace?.is_default ?? true}
          folders={folders.folders.filter((f) => f.can_upload).map((f) => f.path)}
          initialFolder={folderFilter}
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

      {workspaceId && (
        <FoldersPanel
          workspaceId={workspaceId}
          workspaceName={workspace?.name ?? ''}
          selected={folderFilter}
          includeSubfolders={includeSubfolders}
          onSelect={setFolderFilter}
          onIncludeSubfoldersChange={setIncludeSubfolders}
          onChanged={loadDocuments}
        />
      )}

      <div className="card" style={{ marginBottom: 24 }}>
        {loading ? (
          <div className="centered-spinner">
            <div className="spinner" />
          </div>
        ) : documents.length === 0 ? (
          <div className="empty-state">
            Aucun document accessible pour votre rôle dans l'espace de travail « {workspace?.name} ».
            {workspace && !workspace.is_default && workspace.can_sync && ' Connectez son dossier depuis l\'Assistant IA pour le synchroniser.'}
          </div>
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
                  <th>Indexation</th>
                  <th>Ajouté par</th>
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
                        <span>
                          {doc.title}
                          {doc.relative_path && doc.relative_path !== doc.title && (
                            <span className="doc-path"> — {workspace?.name}/{doc.relative_path}</span>
                          )}
                          {doc.subject_name && <span className="personal-tag">👤 {doc.subject_name}</span>}
                        </span>
                      </div>
                    </td>
                    <td>
                      <span className="dept-tag">{doc.department}</span>
                    </td>
                    <td>
                      <ConfidentialityBadge level={doc.confidentiality} />
                    </td>
                    <td>
                      <IndexStatus status={doc.status} progress={doc.index_progress} />
                    </td>
                    <td>
                      {doc.added_by_name ?? '—'}
                      {doc.origin === 'sync' && <div className="doc-origin">dossier synchronisé</div>}
                      {doc.origin === 'upload' && <div className="doc-origin">déposé depuis l'application</div>}
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
                    {selected.subject_name && (
                      <span className="personal-tag">👤 Document personnel de {selected.subject_name}</span>
                    )}
                    {selected.added_by_name && (
                      <span style={{ fontSize: 11.5, color: 'var(--text-muted)' }}>Ajouté par {selected.added_by_name}</span>
                    )}
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
                  {isDirector && (
                    <div className="field" style={{ marginBottom: 14 }}>
                      <label className="field-label">Document personnel de</label>
                      <select className="input" value={editSubject} onChange={(e) => setEditSubject(e.target.value)}>
                        <option value="">— personne (document commun) —</option>
                        {people.map((p) => (
                          <option key={p.id} value={p.id}>
                            {p.full_name} ({p.role})
                          </option>
                        ))}
                      </select>
                      <p className="workspace-hint" style={{ marginTop: 4 }}>
                        Fiche de paie, congés, tâches… : l'employé le voit toujours, ses collègues jamais. Seuls les
                        gestionnaires du département (RH pour HR, comptable pour FINANCE) et le directeur le voient.
                      </p>
                    </div>
                  )}
                  <p className="workspace-hint" style={{ marginBottom: 12 }}>
                    Rappel : un utilisateur voit ce document seulement s'il a accès à son espace de travail{' '}
                    <strong>et</strong> si son rôle l'autorise (département, niveau), s'il s'agit de son document
                    personnel, ou si le directeur le lui a accordé (voir « Qui peut voir ce document ? »).
                  </p>
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
                  {selected && (
                    <DocumentAccessPanel
                      documentId={selected.id}
                      refreshKey={`${selected.department}/${selected.confidentiality}/${selected.subject_user_id ?? ''}`}
                      onWorkspaceChanged={() => void refreshWorkspaces()}
                    />
                  )}
                  {selected && isImageFile(selected.file_mime_type, selected.source_filename) ? (
                    <>
                      <ImagePreview documentId={selected.id} alt={selected.source_filename ?? selected.title} />
                      <h4 className="doc-extracted-title">Texte extrait (OCR)</h4>
                      <p className="workspace-hint" style={{ marginBottom: 8 }}>
                        Reconnu automatiquement dans l'image : c'est ce texte que la recherche et l'assistant IA
                        utilisent.
                      </p>
                      <div className="doc-extracted-text">{selected.content}</div>
                    </>
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
                </>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

function IndexStatus({ status, progress }: { status: string; progress: number | null }) {
  if (status === 'indexed') return <span className="index-status index-ok">✓ Indexé</span>
  if (status === 'indexing')
    return (
      <span className="index-status index-pending" title="Déjà lisible et trouvable par recherche exacte ; recherche par le sens en cours de préparation.">
        ⟳ Indexation {progress ?? 0} %
      </span>
    )
  if (status === 'queued' || status === 'extracting')
    return (
      <span className="index-status index-pending" title="Fichier copié dans ORBIT ; lecture du contenu (texte, OCR) en arrière-plan.">
        ⟳ {status === 'queued' ? 'En attente de lecture' : 'Lecture en cours'}
      </span>
    )
  if (status === 'failed')
    return (
      <span className="index-status index-error" title="Aucun texte n'a pu être extrait de ce fichier, même par OCR.">
        ✗ Illisible
      </span>
    )
  return <span className="index-status index-error">✗ Erreur</span>
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

function joinFolder(...parts: string[]) {
  return parts
    .map((p) => p.replace(/^\/+|\/+$/g, ''))
    .filter(Boolean)
    .join('/')
}

function CreateDocumentForm({
  workspaceId,
  workspaceName,
  isDefaultWorkspace,
  folders,
  initialFolder,
  onCreated,
  onRefresh,
}: {
  workspaceId: string | null
  workspaceName: string
  isDefaultWorkspace: boolean
  /** Folders this user may drop files into. */
  folders: string[]
  initialFolder: string
  onCreated: () => void
  onRefresh: () => void
}) {
  const [mode, setMode] = useState<'file' | 'folder' | 'text'>('file')
  const [destination, setDestination] = useState(initialFolder)
  const [newFolder, setNewFolder] = useState('')
  // Chosen folder (+ a new subfolder typed under it), "" = root.
  const targetFolder = joinFolder(destination === '__new__' ? '' : destination, newFolder)
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
          // Keep the picked folder's own tree under the destination, like a Dropbox upload.
          const subdir = (f.webkitRelativePath || '').split('/').slice(0, -1).join('/')
          await uploadDocument({
            title: stripExtension(f.name),
            department,
            confidentiality,
            file: f,
            workspaceId: workspaceId ?? undefined,
            folder: joinFolder(targetFolder, subdir) || undefined,
          })
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
        await uploadDocument({
          title,
          department,
          confidentiality,
          file,
          workspaceId: workspaceId ?? undefined,
          folder: targetFolder || undefined,
        })
      } else {
        await createDocument({ title, department, confidentiality, content, workspace_id: workspaceId ?? undefined })
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
      <h3 style={{ marginBottom: 4 }}>Ajouter des documents</h3>
      <p className="workspace-hint" style={{ marginBottom: 12 }}>
        Espace de travail cible : <strong>📁 {workspaceName}</strong> (changez-le en haut de la page).
      </p>

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
        {mode !== 'text' && (
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14 }}>
            <div className="field">
              <label className="field-label">Dossier de destination</label>
              <select className="input" value={destination} onChange={(e) => setDestination(e.target.value)}>
                <option value="">{isDefaultWorkspace ? '— aucun dossier —' : '📁 Racine de l\u2019espace'}</option>
                {folders.map((f) => (
                  <option key={f} value={f}>
                    📂 {f}
                  </option>
                ))}
                <option value="__new__">＋ Nouveau dossier…</option>
              </select>
            </div>
            <div className="field">
              <label className="field-label">
                {destination === '__new__' ? 'Nom du nouveau dossier' : 'Sous-dossier (facultatif)'}
              </label>
              <input
                className="input"
                placeholder={destination === '__new__' ? 'ex. RH/Paie' : 'ex. 2026'}
                value={newFolder}
                onChange={(e) => setNewFolder(e.target.value)}
              />
            </div>
          </div>
        )}
        {mode !== 'text' && (
          <p className="workspace-hint" style={{ marginTop: -6, marginBottom: 14 }}>
            Destination : <strong>📁 {workspaceName}{targetFolder ? ` / ${targetFolder}` : ''}</strong>. Le fichier
            apparaît aussitôt pour chaque personne autorisée, avec votre nom. Astuce : un fichier déposé dans{' '}
            <code>Personnel/&lt;identifiant&gt;</code> devient le document personnel de cette personne.
          </p>
        )}
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
              tel quel. Taille max. 50 Mo.
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
