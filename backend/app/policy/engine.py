from dataclasses import dataclass

from sqlalchemy import and_, false, func, not_, or_, true

from app.models.document import CONFIDENTIALITY_RANK, Document
from app.policy.rules import Grant


# --- structured tool outcomes -------------------------------------------------
#
# Every tool error carries one of these as its `code`, so callers decide on
# the code - never on the wording, which is sometimes deliberately identical
# for two different outcomes (DOCUMENT_NOT_FOUND_OR_DENIED, see
# app/mcp/server.py). Only ACCESS_DENIED is a security event: it is the one
# code that may raise a Director-facing alert. A missing file, a validation
# error or a technical failure is logged, never alerted on.
ACCESS_DENIED = "ACCESS_DENIED"
NOT_FOUND = "NOT_FOUND"
WORKSPACE_FILE_NOT_FOUND = "WORKSPACE_FILE_NOT_FOUND"
VALIDATION_ERROR = "VALIDATION_ERROR"
AMBIGUOUS = "AMBIGUOUS"
SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"

_STATUS_BY_CODE = {
    ACCESS_DENIED: "forbidden",
    NOT_FOUND: "not_found",
    WORKSPACE_FILE_NOT_FOUND: "not_found",
    VALIDATION_ERROR: "invalid",
    AMBIGUOUS: "ambiguous",
    SERVICE_UNAVAILABLE: "unavailable",
}

# Keys only the backend may read - stripped before a tool result is shown
# to the LLM, and never part of the API's ToolTraceEntry schema, so an
# outcome that is deliberately worded the same for "absent" and "refused"
# cannot be told apart by the user (or the model) through them.
INTERNAL_RESULT_KEYS = frozenset({"code", "status", "concealed", "alert_raised"})


def tool_error(code: str, message: str, *, concealed: bool = False, **extra) -> dict:
    """`concealed`: the user-facing message is the same whatever the code
    (existence of an unauthorized resource must not leak) - the visible
    trace decision must then stay identical too (see classify_tool_result)."""
    result = {"error": message, "code": code, "status": _STATUS_BY_CODE[code], **extra}
    if concealed:
        result["concealed"] = True
    return result


def error_code(result: dict) -> str | None:
    """The outcome code of a tool result, None on success. A result with an
    error but no code (an older or unclassified path) is treated as a
    denial: failing closed means a real refusal can never go unalerted just
    because someone forgot to tag it."""
    if "error" not in result:
        return None
    if result.get("code"):
        return result["code"]
    if result.get("service_unavailable"):
        return SERVICE_UNAVAILABLE
    return ACCESS_DENIED


def classify_tool_result(result: dict) -> tuple[str, str | None]:
    """(user-visible decision, internal code). The decision is ALLOW, DENY
    (a confirmed permission refusal) or ERROR (not found, invalid, ambiguous,
    unavailable). A concealed outcome always shows ERROR, whether the code
    is NOT_FOUND or ACCESS_DENIED - the alert still fires on the code."""
    code = error_code(result)
    if code is None:
        return "ALLOW", None
    if code == ACCESS_DENIED and not result.get("concealed"):
        return "DENY", code
    return "ERROR", code


def public_tool_result(result: dict) -> dict:
    """What the LLM may see of a tool result: no internal outcome keys, and
    a restricted_match reduced to its existence (its department/level are
    for the audit log, not for a model that might repeat them)."""
    public = {k: v for k, v in result.items() if k not in INTERNAL_RESULT_KEYS}
    if public.get("restricted_match"):
        public["restricted_match"] = True
    return public


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str
    # Refused because the document is not in the caller's current workspace
    # (or was removed from it) - answered as "not found", never alerted on:
    # it is outside the context, not a permission probe.
    out_of_context: bool = False
    # Which rule opened (or closed) the document, for the "who can see it"
    # panel: "role" | "personal" | "granted" | "denied" | None.
    basis: str | None = None


def check_document_access(*, grant: Grant | None, department: str, confidentiality: str) -> PolicyDecision:
    if grant is None:
        return PolicyDecision(False, "unknown role")
    if department not in grant.departments:
        return PolicyDecision(False, f"role {grant.role} has no access to department {department}")
    doc_rank = CONFIDENTIALITY_RANK.get(confidentiality)
    if doc_rank is None:
        return PolicyDecision(False, f"unknown confidentiality level '{confidentiality}'")
    if doc_rank > grant.max_level_rank:
        return PolicyDecision(
            False,
            f"role {grant.role} is capped at {grant.max_level} but document is {confidentiality}",
        )
    return PolicyDecision(True, "allowed")


def check_tool_access(*, grant: Grant | None, tool_name: str) -> PolicyDecision:
    if grant is None:
        return PolicyDecision(False, "unknown role")
    if tool_name not in grant.tools:
        return PolicyDecision(False, f"role {grant.role} is not permitted to use tool '{tool_name}'")
    return PolicyDecision(True, "allowed")


def _in_folders(folders: frozenset[str]):
    path = func.coalesce(Document.relative_path, "")
    return or_(*[path.startswith(folder, autoescape=True) for folder in sorted(folders)])


def _visibility_clause(grant: Grant):
    """Who sees a document, inside its workspace - the SQL twin of
    check_document_visibility below (kept in lockstep by
    tests/test_document_access_rules.py). Every term is NULL-safe, so its
    negation (outside_document_scope) is exact too.

      NOT denied by a Director rule
      AND ( it is my own personal document
            OR a Director rule allows it to me (this is also how a
               restricted folder is shared)
            OR my role allows its department/confidentiality AND
               (it is nobody's personal document OR my role manages the
                personal documents of its department) AND
               (it is not in a restricted folder OR I am the Director /
                the workspace owner) )"""
    terms_denied = []
    if grant.denied_document_ids:
        terms_denied.append(Document.id.in_(grant.denied_document_ids))
    if grant.denied_folders:
        terms_denied.append(_in_folders(grant.denied_folders))

    personal_ok = Document.subject_user_id.is_(None)
    if grant.personal_departments:
        personal_ok = or_(personal_ok, Document.department.in_(grant.personal_departments))
    role_terms = [
        Document.department.in_(grant.departments),
        Document.confidentiality_rank <= grant.max_level_rank,
        personal_ok,
    ]
    if grant.restricted_folders and not grant.bypass_restrictions:
        role_terms.append(not_(_in_folders(grant.restricted_folders)))
    role_ok = and_(*role_terms) if grant.departments and grant.role_scope else false()
    granted = [role_ok]
    if grant.user_id is not None:
        granted.append(and_(Document.subject_user_id.is_not(None), Document.subject_user_id == grant.user_id))
    if grant.allowed_document_ids:
        granted.append(Document.id.in_(grant.allowed_document_ids))
    if grant.allowed_folders:
        granted.append(_in_folders(grant.allowed_folders))

    visible = or_(*granted)
    return and_(not_(or_(*terms_denied)), visible) if terms_denied else visible


def document_scope(grant: Grant | None) -> list:
    """THE SQL visibility filter for documents, used by every retrieval path
    (RAG, exact search, listing, sections, spreadsheets, REST list):
    current workspace AND still active AND visible to this user (role
    department/confidentiality, personal documents, Director rules).
    Applied inside the query, before any row is read - never a post-filter."""
    if grant is None:
        return [false()]
    return [
        Document.workspace_id == grant.workspace_id,
        Document.is_active.is_(True),
        _visibility_clause(grant),
        *_folder_scope_clause(grant),
    ]


def _folder_scope_clause(grant: Grant) -> list:
    """The sub-folders chosen for this turn (Grant.folder_scope) - only ever
    narrows. autoescape: a folder named "50%_final" matches literally.
    Grant.folder_depth "direct": only the documents directly inside them
    (no "/" after the folder prefix); with no folder chosen, only the
    documents at the root of the workspace."""
    if grant.folder_depth == "direct":
        if not grant.folder_scope:
            return [or_(Document.relative_path.is_(None), func.strpos(Document.relative_path, "/") == 0)]
        return [
            or_(
                *(
                    and_(
                        Document.relative_path.startswith(folder, autoescape=True),
                        func.strpos(func.substr(Document.relative_path, len(folder) + 1), "/") == 0,
                    )
                    for folder in sorted(grant.folder_scope)
                )
            )
        ]
    if not grant.folder_scope:
        return []
    return [or_(*(Document.relative_path.startswith(folder, autoescape=True) for folder in sorted(grant.folder_scope)))]


def path_in_scope(path: str | None, grant: Grant) -> bool:
    """Python twin of _folder_scope_clause, for one already-loaded document."""
    path = path or ""
    if grant.folder_depth == "direct":
        if not grant.folder_scope:
            return "/" not in path
        return any(path.startswith(f) and "/" not in path[len(f):] for f in grant.folder_scope)
    return not grant.folder_scope or _path_in(path, grant.folder_scope)


def outside_document_scope(grant: Grant | None) -> list:
    """Documents of the SAME workspace the caller may NOT read - only for the
    "a closer match exists but is forbidden" probes that feed alerts/audit.
    Never other workspaces: their existence is not this caller's business."""
    if grant is None:
        return [false()]
    return [Document.workspace_id == grant.workspace_id, Document.is_active.is_(True), not_(_visibility_clause(grant))]


def path_in_folders(path: str | None, folders: frozenset[str]) -> bool:
    return any((path or "").startswith(folder) for folder in folders)


_path_in = path_in_folders


def check_document_visibility(*, grant: Grant | None, document: Document) -> PolicyDecision:
    """Python twin of _visibility_clause, for one already-loaded document
    (direct access by id, the "who can see this" panel). The reason says
    which rule decided."""
    if grant is None:
        return PolicyDecision(False, "unknown role")
    if document.id in grant.denied_document_ids or _path_in(document.relative_path, grant.denied_folders):
        return PolicyDecision(False, "access explicitly denied by a Director", basis="denied")
    if grant.user_id is not None and document.subject_user_id == grant.user_id:
        return PolicyDecision(True, "own personal document", basis="personal")
    if document.id in grant.allowed_document_ids or _path_in(document.relative_path, grant.allowed_folders):
        return PolicyDecision(True, "access explicitly granted by a Director", basis="granted")
    if not grant.role_scope:
        return PolicyDecision(False, "only the folders shared with this user are visible in this workspace")
    if not grant.bypass_restrictions and _path_in(document.relative_path, grant.restricted_folders):
        return PolicyDecision(False, "restricted folder - not shared with this user")
    decision = check_document_access(grant=grant, department=document.department, confidentiality=document.confidentiality)
    if not decision.allowed:
        return decision
    if document.subject_user_id is not None and document.department not in grant.personal_departments:
        return PolicyDecision(False, f"personal document of another employee - role {grant.role} does not manage them")
    return PolicyDecision(True, decision.reason, basis="role")


def check_document_in_scope(*, grant: Grant | None, document: Document) -> PolicyDecision:
    """Single-document form of document_scope (direct access by id)."""
    if grant is None:
        return PolicyDecision(False, "unknown role")
    if document.workspace_id != grant.workspace_id:
        return PolicyDecision(False, "document belongs to another workspace", out_of_context=True)
    if not document.is_active:
        return PolicyDecision(False, "document removed from its workspace", out_of_context=True)
    if not path_in_scope(document.relative_path, grant):
        return PolicyDecision(False, "document outside the folders selected for this conversation", out_of_context=True)
    return check_document_visibility(grant=grant, document=document)


def retrieval_scope(*, grant: Grant | None) -> tuple[frozenset[str], int]:
    """Departments and max confidentiality rank a grant may retrieve from.

    Consumed directly by the RAG retriever's SQL WHERE clause, so filtering
    happens inside the vector query itself - before any chunk is returned,
    not after the LLM has already seen it.
    """
    if grant is None:
        return frozenset(), -1
    return grant.departments, grant.max_level_rank
