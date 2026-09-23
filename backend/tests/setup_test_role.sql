-- One-time setup for the dedicated test role (see conftest.py and
-- INCIDENTS.md). Run once per Postgres instance, as the `orbitia`
-- superuser:
--
--   docker compose exec -T postgres psql -U orbitia -d postgres -c "CREATE DATABASE orbitia_test;"
--   docker compose exec -T postgres psql -U orbitia -d postgres -f backend/tests/setup_test_role.sql
--
-- `orbitia_test` has no CONNECT privilege on the real `orbitia` database at
-- all - not just weaker permissions on it, no access to it whatsoever. A
-- test suite that somehow points at the wrong database fails immediately
-- with "permission denied for database", instead of silently connecting
-- and running its cleanup TRUNCATE against real data (see INCIDENTS.md).

DO $$
BEGIN
   IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'orbitia_test') THEN
      CREATE ROLE orbitia_test LOGIN PASSWORD 'orbitia_test';
   END IF;
END
$$;

REVOKE CONNECT ON DATABASE orbitia_test FROM PUBLIC;
GRANT CONNECT ON DATABASE orbitia_test TO orbitia_test;
ALTER DATABASE orbitia_test OWNER TO orbitia_test;

-- pgvector/pg_trgm/unaccent's CREATE EXTENSION requires superuser even
-- with IF NOT EXISTS the first time - pre-create them once here so
-- bootstrap_schema's own CREATE EXTENSION IF NOT EXISTS calls (run as
-- orbitia_test during tests) are then no-ops. Must run against the
-- orbitia_test DATABASE specifically, not postgres/orbitia - reconnect:
\c orbitia_test
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS unaccent;
