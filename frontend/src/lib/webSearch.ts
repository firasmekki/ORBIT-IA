// How the sources of an answer are shown: web pages and workspace documents
// always listed apart. Where to look (documents / web / both) is decided by
// the server for each question - there is no mode for the user to pick.
import type { SourceRef } from '../types'

export function splitSources(sources: SourceRef[]): { local: SourceRef[]; web: SourceRef[] } {
  return {
    local: sources.filter((s) => s.kind !== 'web'),
    web: sources.filter((s) => s.kind === 'web'),
  }
}

/** Only http(s) links are ever made clickable. */
export function safeHttpUrl(url: string | null | undefined): string | null {
  if (!url) return null
  try {
    const parsed = new URL(url)
    return parsed.protocol === 'http:' || parsed.protocol === 'https:' ? parsed.href : null
  } catch {
    return null
  }
}

/** "page lue" / "extrait du moteur seulement (…)" - never a snippet shown as a read page. */
export function fetchLabel(source: SourceRef): string {
  if (source.fetch_status === 'fetched') return 'page lue'
  const status = source.fetch_note || 'page non lue'
  return `extrait du moteur seulement — ${status}`
}

/** "Tester la connexion" / last real call, as the backend names it
 * (app/websearch/service.py connection_status). */
export const CONNECTION_LABEL: Record<string, string> = {
  connected: 'Connexion fonctionnelle',
  not_configured: 'Clé API absente',
  invalid_key: 'Clé API invalide (refusée par le fournisseur)',
  rate_limited: 'Quota ou limite de débit dépassé',
  timeout: 'Délai dépassé',
  provider_error: 'Fournisseur indisponible ou en erreur',
  network_error: 'Erreur réseau (serveur injoignable)',
  config_error: 'Paramètres refusés (langue / pays)',
  disabled: 'Recherche Web désactivée',
}

export function connectionLabel(connection: string | null | undefined): string {
  return (connection && CONNECTION_LABEL[connection]) || 'État inconnu'
}
