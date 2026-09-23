import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.agent.orchestrator import run_agent_turn
from app.core.alert_service import create_alert
from app.core.audit_logger import log_event
from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.chat import Conversation, Message
from app.models.user import User
from app.policy.rules import resolve_effective_grant
from app.schemas.chat import ChatRequest, ChatResponse, ConversationDetail, ConversationSummary, MessageOut

router = APIRouter(prefix="/api", tags=["chat"])

HISTORY_LIMIT = 8


@router.get("/conversations", response_model=list[ConversationSummary])
def list_conversations(db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> list[Conversation]:
    return (
        db.query(Conversation)
        .filter(Conversation.user_id == user.id)
        .order_by(Conversation.created_at.desc())
        .all()
    )


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
def get_conversation(
    conversation_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> Conversation:
    conv = db.get(Conversation, conversation_id)
    if conv is None or conv.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation introuvable.")
    return conv


@router.post("/chat", response_model=ChatResponse)
async def chat(
    payload: ChatRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> ChatResponse:
    if not payload.message.strip():
        raise HTTPException(status_code=422, detail="Le message ne peut pas être vide.")

    if payload.conversation_id:
        conv = db.get(Conversation, payload.conversation_id)
        if conv is None or conv.user_id != user.id:
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
    )

    assistant_msg = Message(
        conversation_id=conv.id,
        role="assistant",
        content=result.answer,
        sources=result.sources,
        tool_trace=result.tool_trace,
        reference_context=result.reference_context,
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
        extra={"message_preview": payload.message[:200], "tool_trace": result.tool_trace, "degraded": result.degraded},
    )

    if policy_denials:
        reasons = "; ".join(f"{t['tool']} → {t['reason']}" for t in policy_denials)
        create_alert(
            alert_type="CHAT_ACCESS_DENIED",
            user_id=user.id,
            username=user.username,
            role=user.role,
            title=f"Tentative d'accès refusée dans l'assistant IA ({user.full_name})",
            description=f"Question posée : « {payload.message[:300]} »\nRefus : {reasons}",
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
            created_at=assistant_msg.created_at,
        ),
    )
