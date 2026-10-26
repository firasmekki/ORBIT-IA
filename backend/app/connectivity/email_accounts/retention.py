"""Retention of email results kept in the chat history.

A chat answer built from a mailbox stores its cards (facts + local AI
summary, never a full body) in messages.email_results, with an expiry date
(EMAIL_RESULTS_RETENTION_DAYS). Once expired, the cards and the answer text
are replaced by a short marker; the conversation itself is untouched.
Expired OAuth states are dropped at the same time.
"""

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.database import SessionLocal

logger = logging.getLogger("orbitia.email")
EXPIRED_TEXT = "Résumé d'emails expiré (politique de rétention) : les informations issues de la boîte mail ont été effacées."
INTERVAL_SECONDS = 3600


def purge_expired(db: Session, now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    result = db.execute(
        text(
            "UPDATE messages SET content = :expired_text, "
            "email_results = jsonb_build_object('type', 'email_results', 'expired', true, "
            "  'account', jsonb_build_object('provider', email_results->'account'->>'provider')), "
            "reference_context = NULL "
            "WHERE email_results_expires_at IS NOT NULL AND email_results_expires_at <= :now "
            "AND coalesce(email_results->>'expired', 'false') <> 'true'"
        ),
        {"expired_text": EXPIRED_TEXT, "now": now},
    )
    db.execute(text("DELETE FROM email_oauth_states WHERE expires_at <= :now"), {"now": now})
    db.commit()
    return result.rowcount or 0


async def run_email_retention_worker() -> None:
    while True:
        try:
            def _run():
                with SessionLocal() as db:
                    return purge_expired(db)
            purged = await asyncio.to_thread(_run)
            if purged:
                logger.info("email results expired: %d message(s)", purged)
        except Exception as exc:  # noqa: BLE001 - never kill the app over a purge
            logger.warning("email retention purge failed: %s", type(exc).__name__)
        await asyncio.sleep(INTERVAL_SECONDS)
