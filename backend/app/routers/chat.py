import base64
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.agent.local_files import is_safe_relative_path
from app.agent.orchestrator import AgentTurnResult, run_agent_turn
from app.core.alert_service import create_alert
from app.core.audit_logger import log_event
from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.alert import Alert
from app.models.audit import AuditLog
from app.models.chat import Conversation, Message
from app.models.user import User
from app.policy.rules import ROLE_ACCESS, resolve_effective_grant
from app.schemas.chat import (
    ChatRequest,
    ChatResponse,
    ChatResumeRequest,
    ConversationDetail,
    ConversationSummary,
    DeleteHistoryResult,
    MessageOut,
    PendingClientAction,
)

router = APIRouter(prefix="/api", tags=["chat"])

HISTORY_LIMIT = 8


def _primary_department(role: str) -> str:
    """Best-effort single "home department" for an audit metadata label -
    there is no independent per-user department field in this RBAC model
    (access is granted by role, see policy/rules.py), so this picks the
    role's most specific department (anything beyond the universal
    GENERAL), falling back to GENERAL itself for roles that only ever see
    GENERAL (e.g. EMPLOYEE)."""
    departments = ROLE_ACCESS.get(role, {}).get("departments", set())
    specific = sorted(departments - {"GENERAL"})
    return specific[0] if specific else "GENERAL"


@router.get("/conversations", response_model=list[ConversationSummary])
def list_conversations(db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> list[Conversation]:
    return (
        db.query(Conversation)
        .filter(Conversation.user_id == user.id, Conversation.deleted_at.is_(None))
        .order_by(Conversation.created_at.desc())
        .all()
    )


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
def get_conversation(
    conversation_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> Conversation:
    conv = db.get(Conversation, conversation_id)
    if conv is None or conv.user_id != user.id or conv.deleted_at is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation introuvable.")
    return conv


@router.delete("/history/{conversation_id}", response_model=DeleteHistoryResult)
def delete_conversation(
    conversation_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> DeleteHistoryResult:
    """Soft-deletes exactly one of the caller's own conversations - the
    other DELETE /history (delete_history, below) handles "all of it" and
    is a deliberately separate endpoint/action/audit event, never the same
    code path with a conditional, so the two can never be confused in the
    audit trail or accidentally triggered by the wrong button.

    Ownership mismatch returns 403 here (not the 404 get_conversation/
    delete_history use elsewhere in this file to avoid confirming a
    resource exists to someone who can't see it) - a deliberate, scoped
    exception for this one endpoint, per explicit product requirement: a
    conversation_id is an unguessable UUID (no enumeration risk from
    confirming existence), and a clear "that's not yours" beats a
    misleading "not found" for a delete action specifically.
    """
    conv = db.get(Conversation, conversation_id)
    if conv is None or conv.deleted_at is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation introuvable.")
    if conv.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cette conversation ne vous appartient pas.")

    message_count = db.query(Message).filter(Message.conversation_id == conv.id).count()
    conv.deleted_at = datetime.now(UTC)

    audit_entry = _write_history_deletion_audit(
        db,
        user=user,
        action="HISTORY_CONVERSATION_DELETED",
        resource_id=str(conv.id),
        extra={
            "conversation_id": str(conv.id),
            "deleted_count": 1,
            "deleted_message_count": message_count,
            "department": _primary_department(user.role),
            "request_id": str(uuid.uuid4()),
        },
    )
    db.add(
        Alert(
            alert_type="HISTORY_CONVERSATION_DELETED",
            user_id=user.id,
            username=user.username,
            role=user.role,
            title="Suppression d'historique",
            description=(
                f"L'agent {user.full_name} a supprimé une conversation de son historique "
                f"(« {conv.title} », {message_count} message(s))."
            ),
            resource_type="CONVERSATION",
            resource_id=str(conv.id),
            audit_log_id=audit_entry.id,
        )
    )
    db.commit()

    return DeleteHistoryResult(deleted_conversation_count=1, deleted_message_count=message_count, audit_log_id=audit_entry.id)


@router.delete("/history", response_model=DeleteHistoryResult)
def delete_history(db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> DeleteHistoryResult:
    """Soft-deletes every one of the caller's own conversations - never
    another user's (there is no user_id parameter anywhere in this
    endpoint; current_user is the only source of whose history gets
    touched). Nothing is hard-deleted: rows stay in the database for
    traceability, just excluded from every read endpoint from this point
    on (see list_conversations/get_conversation's deleted_at filter).

    Atomic by construction: the soft-delete, the AuditLog row, and (if
    anything was actually deleted) the Director-facing Alert are all
    written through this one request-scoped `db` session and committed
    together - unlike log_event/create_alert's own helpers (which
    deliberately use an independent session so a DENY gets recorded even if
    the triggering request later fails), this operation must never leave
    "history gone, no audit row" or "audit row, history untouched"
    half-done, so it builds the AuditLog/Alert rows directly on the same
    session instead of calling those helpers.
    """
    conversations = (
        db.query(Conversation)
        .filter(Conversation.user_id == user.id, Conversation.deleted_at.is_(None))
        .all()
    )
    conversation_count = len(conversations)
    message_count = (
        db.query(Message)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .filter(Conversation.user_id == user.id, Conversation.deleted_at.is_(None))
        .count()
        if conversation_count
        else 0
    )

    if conversation_count == 0:
        return DeleteHistoryResult(deleted_conversation_count=0, deleted_message_count=0, audit_log_id=None)

    now = datetime.now(UTC)
    for conv in conversations:
        conv.deleted_at = now

    audit_entry = _write_history_deletion_audit(
        db,
        user=user,
        action="HISTORY_ALL_DELETED",
        resource_id=str(user.id),
        extra={
            "deleted_count": conversation_count,
            "deleted_message_count": message_count,
            "department": _primary_department(user.role),
            "request_id": str(uuid.uuid4()),
        },
    )
    db.add(
        Alert(
            alert_type="HISTORY_ALL_DELETED",
            user_id=user.id,
            username=user.username,
            role=user.role,
            title="Suppression d'historique",
            description=(
                f"L'agent {user.full_name} a supprimé tout son historique de conversation. "
                f"{conversation_count} conversation(s) et {message_count} message(s) supprimés."
            ),
            resource_type="CHAT_HISTORY",
            resource_id=str(user.id),
            audit_log_id=audit_entry.id,
        )
    )

    db.commit()

    return DeleteHistoryResult(
        deleted_conversation_count=conversation_count,
        deleted_message_count=message_count,
        audit_log_id=audit_entry.id,
    )


def _write_history_deletion_audit(
    db: Session, *, user: User, action: str, resource_id: str, extra: dict
) -> AuditLog:
    """Shared tail for delete_conversation/delete_history: builds and
    flushes (not commits - the caller's own db.commit() covers both this
    row and the soft-delete/Alert together) the AuditLog row, returning it
    so the caller can link the Alert to its id."""
    entry = AuditLog(
        user_id=user.id,
        username=user.username,
        role=user.role,
        action=action,
        decision="ALLOW",
        resource_type="CHAT_HISTORY",
        resource_id=resource_id,
        reason="ok",
        extra=extra,
    )
    db.add(entry)
    db.flush()  # assigns entry.id without ending the transaction
    return entry


@router.post("/chat", response_model=ChatResponse)
async def chat(
    payload: ChatRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> ChatResponse:
    if not payload.message.strip():
        raise HTTPException(status_code=422, detail="Le message ne peut pas être vide.")

    if payload.conversation_id:
        conv = db.get(Conversation, payload.conversation_id)
        if conv is None or conv.user_id != user.id or conv.deleted_at is not None:
            # A soft-deleted conversation is treated exactly like a
            # nonexistent one - never silently resurrected by a stale
            # conversation_id the frontend happened to still be holding.
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation introuvable.")
    else:
        title = payload.message.strip()[:60]
        conv = Conversation(user_id=user.id, title=title)
        db.add(conv)
        db.commit()
        db.refresh(conv)

    user_msg = Message(conversation_id=conv.id, role="user", content=payload.message)
    db.add(user_msg)
    db.commit()

    prior = (
        db.query(Message)
        .filter(Message.conversation_id == conv.id, Message.id != user_msg.id)
        .order_by(Message.created_at.desc())
        .limit(HISTORY_LIMIT)
        .all()
    )
    history = [{"role": m.role, "content": m.content} for m in reversed(prior)]
    # Most recent assistant turn's structured result, if any - lets
    # run_agent_turn resolve "la première occurrence" / "ce document" /
    # "le deuxième" / "suite" without re-parsing rendered text (see
    # app/agent/orchestrator.py::_resolve_section_request). Kept out of
    # `history` on purpose - it's not meant to be seen by the LLM prompt.
    last_reference_context = next(
        (m.reference_context for m in prior if m.role == "assistant" and m.reference_context), None
    )

    grant = resolve_effective_grant(user)
    if grant is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Votre rôle n'est pas reconnu par le système de permissions.")

    result = await run_agent_turn(
        user_id=str(user.id),
        grant=grant,
        message=payload.message,
        history=history,
        last_reference_context=last_reference_context,
        workspace_index=payload.workspace_index,
    )

    return _persist_assistant_turn(db, conv=conv, user=user, original_message=payload.message, result=result)


@router.post("/chat/resume", response_model=ChatResponse)
async def chat_resume(
    payload: ChatResumeRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> ChatResponse:
    """Completes a turn the Local File Agent paused on (see
    app/agent/orchestrator.py's pending_client_action) - the frontend has
    now read the requested file via its own FileSystemDirectoryHandle (the
    backend never had disk access) and POSTs the bytes here. Stateless by
    design: nothing about the pending turn was persisted server-side, so
    this recovers the original question from the conversation's own last
    user Message row rather than requiring the frontend to resend it.
    """
    if not is_safe_relative_path(payload.relative_path):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Chemin de fichier invalide.")

    conv = db.get(Conversation, payload.conversation_id)
    if conv is None or conv.user_id != user.id or conv.deleted_at is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation introuvable.")

    last_user_msg = (
        db.query(Message)
        .filter(Message.conversation_id == conv.id, Message.role == "user")
        .order_by(Message.created_at.desc())
        .first()
    )
    if last_user_msg is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Aucune question en attente pour cette conversation.")

    try:
        content = base64.b64decode(payload.content_base64, validate=True)
    except Exception:
        raise HTTPException(status_code=422, detail="Contenu de fichier invalide.") from None

    grant = resolve_effective_grant(user)
    if grant is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Votre rôle n'est pas reconnu par le système de permissions.")

    result = await run_agent_turn(
        user_id=str(user.id),
        grant=grant,
        message=last_user_msg.content,
        history=[],
        client_file_content=content,
        client_file_name=payload.name,
    )

    # Local file reads bypass the company RBAC/ACL system by design (it's
    # the user's own disk, not a shared resource - see
    # _run_local_file_turn's docstring), but every read is still audited
    # for traceability, same as every other action in this app.
    log_event(
        user_id=user.id,
        username=user.username,
        role=user.role,
        action="FILE_READ",
        decision="ALLOW",
        resource_type="LOCAL_FILE",
        resource_id=payload.relative_path,
        reason="ok",
        extra={"name": payload.name, "action_id": str(payload.action_id), "size_bytes": len(content)},
    )

    return _persist_assistant_turn(db, conv=conv, user=user, original_message=last_user_msg.content, result=result)


def _persist_assistant_turn(
    db: Session, *, conv: Conversation, user: User, original_message: str, result: AgentTurnResult
) -> ChatResponse:
    """Shared tail for chat()/chat_resume(): either the turn is still
    pending a client-side file read (nothing persisted yet - there is no
    answer to save), or it's complete and gets persisted/audited exactly
    like every chat turn always has."""
    if result.pending_client_action is not None:
        return ChatResponse(
            conversation_id=conv.id,
            pending_client_action=PendingClientAction(
                action_id=uuid.uuid4(),
                tool=result.pending_client_action["tool"],
                relative_path=result.pending_client_action["relative_path"],
                name=result.pending_client_action["name"],
            ),
        )

    assistant_msg = Message(
        conversation_id=conv.id,
        role="assistant",
        content=result.answer,
        sources=result.sources,
        tool_trace=result.tool_trace,
        reference_context=result.reference_context,
        chart=result.chart,
    )
    db.add(assistant_msg)
    db.commit()
    db.refresh(assistant_msg)

    denied_calls = [t for t in result.tool_trace if t.get("decision") == "DENY"]
    # A tool being unreachable (Ollama/embeddings down) is not a permission
    # decision - only genuine policy refusals should page the Director.
    policy_denials = [t for t in denied_calls if not str(t.get("reason", "")).startswith("service unavailable")]

    log_event(
        user_id=user.id,
        username=user.username,
        role=user.role,
        action="CHAT_MESSAGE",
        decision="DENY" if denied_calls and not result.sources else "ALLOW",
        resource_type="conversation",
        resource_id=str(conv.id),
        reason="one or more tool calls were denied" if denied_calls else "ok",
        extra={"message_preview": original_message[:200], "tool_trace": result.tool_trace, "degraded": result.degraded},
    )

    if policy_denials:
        reasons = "; ".join(f"{t['tool']} → {t['reason']}" for t in policy_denials)
        create_alert(
            alert_type="CHAT_ACCESS_DENIED",
            user_id=user.id,
            username=user.username,
            role=user.role,
            title=f"Tentative d'accès refusée dans l'assistant IA ({user.full_name})",
            description=f"Question posée : « {original_message[:300]} »\nRefus : {reasons}",
            resource_type="conversation",
            resource_id=str(conv.id),
        )

    return ChatResponse(
        conversation_id=conv.id,
        message=MessageOut(
            id=assistant_msg.id,
            role=assistant_msg.role,
            content=assistant_msg.content,
            sources=assistant_msg.sources or [],
            tool_trace=assistant_msg.tool_trace or [],
            chart=assistant_msg.chart,
            created_at=assistant_msg.created_at,
        ),
    )
