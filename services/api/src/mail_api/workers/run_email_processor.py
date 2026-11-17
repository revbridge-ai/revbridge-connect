#!/usr/bin/env python3
"""
Runs the email queue processor
"""

import asyncio
import sys
from pathlib import Path

# Add the root directory to the Python path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Configure observability (OTel) - must be before logging
try:
    from connect_lib.observability import setup_observability
    setup_observability(service_name="mail-queue-processor")
except Exception:
    pass

from mail_api.common.config import setup_logging
from mail_api.workers.email_processor import EmailProcessor


async def main():
    """Main function"""
    # Configure logging
    logger = setup_logging("email-queue-processor")

    logger.info("Starting the email queue processor")

    # Create and start the processor
    processor = EmailProcessor()

    try:
        await processor.start()
    except KeyboardInterrupt:
        logger.info("Interrupt signal received")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)
    finally:
        await processor.stop()
        logger.info("Queue processor stopped")


if __name__ == "__main__":
    asyncio.run(main())
