"""MCP client used by the agent orchestrator to reach the internal MCP server.

One session is opened per chat turn (not per tool call), authenticated with
a single short-lived internal token minted for that turn. The token travels
as an HTTP header on the transport connection - it is never a tool argument,
so the LLM's function-calling arguments never contain it.
"""

import json
from contextlib import asynccontextmanager

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from app.core.config import get_settings

settings = get_settings()


@asynccontextmanager
async def open_mcp_session(internal_token: str):
    headers = {"X-Internal-Auth": internal_token}
    async with streamablehttp_client(settings.mcp_server_url, headers=headers) as (read, write, _get_session_id):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def call_tool(session: ClientSession, name: str, arguments: dict) -> dict:
    result = await session.call_tool(name, arguments)
    text_parts = [block.text for block in result.content if getattr(block, "type", None) == "text"]
    raw = "\n".join(text_parts) if text_parts else "{}"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = {"raw": raw}

    if result.isError and "error" not in parsed:
        parsed = {"error": raw or "tool call failed"}
    return parsed
