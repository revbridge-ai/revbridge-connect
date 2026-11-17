"""send_ledger reaper: closes the durability loop.

Closes what the ledger says was LOST or whose backoff EXPIRED:

  sweep_lost: 'smtp_accepted' whose delivery_deadline expired without ANY delivery
      event (the MTA lost its queue) and 'sending' with an expired lease (the
      worker died before the 250).
  sweep_due : 'deferred' with an expired next_attempt_at (backoff done).

Connect keeps no copy of a message to resend it, so such a row is closed: as
delivered when `deliveries` confirms the delivery (the events updater's
reconciliation was late), otherwise as deadletter with a marker the panel and the
API show (the client can resend to those recipients).

Guards:
  - attempt >= max_attempts → deadletter;
  - everything is best-effort: an error on one row does not stop the cycle.

Runs in the ledger-reaper process (python -m mail_api.workers.run_ledger_reaper).
"""

import asyncio
import json
import logging

from mail_api.common.config import (
    EMAIL_QUEUE_KEY,
    LEDGER_REAPER_BATCH,
    LEDGER_REAPER_INTERVAL_SEC,
)
from mail_api.common.redis_client import redis_client
from mail_api.services import send_ledger

logger = logging.getLogger(__name__)


class LedgerReaper:
    """Sweeps the ledger and closes lost rows and expired deferrals."""

    def __init__(self):
        self.running = False

    async def start(self):
        self.running = True
        logger.info(
            "[LEDGER_REAPER] Starting (interval=%ss, batch=%s)",
            LEDGER_REAPER_INTERVAL_SEC, LEDGER_REAPER_BATCH,
        )
        while self.running:
            try:
                await self._cycle()
            except Exception as e:
                logger.error("[LEDGER_REAPER] Error in the cycle: %s", e, exc_info=True)
            await asyncio.sleep(LEDGER_REAPER_INTERVAL_SEC)

    async def stop(self):
        self.running = False
        logger.info("[LEDGER_REAPER] Stopping")

    async def _cycle(self):
        lost = await send_ledger.sweep_lost(LEDGER_REAPER_BATCH)
        due = await send_ledger.sweep_due(LEDGER_REAPER_BATCH)
        rows = lost + due
        orphans = await self._sweep_queued_orphans()
        if not rows and not orphans:
            return

        counts = {"deadletter": 0, "reconciled": 0, "errors": 0}
        for row in rows:
            try:
                outcome = await self._close(row)
                counts[outcome] = counts.get(outcome, 0) + 1
            except Exception as e:
                counts["errors"] += 1
                logger.warning(
                    "[LEDGER_REAPER] Failed to close %s->%s: %s",
                    row.get("message_id"), row.get("rcpt"), e,
                )

        # log_type=ops: one line per cycle that did work, for monitoring losses.
        logger.info(
            json.dumps({
                "event": "ledger_reaper_cycle",
                "swept_lost": len(lost), "swept_due": len(due),
                "queued_orphans": orphans,
                "deadlettered": counts["deadletter"],
                "reconciled": counts["reconciled"],
                "errors": counts["errors"],
            })
        )

    async def _sweep_queued_orphans(self) -> int:
        """Disposes of orphaned 'queued' rows (the API process was killed in the
        middle of the enqueue, so the item never reached Redis). ONLY runs with the
        queue EMPTY: with a backlog, old queued rows are legitimate and must not be
        touched. Without a payload there is nothing to re-inject: the row is closed
        as delivered (if deliveries confirms it under another identity) or as a
        marked deadletter (counted; the client can resend to the missing ones)."""
        from mail_api.common.config import LEDGER_QUEUED_ORPHAN_GRACE_SEC

        try:
            depth = await redis_client.llen(EMAIL_QUEUE_KEY)
        except Exception:
            return 0
        if depth and int(depth) > 0:
            return 0
        rows = await send_ledger.sweep_stale_queued(
            LEDGER_QUEUED_ORPHAN_GRACE_SEC, LEDGER_REAPER_BATCH
        )
        disposed = 0
        for row in rows:
            try:
                mid, rcpt = row["message_id"], row["rcpt"]
                if await send_ledger.has_confirmed_delivery(mid, rcpt):
                    await send_ledger.mark_delivered(mid, rcpt)
                else:
                    await send_ledger.mark_deadletter(
                        mid, rcpt,
                        "reaper: orphaned queued row (enqueue interrupted; never reached Redis)",
                    )
                disposed += 1
            except Exception as e:
                logger.warning(
                    "[LEDGER_REAPER] Failed to dispose of the orphaned queued row %s->%s: %s",
                    row.get("message_id"), row.get("rcpt"), e,
                )
        return disposed

    async def _close(self, row) -> str:
        message_id = row["message_id"]
        rcpt = row["rcpt"]
        attempt = int(row.get("attempt") or 0)
        max_attempts = int(row.get("max_attempts") or send_ledger.DEFAULT_MAX_ATTEMPTS)

        if attempt >= max_attempts:
            await send_ledger.mark_deadletter(
                message_id, rcpt, f"reaper: out of attempts ({attempt}/{max_attempts})"
            )
            return "deadletter"

        # `deliveries` already confirms the delivery of this (message_id, rcpt): the
        # events updater's reconciliation failed or is late, so close it as delivered.
        if await send_ledger.has_confirmed_delivery(message_id, rcpt):
            await send_ledger.mark_delivered(message_id, rcpt)
            logger.info(
                "[LEDGER_REAPER] %s -> %s already delivered in deliveries; reconciled",
                message_id, rcpt,
            )
            return "reconciled"

        await send_ledger.mark_deadletter(
            message_id, rcpt, "reaper: lost before delivery (no copy kept to resend)"
        )
        return "deadletter"
