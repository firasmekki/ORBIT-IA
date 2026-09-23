import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.core.alert_service import create_alert
from app.core.audit_logger import log_event
from app.core.config import get_settings
from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.storage import download_file, upload_file
from app.models.document import CONFIDENTIALITY_RANK, Document
from app.models.user import User
from app.policy.engine import check_document_access, retrieval_scope
from app.policy.rules import resolve_effective_grant
from app.rag.extract import ExtractionError, UnsupportedFileTypeError, extract
from app.rag.ingest import ingest_document
from app.schemas.document import DocumentCreate, DocumentDetail, DocumentSummary, DocumentUpdate

router = APIRouter(prefix="/api/documents", tags=["documents"])

MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MB


@router.get("/watch-status")
def watch_status(_user: User = Depends(get_current_user)) -> dict:
    settings = get_settings()
    return {
        "enabled": settings.watch_folder_enabled,
        "department": settings.watch_folder_department,
        "confidentiality": settings.watch_folder_confidentiality,
    }


@router.get("", response_model=list[DocumentSummary])
def list_documents(db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> list[Document]:
    grant = resolve_effective_grant(user)
    departments, max_rank = retrieval_scope(grant=grant)
    if not departments:
        return []
    docs = (
        db.query(Document)
        .filter(Document.department.in_(departments), Document.confidentiality_rank <= max_rank)
        .order_by(Document.created_at.desc())
        .all()
    )
    return docs


def _authorize_document_write(
    *, db: Session, user: User, action: str, title: str, department: str, confidentiality: str
) -> None:
    """Shared guard for create_document and upload_document: a user can
    never file a document at a department/confidentiality combination they
    could not themselves read back - prevents a low-privilege account from
    self-granting a visibility bump."""
    if confidentiality not in CONFIDENTIALITY_RANK:
        raise HTTPException(status_code=422, detail="Niveau de confidentialité inconnu.")

    grant = resolve_effective_grant(user)
    decision = check_document_access(grant=grant, department=department, confidentiality=confidentiality)
    log_event(
        user_id=user.id,
        username=user.username,
        role=user.role,
        action=action,
        decision="ALLOW" if decision.allowed else "DENY",
        resource_type="document",
        reason=decision.reason,
        extra={"title": title, "department": department, "confidentiality": confidentiality},
    )
    if not decision.allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Vous ne pouvez pas créer un document à un niveau d'accès supérieur au vôtre.",
        )


@router.get("/{document_id}", response_model=DocumentDetail)
def get_document(
    document_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> Document:
    doc = db.get(Document, document_id)
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document introuvable.")

    grant = resolve_effective_grant(user)
    decision = check_document_access(grant=grant, department=doc.department, confidentiality=doc.confidentiality)
    log_event(
        user_id=user.id,
        username=user.username,
        role=user.role,
        action="GET_DOCUMENT",
        decision="ALLOW" if decision.allowed else "DENY",
        resource_type="document",
        resource_id=str(doc.id),
        reason=decision.reason,
        extra={"title": doc.title, "department": doc.department, "confidentiality": doc.confidentiality},
    )
    if not decision.allowed:
        create_alert(
            alert_type="DOCUMENT_ACCESS_DENIED",
            user_id=user.id,
            username=user.username,
            role=user.role,
            title=f"Tentative d'accès refusée à un document ({user.full_name})",
            description=f"Document visé : « {doc.title} » ({doc.department} / {doc.confidentiality})\nRefus : {decision.reason}",
            resource_type="document",
            resource_id=str(doc.id),
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Accès refusé : vous n'avez pas la permission de consulter ce document.",
        )
    return doc


@router.patch("/{document_id}", response_model=DocumentDetail)
def update_document(
    document_id: uuid.UUID,
    payload: DocumentUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Document:
    """Correct a document's classification (e.g. filed as PUBLIC by mistake).
    Content/file are untouched - only metadata changes, so no re-ingestion
    is needed: the RAG retriever joins on Document.department/confidentiality
    live at query time, it never bakes permissions into the embedding."""
    doc = db.get(Document, document_id)
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document introuvable.")

    grant = resolve_effective_grant(user)

    current_decision = check_document_access(grant=grant, department=doc.department, confidentiality=doc.confidentiality)
    if not current_decision.allowed:
        create_alert(
            alert_type="DOCUMENT_ACCESS_DENIED",
            user_id=user.id,
            username=user.username,
            role=user.role,
            title=f"Tentative de modification refusée ({user.full_name})",
            description=f"Document visé : « {doc.title} » ({doc.department} / {doc.confidentiality})\nRefus : {current_decision.reason}",
            resource_type="document",
            resource_id=str(doc.id),
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Accès refusé à ce document.")

    new_department = payload.department or doc.department
    new_confidentiality = payload.confidentiality or doc.confidentiality
    if new_confidentiality not in CONFIDENTIALITY_RANK:
        raise HTTPException(status_code=422, detail="Niveau de confidentialité inconnu.")

    # Can only move a document within the same range they could themselves
    # read - same rule as creating one. Stops a low-privilege account from
    # using "correct a mistake" to relabel a document into a department or
    # confidentiality level above its own clearance.
    target_decision = check_document_access(grant=grant, department=new_department, confidentiality=new_confidentiality)
    log_event(
        user_id=user.id,
        username=user.username,
        role=user.role,
        action="UPDATE_DOCUMENT",
        decision="ALLOW" if target_decision.allowed else "DENY",
        resource_type="document",
        resource_id=str(doc.id),
        reason=target_decision.reason,
        extra={
            "title": doc.title,
            "from": {"department": doc.department, "confidentiality": doc.confidentiality},
            "to": {"department": new_department, "confidentiality": new_confidentiality},
        },
    )
    if not target_decision.allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Vous ne pouvez pas déplacer ce document vers un niveau d'accès supérieur au vôtre.",
        )

    if payload.title is not None:
        doc.title = payload.title
    doc.department = new_department
    doc.confidentiality = new_confidentiality
    doc.confidentiality_rank = CONFIDENTIALITY_RANK[new_confidentiality]
    db.commit()
    db.refresh(doc)
    return doc


@router.get("/{document_id}/file")
def download_document_file(
    document_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> StreamingResponse:
    """Streams the original uploaded file back (PDF/XLSX/DOCX/...), not the
    extracted text - same permission check as reading the document."""
    doc = db.get(Document, document_id)
    if doc is None or not doc.minio_object_key:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Aucun fichier original pour ce document.")

    grant = resolve_effective_grant(user)
    decision = check_document_access(grant=grant, department=doc.department, confidentiality=doc.confidentiality)
    log_event(
        user_id=user.id,
        username=user.username,
        role=user.role,
        action="DOWNLOAD_DOCUMENT_FILE",
        decision="ALLOW" if decision.allowed else "DENY",
        resource_type="document",
        resource_id=str(doc.id),
        reason=decision.reason,
        extra={"title": doc.title, "filename": doc.source_filename},
    )
    if not decision.allowed:
        create_alert(
            alert_type="DOCUMENT_ACCESS_DENIED",
            user_id=user.id,
            username=user.username,
            role=user.role,
            title=f"Tentative d'accès refusée à un document ({user.full_name})",
            description=f"Fichier visé : « {doc.title} » ({doc.department} / {doc.confidentiality})\nRefus : {decision.reason}",
            resource_type="document",
            resource_id=str(doc.id),
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Accès refusé.")

    try:
        data = download_file(doc.minio_object_key)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Stockage indisponible : {exc}") from None

    filename = doc.source_filename or "document"
    return StreamingResponse(
        iter([data]),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("", response_model=DocumentDetail, status_code=status.HTTP_201_CREATED)
def create_document(
    payload: DocumentCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> Document:
    _authorize_document_write(
        db=db,
        user=user,
        action="CREATE_DOCUMENT",
        title=payload.title,
        department=payload.department,
        confidentiality=payload.confidentiality,
    )

    doc = Document(
        title=payload.title,
        department=payload.department,
        confidentiality=payload.confidentiality,
        confidentiality_rank=CONFIDENTIALITY_RANK[payload.confidentiality],
        content=payload.content,
        owner_id=user.id,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)

    ingest_document(db, doc)
    return doc


@router.post("/upload", response_model=DocumentDetail, status_code=status.HTTP_201_CREATED)
async def upload_document(
    title: str = Form(...),
    department: str = Form(...),
    confidentiality: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Document:
    """Upload a real office file (PDF, Excel, Word, text/markdown). The file
    is stored as-is in MinIO for download; its text is extracted and is what
    actually gets chunked and embedded for the RAG pipeline."""
    _authorize_document_write(
        db=db, user=user, action="CREATE_DOCUMENT", title=title, department=department, confidentiality=confidentiality
    )

    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"Fichier trop volumineux (max {MAX_UPLOAD_BYTES // (1024 * 1024)} Mo).")
    if not raw:
        raise HTTPException(status_code=422, detail="Le fichier est vide.")

    try:
        extraction = extract(file.filename or "", raw)
    except UnsupportedFileTypeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except ExtractionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    object_key = f"{uuid.uuid4()}-{file.filename}"
    try:
        upload_file(object_key, raw, content_type=file.content_type or "application/octet-stream")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"Stockage de fichiers indisponible : {exc}") from None

    doc = Document(
        title=title,
        department=department,
        confidentiality=confidentiality,
        confidentiality_rank=CONFIDENTIALITY_RANK[confidentiality],
        content=extraction.text,
        source_filename=file.filename,
        minio_object_key=object_key,
        owner_id=user.id,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)

    ingest_document(db, doc, pages=extraction.pages)
    return doc
