from dataclasses import dataclass

from app.models.document import CONFIDENTIALITY_RANK
from app.policy.rules import Grant


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str


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


def retrieval_scope(*, grant: Grant | None) -> tuple[frozenset[str], int]:
    """Departments and max confidentiality rank a grant may retrieve from.

    Consumed directly by the RAG retriever's SQL WHERE clause, so filtering
    happens inside the vector query itself - before any chunk is returned,
    not after the LLM has already seen it.
    """
    if grant is None:
        return frozenset(), -1
    return grant.departments, grant.max_level_rank
