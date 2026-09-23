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
from app.routers import admin, audit, auth, chat, documents

settings = get_settings()
logger = logging.getLogger("orbitia")


@asynccontextmanager
async def lifespan(app: FastAPI):
    run_privileged_migration(get_migration_engine(), settings)
    try:
        ensure_bucket()
    except Exception as exc:  # noqa: BLE001 - file upload degrades gracefully, app still starts
        logger.warning("MinIO unavailable at startup, file uploads will fail until it recovers: %s", exc)

    watcher_task = asyncio.create_task(run_folder_watcher())
    yield
    watcher_task.cancel()


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


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}
