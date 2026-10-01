export type Role = 'DIRECTOR' | 'HR' | 'ACCOUNTANT' | 'DEVELOPER' | 'EMPLOYEE'

export interface Me {
  id: string
  username: string
  full_name: string
  email: string
  role: Role
  departments: string[]
  max_confidentiality: string
  allowed_tools: string[]
}

export interface DocumentSummary {
  id: string
  title: string
  department: string
  confidentiality: string
  source_filename: string | null
  created_at: string
}

export interface DocumentDetail extends DocumentSummary {
  content: string
}

export interface SourceRef {
  document_id: string
  title: string
  department: string
  confidentiality: string
  excerpt: string
}

export interface ToolTraceEntry {
  tool: string
  arguments: Record<string, unknown>
  decision: 'ALLOW' | 'DENY'
  reason: string
}

export type ChartType = 'line' | 'bar' | 'pie' | 'scatter'

export interface ChartValueField {
  field: string
  label: string
  unit: string | null
}

export interface ChartSourceRef {
  document_id: string | null
  title: string
  page: number | null
  section: string | null
}

export interface ChartSpec {
  chart_type: ChartType
  title: string
  category_field: string | null
  category_label: string | null
  value_fields: ChartValueField[]
  data: Record<string, unknown>[]
  sources: ChartSourceRef[]
}

export interface MessageOut {
  id: string
  role: 'user' | 'assistant'
  content: string
  sources: SourceRef[]
  tool_trace: ToolTraceEntry[]
  chart?: ChartSpec | null
  created_at: string
}

// Local File Workspace (Phase 1, read-only) - see app/agent/local_files.py
// and frontend/src/lib/localFs.ts. Metadata only; content is never part
// of this type, it's read on demand and sent separately to /api/chat/resume.
export interface LocalFileEntry {
  name: string
  relative_path: string
  extension: string
  size: number
  modified_at: string | null
  parent_folder: string | null
}

export interface PendingClientAction {
  action_id: string
  tool: string
  relative_path: string
  name: string
}

export interface ChatResponse {
  conversation_id: string
  message: MessageOut | null
  pending_client_action: PendingClientAction | null
}

export interface ConversationSummary {
  id: string
  title: string
  created_at: string
}

export interface ConversationDetail extends ConversationSummary {
  messages: MessageOut[]
}

export interface DeleteHistoryResult {
  deleted_conversation_count: number
  deleted_message_count: number
  audit_log_id: string | null
}

export interface AuditLogOut {
  id: string
  user_id: string | null
  username: string | null
  role: string | null
  action: string
  resource_type: string | null
  resource_id: string | null
  decision: 'ALLOW' | 'DENY'
  reason: string | null
  extra: Record<string, unknown> | null
  created_at: string
}

export interface AuditLogPage {
  items: AuditLogOut[]
  total: number
}

export interface UserOut {
  id: string
  username: string
  full_name: string
  email: string
  role: Role
  is_active: boolean
  created_at: string
  extra_departments: string[]
  confidentiality_override: string | null
  extra_tools: string[]
  effective_departments: string[]
  effective_max_confidentiality: string
  effective_tools: string[]
}

export interface PolicyMatrix {
  [role: string]: {
    departments: string[]
    max_level: string
    tools: string[]
  }
}

export interface AdminStats {
  total_users: number
  total_documents: number
  total_audit_events: number
  total_denied_events: number
  users_by_role: Record<string, number>
}

export type AlertType =
  | 'CHAT_ACCESS_DENIED'
  | 'DOCUMENT_ACCESS_DENIED'
  | 'HISTORY_CONVERSATION_DELETED'
  | 'HISTORY_ALL_DELETED'

export interface AlertOut {
  id: string
  alert_type: AlertType
  user_id: string | null
  username: string | null
  role: string | null
  title: string
  description: string
  resource_type: string | null
  resource_id: string | null
  is_read: boolean
  read_at: string | null
  created_at: string
  audit_log_id: string | null
}

export interface AlertPage {
  items: AlertOut[]
  total: number
  unread_count: number
}
