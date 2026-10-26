import { api } from './client'
import type {
  AdminStats,
  AlertPage,
  AuditLogPage,
  ChatResponse,
  ConversationDetail,
  ConversationSummary,
  DeleteHistoryResult,
  AccessRule,
  AccessRules,
  DocumentAccess,
  DocumentDetail,
  DocumentSummary,
  LlmStatus,
  VisionStatus,
  Me,
  PolicyMatrix,
  InventoryItem,
  SourceListing,
  SourceRoot,
  SourceStatus,
  SyncPlan,
  UserOut,
  Workspace,
  WorkspaceFile,
  WorkspaceFolders,
  WorkspaceMember,
  EngineRuntime,
  FolderListing,
  FolderSearchResult,
  ScopeMode,
  WebSearchAdmin,
  WebSearchTestResult,
} from '../types'

export function login(username: string, password: string) {
  return api.post<{ access_token: string; token_type: string }>('/auth/login', { username, password })
}

export function fetchMe() {
  return api.get<Me>('/auth/me')
}

export function updateMe(payload: {
  full_name?: string
  email?: string
  current_password?: string
  new_password?: string
}) {
  return api.patch<Me>('/auth/me', payload)
}

export function listDocuments(workspaceId?: string) {
  return api.get<DocumentSummary[]>(workspaceId ? `/documents?workspace_id=${workspaceId}` : '/documents')
}

// --- workspaces -------------------------------------------------------------

export function listWorkspaces() {
  return api.get<Workspace[]>('/workspaces')
}

export function createWorkspace(payload: {
  name: string
  default_department: string
  default_confidentiality: string
  visibility?: 'private' | 'organization'
  /** A folder of the server PC (Director only) - see listSourceRoots. */
  source_root?: string
  source_path?: string
}) {
  return api.post<Workspace>('/workspaces', payload)
}

// --- server folder sources (read by the backend itself) ---------------------

export function listSourceRoots() {
  return api.get<SourceRoot[]>('/sources/roots')
}

export function browseSource(rootId: string, path: string) {
  return api.get<SourceListing>(`/sources/browse?root=${encodeURIComponent(rootId)}&path=${encodeURIComponent(path)}`)
}

/** A Windows path pasted by the user -> the matching root and sub-path. */
export function resolveSourcePath(hostPath: string) {
  return api.post<{ root_id: string; path: string; host_path: string }>('/sources/resolve', { host_path: hostPath })
}

export function getSourceStatus(workspaceId: string) {
  return api.get<SourceStatus>(`/workspaces/${workspaceId}/source/status`)
}

export function requestSourceScan(workspaceId: string) {
  return api.post<{ requested: boolean }>(`/workspaces/${workspaceId}/source/scan`, {})
}

export function setSourceEnabled(workspaceId: string, enabled: boolean) {
  return api.patch<Workspace>(`/workspaces/${workspaceId}/source`, { enabled })
}

export function listInventory(workspaceId: string, state: InventoryItem['state'], offset = 0, limit = 200) {
  return api.get<InventoryItem[]>(`/workspaces/${workspaceId}/inventory?state=${state}&offset=${offset}&limit=${limit}`)
}

/** Queue the images with no OCR text for a description by the vision model (one at a time, in the background). */
export function requestVisualAnalysis(workspaceId: string) {
  return api.post<{ queued: number }>(`/workspaces/${workspaceId}/documents/visual-analysis`, {})
}

export function retryFailedDocuments(workspaceId: string) {
  return api.post<{ requeued: number }>(`/workspaces/${workspaceId}/documents/retry-failed`, {})
}

/** Removes the workspace from Orbitia (owner only). The local folder is never touched. */
export function deleteWorkspace(workspaceId: string) {
  return api.delete<void>(`/workspaces/${workspaceId}`)
}

export function updateWorkspace(workspaceId: string, payload: { visibility: 'private' | 'organization' }) {
  return api.patch<Workspace>(`/workspaces/${workspaceId}`, payload)
}

export function listWorkspaceMembers(workspaceId: string) {
  return api.get<WorkspaceMember[]>(`/workspaces/${workspaceId}/members`)
}

export function addWorkspaceMember(workspaceId: string, username: string) {
  return api.post<WorkspaceMember>(`/workspaces/${workspaceId}/members`, { username })
}

export function removeWorkspaceMember(workspaceId: string, userId: string) {
  return api.delete<void>(`/workspaces/${workspaceId}/members/${userId}`)
}

export function listFolders(workspaceId: string) {
  return api.get<WorkspaceFolders>(`/workspaces/${workspaceId}/folders`)
}

export function setFolderRestriction(workspaceId: string, folderPath: string, restricted: boolean) {
  return api.put<WorkspaceFolders>(`/workspaces/${workspaceId}/folders/restriction`, { folder_path: folderPath, restricted })
}

export function shareFolder(workspaceId: string, folderPath: string, username: string, effect: 'allow' | 'deny') {
  return api.post<WorkspaceFolders>(`/workspaces/${workspaceId}/folders/shares`, { folder_path: folderPath, username, effect })
}

export function unshareFolder(workspaceId: string, ruleId: string) {
  return api.delete<WorkspaceFolders>(`/workspaces/${workspaceId}/folders/shares/${ruleId}`)
}

export function getDocumentAccess(documentId: string) {
  return api.get<DocumentAccess>(`/documents/${documentId}/access`)
}

export function listAccessRules(documentId: string) {
  return api.get<AccessRules>(`/documents/${documentId}/access-rules`)
}

export function setAccessRule(
  documentId: string,
  payload: { username: string; effect: 'allow' | 'deny'; folder_path: string | null },
) {
  return api.post<AccessRule>(`/documents/${documentId}/access-rules`, payload)
}

export function deleteAccessRule(documentId: string, ruleId: string) {
  return api.delete<void>(`/documents/${documentId}/access-rules/${ruleId}`)
}

export function listWorkspaceFiles(workspaceId: string) {
  return api.get<WorkspaceFile[]>(`/workspaces/${workspaceId}/files`)
}

export function planWorkspaceSync(
  workspaceId: string,
  payload: {
    folder_name: string
    files: { relative_path: string; sha256: string; size: number; last_modified: string | null }[]
    excluded_directories: string[]
  },
) {
  return api.post<SyncPlan>(`/workspaces/${workspaceId}/sync/plan`, payload)
}

export function uploadWorkspaceFile(workspaceId: string, relativePath: string, file: File) {
  const form = new FormData()
  form.set('relative_path', relativePath)
  form.set('last_modified', String(file.lastModified))
  form.set('file', file)
  return api.postForm<{ document_id: string; relative_path: string; outcome: string; status: string }>(
    `/workspaces/${workspaceId}/sync/file`,
    form,
  )
}

export function getWatchStatus() {
  return api.get<{ enabled: boolean; department: string; confidentiality: string }>('/documents/watch-status')
}

export function getDocument(id: string) {
  return api.get<DocumentDetail>(`/documents/${id}`)
}

export function createDocument(payload: {
  title: string
  department: string
  confidentiality: string
  content: string
  workspace_id?: string
}) {
  return api.post<DocumentDetail>('/documents', payload)
}

export function uploadDocument(payload: {
  title: string
  department: string
  confidentiality: string
  file: File
  workspaceId?: string
  /** Destination folder inside the workspace ("RH/Paie"), root when empty. */
  folder?: string
}) {
  const form = new FormData()
  form.set('title', payload.title)
  form.set('department', payload.department)
  form.set('confidentiality', payload.confidentiality)
  form.set('file', payload.file)
  if (payload.workspaceId) form.set('workspace_id', payload.workspaceId)
  if (payload.folder) form.set('folder', payload.folder)
  return api.postForm<DocumentDetail>('/documents/upload', form)
}

export function downloadDocumentFile(id: string) {
  return api.getBlob(`/documents/${id}/file`)
}

/** Same file and same permission check, served inline with its real type (preview). */
export function previewDocumentFile(id: string) {
  return api.getBlob(`/documents/${id}/file?inline=true`)
}

export function updateDocument(
  id: string,
  payload: Partial<{ title: string; department: string; confidentiality: string; subject_user_id: string | null }>,
) {
  return api.patch<DocumentDetail>(`/documents/${id}`, payload)
}

export function getLlmStatus() {
  return api.get<LlmStatus>('/llm/status')
}

export function getVisionStatus() {
  return api.get<VisionStatus>('/vision/status')
}

/** workspaceId is a claim the backend re-checks (403 if not allowed, 409
 * if it differs from an existing conversation's own workspace). */
export function sendChatMessage(
  message: string,
  conversationId?: string,
  workspaceId?: string,
  image?: { mime_type: string; data_base64: string },
  /** Sub-folders of the workspace to search in ("BIC"); empty = all of it. */
  folderScope?: string[],
  /** "folder": those folders only; "recursive": with sub-folders; "all": every workspace. */
  scopeMode: ScopeMode = 'recursive',
) {
  return api.post<ChatResponse>('/chat', {
    message,
    conversation_id: conversationId ?? null,
    workspace_id: workspaceId ?? null,
    image: image ?? null,
    folder_scope: folderScope && folderScope.length ? folderScope : null,
    scope_mode: scopeMode,
  })
}

export function resumeChatMessage(payload: {
  conversation_id: string
  action_id: string
  // one file (read_local_file)...
  relative_path?: string
  name?: string
  content_base64?: string
  // ...or several (search_local_files)
  files?: { relative_path: string; name: string; content_base64: string }[]
  skipped?: { name: string; reason: string }[]
}) {
  return api.post<ChatResponse>('/chat/resume', payload)
}

export function listConversations(workspaceId?: string) {
  return api.get<ConversationSummary[]>(workspaceId ? `/conversations?workspace_id=${workspaceId}` : '/conversations')
}

export function getConversation(id: string) {
  return api.get<ConversationDetail>(`/conversations/${id}`)
}

export function deleteHistory() {
  return api.delete<DeleteHistoryResult>('/history')
}

export function deleteConversation(id: string) {
  return api.delete<DeleteHistoryResult>(`/history/${id}`)
}

export function listAuditLogs(params: { decision?: 'ALLOW' | 'DENY'; limit?: number; offset?: number } = {}) {
  const qs = new URLSearchParams()
  if (params.decision) qs.set('decision', params.decision)
  if (params.limit) qs.set('limit', String(params.limit))
  if (params.offset) qs.set('offset', String(params.offset))
  const suffix = qs.toString() ? `?${qs.toString()}` : ''
  return api.get<AuditLogPage>(`/audit-logs${suffix}`)
}

export function adminListUsers() {
  return api.get<UserOut[]>('/admin/users')
}

export function adminCreateUser(payload: {
  username: string
  email: string
  full_name: string
  role: string
  password: string
}) {
  return api.post<UserOut>('/admin/users', payload)
}

export function adminUpdateUser(
  id: string,
  payload: Partial<{ full_name: string; email: string; role: string; is_active: boolean; password: string }>,
) {
  return api.patch<UserOut>(`/admin/users/${id}`, payload)
}

export function adminDeleteUser(id: string) {
  return api.delete<void>(`/admin/users/${id}`)
}

export function adminUpdateUserAccess(
  id: string,
  payload: { extra_departments?: string[]; confidentiality_override?: string | null; extra_tools?: string[] },
) {
  return api.put<UserOut>(`/admin/users/${id}/access`, payload)
}

export function adminPolicyMatrix() {
  return api.get<PolicyMatrix>('/admin/policy')
}

export function adminStats() {
  return api.get<AdminStats>('/admin/stats')
}

export function adminListAlerts(params: { unread_only?: boolean; limit?: number; offset?: number } = {}) {
  const qs = new URLSearchParams()
  if (params.unread_only) qs.set('unread_only', 'true')
  if (params.limit) qs.set('limit', String(params.limit))
  if (params.offset) qs.set('offset', String(params.offset))
  const suffix = qs.toString() ? `?${qs.toString()}` : ''
  return api.get<AlertPage>(`/admin/alerts${suffix}`)
}

export function adminUnreadAlertCount() {
  return api.get<{ unread_count: number }>('/admin/alerts/unread-count')
}

export function adminMarkAlertRead(id: string) {
  return api.post<void>(`/admin/alerts/${id}/read`)
}

export function adminMarkAllAlertsRead() {
  return api.post<{ updated: number }>('/admin/alerts/read-all')
}

// --- AI engine administration (DIRECTOR) ---------------------------------------

export function adminEngineRuntime() {
  return api.get<EngineRuntime>('/admin/runtime')
}

export function adminVisionQueue(action: 'pause' | 'resume' | 'cancel') {
  return api.post<{ paused?: boolean; cancelled?: number }>(`/admin/runtime/vision/${action}`, {})
}

// --- folder hierarchy (lazy, one level per call) ---------------------------------

export function folderTree(workspaceId: string, parent = '') {
  return api.get<FolderListing>(`/workspaces/${workspaceId}/folders/tree?parent=${encodeURIComponent(parent)}`)
}

export function searchFolders(workspaceId: string, query: string) {
  return api.get<FolderSearchResult>(`/workspaces/${workspaceId}/folders/tree?q=${encodeURIComponent(query)}`)
}

// --- web search (status for everyone; settings for the Director) -----------

export function adminWebSearch() {
  return api.get<WebSearchAdmin>('/admin/websearch')
}

export function adminUpdateWebSearch(values: Partial<Omit<WebSearchAdmin, 'keys' | 'providers' | 'last_status'>>) {
  return api.put<WebSearchAdmin>('/admin/websearch', values)
}

/** Write-only: the key is sent once and never returned by the server. */
export function adminSetWebSearchKey(provider: string, apiKey: string) {
  return api.put<WebSearchAdmin>('/admin/websearch/keys', { provider, api_key: apiKey })
}

export function adminDeleteWebSearchKey(provider: string) {
  return api.delete<WebSearchAdmin>(`/admin/websearch/keys/${provider}`)
}

export function adminTestWebSearch() {
  return api.post<WebSearchTestResult>('/admin/websearch/test', {})
}

export type ConnectorCard = {
  id: string; name: string; category: string; protocol: string; description: string
  state: string; enabled?: boolean; provider?: string; tools: string[]; permissions: string[]
  configured?: boolean; error?: string | null; trigger?: string; last_status?: Record<string, unknown> | null
}
export type ConnectivitySnapshot = {
  connectors: ConnectorCard[]
  email: {
    enabled: boolean; provider: string; imap_host: string; imap_port: number; imap_ssl: boolean
    smtp_host: string; smtp_port: number; smtp_ssl: boolean; username: string; mailbox: string
    allowed_roles: string[]; tool_roles: Record<string, string[]>; send_roles: string[]
    tools: Record<string, boolean>; credentials_configured: boolean
    state: string; last_status: { state?: string; checked_at?: string; error?: string } | null
  }
  history: { at: string | null; user: string | null; action: string; connector: string | null; status: string; correlation_id: string; error: string | null }[]
}
export type EmailHit = { uid: string; from: string; to: string; subject: string; date: string; thread_id: string }
export function adminConnectivity() {
  return api.get<ConnectivitySnapshot>('/admin/connectivity')
}
export function connectivityOverview() {
  return api.get<ConnectivitySnapshot>('/connectivity/overview')
}
export function adminUpdateEmailConnector(values: Partial<ConnectivitySnapshot['email']>) {
  return api.put<ConnectivitySnapshot>('/admin/connectivity/email', values)
}
export function adminSetEmailPassword(password: string) {
  return api.put<ConnectivitySnapshot>('/admin/connectivity/email/secret', { password })
}
export function adminDeleteEmailPassword() {
  return api.delete<ConnectivitySnapshot>('/admin/connectivity/email/secret')
}
export function adminTestEmailConnector() {
  return api.post<{ state: string; checked_at?: string; error?: string }>('/admin/connectivity/email/test', {})
}
export function searchConnectorEmails(filters: { from?: string; to?: string; subject?: string; since?: string; before?: string; limit?: number }) {
  return api.post<{ messages: EmailHit[]; count: number }>('/connectivity/email/search', filters)
}
export function readConnectorEmail(uid: string) {
  return api.post<Record<string, unknown>>('/connectivity/email/read', { uid })
}
export function prepareConnectorEmail(draft: { to: string; subject: string; body: string }) {
  return api.post<{ confirmation: string; expires_in_seconds: number; draft: typeof draft }>('/connectivity/email/prepare', draft)
}
export function sendConnectorEmail(draft: { to: string; subject: string; body: string; confirmation: string }) {
  return api.post<{ status: string; to: string; subject: string }>('/connectivity/email/send', draft)
}

export type GmailAccount = { id: string; email: string; name: string; enabled: boolean; primary: boolean; send_enabled?: boolean; status: string; last_sync: string | null; error: string | null }
export function gmailAccounts() { return api.get<{ accounts: GmailAccount[]; configured: boolean; configuration: { ready: boolean; issues: string[] } }>('/connectivity/gmail/accounts') }
export function startGmailOAuth(enableSend = false, accountId?: string) { const qs = new URLSearchParams(); if (enableSend) qs.set('enable_send', 'true'); if (accountId) qs.set('account_id', accountId); return api.post<{ url: string }>(`/connectivity/gmail/oauth/start${qs.size ? `?${qs}` : ''}`, {}) }
export function updateGmailAccount(id: string, values: { enabled?: boolean; primary?: boolean }) { return api.patch<{ accounts: GmailAccount[] }>(`/connectivity/gmail/accounts/${encodeURIComponent(id)}`, values) }
export function disconnectGmailAccount(id: string) { return api.delete<{ accounts: GmailAccount[] }>(`/connectivity/gmail/accounts/${encodeURIComponent(id)}`) }
export function testGmailAccount(id: string) { return api.post<{ state: string; email: string }>(`/connectivity/gmail/accounts/${encodeURIComponent(id)}/test`, {}) }
export function searchGmail(id: string, filters: { from?: string; to?: string; subject?: string; since?: string; before?: string; limit?: number }) { return api.post<{ messages: EmailHit[]; count: number }>(`/connectivity/gmail/accounts/${encodeURIComponent(id)}/search`, filters) }
export function readGmail(id: string, messageId: string) { return api.post<Record<string, unknown>>(`/connectivity/gmail/accounts/${encodeURIComponent(id)}/read`, { message_id: messageId }) }
export function prepareGmailSend(draft: { account_id: string; to: string; subject: string; body: string }) { return api.post<{ confirmation: string; expires_in_seconds: number; from: string; draft: Omit<typeof draft, 'account_id'>; account_id: string }>('/connectivity/gmail/send/prepare', draft) }
export function sendGmail(payload: { account_id: string; to: string; subject: string; body: string; confirmation: string }) { return api.post<{ status: string; to: string; subject: string }>('/connectivity/gmail/send', payload) }
export type WhatsAppContact = { name: string; phone: string; category?: string | null }
export function whatsappContacts() { return api.get<{ contacts: Record<string, WhatsAppContact> }>('/connectivity/whatsapp/contacts') }
export function addWhatsAppContact(contact: WhatsAppContact) { return api.post<{ contacts: Record<string, WhatsAppContact> }>('/connectivity/whatsapp/contacts', contact) }
export function deleteWhatsAppContact(id: string) { return api.delete<{ contacts: Record<string, WhatsAppContact> }>(`/connectivity/whatsapp/contacts/${encodeURIComponent(id)}`) }
export function prepareWhatsApp(payload: { phone?: string; contact_id?: string; message: string }) { return api.post<{ url: string; phone: string; message: string; sent: false }>('/connectivity/whatsapp/prepare', payload) }
