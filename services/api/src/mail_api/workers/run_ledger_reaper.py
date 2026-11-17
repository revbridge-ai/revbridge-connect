#!/usr/bin/env python3
"""
Runs the send_ledger reaper (closes lost sends and expired deferrals).

    python -m mail_api.workers.run_ledger_reaper
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from connect_lib.observability import setup_observability

    setup_observability(service_name="mail-ledger-reaper")
except Exception:
    pass

from mail_api.common.config import setup_logging
from mail_api.workers.ledger_reaper import LedgerReaper


async def main():
    logger = setup_logging("mail-ledger-reaper")
    logger.info("Starting the ledger reaper")
    reaper = LedgerReaper()
    try:
        await reaper.start()
    except KeyboardInterrupt:
        logger.info("Interrupt signal received")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)
    finally:
        await reaper.stop()
        logger.info("Ledger reaper stopped")


if __name__ == "__main__":
    asyncio.run(main())
