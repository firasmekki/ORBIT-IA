"""Authentication primitives.

Two distinct token types are minted here, deliberately kept separate:

- **User session tokens** (`create_access_token` / `decode_access_token`):
  issued at login, sent by the frontend on every API call, identify a human
  user. This is the layer that would be swapped for Keycloak/OIDC later.

- **Internal service tokens** (`mint_internal_token` / `verify_internal_token`):
  minted by the backend, on the backend's own initiative, right before the
  agent orchestrator calls the MCP server for a given chat turn. They are
  short-lived (default 120s), signed with a *different* secret than user
  tokens, and are the ONLY way the MCP server learns who is asking. The LLM
  never sees this token and never supplies a user id itself - the tool
  schemas exposed to the model have no identity field at all.
"""

import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
import jwt

from app.core.config import get_settings

settings = get_settings()


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        return False


def create_access_token(*, user_id: str, role: str, full_name: str) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": user_id,
        "role": role,
        "name": full_name,
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_expire_minutes),
        "type": "user",
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict[str, Any]:
    payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    if payload.get("type") != "user":
        raise jwt.InvalidTokenError("not a user token")
    return payload


def mint_internal_token(*, user_id: str, role: str, departments: list[str], max_level: str) -> str:
    """Signed, short-lived, server-minted identity for MCP tool calls.

    departments/max_level are the *resolved* permission grant at mint time,
    not something the caller can widen later - the MCP server trusts the
    signature, not the values, but the values themselves are computed by
    the policy engine, never passed through from user/LLM input.
    """
    now = int(time.time())
    payload = {
        "sub": user_id,
        "role": role,
        "departments": departments,
        "max_level": max_level,
        "iat": now,
        "exp": now + settings.internal_auth_expire_seconds,
        "jti": str(uuid.uuid4()),
        "type": "internal",
    }
    return jwt.encode(payload, settings.internal_auth_secret, algorithm=settings.jwt_algorithm)


def verify_internal_token(token: str) -> dict[str, Any]:
    payload = jwt.decode(token, settings.internal_auth_secret, algorithms=[settings.jwt_algorithm])
    if payload.get("type") != "internal":
        raise jwt.InvalidTokenError("not an internal token")
    return payload
