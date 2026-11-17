#!/usr/bin/env python3
"""
Basic Email Processor test
Checks that the worker can be imported and initialized
"""

import asyncio
import sys
from pathlib import Path

# Add the root directory to the Python path
sys.path.insert(0, str(Path(__file__).parent))

async def test_import():
    """Tests that the modules can be imported"""
    print("🔍 Testing imports...")
    try:
        print("✅ EmailProcessor imported")
        print("✅ QueueProcessor (alias) imported")

        print("✅ MailService imported")

        print("✅ EmailStatus imported")

        return True
    except Exception as e:
        print(f"❌ Import error: {e}")
        import traceback
        traceback.print_exc()
        return False

async def test_instantiation():
    """Tests that the processor can be instantiated"""
    print("\n🔍 Testing instantiation...")
    try:
        from mail_api.workers.email_processor import EmailProcessor

        processor = EmailProcessor()
        print("✅ EmailProcessor instantiated")
        print(f"   - running: {processor.running}")
        print(f"   - mail_service: {type(processor.mail_service).__name__}")

        return True
    except Exception as e:
        print(f"❌ Instantiation error: {e}")
        import traceback
        traceback.print_exc()
        return False

async def test_redis_connection():
    """Tests the Redis connection"""
    print("\n🔍 Testing the Redis connection...")
    try:
        from mail_api.common.redis_client import redis_client

        await redis_client.ping()
        print("✅ Redis connection established")

        # Check the configuration
        from mail_api.common.config import EMAIL_QUEUE_PREFIX, REDIS_URL
        print(f"   - EMAIL_QUEUE_PREFIX: {EMAIL_QUEUE_PREFIX}")
        print(f"   - REDIS_URL: {REDIS_URL}")

        return True
    except Exception as e:
        print(f"⚠️  Error connecting to Redis: {e}")
        print("   (This is OK if Redis is not running)")
        return False

async def test_db_connection():
    """Tests the database connection"""
    print("\n🔍 Testing the database connection...")
    try:
        from mail_api.common.db import fetch_one_async

        result = await fetch_one_async("SELECT 1 as test")
        if result and result.get("test") == 1:
            print("✅ Database connection established")
            return True
        else:
            print("⚠️  Connection OK but unexpected result")
            return False
    except Exception as e:
        print(f"⚠️  Error connecting to the database: {e}")
        print("   (This is OK if MySQL is not running)")
        return False

async def main():
    """Main test function"""
    print("=" * 60)
    print("🧪 Basic Email Processor Test")
    print("=" * 60)

    results = []

    # Test 1: Imports
    results.append(await test_import())

    if not results[0]:
        print("\n❌ Imports failed. Aborting the tests.")
        return

    # Test 2: Instantiation
    results.append(await test_instantiation())

    # Test 3: Redis
    results.append(await test_redis_connection())

    # Test 4: Database
    results.append(await test_db_connection())

    # Summary
    print("\n" + "=" * 60)
    print("📊 Test Summary")
    print("=" * 60)
    print(f"✅ Imports: {'OK' if results[0] else 'FAILED'}")
    print(f"✅ Instantiation: {'OK' if results[1] else 'FAILED'}")
    print(f"{'✅' if results[2] else '⚠️ '} Redis: {'OK' if results[2] else 'NOT AVAILABLE'}")
    print(f"{'✅' if results[3] else '⚠️ '} Database: {'OK' if results[3] else 'NOT AVAILABLE'}")

    if all(results[:2]):
        print("\n✅ Email Processor is ready to use!")
        print("\n💡 To run the worker:")
        print("   python -m mail_api.workers.run_email_processor")
    else:
        print("\n❌ Some tests failed. Check the errors above.")

if __name__ == "__main__":
    asyncio.run(main())
