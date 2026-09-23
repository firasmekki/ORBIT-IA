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

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.models.document import CONFIDENTIALITY_LEVELS, CONFIDENTIALITY_RANK

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
        "list_documents",
        "get_document_section",
    }
)

_BASE_TOOLS = {
    "search_documents",
    "get_document",
    "get_company_information",
    "search_keyword",
    "list_documents",
    "get_document_section",
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
    )
