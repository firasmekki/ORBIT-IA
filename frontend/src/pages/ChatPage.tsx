import { useEffect, useRef, useState, type KeyboardEvent } from 'react'
import {
  deleteConversation,
  deleteHistory,
  getConversation,
  listConversations,
  resumeChatMessage,
  sendChatMessage,
} from '../api/endpoints'
import { ApiError } from '../api/client'
import { useAuth } from '../context/AuthContext'
import { ConfidentialityBadge, DecisionBadge } from '../components/Badges'
import { ChartBlock } from '../components/ChartBlock'
import { IconTrash } from '../components/Icons'
import { WorkspacePicker } from '../components/WorkspacePicker'
import {
  clearWorkspaceHandle,
  ensureReadPermission,
  loadWorkspaceHandle,
  readWorkspaceFile,
  saveWorkspaceHandle,
  walkWorkspace,
} from '../lib/localFs'
import { toolLabel } from '../lib/tools'
import type { ConversationSummary, LocalFileEntry, MessageOut, PendingClientAction } from '../types'

const SUGGESTIONS: Record<string, string[]> = {
  DIRECTOR: ['Quel est le budget de la masse salariale 2025 ?', 'Résume le plan stratégique 2026.'],
  HR: ['Trouve-moi la politique de congés.', 'Donne-moi le salaire des employés.'],
  ACCOUNTANT: ['Quel est le budget IT 2025 ?', 'Montre-moi la grille des salaires individuels.'],
  DEVELOPER: ["Quelle est l'architecture technique de la plateforme ?", "Donne-moi les accès à l'infrastructure de production."],
  EMPLOYEE: ['Quelle est la politique de congés ?', 'Quel est le budget marketing 2025 ?'],
}

function isDeniedMessage(msg: MessageOut): boolean {
  return msg.role === 'assistant' && msg.sources.length === 0 && msg.tool_trace.some((t) => t.decision === 'DENY')
}

export function ChatPage() {
  const { user } = useAuth()
  const [conversations, setConversations] = useState<ConversationSummary[]>([])
  const [activeId, setActiveId] = useState<string | null>(null)
  const [messages, setMessages] = useState<MessageOut[]>([])
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [successMessage, setSuccessMessage] = useState<string | null>(null)
  const [showDeleteModal, setShowDeleteModal] = useState(false)
  const [conversationToDelete, setConversationToDelete] = useState<ConversationSummary | null>(null)
  const [workspaceHandle, setWorkspaceHandle] = useState<FileSystemDirectoryHandle | null>(null)
  const [workspaceIndex, setWorkspaceIndex] = useState<LocalFileEntry[]>([])
  const [showWorkspacePicker, setShowWorkspacePicker] = useState(false)
  const scrollRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    refreshConversations()
  }, [])

  useEffect(() => {
    // Restores the previously granted workspace, if any, on page reload -
    // the browser may still silently honor the permission (same-origin,
    // same session) or may require the user to re-confirm; either way we
    // never re-prompt automatically without a user gesture.
    ;(async () => {
      try {
        const handle = await loadWorkspaceHandle()
        if (!handle) return
        const granted = await ensureReadPermission(handle)
        if (!granted) return
        setWorkspaceHandle(handle)
        const entries = await walkWorkspace(handle)
        setWorkspaceIndex(entries)
      } catch {
        // non-fatal - the user can just pick the workspace again
      }
    })()
  }, [])

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' })
  }, [messages, sending])

  useEffect(() => {
    if (!successMessage) return
    const timer = setTimeout(() => setSuccessMessage(null), 4000)
    return () => clearTimeout(timer)
  }, [successMessage])

  async function refreshConversations() {
    try {
      const list = await listConversations()
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

  async function handleWorkspacePicked(handle: FileSystemDirectoryHandle) {
    setWorkspaceHandle(handle)
    setShowWorkspacePicker(false)

    // Persisting the handle (so the workspace survives a page reload) is
    // a separate concern from building this session's index - and must
    // never block it. IndexedDB can legitimately fail (storage quota,
    // private browsing restrictions) without that meaning the workspace
    // itself is unusable right now.
    try {
      await saveWorkspaceHandle(handle)
    } catch {
      // non-fatal: usable for this session, just won't auto-restore later
    }

    try {
      const entries = await walkWorkspace(handle)
      setWorkspaceIndex(entries)
    } catch (err) {
      setError(err instanceof Error ? err.message : "Impossible de parcourir ce dossier.")
    }
  }

  async function handleWorkspaceDisconnect() {
    setWorkspaceHandle(null)
    setWorkspaceIndex([])
    setShowWorkspacePicker(false)
    try {
      await clearWorkspaceHandle()
    } catch {
      // non-fatal
    }
  }

  /** Executes a pending_client_action the backend returned (it has no
   * disk access - only this tab, holding the real handle, can actually
   * read the file) and resumes the chat turn with the result. */
  async function resolvePendingAction(conversationId: string, action: PendingClientAction) {
    if (!workspaceHandle) {
      setError("Le workspace n'est plus connecté.")
      setSending(false)
      return
    }
    try {
      const { base64 } = await readWorkspaceFile(workspaceHandle, action.relative_path)
      const response = await resumeChatMessage({
        conversation_id: conversationId,
        action_id: action.action_id,
        relative_path: action.relative_path,
        name: action.name,
        content_base64: base64,
      })
      if (response.message) {
        setMessages((prev) => [...prev, response.message as MessageOut])
      }
    } catch (err) {
      const message =
        err instanceof ApiError
          ? err.message
          : err instanceof Error
            ? `Impossible de lire « ${action.name} » : ${err.message}`
            : `Impossible de lire « ${action.name} ».`
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

  async function send(text: string) {
    const trimmed = text.trim()
    if (!trimmed || sending) return
    setError(null)

    const optimisticUser: MessageOut = {
      id: `local-${Date.now()}`,
      role: 'user',
      content: trimmed,
      sources: [],
      tool_trace: [],
      created_at: new Date().toISOString(),
    }
    setMessages((prev) => [...prev, optimisticUser])
    setInput('')
    setSending(true)

    try {
      const response = await sendChatMessage(trimmed, activeId ?? undefined, workspaceIndex.length > 0 ? workspaceIndex : undefined)
      setActiveId(response.conversation_id)
      if (response.pending_client_action) {
        // Still "sending" - the backend asked for a local file's content,
        // which only this tab can actually read (see resolvePendingAction).
        await resolvePendingAction(response.conversation_id, response.pending_client_action)
        refreshConversations()
        return
      }
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
              📁 Workspace : {workspaceHandle ? workspaceHandle.name : 'Aucun dossier'}
              {workspaceHandle && (
                <span className="workspace-status">
                  <span className="workspace-dot active" /> Actif
                </span>
              )}
            </button>
            <button type="button" className="btn" style={{ padding: '4px 10px', fontSize: 12 }} onClick={() => setShowWorkspacePicker(true)}>
              {workspaceHandle ? 'Changer' : 'Choisir un dossier'}
            </button>
          </div>
          <div className="chat-messages" ref={scrollRef}>
            {messages.length === 0 && (
              <div className="chat-empty">
                <h3>Posez une question à l'assistant</h3>
                <p>
                  L'agent recherche uniquement dans les documents et données auxquels votre rôle («
                  {user?.role}») a accès. Toute tentative en dehors de ce périmètre est refusée par le
                  serveur, pas par l'assistant lui-même.
                </p>
                {(SUGGESTIONS[user?.role ?? ''] ?? []).map((s) => (
                  <button key={s} className="suggestion-chip" onClick={() => send(s)}>
                    {s}
                  </button>
                ))}
              </div>
            )}

            {messages.map((msg) => (
              <div key={msg.id} className={`msg-row ${msg.role}${isDeniedMessage(msg) ? ' denied' : ''}`}>
                <div className="msg-avatar">{msg.role === 'user' ? (user?.full_name[0] ?? 'U') : 'IA'}</div>
                <div style={{ minWidth: 0 }}>
                  <div className="msg-bubble">{msg.content}</div>

                  {msg.chart && <ChartBlock spec={msg.chart} />}

                  {(msg.sources.length > 0 || msg.tool_trace.length > 0) && (
                    <div className="msg-meta">
                      {msg.sources.length > 0 && (
                        <div className="sources-block">
                          <div className="sources-title">Sources utilisées</div>
                          {msg.sources.map((s) => (
                            <div key={s.document_id} className="source-item">
                              <div className="source-item-title">
                                {s.title} <ConfidentialityBadge level={s.confidentiality} /> <span className="dept-tag">{s.department}</span>
                              </div>
                              <div className="source-item-excerpt">{s.excerpt}</div>
                            </div>
                          ))}
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
            <textarea
              className="input"
              placeholder="Posez votre question…"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              disabled={sending}
            />
            <button className="btn btn-primary" onClick={() => send(input)} disabled={sending || !input.trim()}>
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
          currentWorkspaceName={workspaceHandle?.name ?? null}
          onClose={() => setShowWorkspacePicker(false)}
          onPicked={handleWorkspacePicked}
          onDisconnect={handleWorkspaceDisconnect}
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
