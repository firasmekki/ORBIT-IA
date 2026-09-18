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


@dataclass(frozen=True)
class CallerIdentity:
    user_id: str
    role: str


def extract_identity(ctx: Context) -> CallerIdentity:
    request = getattr(ctx.request_context, "request", None)
    if request is None:
        raise IdentityError("no HTTP request available for this call (unexpected transport)")

    token = request.headers.get("x-internal-auth")
    if not token:
        raise IdentityError("missing X-Internal-Auth header")

    try:
        payload = verify_internal_token(token)
    except jwt.PyJWTError as exc:
        raise IdentityError(f"invalid internal auth token: {exc}") from exc

    return CallerIdentity(user_id=payload["sub"], role=payload["role"])
