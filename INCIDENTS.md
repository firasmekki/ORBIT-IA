# Incidents

## 2026-09-23 — Test suite wiped the demo database

**Cause**

`backend/tests/conftest.py` set the test database connection with
`os.environ.setdefault("DATABASE_URL", ...)`. Whenever the suite ran inside
a container started via `docker compose run backend ...`, `DATABASE_URL`
was already set in that container's environment (the `&backend-env` anchor
in `docker-compose.yml` points it at the real `orbitia` database) -
`setdefault` is a no-op when the variable already exists, so every test run
silently connected to the real demo database instead of a dedicated test
one. Each test's cleanup fixture ran:

```sql
TRUNCATE documents, document_pages, document_chunks, users, audit_logs, alerts RESTART IDENTITY CASCADE;
```

against that real database, after every single test.

**Impact**

- `users`, `documents`, `document_pages`, `document_chunks`, `audit_logs`,
  and `alerts` were emptied, repeatedly, across several test runs during
  the same session.
- The 5 demo accounts and all documents became inaccessible (login failed
  for everyone).

**Recovery**

- Demo users and the 10 seeded documents/financial records: restored by
  re-running `python -m app.seed` (idempotent, recreates its own fixed
  demo set).
- `document_pages`: repopulated by `python -m app.backfill_pages`.
- 6 documents that lived in the auto-watched folder: recovered for free -
  the watcher's next scan found them still on disk and re-imported them.
- 1 document (`Offre SMETA - à valider`, uploaded outside the watched
  folder) was not covered by either of the above. Its original bytes
  happened to still exist in MinIO (deleting a `documents` row never
  deletes the underlying object) - recovered manually by re-downloading,
  re-extracting, and re-creating the `Document` row from it.
- **Not recoverable**: `audit_logs` and `alerts` history prior to the
  incident. There is no backup of an audit trail other than the audit
  trail itself, and none existed yet at the time. A dated `SYSTEM_INCIDENT`
  entry was added to `audit_logs` after the fact so this gap is visible to
  an auditor reading the log, instead of just looking like a fresh
  install.

**Corrective actions**

1. **Backups** (`app/core/backup.py`): automatic `pg_dump` before every
   schema migration and once a day (`backup` service in
   `docker-compose.yml`), 7-day rotation, restore documented and tested
   end-to-end in `README.md`.
2. **Immutable audit log**: the app's own database role (`orbitia_app`)
   now only has `INSERT`/`SELECT` on `audit_logs` - no `UPDATE`, `DELETE`,
   or `TRUNCATE`, enforced by Postgres itself
   (`app/core/migrations.py::bootstrap_roles_and_grants`). Verified: a
   `TRUNCATE`/`DELETE` on `audit_logs` as `orbitia_app` now fails with
   `permission denied for table audit_logs`. `alerts` keeps `UPDATE`
   (the "mark as read" feature legitimately needs it) but loses `DELETE`
   and `TRUNCATE` for the same reason.
3. **Schema migrations run on a separate, privileged connection**
   (`MIGRATION_DATABASE_URL`, role `orbitia`) from the app's normal
   runtime connection (`DATABASE_URL`, role `orbitia_app`) - the app
   itself never has DDL rights at all, closing the class of bug that made
   this incident possible in the first place (the app's own connection
   being powerful enough to TRUNCATE anything).
4. **Test isolation**: the test suite now connects as `orbitia_test`, a
   role with no `CONNECT` privilege on the real `orbitia` database
   whatsoever - a wrong `DATABASE_URL` now fails at the connection itself
   ("permission denied for database"), not by silently succeeding against
   the wrong data. Combined with the pre-existing string-based safety
   check in `conftest.py` (refuses to start unless the resolved database
   name starts with `orbitia_test`) for defense in depth.
5. **`CLAUDE.md`** now states the rule this incident violated: never run
   tests or destructive commands without verifying the target database
   first.
