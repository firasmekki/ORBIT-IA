import base64
import json
import logging
import time
import uuid
from typing import Annotated
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.agent.local_files import MAX_SEARCH_FILES, MAX_SEARCH_TOTAL_BYTES, is_safe_relative_path
from app.agent.chart_dialog import CHART_DIALOG_KIND
from app.agent.llm_client import LLMServiceError, get_llm_status
from app.agent.vision_client import (
    InvalidImageError,
    VisionUnavailableError,
    describe_image,
    get_vision_status,
    wait_until_ready,
)
from app.core.config import get_settings
from app.agent.intent import detect_keyword_intent, detect_search_intent
from app.agent.orchestrator import AgentTurnResult, run_agent_turn
from app.core.activity import foreground_turn
from app.core.alert_service import create_alert, security_event_key
from app.core.secrets_filter import redact_payload, redact_secrets
from app.core.audit_logger import log_event
from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.alert import Alert
from app.models.audit import AuditLog
from app.models.chat import Conversation, Message
from app.models.user import User
from app.policy.engine import ACCESS_DENIED
from app.models.workspace import DEFAULT_WORKSPACE_ID, Workspace
from app.policy.rules import ROLE_ACCESS, resolve_effective_grant
from app.policy.workspace import WorkspaceAccessError, accessible_workspaces, resolve_workspace
from app.services.errors import ServiceError
from app.services.folders import normalize_folder
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
logger = logging.getLogger("orbitia.chat")

HISTORY_LIMIT = 8


async def _timed_turn(username: str, **kwargs) -> AgentTurnResult:
    """run_agent_turn with its start, end and duration logged - the model
    calls inside it are logged one by one by model_provider."""
    started = time.monotonic()
    logger.info("chat turn start user=%s folders=%d", username, len(kwargs.get("folder_scope") or []))
    try:
        with foreground_turn():
            result = await run_agent_turn(**kwargs)
    except Exception:
        logger.info("chat turn end user=%s failed after %.1fs", username, time.monotonic() - started)
        raise
    logger.info(
        "chat turn end user=%s duration=%.1fs sources=%d degraded=%s",
        username, time.monotonic() - started, len(result.sources or []), result.degraded,
    )
    return result


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


@router.get("/llm/status")
async def llm_status(user: User = Depends(get_current_user)) -> dict:
    """State of the local model for the chat UI ("Chargement du modèle
    IA..." / "Modèle IA prêt"). Display name only - never the model file's
    path, which stays in the server-side .env."""
    return await get_llm_status()


@router.get("/vision/status")
async def vision_status(user: User = Depends(get_current_user)) -> dict:
    return await get_vision_status()


def _folder_scope(raw: list[str] | None) -> list[str]:
    """"BIC", "Delice CLN/" -> ["BIC/", "Delice CLN/"] - same path rules as a
    synced file. A malformed entry is refused (422) rather than dropped: a
    silently ignored filter would search every client instead of one."""
    scope: list[str] = []
    for folder in raw or []:
        try:
            normalized = normalize_folder(folder)
        except ServiceError:
            raise HTTPException(status_code=422, detail=f"Dossier de périmètre invalide : {folder!r}") from None
        if normalized not in scope:
            scope.append(normalized)
    return scope


def _scoped_name(workspace: Workspace, scope: list[str], folder_depth: str = "recursive") -> str:
    """"projets › BIC, Delice CLN" - what the answers say they searched;
    "(dossier seul, sans sous-dossiers)" when the depth is "direct"."""
    if folder_depth == "direct":
        where = ", ".join(folder.rstrip("/") for folder in scope) if scope else "racine"
        return f"{workspace.name} › {where} (dossier seul, sans sous-dossiers)"
    if not scope:
        return workspace.name
    return f"{workspace.name} › " + ", ".join(folder.rstrip("/") for folder in scope)


ALL_WORKSPACES_ONLY_SEARCH = (
    "Le mode « Tous les espaces autorisés » s'applique à la recherche de documents "
    "(par exemple « recherche contrat de maintenance » ou « documents qui mentionnent SCADA »). "
    "Pour une synthèse, un rapport ou une analyse, choisissez un espace de travail et un dossier."
)


async def _search_all_workspaces(db: Session, user: User, grant, message: str) -> AgentTurnResult:
    """Mode "Tous les espaces autorisés": the same search, run in every
    workspace this user may use, each with its own signed scope and its own
    rules (re-checked by every MCP tool) - never a wider grant. Only for
    document searches: a synthesis mixing workspaces is not offered."""
    if detect_search_intent(message, allow_bare=False) is None and detect_keyword_intent(message) is None:
        return AgentTurnResult(answer=ALL_WORKSPACES_ONLY_SEARCH)
    sections, sources, trace, degraded = [], [], [], False
    for workspace in accessible_workspaces(db, user):
        result = await _timed_turn(
            user.username, user_id=str(user.id), grant=grant, message=message, history=[],
            workspace_id=str(workspace.id), workspace_name=workspace.name, folder_scope=[],
        )
        trace.extend(result.tool_trace)
        degraded = degraded or result.degraded
        if result.sources:
            sources.extend({**s, "workspace": workspace.name} for s in result.sources)
        sections.append(f"### 📁 Espace « {workspace.name} »\n\n{result.answer}")
    if not sections:
        return AgentTurnResult(answer="Aucun espace de travail n'est accessible avec votre compte.")
    header = f"Recherche dans tous les espaces autorisés ({len(sections)}) :"
    return AgentTurnResult(answer="\n\n".join([header, *sections]), sources=sources, tool_trace=trace, degraded=degraded)


def _workspace_or_403(db: Session, user: User, workspace_id) -> Workspace:
    """The workspace a chat turn may search in - always re-checked against
    the authenticated user (owner/member/default), never taken on trust."""
    try:
        return resolve_workspace(db, user, workspace_id)
    except WorkspaceAccessError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=exc.message) from None


@router.get("/conversations", response_model=list[ConversationSummary])
def list_conversations(
    workspace_id: Annotated[uuid.UUID | None, Query(description="Uniquement les conversations de cet espace")] = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[Conversation]:
    query = db.query(Conversation).filter(Conversation.user_id == user.id, Conversation.deleted_at.is_(None))
    if workspace_id is not None:
        if workspace_id == DEFAULT_WORKSPACE_ID:
            # Conversations from before workspaces existed belong to the default one.
            query = query.filter((Conversation.workspace_id == workspace_id) | Conversation.workspace_id.is_(None))
        else:
            query = query.filter(Conversation.workspace_id == workspace_id)
    return query.order_by(Conversation.created_at.desc()).all()


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


@router.post("/ai/chat", response_model=ChatResponse)
@router.post("/chat", response_model=ChatResponse)
async def chat(
    payload: ChatRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> ChatResponse:
    if not payload.message.strip() and payload.image is None:
        raise HTTPException(status_code=422, detail="Le message ne peut pas être vide.")

    message_text = payload.message.strip() or "Analyse l’image jointe."
    folder_scope = _folder_scope(payload.folder_scope)
    # An attached image is described by the vision model (Qwen3-VL) FIRST;
    # its observation then travels apart from the user's words
    # (run_agent_turn image_observation) - never glued to the message, where
    # the intent detectors took its "confidence": 0.95 for a file name.
    observation = None
    vision_trace = None
    if payload.image is not None:
        try:
            image_bytes = base64.b64decode(payload.image.data_base64, validate=True)
        except (ValueError, TypeError):
            raise HTTPException(status_code=422, detail="Pièce jointe illisible : contenu d’image invalide.") from None
        if len(image_bytes) > get_settings().vision_max_image_bytes:
            raise HTTPException(status_code=413, detail="L’image dépasse la taille autorisée (8 Mio).")
        # The user's image goes before the background visual-analysis queue.
        with foreground_turn():
            status_before = await wait_until_ready()
            if status_before not in ("ready",):
                detail = (
                    "Le modèle vision est encore en cours de chargement : réessayez dans quelques instants."
                    if status_before == "loading"
                    else "Le modèle vision n’est pas disponible (arrêté ou désactivé) : l’image n’a pas été analysée."
                )
                raise HTTPException(status_code=503, detail=detail)
            started = time.monotonic()
            try:
                observation, _usage = await describe_image(
                    image=image_bytes, mime_type=payload.image.mime_type, prompt=message_text
                )
            except InvalidImageError as exc:
                raise HTTPException(status_code=422, detail=f"Pièce jointe illisible : {exc}") from None
            except VisionUnavailableError as exc:
                raise HTTPException(status_code=503, detail=f"Modèle vision indisponible : {exc}") from None
            except LLMServiceError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from None
        vision_trace = {
            "tool": "analyze_image",
            "arguments": {"model": get_settings().vision_model_name, "mime_type": payload.image.mime_type},
            "decision": "ALLOW",
            "reason": f"ok ({time.monotonic() - started:.1f} s)",
        }

    if payload.conversation_id:
        conv = db.get(Conversation, payload.conversation_id)
        if conv is None or conv.user_id != user.id or conv.deleted_at is not None:
            # A soft-deleted conversation is treated exactly like a
            # nonexistent one - never silently resurrected by a stale
            # conversation_id the frontend happened to still be holding.
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation introuvable.")
        # A conversation keeps its workspace for good: it can never be made
        # to search another one, by mistake or on purpose.
        conversation_workspace = conv.workspace_id or DEFAULT_WORKSPACE_ID
        if payload.workspace_id is not None and payload.workspace_id != conversation_workspace:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Cette conversation appartient à un autre espace de travail. Ouvrez une nouvelle conversation.",
            )
        # Re-checked on every turn: a membership removed since still counts.
        workspace = _workspace_or_403(db, user, conversation_workspace)
    else:
        workspace = _workspace_or_403(db, user, payload.workspace_id)
        title = message_text[:60]
        conv = Conversation(user_id=user.id, title=title, workspace_id=workspace.id)
        db.add(conv)
        db.commit()
        db.refresh(conv)

    stored_user_message = message_text + (" [image jointe, non conservée]" if payload.image is not None else "")
    user_msg = Message(conversation_id=conv.id, role="user", content=stored_user_message)
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
    # A pending chart question is only answerable on the very next turn -
    # an older one must never be "continued" several messages later.
    latest_assistant = next((m for m in prior if m.role == "assistant"), None)
    if (
        last_reference_context
        and last_reference_context.get("kind") == CHART_DIALOG_KIND
        and (latest_assistant is None or latest_assistant.reference_context is not last_reference_context)
    ):
        last_reference_context = None

    grant = resolve_effective_grant(user)
    if grant is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Votre rôle n'est pas reconnu par le système de permissions.")

    if payload.scope_mode == "all":
        result = await _search_all_workspaces(db, user, grant, message_text)
    else:
        folder_depth = "direct" if payload.scope_mode == "folder" else "recursive"
        result = await _timed_turn(user.username,
            user_id=str(user.id),
            grant=grant,
            message=message_text,
            image_observation=observation,
            history=history,
            last_reference_context=last_reference_context,
            workspace_index=payload.workspace_index,
            workspace_id=str(workspace.id),
            workspace_name=_scoped_name(workspace, folder_scope, folder_depth),
            folder_scope=folder_scope,
            folder_depth=folder_depth,
            timezone=payload.timezone,
        )

    if vision_trace is not None:  # the vision model really ran: shown first in "actions effectuées"
        result.tool_trace = [vision_trace, *result.tool_trace]
    return _persist_assistant_turn(db, conv=conv, user=user, original_message=stored_user_message, result=result)


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

    Two shapes: one file (read_local_file) or several (`files`, for
    search_local_files - "recherche Tarek" with a Workspace selected).
    """
    multi = payload.files is not None
    paths = [f.relative_path for f in payload.files] if multi else [payload.relative_path or ""]
    if not all(is_safe_relative_path(p) for p in paths) or (not multi and (payload.content_base64 is None or not payload.name)):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Chemin de fichier invalide.")
    if multi and len(payload.files) > MAX_SEARCH_FILES:
        raise HTTPException(status_code=422, detail="Trop de fichiers pour une seule recherche.")

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
        if multi:
            client_files = [
                (f.name, f.relative_path, base64.b64decode(f.content_base64, validate=True)) for f in payload.files
            ]
        else:
            content = base64.b64decode(payload.content_base64, validate=True)
    except Exception:
        raise HTTPException(status_code=422, detail="Contenu de fichier invalide.") from None
    if multi and sum(len(c) for _, _, c in client_files) > MAX_SEARCH_TOTAL_BYTES:
        raise HTTPException(status_code=413, detail="Volume de fichiers trop important pour une seule recherche.")

    grant = resolve_effective_grant(user)
    if grant is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Votre rôle n'est pas reconnu par le système de permissions.")
    workspace = _workspace_or_403(db, user, conv.workspace_id or DEFAULT_WORKSPACE_ID)
    resume_scope = _folder_scope(payload.folder_scope)
    resume_depth = "direct" if payload.scope_mode == "folder" else "recursive"

    if multi:
        result = await _timed_turn(user.username,
            user_id=str(user.id),
            grant=grant,
            message=last_user_msg.content,
            history=[],
            client_files=client_files,
            client_skipped=[s.model_dump() for s in payload.skipped or []],
            workspace_id=str(workspace.id),
            workspace_name=_scoped_name(workspace, resume_scope, resume_depth),
            folder_scope=resume_scope,
            folder_depth=resume_depth,
        )
    else:
        result = await _timed_turn(user.username,
            user_id=str(user.id),
            grant=grant,
            message=last_user_msg.content,
            history=[],
            client_file_content=content,
            client_file_name=payload.name,
            workspace_id=str(workspace.id),
            workspace_name=_scoped_name(workspace, resume_scope, resume_depth),
            folder_scope=resume_scope,
            folder_depth=resume_depth,
        )

    # Local file reads bypass the company RBAC/ACL system by design (it's
    # the user's own disk, not a shared resource - see
    # _run_local_file_turn's docstring), but every read is still audited
    # for traceability, same as every other action in this app - one row
    # per file, searched or read.
    read_files = (
        [(name, relative_path, len(data)) for name, relative_path, data in client_files]
        if multi
        else [(payload.name, payload.relative_path, len(content))]
    )
    for name, relative_path, size in read_files:
        log_event(
            user_id=user.id,
            username=user.username,
            role=user.role,
            action="FILE_READ",
            decision="ALLOW",
            resource_type="LOCAL_FILE",
            resource_id=relative_path,
            reason="ok",
            extra={
                "name": name,
                "action_id": str(payload.action_id),
                "size_bytes": size,
                "purpose": "search" if multi else "read",
            },
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
                relative_path=result.pending_client_action.get("relative_path"),
                name=result.pending_client_action.get("name"),
                query=result.pending_client_action.get("query"),
                files=result.pending_client_action.get("files"),
                skipped=result.pending_client_action.get("skipped"),
            ),
        )

    # Last guard before anything is stored or shown: credentials masked in
    # the answer, the source excerpts and the search results, whatever path
    # produced them (app/core/secrets_filter.py).
    result.answer = redact_secrets(result.answer)
    result.sources = redact_payload(result.sources)
    result.search_results = redact_payload(result.search_results) if result.search_results else None
    result.email_results = redact_payload(result.email_results) if result.email_results else None
    result.tool_trace = redact_payload(result.tool_trace)
    email_expiry = (
        datetime.now(UTC) + timedelta(days=get_settings().email_results_retention_days)
        if result.email_results or result.answer_mode == "email" else None
    )
    assistant_msg = Message(
        conversation_id=conv.id,
        role="assistant",
        content=result.answer,
        sources=result.sources,
        tool_trace=result.tool_trace,
        reference_context=result.reference_context,
        chart=result.chart,
        clarification=result.clarification,
        search_results=result.search_results,
        email_results=result.email_results,
        email_results_expires_at=email_expiry,
        answer_mode=answer_mode(result),
    )
    db.add(assistant_msg)
    db.commit()
    db.refresh(assistant_msg)

    # Alerts are decided on the internal outcome code (see
    # app/policy/engine.py), never on the visible decision or wording: only
    # a confirmed ACCESS_DENIED is a security event. A missing file, an
    # ambiguous name, a validation error or an outage is audited below but
    # never pages a Director. A refusal the tool already alerted on itself
    # (alert_raised, with richer document details) is not alerted twice.
    policy_denials = [t for t in result.tool_trace if t.get("code") == ACCESS_DENIED]
    unalerted_denials = [t for t in policy_denials if not t.get("alert_raised")]
    tool_errors = [t for t in result.tool_trace if t.get("decision") == "ERROR" and t.get("code") != ACCESS_DENIED]

    log_event(
        user_id=user.id,
        username=user.username,
        role=user.role,
        action="CHAT_MESSAGE",
        decision="DENY" if policy_denials and not result.sources else "ALLOW",
        resource_type="conversation",
        resource_id=str(conv.id),
        reason="one or more tool calls were denied" if policy_denials else "ok",
        extra={
            "message_preview": (
                "[message d'email omis]" if any(t.get("tool") in {"search_emails", "read_email", "draft_email"}
                                               for t in result.tool_trace)
                else redact_secrets(original_message[:200])
            ),
            "tool_trace": result.tool_trace,
            "degraded": result.degraded,
        },
    )
    if tool_errors:
        # Technical/functional failures get their own audit trail, separate
        # from security refusals.
        log_event(
            user_id=user.id,
            username=user.username,
            role=user.role,
            action="CHAT_TOOL_ERROR",
            decision="ERROR",
            resource_type="conversation",
            resource_id=str(conv.id),
            reason="; ".join(f"{t['tool']} → {t.get('code')}" for t in tool_errors),
            extra={"errors": [{"tool": t["tool"], "code": t.get("code"), "reason": t.get("reason")} for t in tool_errors]},
        )

    if unalerted_denials:
        reasons = "; ".join(f"{t['tool']} → {t['reason']}" for t in unalerted_denials)
        create_alert(
            alert_type="CHAT_ACCESS_DENIED",
            user_id=user.id,
            username=user.username,
            role=user.role,
            title=f"Tentative d'accès refusée dans l'assistant IA ({user.full_name})",
            description=f"Question posée : « {original_message[:300]} »\nRefus : {reasons}",
            resource_type="conversation",
            resource_id=str(conv.id),
            # The same refusal on the same question in the same conversation
            # (a resent message, a retry) is one security event, one alert.
            dedup_key=security_event_key(
                user.id,
                "CHAT_ACCESS_DENIED",
                conv.id,
                sorted(f"{t['tool']}|{t['reason']}" for t in unalerted_denials),
                original_message,
            ),
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
            clarification=assistant_msg.clarification,
            search_results=assistant_msg.search_results,
            email_results=assistant_msg.email_results,
            answer_mode=assistant_msg.answer_mode,
            created_at=assistant_msg.created_at,
        ),
    )


_DOCUMENT_TOOLS = {
    "search_documents", "get_document", "get_document_section", "search_keyword", "search_document_text",
    "list_documents", "list_folders", "search_database", "read_spreadsheet_data", "find_document_amounts",
    "generate_chart", "get_company_information", "read_local_file", "search_local_files",
}


def answer_mode(result: AgentTurnResult) -> str:
    """How the answer was produced, for the discreet indicator in the chat."""
    if result.answer_mode:
        return result.answer_mode
    tools = {t.get("tool") for t in result.tool_trace or []}
    if result.degraded and not (result.sources or result.chart or result.search_results):
        return "unavailable"
    web = "web_search" in tools
    documents = bool(tools & _DOCUMENT_TOOLS)
    if web and documents:
        return "hybrid"
    if web:
        return "web"
    if documents:
        return "documents"
    return "direct"
