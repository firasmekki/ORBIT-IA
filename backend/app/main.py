import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings
from app.core.database import get_migration_engine
from app.core.migrations import run_privileged_migration
from app.core.storage import ensure_bucket
from app.models import *  # noqa: F401,F403 - populate Base.metadata
from app.rag.watcher import run_folder_watcher
from app.services.embedding_worker import run_embedding_worker
from app.services.extraction_worker import run_extraction_worker
from app.connectivity.email_accounts.retention import run_email_retention_worker
from app.routers import (
    admin, audit, auth, chat, connectivity, documents, email_accounts, runtime, sources, websearch, workspaces,
)
from app.services.source_scanner import run_source_scanner
from app.services.vision_worker import run_vision_worker

settings = get_settings()
# uvicorn only configures its own loggers - without this, every
# orbitia.* INFO line (LLM readiness, chart diagnostics) is silently dropped.
logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s - %(message)s")
# httpx logs every request URL at INFO - for mailbox APIs that is the search
# query and message ids (and for the web search, the user's query). Only
# warnings and errors from the HTTP clients reach the logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("orbitia")


@asynccontextmanager
async def lifespan(app: FastAPI):
    run_privileged_migration(get_migration_engine(), settings)
    try:
        # Copy only, never deletes: see app/connectivity/email_accounts/legacy.py.
        from app.connectivity.email_accounts.legacy import migrate_legacy_gmail_accounts
        from app.core.database import SessionLocal

        with SessionLocal() as db:
            migrate_legacy_gmail_accounts(db)
    except Exception as exc:  # noqa: BLE001 - personal mailboxes are optional, the app still starts
        logger.warning("legacy Gmail accounts not migrated: %s", type(exc).__name__)
    try:
        ensure_bucket()
    except Exception as exc:  # noqa: BLE001 - file upload degrades gracefully, app still starts
        logger.warning("MinIO unavailable at startup, file uploads will fail until it recovers: %s", exc)

    watcher_task = asyncio.create_task(run_folder_watcher())
    embedding_task = asyncio.create_task(run_embedding_worker())
    extraction_task = asyncio.create_task(run_extraction_worker(settings.extraction_concurrency))
    llm_watch_task = asyncio.create_task(_log_llm_readiness())
    source_scanner_task = asyncio.create_task(run_source_scanner())
    vision_task = asyncio.create_task(run_vision_worker())
    email_retention_task = asyncio.create_task(run_email_retention_worker())
    yield
    email_retention_task.cancel()
    vision_task.cancel()
    source_scanner_task.cancel()
    watcher_task.cancel()
    embedding_task.cancel()
    extraction_task.cancel()
    llm_watch_task.cancel()


async def _log_llm_readiness() -> None:
    """The model is loaded by the `llm` container, once, independently of
    this process - startup never waits for it (the UI shows "Chargement du
    modèle IA..." meanwhile). This only records in the logs when it becomes
    usable, so a restart's effect is visible: a backend restart finds the
    model already resident, an `llm` restart reloads it once."""
    from app.agent.llm_client import get_llm_status

    last = None
    while True:
        state = await get_llm_status()
        if state["status"] != last:
            logger.info("LLM %s: %s", state["model"], state["message"])
            last = state["status"]
        if state["status"] == "ready":
            return
        await asyncio.sleep(5)


app = FastAPI(title="Orbitia - Private Enterprise AI Agent", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(documents.router)
app.include_router(chat.router)
app.include_router(audit.router)
app.include_router(admin.router)
app.include_router(workspaces.router)
app.include_router(sources.router)
app.include_router(runtime.router)
app.include_router(websearch.router)
app.include_router(connectivity.router)
app.include_router(email_accounts.router)


@app.get("/health")
@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}
