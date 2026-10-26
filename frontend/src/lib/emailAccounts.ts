// Personal mailboxes: labels and messages shared by the Connectivity page
// (EmailAccountsPanel) and the chat cards (EmailResultsBlock). Pure functions,
// tested in tests/emailAccounts.test.mjs.

export const EMAIL_STATUS_LABEL: Record<string, string> = {
  not_configured: 'Non configuré',
  config_required: 'Configuration requise',
  pending: 'Connexion en attente',
  ready: 'Prêt à connecter',
  authorization_required: 'Autorisation requise',
  connected: 'Connecté',
  token_expired_or_revoked: 'Jeton expiré ou révoqué',
  policy_blocked: 'Bloqué par une politique',
  temporary_error: 'Erreur temporaire',
  disconnected: 'Compte déconnecté',
}

export const TOKEN_STATE_LABEL: Record<string, string> = {
  valid: 'Jeton valide',
  expired_refreshable: 'Jeton expiré, renouvelé automatiquement à la prochaine lecture',
  absent: 'Aucun jeton',
}

/** ?email_oauth=<code> set by the backend callback - never a provider message. */
const OAUTH_OUTCOME: Record<string, string> = {
  connected: 'Compte connecté en lecture seule. Vous pouvez demander vos derniers emails directement dans le chat.',
  oauth_state_invalid: 'OAuth : la demande d’autorisation est invalide. Recommencez la connexion depuis ORBIT.',
  oauth_state_expired: 'OAuth : la demande d’autorisation a expiré (10 minutes). Recommencez la connexion.',
  oauth_browser_mismatch: 'OAuth : la connexion a été terminée dans un autre navigateur. Recommencez depuis ce navigateur.',
  oauth_denied: 'OAuth : l’autorisation a été refusée chez le fournisseur.',
  oauth_exchange_failed: 'OAuth : le fournisseur a refusé l’échange du code d’autorisation.',
  oauth_config_invalid: 'Configuration OAuth du serveur invalide.',
  permission_missing: 'Permission : la lecture des emails n’a pas été accordée. Recommencez en cochant l’accès en lecture.',
  tenant_policy_blocked: 'Politique Microsoft : votre organisation exige l’approbation d’un administrateur pour cette application.',
  account_mismatch: 'OAuth : le compte choisi n’est pas celui à reconnecter.',
  provider_unavailable: 'Fournisseur momentanément indisponible. Réessayez plus tard.',
  network_error: 'Réseau : le fournisseur est injoignable.',
  timeout: 'Réseau : le fournisseur n’a pas répondu à temps.',
  provider_disabled: 'Ce fournisseur n’est pas encore activé.',
}

export function oauthOutcomeMessage(code: string | null, providerLabel?: string): { ok: boolean; text: string } | null {
  if (!code) return null
  const text = OAUTH_OUTCOME[code] ?? 'La connexion a échoué.'
  return { ok: code === 'connected', text: providerLabel ? `${providerLabel} — ${text}` : text }
}

export const PROVIDER_LABEL: Record<string, string> = { gmail: 'Gmail', outlook: 'Outlook', yahoo: 'Yahoo Mail' }

/** The display name when the provider gave one, otherwise the address - never a guess. */
export function senderLabel(sender: { address: string; name: string | null } | null | undefined): string {
  if (!sender) return 'Expéditeur inconnu'
  return sender.name ? `${sender.name} <${sender.address}>` : sender.address
}

export function formatEmailDate(value: string | null | undefined, timeZone?: string): string {
  if (!value) return 'Date inconnue'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return 'Date inconnue'
  try {
    return date.toLocaleString('fr-FR', { timeZone, dateStyle: 'short', timeStyle: 'short' })
  } catch {
    return date.toLocaleString('fr-FR', { dateStyle: 'short', timeStyle: 'short' })
  }
}

export function formatBytes(size: number | null | undefined): string {
  if (size == null) return ''
  if (size < 1024) return `${size} o`
  if (size < 1024 * 1024) return `${Math.round(size / 1024)} Ko`
  return `${(size / (1024 * 1024)).toFixed(1)} Mo`
}

/** Only https links from the provider are rendered as links. */
export function safeExternalLink(url: string | null | undefined): string | null {
  if (!url) return null
  try {
    return new URL(url).protocol === 'https:' ? url : null
  } catch {
    return null
  }
}

export function browserTimeZone(): string | undefined {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || undefined
  } catch {
    return undefined
  }
}
