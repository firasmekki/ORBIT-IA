from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import get_settings

settings = get_settings()

# Everyday app connection: `orbitia_app`, the low-privilege role. Nothing
# in normal request handling should ever need more than this.
engine = create_engine(settings.database_url, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

# Schema owner (`orbitia`) - only for migrations/backups/restores (see
# app.core.migrations.run_privileged_migration). Created lazily so a
# process that never touches migrations (e.g. the mcp server) never opens
# a connection pool with elevated rights it doesn't need.
_migration_engine: Engine | None = None


def get_migration_engine() -> Engine:
    global _migration_engine
    if _migration_engine is None:
        _migration_engine = create_engine(settings.migration_database_url, pool_pre_ping=True, future=True)
    return _migration_engine


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
