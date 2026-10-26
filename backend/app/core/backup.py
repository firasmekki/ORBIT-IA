"""pg_dump-based backups: one taken automatically right before every schema
migration (app.core.migrations.run_privileged_migration) and one taken
daily by the standalone `backup` service in docker-compose.yml (via
`python -m app.core.backup daily`) - both go through `run_backup()`, so
there is exactly one code path to trust, not a Python path and a separate
shell script that can drift apart.

One dump at a time, never a half-written one passed off as a backup:
- a PostgreSQL advisory lock serializes the backend, `seed` and `backup`
  containers, which all start together on `docker compose up` and used to
  dump the whole database three times in parallel;
- once the lock is held, a complete dump younger than the reuse window is
  returned instead of taking a new one (the API waits for this call before
  serving requests, so every redundant dump was minutes of 502);
- pg_dump writes to `<name>.dump.partial`, renamed to `<name>.dump` only
  after a successful exit - a dump interrupted by a container restart or a
  timeout can no longer be mistaken for a valid backup.

Restore is deliberately NOT automated here - see README's "Sauvegardes et
restauration" section for the pg_restore command. A restore overwrites the
live database; it should never be one accidental function call away,
especially in a module that also runs unattended on a schedule.
"""

import logging
import os
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import text

from app.core.config import get_settings

logger = logging.getLogger("orbitia.backup")

# Arbitrary, fixed key shared by every container that may take a backup.
BACKUP_LOCK_KEY = 7400099
PARTIAL_SUFFIX = ".partial"


def _connection_parts():
    settings = get_settings()
    # urlparse doesn't know the "+psycopg" dialect suffix SQLAlchemy uses.
    url = urlparse(settings.migration_database_url.replace("postgresql+psycopg://", "postgresql://"))
    return url, settings


@contextmanager
def _backup_lock() -> Iterator[None]:
    """Session-level advisory lock on the privileged connection, held for the
    whole dump: a second container blocks here, then finds the fresh dump
    and reuses it instead of starting its own."""
    from app.core.database import get_migration_engine

    with get_migration_engine().connect() as conn:
        conn.execute(text("SELECT pg_advisory_lock(:key)"), {"key": BACKUP_LOCK_KEY})
        try:
            yield
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": BACKUP_LOCK_KEY})


def latest_complete_backup(backup_dir: Path) -> Path | None:
    """Newest finished, non-empty dump. `.partial` files never qualify."""
    dumps = [p for p in backup_dir.glob("orbitia_*.dump") if p.is_file() and p.stat().st_size > 0]
    return max(dumps, key=lambda p: p.stat().st_mtime, default=None)


def _reusable_backup(backup_dir: Path, max_age_minutes: int) -> Path | None:
    if max_age_minutes <= 0:
        return None
    latest = latest_complete_backup(backup_dir)
    if latest is None:
        return None
    age_seconds = time.time() - latest.stat().st_mtime
    return latest if age_seconds < max_age_minutes * 60 else None


def run_backup(reason: str, *, reuse_minutes: int | None = None) -> Path:
    """Runs `pg_dump` against the privileged (migration_database_url)
    connection - never the app's own low-privilege role, which shouldn't
    need pg_dump's read-everything access as a standing grant. Returns the
    path to the new dump file, or to a complete dump younger than
    `reuse_minutes` (default: settings.backup_reuse_minutes); raises on
    failure (the caller decides whether that should block whatever
    triggered the backup)."""
    url, settings = _connection_parts()
    backup_dir = Path(settings.backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    window = settings.backup_reuse_minutes if reuse_minutes is None else reuse_minutes

    with _backup_lock():
        reused = _reusable_backup(backup_dir, window)
        if reused is not None:
            logger.info("Backup reused: %s (%s, younger than %d min)", reused, reason, window)
            return reused

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        safe_reason = "".join(c if c.isalnum() else "_" for c in reason) or "manual"
        dest = backup_dir / f"orbitia_{safe_reason}_{timestamp}.dump"
        partial = dest.with_name(dest.name + PARTIAL_SUFFIX)

        env = {**os.environ, "PGPASSWORD": url.password or ""}
        args = [
            "pg_dump",
            "-h", url.hostname or "localhost",
            "-p", str(url.port or 5432),
            "-U", url.username or "orbitia",
            "-d", (url.path or "/orbitia").lstrip("/"),
            "-Fc",
            "-f", str(partial),
        ]
        try:
            result = subprocess.run(
                args, env=env, capture_output=True, text=True, timeout=settings.backup_timeout_seconds
            )
        except subprocess.TimeoutExpired:
            partial.unlink(missing_ok=True)
            raise RuntimeError(f"pg_dump timed out after {settings.backup_timeout_seconds}s") from None
        if result.returncode != 0:
            partial.unlink(missing_ok=True)
            raise RuntimeError(f"pg_dump exited {result.returncode}: {result.stderr.strip()}")
        os.replace(partial, dest)

        logger.info("Backup written: %s (%s)", dest, reason)
        _rotate(backup_dir, settings.backup_retention_days)
        return dest


def _rotate(backup_dir: Path, keep_days: int) -> None:
    cutoff = time.time() - keep_days * 86400
    for path in backup_dir.glob("orbitia_*.dump"):
        if path.stat().st_mtime < cutoff:
            path.unlink()
            logger.info("Rotated out backup older than %d day(s): %s", keep_days, path)
    # Leftovers of a dump killed mid-way (container stopped before the lock
    # was released). Never a valid backup; an hour is far longer than a dump.
    stale = time.time() - 3600
    for path in backup_dir.glob(f"orbitia_*.dump{PARTIAL_SUFFIX}"):
        if path.stat().st_mtime < stale:
            path.unlink()
            logger.info("Removed unfinished dump: %s", path)


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)
    reason = sys.argv[1] if len(sys.argv) > 1 else "manual"
    reuse = get_settings().backup_daily_reuse_minutes if reason == "daily" else None
    written = run_backup(reason, reuse_minutes=reuse)
    print(f"Backup: {written}")
