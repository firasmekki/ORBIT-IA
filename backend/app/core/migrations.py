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
from app.models.workspace import DEFAULT_WORKSPACE_ID, DEFAULT_WORKSPACE_NAME

logger = logging.getLogger("orbitia.migrations")


def bootstrap_schema(conn: Connection) -> None:
    conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
    conn.execute(text("CREATE EXTENSION IF NOT EXISTS unaccent"))
    Base.metadata.create_all(bind=conn)

    conn.execute(text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS content_hash VARCHAR(64)"))

    # Workspaces (app/models/workspace.py). Every existing document lands in
    # the default workspace through the column DEFAULT - nothing is moved,
    # rewritten or deleted.
    conn.execute(
        text(
            "INSERT INTO workspaces (id, name, owner_id, default_department, default_confidentiality, created_at) "
            "VALUES (:id, :name, NULL, 'GENERAL', 'INTERNAL', now()) ON CONFLICT (id) DO NOTHING"
        ),
        {"id": DEFAULT_WORKSPACE_ID, "name": DEFAULT_WORKSPACE_NAME},
    )
    conn.execute(
        text(f"ALTER TABLE documents ADD COLUMN IF NOT EXISTS workspace_id UUID NOT NULL DEFAULT '{DEFAULT_WORKSPACE_ID}'")
    )
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_documents_workspace_id ON documents (workspace_id)"))
    conn.execute(text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS relative_path VARCHAR(1024)"))
    conn.execute(text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS size_bytes BIGINT"))
    conn.execute(text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS mime_type VARCHAR(128)"))
    conn.execute(text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS file_last_modified TIMESTAMPTZ"))
    conn.execute(text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS indexed_at TIMESTAMPTZ"))
    conn.execute(text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS status VARCHAR(16) NOT NULL DEFAULT 'indexed'"))
    conn.execute(text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT true"))
    conn.execute(text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS index_progress INTEGER"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_documents_status ON documents (status)"))
    # One document per synced file: makes a repeated sync idempotent at the
    # database level, not just in the service.
    conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_documents_workspace_relative_path "
            "ON documents (workspace_id, relative_path) WHERE relative_path IS NOT NULL"
        )
    )
    conn.execute(text("ALTER TABLE conversations ADD COLUMN IF NOT EXISTS workspace_id UUID"))
    conn.execute(text("ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS visibility VARCHAR(16) NOT NULL DEFAULT 'private'"))
    # Personal documents (Document.subject_user_id). NULL for every existing
    # document: nothing becomes more or less visible until someone assigns one.
    conn.execute(
        text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS subject_user_id UUID REFERENCES users(id) ON DELETE SET NULL")
    )
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_documents_subject_user_id ON documents (subject_user_id)"))
    # "sync" | "upload" | NULL for every existing document (treated as before).
    conn.execute(text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS origin VARCHAR(16)"))
    conn.execute(text("ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_conversations_workspace_id ON conversations (workspace_id)"))

    # search_documents page/section enrichment. Nullable and backfilled
    # lazily (only on the next re-ingestion of each document, no forced
    # re-embed of everything that already exists) - a chunk with no known
    # page yet just reports page/section as null, never a guess.
    conn.execute(text("ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS document_page_id UUID"))
    conn.execute(
        text(
            "DO $$ BEGIN "
            "IF NOT EXISTS (SELECT 1 FROM information_schema.table_constraints "
            "WHERE constraint_name = 'document_chunks_document_page_id_fkey') THEN "
            "ALTER TABLE document_chunks ADD CONSTRAINT document_chunks_document_page_id_fkey "
            "FOREIGN KEY (document_page_id) REFERENCES document_pages(id) ON DELETE SET NULL; "
            "END IF; "
            "END $$;"
        )
    )
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_document_chunks_document_page_id ON document_chunks (document_page_id)"))

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

    # generate_chart (app/agent/chart.py) - chart spec rendered by the
    # frontend, see the Message model's docstring.
    conn.execute(text("ALTER TABLE messages ADD COLUMN IF NOT EXISTS chart JSONB"))
    # Structured clarification question (chart dialog) - NULL for old rows.
    conn.execute(text("ALTER TABLE messages ADD COLUMN IF NOT EXISTS clarification JSONB"))
    conn.execute(text("ALTER TABLE messages ADD COLUMN IF NOT EXISTS search_results JSONB"))
    # How the answer was produced (direct / general / documents / web /
    # hybrid / unavailable) - NULL for old rows (derived by the frontend).
    conn.execute(text("ALTER TABLE messages ADD COLUMN IF NOT EXISTS answer_mode VARCHAR(16)"))
    # Personal mailbox answers (app/agent/email_turn.py) and their retention
    # (app/connectivity/email_accounts/retention.py). email_connections and
    # email_oauth_states are new tables: created by create_all above.
    conn.execute(text("ALTER TABLE messages ADD COLUMN IF NOT EXISTS email_results JSONB"))
    conn.execute(text("ALTER TABLE messages ADD COLUMN IF NOT EXISTS email_results_expires_at TIMESTAMPTZ"))
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_messages_email_results_expires_at ON messages (email_results_expires_at) "
            "WHERE email_results_expires_at IS NOT NULL"
        )
    )

    # Soft-delete for "Supprimer l'historique" (routers/chat.py's
    # delete_history) - see Conversation.deleted_at's docstring. Every read
    # endpoint filters deleted_at IS NULL; the column is never the only
    # trace of the deletion (the AuditLog row written in the same
    # transaction is).
    conn.execute(text("ALTER TABLE conversations ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_conversations_deleted_at ON conversations (deleted_at)"))

    # Links a Director-facing alert to the immutable audit_logs row that
    # backs it - see Alert.audit_log_id's docstring.
    conn.execute(text("ALTER TABLE alerts ADD COLUMN IF NOT EXISTS audit_log_id UUID"))
    conn.execute(
        text(
            "DO $$ BEGIN "
            "IF NOT EXISTS (SELECT 1 FROM information_schema.table_constraints "
            "WHERE constraint_name = 'alerts_audit_log_id_fkey') THEN "
            "ALTER TABLE alerts ADD CONSTRAINT alerts_audit_log_id_fkey "
            "FOREIGN KEY (audit_log_id) REFERENCES audit_logs(id) ON DELETE SET NULL; "
            "END IF; "
            "END $$;"
        )
    )
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_alerts_audit_log_id ON alerts (audit_log_id)"))

    # One alert per security event, not per retry - see
    # app/core/alert_service.py's deduplication. Existing alerts keep NULL.
    conn.execute(text("ALTER TABLE alerts ADD COLUMN IF NOT EXISTS dedup_key VARCHAR(64)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_alerts_dedup_key ON alerts (dedup_key, created_at)"))

    # Server folder sources (app/services/source_scanner.py) and the
    # per-document failure reason/attempt counter of the background workers.
    # All nullable or defaulted: existing workspaces/documents are unchanged.
    conn.execute(text("ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS source_root VARCHAR(16)"))
    conn.execute(text("ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS source_path VARCHAR(1024)"))
    conn.execute(text("ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS source_enabled BOOLEAN NOT NULL DEFAULT true"))
    conn.execute(text("ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS source_status VARCHAR(16)"))
    conn.execute(text("ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS source_error TEXT"))
    conn.execute(text("ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS source_scan_started_at TIMESTAMPTZ"))
    conn.execute(text("ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS source_scan_seen INTEGER"))
    conn.execute(text("ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS source_scan_requested_at TIMESTAMPTZ"))
    conn.execute(text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS index_error TEXT"))
    conn.execute(text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS index_attempts INTEGER NOT NULL DEFAULT 0"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_source_files_state ON source_files (workspace_id, state)"))
    conn.execute(text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS issue_kind VARCHAR(32)"))
    conn.execute(text("ALTER TABLE source_files ADD COLUMN IF NOT EXISTS file_count INTEGER"))
    # Images and scans with no text were recorded as "failed" before
    # ExtractionError.kind existed: they are not errors. Reclassified from
    # their stored reason - idempotent, touches nothing else.
    conn.execute(
        text(
            "UPDATE documents SET status = 'no_text', issue_kind = 'no_text_image' "
            "WHERE status = 'failed' AND issue_kind IS NULL AND index_error ILIKE '%aucun texte détecté par OCR%'"
        )
    )
    conn.execute(
        text(
            "UPDATE documents SET status = 'no_text', issue_kind = 'no_text_scan' "
            "WHERE status = 'failed' AND issue_kind IS NULL AND index_error ILIKE '%même après OCR%'"
        )
    )
    conn.execute(
        text(
            "UPDATE documents SET issue_kind = CASE "
            "WHEN index_error ILIKE '%embeddings impossibles%' THEN 'embedding' "
            "WHEN index_error ILIKE '%aucune donnée%' OR index_error ILIKE '%aucun texte%' THEN 'empty' "
            "ELSE 'corrupt' END "
            "WHERE status = 'failed' AND issue_kind IS NULL AND index_error IS NOT NULL"
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
