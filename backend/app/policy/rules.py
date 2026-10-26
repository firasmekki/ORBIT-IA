"""Declarative policy rules.

This is the single source of truth for "who can see what". It is consulted
identically by three independent layers - the documents REST router, the
RAG retriever's SQL filter, and every MCP tool - so an authorization bug in
one layer does not become a bypass: the others still enforce it
(defense in depth).

Implemented as a plain data structure rather than Casbin/OPA to keep the
reference implementation easy to read end-to-end for this prototype; the
architecture note explains how a Casbin/OPA policy-as-code engine is a
drop-in replacement encoding the exact same rules for a production system.
"""

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.models.document import CONFIDENTIALITY_LEVELS, CONFIDENTIALITY_RANK
from app.models.workspace import DEFAULT_WORKSPACE_ID

if TYPE_CHECKING:
    from app.models.user import User

# role -> departments it may read from, and the highest confidentiality
# level it may see within those departments.
ROLE_ACCESS: dict[str, dict] = {
    "DIRECTOR": {"departments": {"HR", "FINANCE", "TECH", "GENERAL", "EXEC"}, "max_level": "SECRET"},
    "HR": {"departments": {"HR", "GENERAL"}, "max_level": "CONFIDENTIAL"},
    "ACCOUNTANT": {"departments": {"FINANCE", "GENERAL"}, "max_level": "CONFIDENTIAL"},
    "DEVELOPER": {"departments": {"TECH", "GENERAL"}, "max_level": "INTERNAL"},
    "EMPLOYEE": {"departments": {"GENERAL"}, "max_level": "INTERNAL"},
}

ALL_TOOLS: frozenset[str] = frozenset(
    {
        "search_documents",
        "get_document",
        "search_database",
        "get_company_information",
        "search_keyword",
        "search_document_text",
        "list_documents",
        "get_document_section",
        "generate_chart",
        "read_spreadsheet_data",
    }
)

_BASE_TOOLS = {
    "search_documents",
    "get_document",
    "get_company_information",
    "search_keyword",
    # Exact text search (count/occurrences) - same scope as search_keyword.
    "search_document_text",
    "list_documents",
    "get_document_section",
    # Available to every role: generate_chart never fetches data itself
    # (see app/agent/chart.py) - the actual ACL enforcement happens
    # upstream, at whichever data-source tool (read_spreadsheet_data,
    # search_database [DIRECTOR/ACCOUNTANT only]) or user-supplied input
    # produced the numbers it's asked to render.
    "generate_chart",
    # Same department/confidentiality check as get_document_section (it's
    # just another way of reading an authorized document's content) - no
    # extra role restriction on top.
    "read_spreadsheet_data",
}

# role -> departments in which it may read OTHER employees' personal
# documents (Document.subject_user_id set), still capped by max_level.
# Everyone always reads their own; a developer or an employee never reads a
# colleague's payslip, leave record or task list through their role -
# only through an explicit Director rule (DocumentAccessRule).
PERSONAL_DOC_READERS: dict[str, set[str]] = {
    "DIRECTOR": {"HR", "FINANCE", "TECH", "GENERAL", "EXEC"},
    "HR": {"HR"},
    "ACCOUNTANT": {"FINANCE"},
    "DEVELOPER": set(),
    "EMPLOYEE": set(),
}

# role -> MCP tools it is even allowed to invoke. A tool absent from a
# role's set is not offered to the LLM for that session at all - there is
# nothing for the model to be refused, because it was never given the
# option to call it (least privilege at the tool-catalog level).
ROLE_TOOLS: dict[str, set[str]] = {
    "DIRECTOR": _BASE_TOOLS | {"search_database"},
    "HR": set(_BASE_TOOLS),
    "ACCOUNTANT": _BASE_TOOLS | {"search_database"},
    "DEVELOPER": set(_BASE_TOOLS),
    "EMPLOYEE": set(_BASE_TOOLS),
}


@dataclass(frozen=True)
class Grant:
    role: str
    departments: frozenset[str]
    max_level: str
    max_level_rank: int
    tools: frozenset[str]
    # The workspace this grant is evaluated in. Set ONLY by the server after
    # checking the user may use that workspace (app/policy/workspace.py) -
    # never from LLM or request input as such. Every document query adds
    # "workspace_id = this" to the RBAC/ABAC filter (engine.document_scope).
    workspace_id: uuid.UUID = DEFAULT_WORKSPACE_ID
    # Whose grant this is (personal documents: Document.subject_user_id).
    user_id: uuid.UUID | None = None
    # Departments whose personal documents of OTHER users this role may read.
    personal_departments: frozenset[str] = frozenset()
    # A Director's explicit exceptions for this user in `workspace_id`, loaded
    # by policy.workspace.grant_in_workspace (DocumentAccessRule). Folders end
    # with "/". A deny always wins over everything else.
    allowed_document_ids: frozenset[uuid.UUID] = frozenset()
    denied_document_ids: frozenset[uuid.UUID] = frozenset()
    allowed_folders: frozenset[str] = frozenset()
    denied_folders: frozenset[str] = frozenset()
    # Folders of `workspace_id` shared "like Dropbox" (RestrictedFolder): their
    # documents are visible only through an allow rule (or to their subject)
    # - unless this user is the Director or the workspace owner.
    restricted_folders: frozenset[str] = frozenset()
    bypass_restrictions: bool = False
    # False when this user is in the workspace ONLY because a folder or a
    # file of it was shared with them (Dropbox-like): they then see what
    # was shared, and their own personal documents - nothing by role.
    role_scope: bool = True
    # The sub-folders of the workspace the user chose to work in for this
    # turn ("BIC/", "Delice CLN/"), each ending with "/". Empty = the whole
    # workspace. It only ever NARROWS what the grant shows - a convenience
    # filter on top of every rule above, never a permission.
    folder_scope: frozenset[str] = frozenset()
    # "recursive": the chosen folders and everything under them (default).
    # "direct": only the documents directly inside them - with no folder
    # chosen, only the documents at the root of the workspace.
    folder_depth: str = "recursive"


def resolve_grant(role: str) -> Grant | None:
    rules = ROLE_ACCESS.get(role)
    if rules is None:
        return None
    return Grant(
        role=role,
        departments=frozenset(rules["departments"]),
        max_level=rules["max_level"],
        max_level_rank=CONFIDENTIALITY_RANK[rules["max_level"]],
        tools=frozenset(ROLE_TOOLS.get(role, set())),
        personal_departments=frozenset(PERSONAL_DOC_READERS.get(role, set())),
    )


def is_valid_level(level: str) -> bool:
    return level in CONFIDENTIALITY_LEVELS


def resolve_effective_grant(user: "User") -> Grant | None:
    """Role grant, widened by whatever a Director has explicitly granted
    this specific user (`extra_departments` / `confidentiality_override` /
    `extra_tools`). Overrides only ever widen access, never narrow it below
    the role's baseline - revoking access means changing the role or
    clearing the override, not setting a "negative" grant.

    This is the single entry point every layer (REST API, RAG retriever,
    MCP tools) should call instead of `resolve_grant(role)` directly, so a
    per-user exception is honored everywhere consistently.
    """
    base = resolve_grant(user.role)
    if base is None:
        return None

    departments = set(base.departments) | set(user.extra_departments or [])

    max_level = base.max_level
    max_level_rank = base.max_level_rank
    override = user.confidentiality_override
    if override and is_valid_level(override):
        override_rank = CONFIDENTIALITY_RANK[override]
        if override_rank > max_level_rank:
            max_level, max_level_rank = override, override_rank

    tools = set(base.tools) | set(user.extra_tools or [])

    return Grant(
        role=user.role,
        departments=frozenset(departments),
        max_level=max_level,
        max_level_rank=max_level_rank,
        tools=frozenset(tools),
        user_id=user.id,
        personal_departments=base.personal_departments,
    )
