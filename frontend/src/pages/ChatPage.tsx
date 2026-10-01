import { useEffect, useRef, useState, type KeyboardEvent } from 'react'
import { getConversation, listConversations, sendChatMessage } from '../api/endpoints'
import { ApiError } from '../api/client'
import { useAuth } from '../context/AuthContext'
import { ConfidentialityBadge, DecisionBadge } from '../components/Badges'
import { ChartBlock } from '../components/ChartBlock'
import { toolLabel } from '../lib/tools'
import type { ConversationSummary, MessageOut } from '../types'

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
  const scrollRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    refreshConversations()
  }, [])

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' })
  }, [messages, sending])

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
      const response = await sendChatMessage(trimmed, activeId ?? undefined)
      setActiveId(response.conversation_id)
      setMessages((prev) => [...prev, response.message])
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
                {c.title}
              </div>
            ))}
          </div>
        </div>

        <div className="card chat-panel">
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
    </div>
  )
}
