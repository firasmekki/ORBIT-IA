"""Additive-only schema bootstrap for this prototype (no Alembic).

`Base.metadata.create_all` creates missing tables but never alters existing
ones, so columns added to a model after the first run need an explicit,
idempotent `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`. Called after
create_all from both the API lifespan and the seed script.
"""

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.core.database import Base


def bootstrap_schema(conn: Connection) -> None:
    conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    Base.metadata.create_all(bind=conn)

    conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS extra_departments VARCHAR(32)[] NOT NULL DEFAULT '{}'"))
    conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS confidentiality_override VARCHAR(32)"))
    conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS extra_tools VARCHAR(64)[] NOT NULL DEFAULT '{}'"))
    # Belt and suspenders: guarantees the server-side default is present
    # even if the column already existed from a run before server_default
    # was added to the model (ADD COLUMN IF NOT EXISTS above is a no-op
    # in that case and would silently skip setting it).
    conn.execute(text("ALTER TABLE users ALTER COLUMN extra_departments SET DEFAULT '{}'"))
    conn.execute(text("ALTER TABLE users ALTER COLUMN extra_tools SET DEFAULT '{}'"))
