"""Additive-only schema bootstrap for this prototype (no Alembic).

`Base.metadata.create_all` creates missing tables but never alters existing
ones, so columns added to a model after the first run need an explicit,
idempotent `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`. Called after
create_all from both the API lifespan and the seed script.

`bootstrap_schema` alone (tables/columns/indexes) is what the test suite
calls too, against orbitia_test - it must stay usable with nothing more
than CREATE-on-schema rights. `bootstrap_roles_and_grants` and
`run_privileged_migration` are for the real app only (main.py's lifespan,
seed.py, backfill_pages.py): they need the schema-owner connection
(settings.migration_database_url) and have no reason to ever touch
orbitia_test.
"""

import logging

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from app.core.config import Settings
from app.core.database import Base

logger = logging.getLogger("orbitia.migrations")


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

    # get_document_section follow-up resolution (app/agent/intent.py /
    # orchestrator.py) - structured continuity between turns, see the
    # Message model's docstring for the shape.
    conn.execute(text("ALTER TABLE messages ADD COLUMN IF NOT EXISTS reference_context JSONB"))

    # Case/accent-insensitive exact-then-partial title lookup for
    # get_document_section's document_name resolution.
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_documents_title_trgm ON documents "
            "USING gin (title gin_trgm_ops)"
        )
    )


def bootstrap_roles_and_grants(conn: Connection, settings: Settings) -> None:
    """Creates/refreshes `orbitia_app`, the low-privilege role the running
    app actually connects as (settings.database_url) - separate from
    `orbitia`, the schema owner used only for migrations/backups/restores
    (settings.migration_database_url). Re-asserted on every migration run
    so a broader grant can never silently outlive a schema change.

    Why this exists: a test-suite bug once pointed the app's own
    DATABASE_URL at the real demo database, and its cleanup TRUNCATE wiped
    users/documents/audit_logs/alerts (see INCIDENTS.md). The app's normal
    runtime connection having TRUNCATE/DDL rights at all was part of what
    made that possible - this closes that gap regardless of what future
    code (app, a script, a bug) runs through app.core.database.SessionLocal.
    """
    password = settings.app_db_password.replace("'", "''")
    conn.execute(
        text(
            f"""
            DO $$
            BEGIN
               IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'orbitia_app') THEN
                  CREATE ROLE orbitia_app LOGIN PASSWORD '{password}';
               ELSE
                  ALTER ROLE orbitia_app WITH LOGIN PASSWORD '{password}';
               END IF;
            END
            $$;
            """
        )
    )
    conn.execute(text("REVOKE CONNECT ON DATABASE orbitia FROM PUBLIC"))
    conn.execute(text("GRANT CONNECT ON DATABASE orbitia TO orbitia_app"))
    conn.execute(text("GRANT USAGE ON SCHEMA public TO orbitia_app"))
    conn.execute(text("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO orbitia_app"))
    conn.execute(text("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO orbitia_app"))
    conn.execute(text("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO orbitia_app"))
    conn.execute(text("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO orbitia_app"))

    # Immutable audit trail: INSERT + SELECT only, never UPDATE/DELETE/
    # TRUNCATE - matches AuditLog's own docstring ("append-only, no
    # update/delete route is ever exposed"), now enforced by Postgres
    # itself rather than just by which routes happen to exist.
    conn.execute(text("REVOKE UPDATE, DELETE, TRUNCATE ON audit_logs FROM orbitia_app"))
    conn.execute(text("GRANT SELECT, INSERT ON audit_logs TO orbitia_app"))

    # Alerts: routers/admin.py's "mark as read"/"mark all as read" features
    # legitimately mutate is_read/read_at, so UPDATE stays - but scoped to
    # exactly those two columns (a table-level UPDATE grant would make this
    # column list moot, so the blanket grant above must be revoked first).
    # No DELETE/TRUNCATE, and no UPDATE on any other column (user_id, role,
    # title, description, ...) - verified: an UPDATE on `description` as
    # orbitia_app fails with "permission denied for table alerts".
    conn.execute(text("REVOKE UPDATE, DELETE, TRUNCATE ON alerts FROM orbitia_app"))
    conn.execute(text("GRANT SELECT, INSERT ON alerts TO orbitia_app"))
    conn.execute(text("GRANT UPDATE (is_read, read_at) ON alerts TO orbitia_app"))


def run_privileged_migration(engine: Engine, settings: Settings) -> None:
    """Entry point for the real app (main.py's lifespan, seed.py,
    backfill_pages.py) - takes a backup first, then applies schema changes
    and role grants, all against the privileged `orbitia` connection.
    Tests call bootstrap_schema(conn) directly instead: no backup, no role
    grants, the low-privilege test role has no use for either and no
    rights to run them anyway."""
    try:
        from app.core.backup import run_backup

        run_backup("pre_migration")
    except Exception:
        logger.exception("Pre-migration backup failed - continuing with the migration anyway")

    with engine.begin() as conn:
        bootstrap_schema(conn)
        bootstrap_roles_and_grants(conn, settings)
