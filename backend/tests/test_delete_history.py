"""Tests for history deletion (routers/chat.py): two distinct, separately
audited actions - delete_conversation (one conversation,
HISTORY_CONVERSATION_DELETED) and delete_history (everything,
HISTORY_ALL_DELETED) - each a soft-delete + an immutable AuditLog row + a
Director-facing Alert, written atomically in one transaction. Calls the
router functions directly against a real `db` session/real users (same
convention as every other test in this suite) except for the two
HTTP-level role-gating checks, which use a FastAPI TestClient against a
minimal isolated app - never app.main.app, whose lifespan would try to
migrate/connect to the real privileged database and real MinIO.
"""

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import app.routers.chat as chat_router
from app.core.deps import get_current_user
from app.core.database import get_db
from app.models.alert import Alert
from app.models.audit import AuditLog
from app.models.chat import Conversation
from app.routers.audit import router as audit_router


# --- per-conversation deletion ----------------------------------------------


def test_delete_one_conversation_leaves_others_intact(db, make_test_user, make_conversation):
    """TEST 1: 12 conversations, delete #5 -> 11 remain, all others untouched."""
    agent = make_test_user(username="dc_agent1", role="EMPLOYEE")
    convs = [make_conversation(user_id=agent.id, title=f"Conversation {i}", message_count=2) for i in range(1, 13)]
    target = convs[4]  # "#5"

    result = chat_router.delete_conversation(conversation_id=target.id, db=db, user=agent)

    assert result.deleted_conversation_count == 1
    remaining = chat_router.list_conversations(db=db, user=agent)
    assert len(remaining) == 11
    assert target.id not in {c.id for c in remaining}

    db.expire_all()
    for i, conv in enumerate(convs):
        fresh = db.get(Conversation, conv.id)
        if conv.id == target.id:
            assert fresh.deleted_at is not None
        else:
            assert fresh.deleted_at is None, f"conversation #{i + 1} must stay intact"


def test_delete_all_history_leaves_nothing_visible(db, make_test_user, make_conversation):
    """TEST 2: 12 conversations, delete_history -> 0 visible."""
    agent = make_test_user(username="dc_agent2", role="EMPLOYEE")
    for i in range(1, 13):
        make_conversation(user_id=agent.id, title=f"Conversation {i}", message_count=1)

    result = chat_router.delete_history(db=db, user=agent)

    assert result.deleted_conversation_count == 12
    assert chat_router.list_conversations(db=db, user=agent) == []


def test_agent_cannot_delete_another_agents_conversation(db, make_test_user, make_conversation):
    """TEST 3: Agent A can't delete Agent B's conversation -> 403."""
    agent_a = make_test_user(username="dc_agent3a", role="EMPLOYEE")
    agent_b = make_test_user(username="dc_agent3b", role="EMPLOYEE")
    conv_b = make_conversation(user_id=agent_b.id, message_count=2)

    with pytest.raises(HTTPException) as exc_info:
        chat_router.delete_conversation(conversation_id=conv_b.id, db=db, user=agent_a)
    assert exc_info.value.status_code == 403

    db.expire_all()
    fresh = db.get(Conversation, conv_b.id)
    assert fresh.deleted_at is None  # untouched


def test_individual_deletion_audit_count_is_one(db, make_test_user, make_conversation):
    """TEST 4: individual deletion -> AuditEvent deleted_count == 1."""
    agent = make_test_user(username="dc_agent4", role="HR")
    conv = make_conversation(user_id=agent.id, message_count=6)

    result = chat_router.delete_conversation(conversation_id=conv.id, db=db, user=agent)

    entry = db.get(AuditLog, result.audit_log_id)
    assert entry.action == "HISTORY_CONVERSATION_DELETED"
    assert entry.decision == "ALLOW"
    assert entry.user_id == agent.id
    assert entry.username == agent.username
    assert entry.created_at is not None
    assert entry.extra["conversation_id"] == str(conv.id)
    assert entry.extra["deleted_count"] == 1
    assert entry.extra["deleted_message_count"] == 6
    assert entry.extra["department"] == "HR"
    assert entry.extra["request_id"]  # present and non-empty


def test_global_deletion_audit_count_matches_total(db, make_test_user, make_conversation):
    """TEST 5: global deletion -> AuditEvent deleted_count == actual total."""
    agent = make_test_user(username="dc_agent5", role="ACCOUNTANT")
    for _ in range(4):
        make_conversation(user_id=agent.id, message_count=3)

    result = chat_router.delete_history(db=db, user=agent)

    entry = db.get(AuditLog, result.audit_log_id)
    assert entry.action == "HISTORY_ALL_DELETED"
    assert entry.extra["deleted_count"] == 4
    assert entry.extra["deleted_message_count"] == 12
    assert entry.extra["request_id"]


def test_director_notification_correct_for_both_actions(db, make_test_user, make_conversation):
    """TEST 6: Director-facing notification content is correct in both cases."""
    agent = make_test_user(username="dc_agent6", role="EMPLOYEE")
    conv_1 = make_conversation(user_id=agent.id, title="Budget Q3", message_count=3)
    make_conversation(user_id=agent.id, title="Autre sujet", message_count=2)

    result_single = chat_router.delete_conversation(conversation_id=conv_1.id, db=db, user=agent)
    alert_single = db.query(Alert).filter(Alert.alert_type == "HISTORY_CONVERSATION_DELETED").one()
    assert alert_single.audit_log_id == result_single.audit_log_id
    assert agent.full_name in alert_single.description
    assert "une conversation" in alert_single.description
    assert "Budget Q3" in alert_single.description

    result_all = chat_router.delete_history(db=db, user=agent)
    alert_all = db.query(Alert).filter(Alert.alert_type == "HISTORY_ALL_DELETED").one()
    assert alert_all.audit_log_id == result_all.audit_log_id
    assert agent.full_name in alert_all.description
    assert "tout son historique" in alert_all.description
    assert "1 conversation" in alert_all.description  # only "Autre sujet" was left to delete


def test_deleting_unknown_conversation_is_404(db, make_test_user):
    agent = make_test_user(username="dc_agent7", role="EMPLOYEE")
    import uuid as _uuid

    with pytest.raises(HTTPException) as exc_info:
        chat_router.delete_conversation(conversation_id=_uuid.uuid4(), db=db, user=agent)
    assert exc_info.value.status_code == 404


# --- delete_history (all) ---------------------------------------------------


def test_delete_history_returns_counts(db, make_test_user, make_conversation):
    agent = make_test_user(username="dh_agent1", role="EMPLOYEE")
    make_conversation(user_id=agent.id, message_count=5)
    make_conversation(user_id=agent.id, message_count=7)

    result = chat_router.delete_history(db=db, user=agent)

    assert result.deleted_conversation_count == 2
    assert result.deleted_message_count == 12
    assert result.audit_log_id is not None


def test_history_no_longer_visible_after_deletion(db, make_test_user, make_conversation):
    agent = make_test_user(username="dh_agent2", role="EMPLOYEE")
    conv = make_conversation(user_id=agent.id, message_count=3)

    chat_router.delete_history(db=db, user=agent)

    visible = chat_router.list_conversations(db=db, user=agent)
    assert visible == []

    with pytest.raises(HTTPException) as exc_info:
        chat_router.get_conversation(conversation_id=conv.id, db=db, user=agent)
    assert exc_info.value.status_code == 404

    # the row itself still exists (soft delete, not destroyed)
    db.expire_all()
    still_there = db.get(Conversation, conv.id)
    assert still_there is not None
    assert still_there.deleted_at is not None


def test_audit_event_created(db, make_test_user, make_conversation):
    agent = make_test_user(username="dh_agent3", role="HR")
    make_conversation(user_id=agent.id, message_count=4)

    assert db.query(AuditLog).filter(AuditLog.action == "HISTORY_ALL_DELETED").count() == 0
    chat_router.delete_history(db=db, user=agent)

    entries = db.query(AuditLog).filter(AuditLog.action == "HISTORY_ALL_DELETED").all()
    assert len(entries) == 1

    entry = entries[0]
    assert entry.user_id == agent.id
    assert entry.username == agent.username
    assert entry.role == "HR"
    assert entry.decision == "ALLOW"
    assert entry.resource_type == "CHAT_HISTORY"
    assert entry.resource_id == str(agent.id)
    assert entry.created_at is not None
    assert entry.extra["deleted_count"] == 1
    assert entry.extra["deleted_message_count"] == 4
    assert entry.extra["department"] == "HR"


def test_director_notification_created(db, make_test_user, make_conversation):
    agent = make_test_user(username="dh_agent4", role="EMPLOYEE")
    make_conversation(user_id=agent.id, message_count=2)

    result = chat_router.delete_history(db=db, user=agent)

    alerts = db.query(Alert).filter(Alert.alert_type == "HISTORY_ALL_DELETED").all()
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.user_id == agent.id
    assert alert.username == agent.username
    assert "a supprimé tout son historique" in alert.description
    assert alert.audit_log_id == result.audit_log_id


def test_other_agent_not_referenced_by_notification(db, make_test_user, make_conversation):
    agent_a = make_test_user(username="dh_agent5a", role="EMPLOYEE")
    agent_b = make_test_user(username="dh_agent5b", role="EMPLOYEE")
    make_conversation(user_id=agent_a.id, message_count=2)
    conv_b = make_conversation(user_id=agent_b.id, message_count=3)

    chat_router.delete_history(db=db, user=agent_a)

    alerts = db.query(Alert).filter(Alert.alert_type == "HISTORY_ALL_DELETED").all()
    assert len(alerts) == 1
    assert alerts[0].user_id == agent_a.id
    assert alerts[0].username != agent_b.username

    # delete_history has no user_id parameter at all, so there is
    # structurally no way for A's call to target B.
    db.expire_all()
    fresh_conv_b = db.get(Conversation, conv_b.id)
    assert fresh_conv_b.deleted_at is None
    visible_for_b = chat_router.list_conversations(db=db, user=agent_b)
    assert len(visible_for_b) == 1


def test_deleting_empty_history_is_a_no_op_no_spam(db, make_test_user):
    agent = make_test_user(username="dh_agent6", role="EMPLOYEE")

    result = chat_router.delete_history(db=db, user=agent)

    assert result.deleted_conversation_count == 0
    assert result.audit_log_id is None
    assert db.query(AuditLog).filter(AuditLog.action == "HISTORY_ALL_DELETED").count() == 0
    assert db.query(Alert).filter(Alert.alert_type == "HISTORY_ALL_DELETED").count() == 0


# --- /audit-logs is DIRECTOR-only -------------------------------------------


def _audit_client(db, user):
    test_app = FastAPI()
    test_app.include_router(audit_router)
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(test_app)


def test_employee_cannot_list_audit_logs(db, make_test_user):
    employee = make_test_user(username="dh_emp_audit", role="EMPLOYEE")
    client = _audit_client(db, employee)

    response = client.get("/api/audit-logs")

    assert response.status_code == 403


def test_director_can_list_audit_logs(db, make_test_user):
    director = make_test_user(username="dh_dir_audit", role="DIRECTOR")
    client = _audit_client(db, director)

    response = client.get("/api/audit-logs")

    assert response.status_code == 200


def test_no_route_exists_to_delete_an_audit_event(db, make_test_user):
    director = make_test_user(username="dh_dir_audit2", role="DIRECTOR")
    client = _audit_client(db, director)

    # The audit router exposes exactly one route (GET /api/audit-logs) - no
    # delete/patch/put capability was ever wired up, for any role.
    response = client.delete("/api/audit-logs/00000000-0000-0000-0000-000000000000")

    assert response.status_code in (404, 405)


# --- atomicity ---------------------------------------------------------------


def test_transaction_rolls_back_cleanly_on_failure(db, make_test_user, make_conversation, monkeypatch):
    agent = make_test_user(username="dh_agent_fail", role="EMPLOYEE")
    conv = make_conversation(user_id=agent.id, message_count=3)

    def failing_commit():
        raise RuntimeError("simulated DB failure mid-transaction")

    monkeypatch.setattr(db, "commit", failing_commit)

    with pytest.raises(RuntimeError):
        chat_router.delete_history(db=db, user=agent)

    monkeypatch.undo()
    db.rollback()
    db.expire_all()

    fresh_conv = db.get(Conversation, conv.id)
    assert fresh_conv.deleted_at is None
    assert db.query(AuditLog).filter(AuditLog.action == "HISTORY_ALL_DELETED").count() == 0
    assert db.query(Alert).filter(Alert.alert_type == "HISTORY_ALL_DELETED").count() == 0


def test_single_conversation_transaction_rolls_back_cleanly_on_failure(db, make_test_user, make_conversation, monkeypatch):
    agent = make_test_user(username="dh_agent_fail2", role="EMPLOYEE")
    conv = make_conversation(user_id=agent.id, message_count=2)

    def failing_commit():
        raise RuntimeError("simulated DB failure mid-transaction")

    monkeypatch.setattr(db, "commit", failing_commit)

    with pytest.raises(RuntimeError):
        chat_router.delete_conversation(conversation_id=conv.id, db=db, user=agent)

    monkeypatch.undo()
    db.rollback()
    db.expire_all()

    fresh_conv = db.get(Conversation, conv.id)
    assert fresh_conv.deleted_at is None
    assert db.query(AuditLog).filter(AuditLog.action == "HISTORY_CONVERSATION_DELETED").count() == 0
