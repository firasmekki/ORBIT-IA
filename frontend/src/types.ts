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
  workspace_id: string
  /** Synced workspace file: its path inside the connected folder. */
  relative_path: string | null
  status: string
  index_progress: number | null
  indexed_at: string | null
  /** Personal document: the employee it concerns (always sees it; colleagues never do by role alone). */
  subject_user_id: string | null
  subject_name: string | null
  /** Who added it: the uploader, or the owner who synced the folder. */
  added_by_name: string | null
  /** "sync" (server folder) | "upload" (dropped from any PC) | null (older). */
  origin: string | null
  /** Type of the original file ("image/png", "application/pdf"...). */
  file_mime_type: string | null
}

export interface WorkspaceFolderShare {
  id: string
  username: string
  full_name: string
  effect: 'allow' | 'deny'
}

export interface WorkspaceFolder {
  path: string
  document_count: number
  restricted: boolean
  restricted_by: string | null
  shares: WorkspaceFolderShare[]
  can_upload: boolean
}

export interface WorkspaceFolders {
  can_manage: boolean
  folders: WorkspaceFolder[]
}

export interface Workspace {
  id: string
  name: string
  is_default: boolean
  is_owner: boolean
  /** Only the owner can connect a folder and sync it. */
  can_sync: boolean
  default_department: string
  default_confidentiality: string
  folder_name: string | null
  last_synced_at: string | null
  /** Indexed documents of this workspace that the user's role may read. */
  document_count: number
  /** Of those, still getting their embeddings in the background. */
  pending_count: number
  /** "private": owner + members; "organization": every user (RBAC still applies per document). */
  visibility: 'private' | 'organization'
  owner_name: string | null
  member_count: number
  /** A folder of the server PC read by the backend itself (no browser tab needed). */
  source?: WorkspaceSource | null
}

export interface WorkspaceSource {
  root_id: string
  path: string
  /** "C:\Users\ORBIT\Desktop\projets" - display only. */
  host_path: string
  enabled: boolean
  status: 'idle' | 'scanning' | 'missing' | 'error' | null
  error: string | null
  scan_started_at: string | null
  scan_seen: number | null
}

export interface SourceRoot {
  id: string
  label: string
  host_path: string
  available: boolean
}

export interface SourceListing {
  root_id: string
  path: string
  host_path: string
  parent: string | null
  folders: string[]
  file_count: number
}

export interface SourceStatus {
  source: WorkspaceSource | null
  last_synced_at: string | null
  detected_files: number
  excluded_directories: number
  /** Files inside the excluded tool directories (counted, never read). */
  excluded_files: number
  /** Why documents have no text / no full index: no_text_image, no_text_scan, ocr_failed, corrupt, empty, embedding. */
  issues: Record<string, number>
  visual_described: number
  visual_pending: number
  ignored: number
  unreadable: number
  documents: {
    queued: number
    extracting: number
    indexing: number
    indexed: number
    failed: number
    no_text: number
    visual_queued: number
    visual_running: number
  }
  text_ready: number
  fully_indexed: number
  in_progress: number
  complete: boolean
  reasons: { reason: string; count: number }[]
}

export interface InventoryItem {
  relative_path: string
  state: 'ignored' | 'unreadable' | 'failed' | 'no_text'
  reason: string | null
  size_bytes: number | null
  issue_kind?: string | null
}

export interface WorkspaceMember {
  user_id: string
  username: string
  full_name: string
}

export interface DocumentAccessPerson {
  full_name: string
  username: string
  role: string
  /** Why they see it: their role, their own personal document, or a Director rule. */
  basis: 'role' | 'personal' | 'granted' | 'denied' | null
}

export interface AccessRule {
  id: string
  username: string
  full_name: string
  effect: 'allow' | 'deny'
  folder_path: string | null
  created_at: string | null
}

export interface AccessRules {
  rules: AccessRule[]
  folders: string[]
}

export interface DocumentAccess {
  workspace: { id: string; name: string; visibility: 'private' | 'organization'; is_default: boolean; is_owner: boolean }
  visible_to: DocumentAccessPerson[]
  blocked_by_workspace: DocumentAccessPerson[]
  blocked_by_role_count: number
  denied_by_rule: DocumentAccessPerson[]
  can_manage_rules: boolean
}

export interface WorkspaceFile {
  id: string
  title: string
  relative_path: string | null
  status: string
  department: string
  confidentiality: string
  size_bytes: number | null
  indexed_at: string | null
}

export interface SyncPlan {
  to_upload: string[]
  unchanged: string[]
  removed: string[]
  rejected: { relative_path: string; reason: string }[]
}

export interface DocumentDetail extends DocumentSummary {
  content: string
}

export interface SourceRef {
  /** "local": a workspace document; "web": a page found by the web search. */
  kind?: 'local' | 'web'
  document_id: string | null
  title: string
  department: string
  confidentiality: string
  excerpt: string
  relative_path?: string | null
  page?: number | null
  citation?: string | null
  url?: string | null
  domain?: string | null
  published?: string | null
  accessed_at?: string | null
  provider?: string | null
  fetch_status?: string | null
  fetch_note?: string | null
}

export interface ToolTraceEntry {
  tool: string
  arguments: Record<string, unknown>
  // DENY = permission refused; ERROR = not found / invalid / unavailable
  // (never a security event - see backend app/policy/engine.py)
  decision: 'ALLOW' | 'DENY' | 'ERROR'
  reason: string
}

export type ChartType = 'line' | 'bar' | 'pie' | 'scatter' | 'kpi'

export interface ChartValueField {
  field: string
  label: string
  unit: string | null
  /** Id of the underlying data series (multi-series charts), for drill-down. */
  id?: string
}

/** Everything known about one plotted item, already ACL-filtered by the
 * backend - what a click on its bar/point/slice opens. */
export interface ChartDrilldownItem {
  id: string
  label: string
  unit: string | null
  points: { period: string; value: number }[]
  compare_with: string[]
}

export interface ChartSourceRef {
  document_id: string | null
  title: string
  page: number | null
  section: string | null
}

export interface ChartSpec {
  type?: 'chart'
  status?: 'ready'
  chart_type: ChartType
  title: string
  category_field: string | null
  category_label: string | null
  value_fields: ChartValueField[]
  /** Each row may carry an `id` - the identifier of the item it plots. */
  data: Record<string, unknown>[]
  sources: ChartSourceRef[]
  statistics?: Record<string, {
    unit?: string | null
    count: number
    missing_count: number
    mean?: number
    median?: number
    min?: number
    max?: number
    stddev_sample?: number | null
    quality: number
    warnings: string[]
  }>
  x_axis?: { label: string | null }
  y_axis?: { label: string | null }
  drilldown?: Record<string, ChartDrilldownItem>
  /** What the chart claims to show (backend orchestrator._chart_contract), re-checked before drawing. */
  contract?: ChartContract
}

export interface ClarificationOption {
  label: string
  /** Sent as the user's answer when the option is clicked. */
  value: string
}

/** The agent needs more information before it can answer. */
export interface Clarification {
  type: 'clarification'
  status: 'need_more_information'
  message: string
  questions: string[]
  options: ClarificationOption[]
}

/** Exact search result ("combien de fois…", "recherche X"). */
export interface SearchOccurrence {
  page: number | null
  section: string | null
  line: number
  excerpt: string
}

export interface SearchResultDocument {
  document_id: string
  title: string
  relative_path: string | null
  department: string
  confidentiality: string
  /** Every occurrence in the file - `occurrences` lists the first ones. */
  count: number
  occurrences: SearchOccurrence[]
}

export interface SearchResults {
  type: 'keyword_search'
  term: string
  total: number
  document_count: number
  workspace: string | null
  documents: SearchResultDocument[]
}

export interface LlmStatus {
  status: 'ready' | 'loading' | 'unavailable' | 'error' | 'misconfigured'
  model: string
  message: string
  /** Why, in plain words (no path, no key) - shown as the banner's tooltip. */
  detail?: string | null
}

export interface VisionStatus {
  status: 'ready' | 'loading' | 'unavailable' | 'disabled'
  model: string
}

export interface MessageOut {
  id: string
  role: 'user' | 'assistant'
  content: string
  sources: SourceRef[]
  tool_trace: ToolTraceEntry[]
  chart?: ChartSpec | null
  clarification?: Clarification | null
  search_results?: SearchResults | null
  email_results?: EmailResults | null
  /** direct | general | documents | web | hybrid | email | unavailable (absent on older messages) */
  answer_mode?: string | null
  created_at: string
}

/** Personal mailbox answer (app/agent/email_turn.py): facts from the
 * provider, the local model's summaries kept apart. */
export interface EmailResultCard {
  ref: string
  web_link: string | null
  status: 'listed' | 'summarized' | 'facts_only' | 'error'
  facts: {
    sender: { address: string; name: string | null } | null
    subject: string | null
    received_at: string | null
    is_unread: boolean | null
    has_attachments: boolean | null
    attachments: { filename: string | null; mime_type: string | null; size_bytes: number | null }[]
    snippet: string | null
  }
  ai: { summary: string | null; requests: string[]; decisions: string[]; needs_action: boolean } | null
  deadlines: { text: string; evidence: string }[]
  suggestions: string[]
  note?: string
  error?: string
}

export interface EmailResults {
  type: 'email_results'
  expired?: boolean
  account?: { id: string; provider: string; provider_label: string; email_address: string; label: string | null }
  query?: { limit: number; limit_is_default: boolean; period: string | null; unread_only: boolean; from: string | null; needs_action: boolean; timezone?: string }
  items: EmailResultCard[]
  total_examined: number
  report: {
    facts: { examined: number; unread: number; with_attachments: number; senders: string[] }
    ai: { summarized: number; needs_action: number; deadlines: { subject: string | null; text: string; evidence: string }[] }
    suggestions: string[]
  } | null
  notices: string[]
  has_more?: boolean
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
  excluded_directory: string | null
  size_known: boolean
}

export interface PendingClientAction {
  action_id: string
  /** "read_local_file" (one file) or "search_local_files" (every file in
   * `files`, searched by the backend for `query`). */
  tool: string
  relative_path?: string | null
  name?: string | null
  query?: string | null
  files?: { relative_path: string; name: string }[] | null
  skipped?: { name: string; reason: string }[] | null
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
  workspace_id: string | null
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
  decision: 'ALLOW' | 'DENY' | 'ERROR'
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

export type ModelState = 'STOPPED' | 'STARTING' | 'READY' | 'BUSY' | 'IDLE' | 'SLEEPING' | 'ERROR' | 'DISABLED'

export interface ModelRuntime {
  kind: 'llm' | 'vision'
  model: string
  state: ModelState
  detail: string | null
  in_flight: number
  idle_seconds: number | null
  sleep_after_seconds: number | null
  sleep_in_seconds: number | null
}

export interface WorkerRuntime {
  name: string
  state: 'idle' | 'busy' | 'paused' | 'waiting_model' | 'waiting_chat'
  items_this_batch: number
  items_since_start: number
  last_item_at: string | null
}

export interface EngineRuntime {
  generated_at: string
  models: ModelRuntime[]
  chat_turns_in_progress: number
  queues: Record<'queued' | 'extracting' | 'indexing' | 'visual_queued' | 'visual_running' | 'failed' | 'visual_retrying', number>
  vision_queue: { paused: boolean; updated_by: string | null; updated_at: string | null }
  workers: WorkerRuntime[]
  docker_vm_memory: { total_mib: number; available_mib: number; processes_mib: number; file_cache_mib: number } | null
}

export interface ChartContract {
  metrics: { id: string; label: string; unit: string | null }[]
  unit: string | null
  periods: string[]
  categories: string[]
  sources: string[]
  recommended_type: ChartType
  rationale: string
  /** Every plotted value with where it comes from (never a value without source). */
  points?: ChartPoint[]
}

export type ChartSourceKind = 'local' | 'web' | 'financial_table' | 'user'

export interface ChartPointSource {
  kind: ChartSourceKind
  title?: string | null
  path?: string | null
  page?: number | null
  section?: string | null
  excerpt?: string | null
  document_id?: string | null
  record?: string | null
  url?: string | null
  accessed_at?: string | null
}

export interface ChartPoint {
  series?: string
  period: string
  value: number
  unit?: string | null
  source?: ChartPointSource
}

/** One folder of the hierarchy (GET /workspaces/{id}/folders/tree). The
 * three counters are never mixed: documents directly inside it, its direct
 * sub-folders, and documents in it plus all its sub-folders. */
export interface FolderTreeEntry {
  path: string
  name: string
  parent: string
  direct_documents: number
  subfolder_count: number
  total_documents: number
  has_children: boolean
  restricted: boolean
  restricted_by: string | null
  shares: WorkspaceFolderShare[]
  can_upload: boolean
}

export interface FolderListing {
  can_manage: boolean
  workspace_name: string
  folder: FolderTreeEntry
  breadcrumb: { name: string; path: string }[]
  folders: FolderTreeEntry[]
  folders_truncated: boolean
  documents: { id: string; title: string; relative_path: string | null; status: string }[]
  documents_truncated: boolean
}

export interface FolderSearchResult {
  can_manage: boolean
  query: string
  results: FolderTreeEntry[]
  result_count: number
}

/** Where the assistant searches: the chosen folders only ("folder"), with
 * all their sub-folders ("recursive", default), or every workspace the
 * user may use ("all", document search only). */
export type ScopeMode = 'folder' | 'recursive' | 'all'

export interface WebSearchAdmin {
  enabled: boolean
  provider: string
  trigger: 'auto' | 'manual'
  max_results: number
  fetch_pages: number
  timeout_seconds: number
  language: string
  country: string
  cache_ttl_seconds: number
  max_per_hour: number
  google_engine_id: string
  searxng_url: string
  keys: Record<string, { configured: boolean; source: 'env' | 'admin' | null }>
  providers: { id: string; label: string; note: string }[]
  last_status: { provider?: string; status?: string; connection?: string; error_kind?: string | null; error?: string | null; at?: string; results?: number; test?: boolean; unresponsive_engines?: string[] }
}

export interface WebSearchTestResult {
  provider: string
  provider_label: string
  status: string
  /** connected | not_configured | invalid_key | rate_limited | timeout | provider_error | network_error | config_error */
  connection: string
  error_kind: string | null
  error: string | null
  results: number
  sample: { title: string; url: string }[]
  at: string
  unresponsive_engines?: string[]
}
