"""Lifecycle of the visual analysis queue and of the model servers' state
(GPU/RAM: no inference after the work is done, no tight retry loop, pause /
resume / cancel without losing anything, chat first)."""

import asyncio

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agent.llm_client import LLMServiceError
from app.agent.model_provider import OpenAICompatibleProvider
from app.agent.vision_client import VisionUnavailableError
from app.core import activity
from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.audit import AuditLog
from app.models.document import Document
from app.routers.runtime import router as runtime_router
from app.services import embedding_worker, extraction_worker, model_runtime, runtime_control, vision_worker, workspace_sync
from tests.conftest import hot_vector
from tests.test_reports import _png, _workspace


@pytest.fixture
def queued_images(db, make_test_user):
    """Images without OCR text, queued for visual analysis (what the
    "Analyser visuellement" button does)."""

    def make(count: int, username: str = "vq_owner"):
        owner = make_test_user(username=username, role="DIRECTOR")
        ws = _workspace(db, owner, "vision-queue")
        docs = []
        for i in range(count):
            doc, _ = workspace_sync.index_file(db, owner, ws, f"photos/site-{i}.png", _png(), defer_extraction=True)
            docs.append(doc)
        while extraction_worker.process_next_extraction():
            pass
        db.query(Document).filter(Document.id.in_([d.id for d in docs])).update(
            {Document.status: "visual_queued"}, synchronize_session=False
        )
        db.commit()
        return owner, [d.id for d in docs]

    return make


class FakeVision:
    """Stands for the vision server; counts every call it receives."""

    def __init__(self, monkeypatch, *, status="ready", answer=None, error=None):
        self.health_calls = 0
        self.describe_calls = 0
        self.status = status
        self.error = error
        self.answer = answer or {"description": "Catégorie : photo. Armoire électrique sur site.", "text": []}

        async def get_status():
            self.health_calls += 1
            return {"status": self.status}

        async def describe(**kwargs):
            self.describe_calls += 1
            if self.error is not None:
                raise self.error
            return self.answer, None

        monkeypatch.setattr(vision_worker, "get_vision_status", get_status)
        monkeypatch.setattr(vision_worker, "describe_image", describe)


def _statuses(db, ids):
    db.expire_all()
    return [db.get(Document, i).status for i in ids]


# --- 2. an image is analysed, then only "indexed" once embedded -------------------------------------------------


def test_image_is_described_then_indexed_only_after_embedding(db, queued_images, monkeypatch):
    _, ids = queued_images(1)
    FakeVision(monkeypatch)
    monkeypatch.setattr("app.rag.ingest.embed_texts", lambda texts: [hot_vector(0) for _ in texts])
    assert vision_worker.process_next_visual() == vision_worker.WORKED
    assert _statuses(db, ids) == ["indexing"]  # described, not searchable by meaning yet
    assert embedding_worker.process_next_document() is True
    doc = db.get(Document, ids[0])
    db.refresh(doc)
    assert doc.status == "indexed" and doc.issue_kind == "visual_photo"
    assert doc.content.startswith(vision_worker.VISUAL_PREFIX)


# --- 3. / 4. pause, resume, no duplicates ------------------------------------------------------------------------


def test_pause_takes_no_new_image_and_resume_continues_without_duplicates(db, queued_images, monkeypatch):
    _, ids = queued_images(2)
    vision = FakeVision(monkeypatch)
    runtime_control.set_vision_paused(db, True, updated_by="test")
    for _ in range(3):
        assert vision_worker.process_next_visual() == vision_worker.PAUSED
    assert vision.describe_calls == 0 and vision.health_calls == 0  # not even a health check
    assert _statuses(db, ids) == ["visual_queued", "visual_queued"]  # queue kept as it was

    runtime_control.set_vision_paused(db, False, updated_by="test")
    assert vision_worker.process_next_visual() == vision_worker.WORKED
    assert vision_worker.process_next_visual() == vision_worker.WORKED
    assert vision_worker.process_next_visual() == vision_worker.IDLE
    assert vision.describe_calls == 2  # each image exactly once
    assert _statuses(db, ids) == ["indexing", "indexing"]


def test_pause_survives_a_backend_restart(db):
    runtime_control.set_vision_paused(db, True, updated_by="directeur")
    db.expire_all()
    state = runtime_control.vision_queue_state(db)
    assert state["paused"] is True and state["updated_by"] == "directeur"


# --- 5. stop during a task --------------------------------------------------------------------------------------


def test_server_stopped_mid_image_puts_it_back_without_counting_an_attempt(db, queued_images, monkeypatch):
    _, ids = queued_images(1)
    vision = FakeVision(monkeypatch, error=VisionUnavailableError("gone"))
    assert vision_worker.process_next_visual() == vision_worker.UNAVAILABLE
    db.expire_all()
    doc = db.get(Document, ids[0])
    assert doc.status == "visual_queued" and doc.index_attempts == 0
    vision.error = None
    assert vision_worker.process_next_visual() == vision_worker.WORKED
    assert _statuses(db, ids) == ["indexing"]


def test_image_left_running_by_a_restart_is_queued_again(db, queued_images):
    _, ids = queued_images(1)
    db.query(Document).filter(Document.id == ids[0]).update({Document.status: "visual_running"})
    db.commit()
    assert vision_worker.requeue_interrupted() == 1
    assert _statuses(db, ids) == ["visual_queued"]


# --- 6. empty queue: no model call at all -----------------------------------------------------------------------


def test_empty_queue_never_calls_the_model(db, monkeypatch):
    vision = FakeVision(monkeypatch)
    for _ in range(5):
        assert vision_worker.process_next_visual() == vision_worker.IDLE
    assert vision.health_calls == 0 and vision.describe_calls == 0


# --- 7. unavailable server: progressive backoff, nothing claimed -------------------------------------------------


def test_unavailable_server_claims_nothing(db, queued_images, monkeypatch):
    _, ids = queued_images(1)
    vision = FakeVision(monkeypatch, status="unavailable")
    assert vision_worker.process_next_visual() == vision_worker.UNAVAILABLE
    assert vision.describe_calls == 0
    assert _statuses(db, ids) == ["visual_queued"]


def test_worker_waits_longer_and_longer_while_the_server_is_away(monkeypatch):
    outcomes = iter([vision_worker.UNAVAILABLE] * 6 + [vision_worker.WORKED, vision_worker.UNAVAILABLE])
    delays = []

    def next_outcome():
        try:
            return next(outcomes)
        except StopIteration:
            raise asyncio.CancelledError from None

    async def fake_sleep(seconds):
        delays.append(seconds)

    monkeypatch.setattr(vision_worker, "requeue_interrupted", lambda: 0)
    monkeypatch.setattr(vision_worker, "process_next_visual", next_outcome)
    monkeypatch.setattr(vision_worker.asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(vision_worker.run_vision_worker())
    assert delays == [30, 60, 120, 240, 480, 600, 30]  # capped, reset after a success


# --- failures: bounded attempts, a bad image never blocks the queue ----------------------------------------------


def test_a_failing_image_is_retried_after_the_others_then_set_aside(db, queued_images, monkeypatch):
    _, ids = queued_images(2)
    bad, good = ids
    vision = FakeVision(monkeypatch)
    calls = []

    async def describe(**kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise LLMServiceError("réponse invalide")
        return vision.answer, None

    monkeypatch.setattr(vision_worker, "describe_image", describe)
    # first claim takes the oldest (bad) image; it fails and goes behind the good one
    assert vision_worker.process_next_visual() == vision_worker.WORKED
    db.expire_all()
    assert db.get(Document, bad).status == "visual_queued" and db.get(Document, bad).index_attempts == 1
    assert vision_worker.process_next_visual() == vision_worker.WORKED
    assert _statuses(db, [good]) == ["indexing"]

    async def always_fails(**kwargs):
        raise LLMServiceError("réponse invalide")

    monkeypatch.setattr(vision_worker, "describe_image", always_fails)
    assert vision_worker.process_next_visual() == vision_worker.WORKED  # attempt 2
    assert vision_worker.process_next_visual() == vision_worker.WORKED  # attempt 3 -> set aside
    assert vision_worker.process_next_visual() == vision_worker.IDLE
    db.expire_all()
    doc = db.get(Document, bad)
    assert doc.status == "no_text" and "3 tentatives" in doc.index_error and doc.is_active


# --- chat first --------------------------------------------------------------------------------------------------


def test_queue_waits_while_a_chat_answer_is_generated(db, queued_images, monkeypatch):
    _, ids = queued_images(1)
    vision = FakeVision(monkeypatch)
    with activity.foreground_turn():
        assert vision_worker.process_next_visual() == vision_worker.YIELDED
    assert vision.describe_calls == 0
    assert vision_worker.process_next_visual() == vision_worker.WORKED


# --- model server state (never wakes a sleeping model, BUSY from real in-flight calls) ---------------------------


_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _probe_with(monkeypatch, handler, *, model="vision-test", sleep_after=300):
    class Client(_REAL_ASYNC_CLIENT):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    provider = OpenAICompatibleProvider(base_url="http://vision.test", model_name=model)
    return asyncio.run(model_runtime._probe(provider, model, "Vision test", sleep_after))


def _server(paths, *, health=200, sleeping=False):
    def handler(request):
        paths.append(request.url.path)
        if request.url.path == "/health":
            return httpx.Response(health, json={"status": "ok"})
        if request.url.path == "/props":
            return httpx.Response(200, json={"is_sleeping": sleeping})
        return httpx.Response(200, json={})

    return handler


def test_states_stopped_starting_sleeping(monkeypatch):
    def refused(request):
        raise httpx.ConnectError("refused")

    assert _probe_with(monkeypatch, refused)["state"] == "STOPPED"
    assert _probe_with(monkeypatch, _server([], health=503))["state"] == "STARTING"
    paths = []
    state = _probe_with(monkeypatch, _server(paths, sleeping=True))
    assert state["state"] == "SLEEPING"
    assert "/slots" not in paths  # /slots wakes a sleeping llama-server


def test_busy_comes_from_in_flight_calls_and_idle_counts_down_to_sleep(monkeypatch):
    model = "vision-busy-test"
    activity.inference_started(model)
    try:
        # a request sent to a sleeping server: it is reloading for that request
        assert _probe_with(monkeypatch, _server([], sleeping=True), model=model)["state"] == "BUSY"
        assert _probe_with(monkeypatch, _server([]), model=model)["state"] == "BUSY"
    finally:
        activity.inference_finished(model)
    idle = _probe_with(monkeypatch, _server([]), model=model, sleep_after=300)
    assert idle["state"] == "IDLE" and 0 < idle["sleep_in_seconds"] <= 300
    assert _probe_with(monkeypatch, _server([]), model="never-called")["state"] == "READY"


# --- administration API -----------------------------------------------------------------------------------------


def _client(db, user):
    app = FastAPI()
    app.include_router(runtime_router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def test_only_a_director_can_see_or_control_the_engine(db, make_test_user):
    employee = make_test_user(username="vq_emp", role="EMPLOYEE")
    client = _client(db, employee)
    assert client.get("/api/admin/runtime").status_code == 403
    for action in ("pause", "resume", "cancel"):
        assert client.post(f"/api/admin/runtime/vision/{action}").status_code == 403
    assert runtime_control.is_vision_paused(db) is False


def test_pause_resume_cancel_are_audited_and_cancel_deletes_nothing(db, queued_images, monkeypatch):
    owner, ids = queued_images(2, username="vq_director")
    client = _client(db, owner)

    async def no_models():
        return []

    monkeypatch.setattr(model_runtime, "models_state", no_models)
    assert client.post("/api/admin/runtime/vision/pause").json()["paused"] is True
    state = client.get("/api/admin/runtime").json()
    assert state["vision_queue"]["paused"] is True and state["queues"]["visual_queued"] == 2
    assert isinstance(state["workers"], list) and "docker_vm_memory" in state
    assert client.post("/api/admin/runtime/vision/resume").json()["paused"] is False

    before = {d.id: d.content for d in db.query(Document).filter(Document.id.in_(ids))}
    assert client.post("/api/admin/runtime/vision/cancel").json() == {"cancelled": 2}
    db.expire_all()
    docs = db.query(Document).filter(Document.id.in_(ids)).all()
    assert len(docs) == 2 and all(d.is_active and d.status == "no_text" for d in docs)
    assert {d.id: d.content for d in docs} == before  # nothing removed or rewritten
    actions = [a for (a,) in db.query(AuditLog.action).filter(AuditLog.resource_type == "runtime").order_by(AuditLog.created_at)]
    assert actions == ["VISION_QUEUE_PAUSED", "VISION_QUEUE_RESUMED", "VISION_QUEUE_CANCELLED"]


def test_cancelled_images_can_be_queued_again(db, queued_images, monkeypatch):
    owner, ids = queued_images(1, username="vq_requeue")
    _client(db, owner).post("/api/admin/runtime/vision/cancel")
    from app.routers.workspaces import request_visual_analysis

    ws_id = db.get(Document, ids[0]).workspace_id
    monkeypatch.setattr("app.routers.workspaces.get_settings", lambda: type("S", (), {"vision_enabled": True})())
    assert request_visual_analysis(ws_id, db=db, user=owner) == {"queued": 1}
    db.expire_all()
    doc = db.get(Document, ids[0])
    assert doc.status == "visual_queued" and doc.index_attempts == 0 and doc.index_error is None
