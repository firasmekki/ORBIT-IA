"""A slow extraction in /sync/file (scanned PDF -> Tesseract OCR) must not
freeze the rest of the API: chat, login and the health check have to keep
answering while it runs.

Extraction is replaced by a 3 s sleep and the workspace/user lookups by
fakes - no database, no real file, no OCR involved."""

import asyncio
import time
import types
import uuid

import httpx
import pytest

from app.core.database import get_db
from app.core.deps import get_current_user
from app.main import app
from app.routers import workspaces as workspaces_router
from app.services import workspace_sync

SLOW_SECONDS = 3.0


@pytest.fixture
def slow_sync(monkeypatch):
    user = types.SimpleNamespace(id=uuid.uuid4(), username="owner", role="DIRECTOR")
    ws = types.SimpleNamespace(id=uuid.uuid4(), has_server_source=False)

    def slow_index_file(db, user, workspace, relative_path, content, **kwargs):
        time.sleep(SLOW_SECONDS)  # blocking, like pypdf/Tesseract
        doc = types.SimpleNamespace(id=uuid.uuid4(), relative_path=relative_path, status="indexing")
        return doc, "created"

    monkeypatch.setattr(workspaces_router, "_workspace_or_403", lambda db, u, wid: ws)
    monkeypatch.setattr(workspace_sync, "index_file", slow_index_file)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: None
    yield ws
    app.dependency_overrides.pop(get_current_user, None)
    app.dependency_overrides.pop(get_db, None)


async def test_health_answers_while_a_file_is_being_extracted(slow_sync):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        upload = asyncio.create_task(
            client.post(
                f"/api/workspaces/{slow_sync.id}/sync/file",
                data={"relative_path": "scan.pdf"},
                files={"file": ("scan.pdf", b"%PDF-1.7 fake", "application/pdf")},
            )
        )
        await asyncio.sleep(0.3)  # the slow extraction is now running
        latencies = []
        for _ in range(10):
            start = time.perf_counter()
            response = await client.get("/api/health")
            latencies.append(time.perf_counter() - start)
            assert response.status_code == 200
        assert not upload.done(), "the slow extraction should still be running"
        result = await upload

    assert result.status_code == 200
    assert max(latencies) < 0.5, f"health blocked by the extraction: {max(latencies):.2f}s"
