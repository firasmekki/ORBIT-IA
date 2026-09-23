"""pg_dump-based backups: one taken automatically right before every schema
migration (app.core.migrations.run_privileged_migration) and one taken
daily by the standalone `backup` service in docker-compose.yml (via
`python -m app.core.backup daily`) - both go through `run_backup()`, so
there is exactly one code path to trust, not a Python path and a separate
shell script that can drift apart.

Restore is deliberately NOT automated here - see README's "Sauvegardes et
restauration" section for the pg_restore command. A restore overwrites the
live database; it should never be one accidental function call away,
especially in a module that also runs unattended on a schedule.
"""

import logging
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from app.core.config import get_settings

logger = logging.getLogger("orbitia.backup")


def _connection_parts():
    settings = get_settings()
    # urlparse doesn't know the "+psycopg" dialect suffix SQLAlchemy uses.
    url = urlparse(settings.migration_database_url.replace("postgresql+psycopg://", "postgresql://"))
    return url, settings


def run_backup(reason: str) -> Path:
    """Runs `pg_dump` against the privileged (migration_database_url)
    connection - never the app's own low-privilege role, which shouldn't
    need pg_dump's read-everything access as a standing grant. Returns the
    path to the new dump file; raises on failure (the caller decides
    whether that should block whatever triggered the backup)."""
    url, settings = _connection_parts()
    backup_dir = Path(settings.backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    safe_reason = "".join(c if c.isalnum() else "_" for c in reason) or "manual"
    dest = backup_dir / f"orbitia_{safe_reason}_{timestamp}.dump"

    env = {**os.environ, "PGPASSWORD": url.password or ""}
    args = [
        "pg_dump",
        "-h", url.hostname or "localhost",
        "-p", str(url.port or 5432),
        "-U", url.username or "orbitia",
        "-d", (url.path or "/orbitia").lstrip("/"),
        "-Fc",
        "-f", str(dest),
    ]
    result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        dest.unlink(missing_ok=True)
        raise RuntimeError(f"pg_dump exited {result.returncode}: {result.stderr.strip()}")

    logger.info("Backup written: %s (%s)", dest, reason)
    _rotate(backup_dir, settings.backup_retention_days)
    return dest


def _rotate(backup_dir: Path, keep_days: int) -> None:
    cutoff = time.time() - keep_days * 86400
    for path in backup_dir.glob("orbitia_*.dump"):
        if path.stat().st_mtime < cutoff:
            path.unlink()
            logger.info("Rotated out backup older than %d day(s): %s", keep_days, path)


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)
    reason = sys.argv[1] if len(sys.argv) > 1 else "manual"
    written = run_backup(reason)
    print(f"Backup written: {written}")
