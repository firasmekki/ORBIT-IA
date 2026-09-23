import { api } from './client'
import type {
  AdminStats,
  AlertPage,
  AuditLogPage,
  ConversationDetail,
  ConversationSummary,
  DocumentDetail,
  DocumentSummary,
  Me,
  MessageOut,
  PolicyMatrix,
  UserOut,
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

export function listDocuments() {
  return api.get<DocumentSummary[]>('/documents')
}

export function getWatchStatus() {
  return api.get<{ enabled: boolean; department: string; confidentiality: string }>('/documents/watch-status')
}

export function getDocument(id: string) {
  return api.get<DocumentDetail>(`/documents/${id}`)
}

export function createDocument(payload: { title: string; department: string; confidentiality: string; content: string }) {
  return api.post<DocumentDetail>('/documents', payload)
}

export function uploadDocument(payload: { title: string; department: string; confidentiality: string; file: File }) {
  const form = new FormData()
  form.set('title', payload.title)
  form.set('department', payload.department)
  form.set('confidentiality', payload.confidentiality)
  form.set('file', payload.file)
  return api.postForm<DocumentDetail>('/documents/upload', form)
}

export function downloadDocumentFile(id: string) {
  return api.getBlob(`/documents/${id}/file`)
}

export function updateDocument(
  id: string,
  payload: Partial<{ title: string; department: string; confidentiality: string }>,
) {
  return api.patch<DocumentDetail>(`/documents/${id}`, payload)
}

export function sendChatMessage(message: string, conversationId?: string) {
  return api.post<{ conversation_id: string; message: MessageOut }>('/chat', {
    message,
    conversation_id: conversationId ?? null,
  })
}

export function listConversations() {
  return api.get<ConversationSummary[]>('/conversations')
}

export function getConversation(id: string) {
  return api.get<ConversationDetail>(`/conversations/${id}`)
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
