"""
Shared fixtures for the milter service tests.

Provides:
- database mock
- Redis mock
- email/SMTP mocks
"""
import asyncio
from datetime import datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

# =============================================================================
# DATABASE FIXTURES
# =============================================================================

@pytest.fixture
def mock_db():
    """Mock of the database connection."""
    db = MagicMock()
    db.execute = MagicMock()
    db.fetchone = MagicMock(return_value=None)
    db.fetchall = MagicMock(return_value=[])
    db.commit = MagicMock()
    db.rollback = MagicMock()
    return db


# =============================================================================
# REDIS FIXTURES
# =============================================================================

@pytest.fixture
def mock_redis():
    """Mock of the Redis client."""
    redis = MagicMock()
    redis.get = MagicMock(return_value=None)
    redis.set = MagicMock(return_value=True)
    redis.setex = MagicMock(return_value=True)
    redis.delete = MagicMock(return_value=True)
    redis.exists = MagicMock(return_value=False)
    return redis


# =============================================================================
# EMAIL/MILTER FIXTURES
# =============================================================================

@pytest.fixture
def sample_email_headers() -> dict[str, str]:
    """Email headers for tests."""
    return {
        "From": "sender@test.com",
        "To": "recipient@test.com",
        "Subject": "Test Email",
        "Message-ID": "<test123@test.com>",
        "Date": datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000"),
        "Content-Type": "text/html; charset=utf-8"
    }


@pytest.fixture
def sample_email_body() -> str:
    """HTML email body for tests."""
    return """
    <html>
    <body>
        <h1>Test Email</h1>
        <p>This is a test email with a <a href="https://example.com/link">link</a>.</p>
        <p>Click <a href="https://example.com/cta">here</a> for more info.</p>
    </body>
    </html>
    """


@pytest.fixture
def mock_milter_context():
    """Mock of the milter context."""
    ctx = MagicMock()
    ctx.getpriv = MagicMock(return_value={})
    ctx.setpriv = MagicMock()
    ctx.getsymval = MagicMock(return_value=None)
    ctx.setreply = MagicMock()
    ctx.addheader = MagicMock()
    ctx.chgheader = MagicMock()
    ctx.replacebody = MagicMock()
    return ctx


# =============================================================================
# TRACKING TOKEN FIXTURES
# =============================================================================

@pytest.fixture
def sample_tracking_data() -> dict[str, Any]:
    """Tracking data for tests."""
    return {
        "message_id": "msg_test123",
        "customer_id": "cust_456",
        "campaign_id": "camp_789",
        "recipient_email": "recipient@test.com",
        "sender_email": "sender@test.com",
        "created_at": datetime.utcnow().isoformat()
    }


@pytest.fixture
def sample_tracking_token() -> str:
    """Tracking token for tests."""
    # Fake Fernet token, for tests
    return "gAAAAABtest_token_for_testing_purposes_only"


# =============================================================================
# WEBHOOK FIXTURES
# =============================================================================

@pytest.fixture
def sample_webhook_payload() -> dict[str, Any]:
    """Webhook payload for tests."""
    return {
        "event_type": "delivered",
        "message_id": "msg_test123",
        "timestamp": datetime.utcnow().isoformat(),
        "recipient": "recipient@test.com",
        "details": {
            "status_code": 250,
            "response": "OK"
        }
    }


# =============================================================================
# EVENTS UPDATER FIXTURES
# =============================================================================

@pytest.fixture
def sample_postfix_log_line() -> str:
    """Postfix log line for tests."""
    return (
        "Jan 25 10:30:45 mail postfix/smtp[12345]: "
        "ABCDEF123456: to=<recipient@test.com>, relay=mx.test.com[192.0.2.4]:25, "
        "delay=1.5, delays=0.1/0.1/0.5/0.8, dsn=2.0.0, status=sent "
        "(250 2.0.0 OK)"
    )


# =============================================================================
# EVENT LOOP FIXTURES
# =============================================================================

@pytest.fixture(scope="session")
def event_loop():
    """Create event loop for async tests."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()
