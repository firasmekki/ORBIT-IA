// The discreet line under an assistant answer saying how it was produced
// (set by the backend: routers/chat.py answer_mode). Nothing is shown for
// older messages that have no mode: no label is ever guessed.
export type AnswerMode = 'direct' | 'general' | 'documents' | 'web' | 'hybrid' | 'unavailable'

const LABELS: Record<AnswerMode, string> = {
  direct: 'Réponse locale',
  general: 'Réponse locale · connaissances générales du modèle',
  documents: 'Documents du projet',
  web: 'Recherche Web',
  hybrid: 'Documents + Web',
  unavailable: 'Modèle indisponible · aucune réponse rédigée',
}

export function answerModeLabel(mode: string | null | undefined): string | null {
  return mode && mode in LABELS ? LABELS[mode as AnswerMode] : null
}

/** The status banner: what the server itself reports, never the configuration. */
export const LLM_STATUS_CLASS: Record<string, string> = {
  ready: 'llm-status-ready',
  loading: 'llm-status-loading',
  unavailable: 'llm-status-unavailable',
  error: 'llm-status-unavailable',
  misconfigured: 'llm-status-unavailable',
}
