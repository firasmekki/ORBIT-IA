"""Regression test: GET /api/conversations/{id} 500'd on its first user
message, because sources/tool_trace are NULL in the DB for role="user"
(never set) and MessageOut's `= []` default only applies when a field is
missing, not when it's present-but-None - which is what from_attributes
serialization of an ORM object always gives it."""

import uuid
from datetime import datetime, timezone

from app.schemas.chat import MessageOut


def test_message_out_accepts_null_sources_and_tool_trace():
    out = MessageOut(
        id=uuid.uuid4(),
        role="user",
        content="bonjour",
        sources=None,
        tool_trace=None,
        created_at=datetime.now(timezone.utc),
    )
    assert out.sources == []
    assert out.tool_trace == []


def test_message_out_still_accepts_real_lists():
    out = MessageOut(
        id=uuid.uuid4(),
        role="assistant",
        content="réponse",
        sources=[{"document_id": uuid.uuid4(), "title": "Doc", "department": "GENERAL", "confidentiality": "INTERNAL", "excerpt": "x"}],
        tool_trace=[{"tool": "search_documents", "arguments": {}, "decision": "ALLOW", "reason": "ok"}],
        created_at=datetime.now(timezone.utc),
    )
    assert len(out.sources) == 1
    assert len(out.tool_trace) == 1
