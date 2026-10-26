"""Per-call caller identity for the MCP server.

The identity never comes from a tool argument - it comes from the signed
`X-Internal-Auth` header set by the agent orchestrator on the MCP HTTP
connection, read here from the transport's raw request object. Tool
functions receive a `ctx: Context` parameter that FastMCP injects itself
(it is excluded from the JSON schema shown to the LLM), so there is no
argument in any tool's signature the model could use to claim an identity.
"""

from dataclasses import dataclass

import jwt
from mcp.server.fastmcp import Context

from app.core.security import verify_internal_token


class IdentityError(Exception):
    pass


class IdentityExpired(IdentityError):
    """A genuine token whose lifetime ran out during a long turn: a technical
    timeout, not an access attempt - must never be reported as a denial."""


@dataclass(frozen=True)
class CallerIdentity:
    user_id: str
    role: str
    # Signed by the backend after checking the user may use it; re-checked
    # by every tool (server._load_user_and_grant). None = default workspace.
    workspace_id: str | None = None
    # Sub-folders of that workspace chosen for this turn ("BIC/"), signed by
    # the backend - narrows every tool's document_scope. Empty = all of it.
    folder_scope: tuple[str, ...] = ()
    # "recursive" (default) or "direct" - see policy.rules.Grant.folder_depth.
    folder_depth: str = "recursive"


def extract_identity(ctx: Context) -> CallerIdentity:
    request = getattr(ctx.request_context, "request", None)
    if request is None:
        raise IdentityError("no HTTP request available for this call (unexpected transport)")

    token = request.headers.get("x-internal-auth")
    if not token:
        raise IdentityError("missing X-Internal-Auth header")

    try:
        payload = verify_internal_token(token)
    except jwt.ExpiredSignatureError as exc:
        raise IdentityExpired(f"session interne expirée : {exc}") from exc
    except jwt.PyJWTError as exc:
        raise IdentityError(f"invalid internal auth token: {exc}") from exc

    scope = payload.get("folder_scope") or []
    return CallerIdentity(
        user_id=payload["sub"],
        role=payload["role"],
        workspace_id=payload.get("workspace_id"),
        folder_scope=tuple(str(folder) for folder in scope if isinstance(folder, str) and folder),
        folder_depth="direct" if payload.get("folder_depth") == "direct" else "recursive",
    )
