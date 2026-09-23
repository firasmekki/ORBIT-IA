# Orbitia — notes for Claude Code

## Never run tests or destructive commands without verifying the target database

Before running the test suite, a migration, a `TRUNCATE`/`DELETE`, or any
other destructive database command, confirm which database it will
actually hit (`echo $DATABASE_URL` / `docker compose exec <service> env |
grep DATABASE_URL`, or check the connection string in the command itself).
Never assume an ambient environment variable points where you expect.

**Why**: on 2026-09-23, the test suite's cleanup fixture ran a `TRUNCATE`
against the real demo database instead of a test one, because an ambient
`DATABASE_URL` set by docker-compose silently overrode what the test code
intended to connect to. See [INCIDENTS.md](INCIDENTS.md) for the full
story and the defenses now in place (a dedicated `orbitia_test` role with
no access to the real database, a low-privilege `orbitia_app` role with no
`TRUNCATE`/DDL rights at all, automatic backups). Those defenses make a
repeat harder, but they are not a substitute for checking first.
