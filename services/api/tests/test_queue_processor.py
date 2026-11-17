"""
Test script for queue_processor.py using real connections

This script tests the queue processor against real Redis and database connections.
It reads straight from Redis, fetches the keys and runs the processing.

Usage:
    python test_queue_processor.py [--queue-key KEY] [--read-key KEY] [--max-emails N]

    --queue-key: Specific Redis key to read (e.g. e:domain:message_id)
    --read-key: Specific Redis key to read (e.g. e:domain:message_id)
    --max-emails: Maximum number of emails to process (default: 10)
    --process-only: Only process emails from the queue, without reading specific keys
    --read-only: Only read emails from the queue, without processing or removing them
"""

import argparse
import asyncio
import json
import logging
import sys

# Logging setup
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)

logger = logging.getLogger(__name__)

# Project dependencies
from mail_api.common.config import EMAIL_QUEUE_PREFIX
from mail_api.common.db import fetch_one_async
from mail_api.common.redis_client import redis_client
from mail_api.workers.email_processor import EmailProcessor


async def test_redis_connection():
    """Tests the Redis connection"""
    try:
        logger.info("Testing the Redis connection...")
        await redis_client.ping()
        logger.info("✅ Redis connection established")
        return True
    except Exception as e:
        logger.error(f"❌ Error connecting to Redis: {e}")
        return False


async def test_db_connection():
    """Tests the database connection"""
    try:
        logger.info("Testing the database connection...")
        result = await fetch_one_async("SELECT 1 as test")
        if result and result.get("test") == 1:
            logger.info("✅ Database connection established")
            return True
        else:
            logger.error("❌ Unexpected response from the database")
            return False
    except Exception as e:
        logger.error(f"❌ Error connecting to the database: {e}")
        return False


async def read_redis_key(key: str) -> str | None:
    """Reads a Redis key"""
    try:
        logger.info(f"Reading Redis key: {key}")
        value = await redis_client.get(key)
        if value:
            logger.info(f"✅ Key found: {key} (size: {len(value)} bytes)")
            return value
        else:
            logger.warning(f"⚠️ Key not found: {key}")
            return None
    except Exception as e:
        logger.error(f"❌ Error reading key {key}: {e}")
        return None


async def list_redis_keys(pattern: str) -> list:
    """Lists the Redis keys that match the pattern"""
    try:
        logger.info(f"Looking for keys with pattern: {pattern}")
        keys = await redis_client.scan_keys(pattern)
        logger.info(f"✅ Found {len(keys)} key(s)")
        for key in keys[:10]:  # Show only the first 10
            logger.info(f"  - {key}")
        if len(keys) > 10:
            logger.info(f"  ... and {len(keys) - 10} more key(s)")
        return keys
    except Exception as e:
        logger.error(f"❌ Error looking for keys: {e}")
        return []


async def get_all_email_keys():
    """Fetches every email key available in Redis
    Each key holds one email that needs to be processed"""
    try:
        pattern = f"{EMAIL_QUEUE_PREFIX}:*:*"
        all_keys = await redis_client.scan_keys(pattern)

        if not all_keys:
            logger.debug(f"No key found with pattern {pattern}")
            return []

        logger.info(f"Found {len(all_keys)} email key(s)")
        return all_keys
    except Exception as e:
        logger.error(f"Error looking for email keys: {e}")
        return []


async def get_total_email_keys(email_keys: list) -> int:
    """Returns the total number of email keys"""
    return len(email_keys)


async def read_from_all_keys(
    email_keys: list, max_emails: int = 10, remove: bool = True
) -> list:
    """
    Reads emails from every key, the same way the queue processor does

    Args:
        email_keys: List of email keys (each key holds one JSON)
        max_emails: Maximum number of emails to read
        remove: If True, removes the keys after processing. If False, only reads them
    """
    emails = []
    try:
        logger.info(
            f"Reading up to {max_emails} email(s) from {len(email_keys)} key(s) (remove={remove})"
        )

        # Process up to max_emails keys
        keys_to_process = email_keys[:max_emails]

        for i, key in enumerate(keys_to_process):
            try:
                # Read the key's content (it is not a list, it is a key holding JSON)
                email_json = await redis_client.get(key)

                if not email_json:
                    logger.debug(f"Key '{key}' does not exist or is empty, skipping")
                    continue

                try:
                    email_dict = json.loads(email_json)
                    emails.append(email_dict)
                    message_id = email_dict.get("message_id", "unknown")
                    domain_name = email_dict.get("domain_name", "unknown")
                    logger.info(
                        f"✅ Email {i+1} read from key '{key}': message_id={message_id}, domain_name={domain_name}"
                    )

                    if remove:
                        # Remove the key after reading it
                        await redis_client.delete(key)
                        logger.debug(f"Key '{key}' removed")

                except json.JSONDecodeError as e:
                    logger.error(f"❌ Error decoding the JSON of key '{key}': {e}")
                    logger.debug(f"Content: {email_json[:200]}")
                    if remove:
                        # Remove the invalid key
                        await redis_client.delete(key)

            except Exception as e:
                logger.error(f"❌ Error reading key '{key}': {e}")

        logger.info(f"Total emails read: {len(emails)}")
        return emails

    except Exception as e:
        logger.error(f"❌ Error reading from the keys: {e}")
        return emails


async def process_emails_with_processor(emails: list):
    """Processes emails with the queue processor"""
    try:
        logger.info(
            f"Starting the queue processor to process {len(emails)} email(s)..."
        )

        # Processor instance
        processor = EmailProcessor()

        # Process the emails one by one
        processed = 0
        successful = 0
        failed = 0

        for idx, email_dict in enumerate(emails, 1):
            try:
                message_id = email_dict.get("message_id", "unknown")
                domain_name = email_dict.get("domain_name", "unknown")
                logger.info(
                    f"\n[{idx}/{len(emails)}] Processing email: message_id={message_id}, domain_name={domain_name}"
                )

                # Validate the basic structure
                if not isinstance(email_dict, dict):
                    logger.error(f"  ❌ Email is not a dictionary: {type(email_dict)}")
                    failed += 1
                    processed += 1
                    continue

                if not message_id or not domain_name:
                    logger.error(
                        f"  ❌ Email is missing required fields: message_id={message_id}, domain_name={domain_name}"
                    )
                    failed += 1
                    processed += 1
                    continue

                # Process the email with the processor's method
                try:
                    await processor._process_single_email(email_dict)
                    successful += 1
                    logger.info("  ✅ Email processed")
                except Exception as process_error:
                    logger.error(
                        f"  ❌ Error processing the email: {process_error}",
                        exc_info=True,
                    )
                    failed += 1

                processed += 1

            except Exception as e:
                logger.error(f"  ❌ Error processing the email: {e}", exc_info=True)
                failed += 1
                processed += 1

        logger.info(
            f"\nProcessing done: {processed} processed, {successful} succeeded, {failed} failed"
        )
        return {"processed": processed, "successful": successful, "failed": failed}

    except Exception as e:
        logger.error(f"❌ Error processing the emails: {e}", exc_info=True)
        return {"processed": 0, "successful": 0, "failed": 0}


async def test_queue_processor(
    queue_key: str | None = None,
    read_key: str | None = None,
    max_emails: int = 10,
    process_only: bool = False,
    read_only: bool = False,
):
    """Main test function"""
    logger.info("=" * 80)
    logger.info("QUEUE PROCESSOR TEST")
    logger.info("=" * 80)

    # Test the connections
    logger.info("\n1. Testing the connections...")
    redis_ok = await test_redis_connection()
    db_ok = await test_db_connection()

    if not redis_ok or not db_ok:
        logger.error("❌ Connection failure. Aborting the test.")
        return

    # Fetch every available email key
    logger.info("\n2. Looking for available email keys...")
    all_keys = await get_all_email_keys()

    if not all_keys:
        logger.warning("⚠️ No email key found")
        if queue_key:
            logger.info(f"Using the given key: {queue_key}")
            all_keys = [queue_key]
        else:
            logger.error("❌ No key available. Aborting the test.")
            return
    else:
        logger.info(f"✅ Found {len(all_keys)} email key(s)")

    # Check the total number of keys
    logger.info("\n3. Checking the number of keys...")
    total_keys = await get_total_email_keys(all_keys)
    logger.info(f"Total keys: {total_keys}")

    # Read the specific key, if given
    if read_key and not process_only:
        logger.info(f"\n4. Reading the given key: {read_key}")
        value = await read_redis_key(read_key)
        if value:
            try:
                data = json.loads(value)
                logger.info(
                    f"Key data: {json.dumps(data, indent=2, default=str)}"
                )
            except json.JSONDecodeError:
                logger.info(f"Content (not JSON): {value[:500]}")

    # Read the emails from the keys
    if total_keys > 0 or process_only or read_only:
        logger.info("\n5. Reading the emails from the keys...")
        # With read_only, keep the keys. Otherwise remove them (as the processor does)
        emails = await read_from_all_keys(
            all_keys, max_emails=max_emails, remove=not read_only
        )

        if emails and not read_only:
            logger.info(
                f"\n6. Processing {len(emails)} email(s) with the queue processor..."
            )
            results = await process_emails_with_processor(emails)

            logger.info("\n" + "=" * 80)
            logger.info("TEST RESULT")
            logger.info("=" * 80)
            logger.info(f"Emails processed: {results['processed']}")
            logger.info(f"Succeeded: {results['successful']}")
            logger.info(f"Failed: {results['failed']}")
        elif emails and read_only:
            logger.info(f"\n✅ {len(emails)} email(s) read (keys not removed)")
            logger.info("\nFirst email (sample):")
            if emails:
                logger.info(json.dumps(emails[0], indent=2, default=str))
        else:
            logger.info("\n⚠️ No email found in the keys to process")
    else:
        logger.info("\n⚠️ No email key found. Nothing to process.")

    logger.info("\n" + "=" * 80)
    logger.info("TEST DONE")
    logger.info("=" * 80)


def main():
    """Main function"""
    parser = argparse.ArgumentParser(
        description="Tests queue_processor.py against real Redis and database connections"
    )
    parser.add_argument(
        "--queue-key",
        type=str,
        default=None,
        help="Specific Redis key to read (optional, default: every key matching e:*:*)",
    )
    parser.add_argument(
        "--read-key",
        type=str,
        default=None,
        help="Specific Redis key to read (e.g. e:domain:message_id)",
    )
    parser.add_argument(
        "--max-emails",
        type=int,
        default=10,
        help="Maximum number of emails to process (default: 10)",
    )
    parser.add_argument(
        "--process-only",
        action="store_true",
        help="Only process emails from the queue, without reading specific keys",
    )
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="Only read emails from the queue, without processing or removing them",
    )

    args = parser.parse_args()

    try:
        asyncio.run(
            test_queue_processor(
                queue_key=args.queue_key,
                read_key=args.read_key,
                max_emails=args.max_emails,
                process_only=args.process_only,
                read_only=args.read_only,
            )
        )
    except KeyboardInterrupt:
        logger.info("\n⚠️ Test interrupted by the user")
    except Exception as e:
        logger.error(f"\n❌ Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
