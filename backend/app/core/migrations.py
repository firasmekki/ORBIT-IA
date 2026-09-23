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
    conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
    conn.execute(text("CREATE EXTENSION IF NOT EXISTS unaccent"))
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

    # search_keyword / get_document_section support. `simple` config on
    # purpose (not `french`/`arabic`): stemming would merge distinct words
    # together, which is wrong for an *exact* occurrence count - this index
    # only needs to be a cheap pre-filter, the real count is a regex pass in
    # Python over the normalized text (see app/rag/normalize.py).
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_document_pages_text_fts ON document_pages "
            "USING gin (to_tsvector('simple', text))"
        )
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_document_pages_text_trgm ON document_pages "
            "USING gin (text gin_trgm_ops)"
        )
    )
