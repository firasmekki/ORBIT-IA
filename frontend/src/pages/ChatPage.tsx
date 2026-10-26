import { useEffect, useRef, useState, type KeyboardEvent } from 'react'
import {
  deleteConversation,
  deleteHistory,
  getConversation,
  getLlmStatus,
  getVisionStatus,
  listConversations,
  sendChatMessage,
} from '../api/endpoints'
import { ApiError } from '../api/client'
import { useAuth } from '../context/AuthContext'
import { ConfidentialityBadge, DecisionBadge } from '../components/Badges'
import { ChartBlock } from '../components/ChartBlock'
import { SearchResultsBlock } from '../components/SearchResultsBlock'
import { IconTrash } from '../components/Icons'
import { WorkspacePicker } from '../components/WorkspacePicker'
import { FolderScopePicker } from '../components/FolderScopePicker'
import { loadScope, saveScope } from '../lib/folderScope'
import { loadScopeMode, saveScopeMode } from '../lib/folderTree'
import { fetchLabel, safeHttpUrl, splitSources } from '../lib/webSearch'
import { LLM_STATUS_CLASS, answerModeLabel } from '../lib/answerMode'
import { MarkdownMessage } from '../components/MarkdownMessage'
import { ChatWelcome } from '../components/ChatWelcome'
import { CHAT_INPUT_PLACEHOLDER } from '../lib/chatWelcome'
import { looksLikeReport } from '../lib/markdown'
import { FOLDER_POLL_MS, useWorkspaceContext } from '../context/WorkspaceContext'
import { isWorkspaceSupported, pickWorkspaceDirectory } from '../lib/localFs'
import { toolLabel } from '../lib/tools'
import type { ConversationSummary, LlmStatus, MessageOut, ScopeMode, VisionStatus } from '../types'

function isDeniedMessage(msg: MessageOut): boolean {
  return msg.role === 'assistant' && msg.sources.length === 0 && msg.tool_trace.some((t) => t.decision === 'DENY')
}

export function ChatPage() {
  const { user } = useAuth()
  const [conversations, setConversations] = useState<ConversationSummary[]>([])
  const [activeId, setActiveId] = useState<string | null>(null)
  const [messages, setMessages] = useState<MessageOut[]>([])
  const [input, setInput] = useState('')
  const [imageFile, setImageFile] = useState<File | null>(null)
  const [imagePreviewUrl, setImagePreviewUrl] = useState<string | null>(null)
  const imagePickerRef = useRef<HTMLInputElement>(null)
  const [sending, setSending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [successMessage, setSuccessMessage] = useState<string | null>(null)
  const [showDeleteModal, setShowDeleteModal] = useState(false)
  const [conversationToDelete, setConversationToDelete] = useState<ConversationSummary | null>(null)
  // The workspace this chat searches in (checked server-side on every
  // message) and its connected folder, watched app-wide (WorkspaceContext).
  const {
    workspaces,
    workspace,
    workspaceId,
    selectWorkspace,
    refreshWorkspaces,
    removeWorkspace,
    folderName,
    needsPermission,
    syncState,
    syncProgress,
    syncReport,
    lastSyncAt,
    syncError,
    connectFolder,
    disconnectFolder,
    reauthorizeFolder,
    syncNow,
  } = useWorkspaceContext()
  const [showWorkspacePicker, setShowWorkspacePicker] = useState(false)
  const [exportingId, setExportingId] = useState<string | null>(null)
  // Sub-folders the assistant searches in ("BIC"), remembered per workspace.
  const [folderScope, setFolderScope] = useState<string[]>([])
  useEffect(() => setFolderScope(loadScope(workspaceId)), [workspaceId])
  function changeScope(scope: string[]) {
    setFolderScope(scope)
    if (workspaceId) saveScope(workspaceId, scope)
  }
  // How deep: those folders only, with sub-folders (default), or every workspace.
  const [scopeMode, setScopeMode] = useState<ScopeMode>('recursive')
  useEffect(() => setScopeMode(loadScopeMode(workspaceId)), [workspaceId])
  function changeScopeMode(mode: ScopeMode) {
    setScopeMode(mode)
    if (workspaceId) saveScopeMode(workspaceId, mode)
  }
  const [ignoredReasonFilter, setIgnoredReasonFilter] = useState('all')
  const [ignoredDisplayLimit, setIgnoredDisplayLimit] = useState(100)
  const [llmStatus, setLlmStatus] = useState<LlmStatus | null>(null)
  const [visionStatus, setVisionStatus] = useState<VisionStatus | null>(null)
  const scrollRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    // The local model loads once, in its own container, independently of
    // this page - poll its state (quickly while it loads, rarely once
    // ready) so the user sees "Chargement du modèle IA..." / "prêt".
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | undefined
    async function poll() {
      try {
        const status = await getLlmStatus()
        if (cancelled) return
        setLlmStatus(status)
        timer = setTimeout(poll, status.status === 'ready' ? 30000 : 4000)
      } catch {
        if (!cancelled) timer = setTimeout(poll, 10000)
      }
    }
    poll()
    return () => {
      cancelled = true
      if (timer) clearTimeout(timer)
    }
  }, [])

  useEffect(() => {
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | undefined
    async function pollVision() {
      try {
        const status = await getVisionStatus()
        if (cancelled) return
        setVisionStatus(status)
        timer = setTimeout(pollVision, status.status === 'ready' ? 30000 : 5000)
      } catch {
        if (!cancelled) timer = setTimeout(pollVision, 10000)
      }
    }
    void pollVision()
    return () => {
      cancelled = true
      if (timer) clearTimeout(timer)
    }
  }, [])

  useEffect(() => {
    if (workspaceId) void refreshConversations(workspaceId)
    // refreshConversations only depends on workspaceId
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspaceId])

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' })
  }, [messages, sending])

  useEffect(() => {
    if (!successMessage) return
    const timer = setTimeout(() => setSuccessMessage(null), 4000)
    return () => clearTimeout(timer)
  }, [successMessage])

  const canSync = workspace?.can_sync ?? false

  useEffect(() => {
    if (!imageFile) {
      setImagePreviewUrl(null)
      return
    }
    const url = URL.createObjectURL(imageFile)
    setImagePreviewUrl(url)
    return () => URL.revokeObjectURL(url)
  }, [imageFile])

  async function refreshConversations(id: string | null = workspaceId) {
    if (!id) return
    try {
      const list = await listConversations(id)
      setConversations(list)
    } catch {
      // non-fatal
    }
  }

  async function selectConversation(id: string) {
    setError(null)
    try {
      const conv = await getConversation(id)
      setActiveId(conv.id)
      setMessages(conv.messages)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Impossible de charger la conversation.')
    }
  }

  function newConversation() {
    setActiveId(null)
    setMessages([])
    setError(null)
  }

  function handleHistoryDeleted() {
    setConversations([])
    setActiveId(null)
    setMessages([])
    setError(null)
    setShowDeleteModal(false)
    setSuccessMessage('Historique supprimé avec succès.')
  }

  function handleConversationDeleted(id: string) {
    setConversations((prev) => prev.filter((c) => c.id !== id))
    if (activeId === id) {
      setActiveId(null)
      setMessages([])
    }
    setError(null)
    setConversationToDelete(null)
    setSuccessMessage('Conversation supprimée avec succès.')
  }

  function handleSelectWorkspace(id: string) {
    if (id === workspaceId) return
    // A conversation belongs to one workspace: switching starts a new one.
    setActiveId(null)
    setMessages([])
    setConversations([])
    setError(null)
    selectWorkspace(id)
  }

  async function handleConnectClick() {
    try {
      const handle = await pickWorkspaceDirectory()
      setConversations([])
      setActiveId(null)
      setMessages([])
      await connectFolder(handle)
    } catch (err) {
      if (!(err instanceof DOMException && err.name === 'AbortError')) {
        setError(err instanceof Error ? err.message : "Impossible d'accéder à ce dossier.")
      }
    }
  }

  async function handleFolderPicked(handle: FileSystemDirectoryHandle) {
    setShowWorkspacePicker(false)
    try {
      await connectFolder(handle)
    } catch (err) {
      setError(err instanceof Error ? err.message : "Impossible d'accéder à ce dossier.")
    }
  }

  async function handleDisconnectFolder() {
    setShowWorkspacePicker(false)
    await disconnectFolder()
  }

  async function send(text: string) {
    const trimmed = text.trim()
    const selectedImage = imageFile
    if ((!trimmed && !selectedImage) || sending) return
    if (selectedImage && visionStatus?.status !== 'ready') {
      setError('Le moteur vision n’est pas prêt. Réessayez lorsque son état sera « prêt ».')
      return
    }
    if (selectedImage && (!['image/jpeg', 'image/png', 'image/webp'].includes(selectedImage.type) || selectedImage.size > 8 * 1024 * 1024)) {
      setError('Choisissez une image JPEG, PNG ou WebP de 8 Mio maximum.')
      return
    }
    setError(null)

    let imagePayload: { mime_type: string; data_base64: string } | undefined
    if (selectedImage) {
      try {
        const dataUrl = await new Promise<string>((resolve, reject) => {
          const reader = new FileReader()
          reader.onerror = () => reject(new Error('Impossible de lire cette image.'))
          reader.onload = () => resolve(String(reader.result))
          reader.readAsDataURL(selectedImage)
        })
        imagePayload = { mime_type: selectedImage.type, data_base64: dataUrl.split(',', 2)[1] }
      } catch {
        setError('Impossible de lire cette image.')
        return
      }
    }
    const displayedText = trimmed || 'Analyse l’image jointe.'

    const optimisticUser: MessageOut = {
      id: `local-${Date.now()}`,
      role: 'user',
      content: displayedText + (selectedImage ? ` [image : ${selectedImage.name}]` : ''),
      sources: [],
      tool_trace: [],
      created_at: new Date().toISOString(),
    }
    setMessages((prev) => [...prev, optimisticUser])
    setInput('')
    setSending(true)

    try {
      const response = await sendChatMessage(
        displayedText,
        activeId ?? undefined,
        workspaceId ?? undefined,
        imagePayload,
        folderScope,
        scopeMode,
      )
      setImageFile(null)
      setActiveId(response.conversation_id)
      if (response.message) {
        setMessages((prev) => [...prev, response.message as MessageOut])
      }
      refreshConversations()
    } catch (err) {
      const message = err instanceof ApiError ? err.message : 'Une erreur est survenue, réessayez.'
      setError(message)
      setMessages((prev) => [
        ...prev,
        {
          id: `error-${Date.now()}`,
          role: 'assistant',
          content: `Erreur : ${message}`,
          sources: [],
          tool_trace: [],
          created_at: new Date().toISOString(),
        },
      ])
    } finally {
      setSending(false)
    }
  }

  async function exportPdf(msg: MessageOut, index: number) {
    setExportingId(msg.id)
    setError(null)
    try {
      // The request that produced this answer: the closest user message above it.
      const question = [...messages.slice(0, index)].reverse().find((m) => m.role === 'user')?.content ?? null
      const { downloadReportPdf } = await import('../lib/reportPdf')
      const filename = await downloadReportPdf({
        markdown: msg.content,
        question,
        workspace: workspace?.name ?? null,
        scope: folderScope,
        sources: msg.sources.map((s) => ({ title: s.title, relative_path: s.relative_path, excerpt: s.excerpt })),
        generatedAt: new Date(),
      })
      setSuccessMessage(`PDF téléchargé : ${filename}`)
    } catch (err) {
      setError(err instanceof Error ? err.message : "L'export PDF a échoué.")
    } finally {
      setExportingId(null)
    }
  }

  function handleKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      send(input)
    }
  }

  return (
    <div className="page">
      <div className="chat-layout">
        <div className="card chat-history">
          <div className="chat-history-header">
            <strong style={{ fontSize: 13 }}>Historique</strong>
            <button className="btn" style={{ padding: '4px 10px', fontSize: 12 }} onClick={newConversation}>
              + Nouveau
            </button>
          </div>
          <div className="chat-history-list">
            {conversations.length === 0 && (
              <div style={{ color: 'var(--text-muted)', fontSize: 12.5, padding: 10 }}>
                Aucune conversation pour le moment.
              </div>
            )}
            {conversations.map((c) => (
              <div
                key={c.id}
                className={`chat-history-item${c.id === activeId ? ' active' : ''}`}
                onClick={() => selectConversation(c.id)}
              >
                <span className="chat-history-item-title">{c.title}</span>
                <button
                  type="button"
                  className="chat-history-item-delete"
                  title="Supprimer cette conversation"
                  onClick={(e) => {
                    e.stopPropagation()
                    setConversationToDelete(c)
                  }}
                >
                  <IconTrash size={13} />
                </button>
              </div>
            ))}
          </div>
          {conversations.length > 0 && (
            <div className="chat-history-footer">
              <button
                className="btn btn-icon-text"
                style={{ width: '100%', justifyContent: 'center', padding: '7px 10px', fontSize: 12, color: 'var(--deny)' }}
                onClick={() => setShowDeleteModal(true)}
              >
                <IconTrash size={14} />
                Supprimer l'historique
              </button>
            </div>
          )}
        </div>

        <div className="card chat-panel">
          <div className="chat-panel-header">
            <button type="button" className="workspace-button" onClick={() => setShowWorkspacePicker(true)}>
              {workspace?.source ? '🖥️' : '📁'} Workspace : {workspace?.name ?? '…'}
            </button>
            {workspaceId && (
              <FolderScopePicker
                workspaceId={workspaceId}
                workspaceName={workspace?.name ?? ''}
                value={folderScope}
                mode={scopeMode}
                onChange={changeScope}
                onModeChange={changeScopeMode}
              />
            )}
            <span className="workspace-sync-status" role="status">
              {workspace?.source ? (
                <span title={`${workspace.source.host_path}\nLu directement par ORBIT (lecture seule), en continu, même page fermée.`}>
                  {!workspace.source.enabled
                    ? '⏸ Synchronisation en pause'
                    : workspace.source.status === 'scanning'
                      ? `🟡 Analyse du dossier… ${workspace.source.scan_seen ?? 0} élément(s)`
                      : workspace.source.status === 'missing' || workspace.source.status === 'error'
                        ? `🔴 ${workspace.source.error ?? 'Dossier inaccessible'}`
                        : '🟢 Dossier du PC · synchronisation auto'}
                </span>
              ) : syncState === 'syncing' ? (
                <>
                  🟡 Synchronisation…
                  {syncProgress && syncProgress.total > 0 && (
                    <>
                      {' '}
                      {syncProgress.phase === 'hashing' ? 'analyse' : 'envoi'} {syncProgress.done + 1}/{syncProgress.total}
                    </>
                  )}
                </>
              ) : needsPermission ? (
                <button type="button" className="btn btn-primary" style={{ padding: '3px 10px', fontSize: 12 }} onClick={reauthorizeFolder}>
                  🔓 Réactiver la synchronisation de « {folderName} »
                </button>
              ) : syncState === 'error' ? (
                <span title={syncError ?? syncReport?.failed.map((f) => `${f.relative_path} : ${f.reason}`).join('\n')}>
                  🔴 Synchronisation incomplète{syncReport ? ` (${syncReport.failed.length} échec(s))` : ''}
                </span>
              ) : folderName ? (
                <span title={`Le dossier « ${folderName} » est vérifié toutes les ${FOLDER_POLL_MS / 1000} s : nouveaux fichiers et sous-dossiers ajoutés automatiquement.`}>
                  🟢 {syncState === 'synced' ? 'Synchronisé' : 'Connecté'} · surveillance auto
                  {lastSyncAt && <span className="workspace-count"> · {lastSyncAt.toLocaleTimeString('fr-FR')}</span>}
                </span>
              ) : (
                <>
                  {workspace?.folder_name && !workspace.is_default ? (
                    <span title="Ce dossier a été synchronisé depuis un autre navigateur ou une autre adresse ; reconnectez-le ici pour reprendre la synchronisation automatique.">
                      ⚠ Dossier « {workspace.folder_name} » non connecté sur ce navigateur
                    </span>
                  ) : (
                    <span>⚪ Aucun dossier synchronisé</span>
                  )}
                  {isWorkspaceSupported() && (
                    <button type="button" className="btn btn-primary" style={{ padding: '3px 10px', fontSize: 12 }} onClick={handleConnectClick}>
                      📂 {workspace?.folder_name && !workspace.is_default ? 'Reconnecter' : 'Importer un petit dossier depuis le navigateur'}
                    </button>
                  )}
                </>
              )}
              {syncReport && (
                <span className="workspace-count" title="Résumé de la dernière synchronisation">
                  Total : {syncReport.totalFiles} · indexables : {syncReport.localFiles} · ignorés : {syncReport.ignored.length} · erreurs réelles : {syncReport.failed.length}
                </span>
              )}
              {workspace && <span className="workspace-count">🔎 {workspace.document_count} indexé(s)</span>}
              {workspace && workspace.pending_count > 0 && (
                <span
                  className="workspace-count"
                  title="Fichiers déjà copiés dans ORBIT ; lecture du contenu (texte, OCR) puis recherche par le sens en arrière-plan."
                >
                  ⟳ {workspace.pending_count} en cours d'analyse
                </span>
              )}
              {syncReport && (
                <details className="workspace-ignored">
                  <summary>Formats détectés</summary>
                  <ul>
                    {Object.entries(syncReport.formatCounts).sort(([a], [b]) => a.localeCompare(b)).map(([format, count]) => (
                      <li key={format}>{format} : {count}</li>
                    ))}
                  </ul>
                </details>
              )}
              {syncReport && syncReport.ignored.length > 0 && (
                <details className="workspace-ignored">
                  <summary>Voir les fichiers ignorés ({syncReport.ignored.length})</summary>
                  <div className="workspace-ignored-tools">
                    <label>
                      Filtrer par raison{' '}
                      <select value={ignoredReasonFilter} onChange={(event) => setIgnoredReasonFilter(event.target.value)}>
                        <option value="all">Toutes les raisons</option>
                        {[...new Set(syncReport.ignored.map((file) => file.code))].map((code) => (
                          <option key={code} value={code}>{code}</option>
                        ))}
                      </select>
                    </label>
                  </div>
                  <ul>
                    {syncReport.ignored.filter((file) => ignoredReasonFilter === 'all' || file.code === ignoredReasonFilter).slice(0, ignoredDisplayLimit).map((file) => (
                      <li key={`${file.relative_path}:${file.code}`}>
                        <strong>{file.relative_path}</strong> — {file.reason}
                      </li>
                    ))}
                  </ul>
                  {syncReport.ignored.filter((file) => ignoredReasonFilter === 'all' || file.code === ignoredReasonFilter).length > ignoredDisplayLimit && (
                    <button type="button" className="btn" onClick={() => setIgnoredDisplayLimit((limit) => limit + 100)}>
                      Afficher 100 fichiers ignorés supplémentaires
                    </button>
                  )}
                </details>
              )}
            </span>
            {folderName && !needsPermission && canSync && syncState !== 'syncing' && (
              <button type="button" className="btn" style={{ padding: '4px 10px', fontSize: 12 }} onClick={syncNow}>
                ⟳ Synchroniser
              </button>
            )}
            <button type="button" className="btn" style={{ padding: '4px 10px', fontSize: 12 }} onClick={() => setShowWorkspacePicker(true)}>
              Changer
            </button>
            {llmStatus && (
              <span
                className={`llm-status ${LLM_STATUS_CLASS[llmStatus.status] ?? 'llm-status-unavailable'}`}
                title={llmStatus.detail ? `${llmStatus.model} — ${llmStatus.detail}` : llmStatus.model}
                role="status"
              >
                <span className="llm-status-dot" />
                {llmStatus.message}
              </span>
            )}
          </div>
          <div className="chat-messages" ref={scrollRef}>
            {messages.length === 0 && <ChatWelcome />}

            {messages.map((msg, index) => (
              <div key={msg.id} className={`msg-row ${msg.role}${isDeniedMessage(msg) ? ' denied' : ''}`}>
                <div className="msg-avatar">{msg.role === 'user' ? (user?.full_name[0] ?? 'U') : 'IA'}</div>
                <div style={{ minWidth: 0 }}>
                  {msg.role === 'assistant' ? (
                    <div className={`msg-bubble msg-markdown${msg.answer_mode === 'unavailable' ? ' msg-unavailable' : ''}`}>
                      <MarkdownMessage content={msg.content} />
                      {answerModeLabel(msg.answer_mode) && (
                        <div className={`answer-mode answer-mode-${msg.answer_mode}`}>{answerModeLabel(msg.answer_mode)}</div>
                      )}
                    </div>
                  ) : (
                    <div className="msg-bubble">{msg.content}</div>
                  )}

                  {msg.role === 'assistant' && looksLikeReport(msg.content) && (
                    <div className="msg-actions">
                      <button
                        type="button"
                        className="btn"
                        disabled={exportingId === msg.id}
                        onClick={() => exportPdf(msg, index)}
                        title="Télécharger cette réponse sous forme de fichier PDF (texte, titres, tableaux et sources)"
                      >
                        {exportingId === msg.id ? 'Génération du PDF…' : '⬇ Télécharger en PDF'}
                      </button>
                    </div>
                  )}

                  {msg.clarification && msg.clarification.options.length > 0 && (
                    // Clicking an option sends it as the user's answer. Only
                    // the latest question is still answerable.
                    <div className="clarification-options" role="group" aria-label="Réponses proposées">
                      {msg.clarification.options.map((option) => (
                        <button
                          key={option.value}
                          type="button"
                          className="chip-button"
                          disabled={sending || index !== messages.length - 1}
                          onClick={() => send(option.value)}
                        >
                          {option.label}
                        </button>
                      ))}
                    </div>
                  )}

                  {msg.chart && <ChartBlock spec={msg.chart} onAsk={sending ? undefined : (text) => send(text)} />}

                  {msg.search_results && <SearchResultsBlock results={msg.search_results} />}

                  {(msg.sources.length > 0 || msg.tool_trace.length > 0) && (
                    <div className="msg-meta">
                      {/* For an exact search the results panel already lists
                          every source document - no duplicate list. */}
                      {msg.sources.length > 0 && !msg.search_results && splitSources(msg.sources).local.length > 0 && (
                        <div className="sources-block">
                          <div className="sources-title">Documents de l’espace de travail</div>
                          {splitSources(msg.sources).local.map((s, i) => (
                            <div key={s.document_id ?? `local-${i}`} className="source-item">
                              <div className="source-item-title">
                                📄 {s.citation ? `[${s.citation}] ` : ''}
                                {s.title} {s.confidentiality && <ConfidentialityBadge level={s.confidentiality} />}{' '}
                                {s.department && <span className="dept-tag">{s.department}</span>}
                              </div>
                              {s.relative_path && workspace && (
                                <div className="source-item-path">
                                  📁 {workspace.name}/{s.relative_path}
                                  {s.page ? `, page ${s.page}` : ''}
                                </div>
                              )}
                              <div className="source-item-excerpt">{s.excerpt}</div>
                            </div>
                          ))}
                        </div>
                      )}

                      {splitSources(msg.sources).web.length > 0 && (
                        <div className="sources-block sources-web">
                          <div className="sources-title">Sources Web</div>
                          {splitSources(msg.sources).web.map((s, i) => {
                            const href = safeHttpUrl(s.url)
                            return (
                              <div key={`web-${i}`} className="source-item">
                                <div className="source-item-title">
                                  🌐 {s.citation ? `[${s.citation}] ` : ''}
                                  {href ? (
                                    <a href={href} target="_blank" rel="noopener noreferrer">
                                      {s.title}
                                    </a>
                                  ) : (
                                    s.title
                                  )}
                                  <span className={`fetch-badge ${s.fetch_status === 'fetched' ? 'fetch-read' : 'fetch-snippet'}`}>
                                    {fetchLabel(s)}
                                  </span>
                                </div>
                                <div className="source-item-path">
                                  {s.domain}
                                  {s.published ? ` · publié : ${s.published.slice(0, 10)}` : ''}
                                  {s.accessed_at ? ` · consulté le ${s.accessed_at.slice(0, 10)}` : ''}
                                  {s.provider ? ` · via ${s.provider}` : ''}
                                </div>
                                {href && <div className="source-item-url mono">{href}</div>}
                                <div className="source-item-excerpt">{s.excerpt}</div>
                              </div>
                            )
                          })}
                        </div>
                      )}

                      {msg.tool_trace.length > 0 && (
                        <details className="tool-trace">
                          <summary>{msg.tool_trace.length} action(s) effectuée(s) — voir le détail</summary>
                          {msg.tool_trace.map((t, idx) => (
                            <div key={idx} className="tool-trace-item">
                              <DecisionBadge decision={t.decision} />
                              <span>{toolLabel(t.tool)}</span>
                              {t.decision === 'DENY' && <span style={{ color: 'var(--deny)' }}>— {t.reason}</span>}
                              {t.decision === 'ERROR' && <span style={{ color: 'var(--text-muted)' }}>— {t.reason}</span>}
                            </div>
                          ))}
                        </details>
                      )}
                    </div>
                  )}
                </div>
              </div>
            ))}

            {sending && (
              <div className="msg-row assistant">
                <div className="msg-avatar">IA</div>
                <div className="msg-bubble">
                  <div className="typing-indicator">
                    <span />
                    <span />
                    <span />
                  </div>
                </div>
              </div>
            )}
          </div>

          {error && (
            <div className="error-banner" style={{ margin: '0 16px' }}>
              {error}
            </div>
          )}
          {successMessage && (
            <div className="success-banner" style={{ margin: '0 16px' }}>
              ✓ {successMessage}
            </div>
          )}

          <div className="chat-composer">
            {imageFile && (
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12 }}>
                {imagePreviewUrl && <img src={imagePreviewUrl} alt="Aperçu de l’image jointe" style={{ maxWidth: 96, maxHeight: 72, objectFit: 'contain', borderRadius: 4 }} />}
                <span>Image jointe : {imageFile.name} ({(imageFile.size / 1024 / 1024).toFixed(1)} Mio)</span>
                <button type="button" className="btn" onClick={() => setImageFile(null)} disabled={sending}>Retirer</button>
              </div>
            )}
            <textarea
              className="input"
              placeholder={CHAT_INPUT_PLACEHOLDER}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              disabled={sending}
            />
            <input
              ref={imagePickerRef}
              type="file"
              accept="image/jpeg,image/png,image/webp"
              hidden
              onChange={(event) => {
                const file = event.target.files?.[0] ?? null
                if (file && (!['image/jpeg', 'image/png', 'image/webp'].includes(file.type) || file.size > 8 * 1024 * 1024)) {
                  setError('Choisissez une image JPEG, PNG ou WebP de 8 Mio maximum.')
                  event.target.value = ''
                  return
                }
                setError(null)
                setImageFile(file)
              }}
            />
            <button
              type="button"
              className="btn"
              onClick={() => imagePickerRef.current?.click()}
              disabled={sending || visionStatus?.status !== 'ready'}
              title={visionStatus?.status === 'ready' ? visionStatus.model : 'Le moteur vision est indisponible.'}
            >
              {visionStatus?.status === 'ready' ? 'Image' : visionStatus?.status === 'loading' ? 'Vision…' : 'Vision indisponible'}
            </button>
            <button
              className="btn btn-primary"
              onClick={() => send(input)}
              disabled={sending || (!input.trim() && !imageFile) || Boolean(imageFile && visionStatus?.status !== 'ready')}
            >
              Envoyer
            </button>
          </div>
        </div>
      </div>

      {showDeleteModal && (
        <DeleteHistoryModal
          conversationCount={conversations.length}
          onClose={() => setShowDeleteModal(false)}
          onDeleted={handleHistoryDeleted}
        />
      )}

      {conversationToDelete && (
        <DeleteConversationModal
          conversation={conversationToDelete}
          onClose={() => setConversationToDelete(null)}
          onDeleted={handleConversationDeleted}
        />
      )}

      {showWorkspacePicker && (
        <WorkspacePicker
          workspaces={workspaces}
          currentId={workspaceId}
          connectedFolder={folderName}
          onSelect={handleSelectWorkspace}
          onCreated={async (created) => {
            await refreshWorkspaces()
            handleSelectWorkspace(created.id)
          }}
          onFolderPicked={handleFolderPicked}
          onDisconnectFolder={handleDisconnectFolder}
          onWorkspaceChanged={() => void refreshWorkspaces()}
          onDeleteWorkspace={removeWorkspace}
          onClose={() => setShowWorkspacePicker(false)}
        />
      )}
    </div>
  )
}

function DeleteHistoryModal({
  conversationCount,
  onClose,
  onDeleted,
}: {
  conversationCount: number
  onClose: () => void
  onDeleted: () => void
}) {
  const [deleting, setDeleting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function handleConfirm() {
    setDeleting(true)
    setError(null)
    try {
      await deleteHistory()
      onDeleted()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Impossible de supprimer votre historique, réessayez.')
    } finally {
      setDeleting(false)
    }
  }

  return (
    <div className="modal-overlay" onClick={deleting ? undefined : onClose}>
      <div className="modal-card" style={{ maxWidth: 440 }} onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h3>⚠️ Supprimer tout l'historique ?</h3>
          {!deleting && (
            <button className="modal-close" onClick={onClose}>
              ✕
            </button>
          )}
        </div>
        <div className="modal-body" style={{ whiteSpace: 'normal' }}>
          {error && <div className="error-banner">{error}</div>}
          <p style={{ fontSize: 13.5, color: 'var(--text)' }}>
            Vous êtes sur le point de supprimer {conversationCount} conversation{conversationCount > 1 ? 's' : ''} de
            votre espace. Une trace de sécurité sera conservée pour assurer la traçabilité.
          </p>
          <div style={{ display: 'flex', gap: 10, justifyContent: 'flex-end', marginTop: 20 }}>
            <button type="button" className="btn" onClick={onClose} disabled={deleting}>
              Annuler
            </button>
            <button
              type="button"
              className="btn"
              style={{ background: 'var(--deny)', borderColor: 'var(--deny)', color: '#fff' }}
              onClick={handleConfirm}
              disabled={deleting}
            >
              {deleting
                ? 'Suppression…'
                : `Supprimer les ${conversationCount} conversation${conversationCount > 1 ? 's' : ''}`}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}

function DeleteConversationModal({
  conversation,
  onClose,
  onDeleted,
}: {
  conversation: ConversationSummary
  onClose: () => void
  onDeleted: (id: string) => void
}) {
  const [deleting, setDeleting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function handleConfirm() {
    setDeleting(true)
    setError(null)
    try {
      await deleteConversation(conversation.id)
      onDeleted(conversation.id)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Impossible de supprimer cette conversation, réessayez.')
    } finally {
      setDeleting(false)
    }
  }

  return (
    <div className="modal-overlay" onClick={deleting ? undefined : onClose}>
      <div className="modal-card" style={{ maxWidth: 440 }} onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h3>Supprimer cette conversation ?</h3>
          {!deleting && (
            <button className="modal-close" onClick={onClose}>
              ✕
            </button>
          )}
        </div>
        <div className="modal-body" style={{ whiteSpace: 'normal' }}>
          {error && <div className="error-banner">{error}</div>}
          <p style={{ fontSize: 13.5, color: 'var(--text)' }}>
            Cette conversation sera supprimée de votre historique. Une trace de sécurité sera conservée pour
            assurer la traçabilité.
          </p>
          <p style={{ fontSize: 12.5, color: 'var(--text-muted)', marginTop: 10 }}>
            « <strong>{conversation.title}</strong> »
          </p>
          <div style={{ display: 'flex', gap: 10, justifyContent: 'flex-end', marginTop: 20 }}>
            <button type="button" className="btn" onClick={onClose} disabled={deleting}>
              Annuler
            </button>
            <button
              type="button"
              className="btn"
              style={{ background: 'var(--deny)', borderColor: 'var(--deny)', color: '#fff' }}
              onClick={handleConfirm}
              disabled={deleting}
            >
              {deleting ? 'Suppression…' : 'Supprimer'}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
