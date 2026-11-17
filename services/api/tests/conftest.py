"""
Shared test configuration and fixtures
"""
import asyncio
import os
import sys
from collections.abc import AsyncGenerator, Generator
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

# Add the project root directory to the path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# Import the application (after adding it to the path). The TRACKING app lives in
# services/tracking/; its fixtures (tracking_client/tracking_test_client) are in
# services/tracking/tests/conftest.py.
from mail_api.app.api.app import app as api_app

# Environment variables for the tests
os.environ.setdefault("TESTING", "true")
os.environ.setdefault("JWT_SECRET", "test-secret-key-for-testing-only")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
# DB 15 for the tests
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")


@pytest.fixture(scope="session")
def event_loop():
    """Creates an event loop for the whole test session"""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
async def api_client() -> AsyncGenerator[AsyncClient, None]:
    """Async HTTP client for the API Service"""
    async with AsyncClient(
        transport=ASGITransport(app=api_app),
        base_url="http://test",
        timeout=30.0
    ) as client:
        yield client



@pytest.fixture
def api_test_client() -> Generator[TestClient, None, None]:
    """Sync HTTP client for the API Service (for simple tests)"""
    with TestClient(app=api_app) as client:
        yield client



@pytest.fixture
async def auth_token(api_client: AsyncClient) -> str:
    """Fixture that gets a valid JWT for the tests"""
    # Mocked login - adjust it to your implementation
    login_data = {
        "email": "test@example.com",
        "password": "testpassword123"
    }

    try:
        response = await api_client.post("/auth/login", json=login_data)
        if response.status_code == 200:
            return response.json().get("access_token", "")
    except Exception:
        pass

    # If the real login fails, create a mocked token
    from connect_lib.auth.jwt import jwt_manager
    token = jwt_manager.create_access_token({
        "user_id": "test_user_123",
        "email": "test@example.com",
        "username": "test@example.com",
        "role": "admin",
        "domain_id": 1,
        "domain_name": "test.com"
    })
    return token


@pytest.fixture
def auth_headers(auth_token: str) -> dict:
    """Authentication headers for requests"""
    return {"Authorization": f"Bearer {auth_token}"}


@pytest.fixture
async def mock_api_key() -> str:
    """Fixture that creates a mocked API Key"""
    return "test_api_key_1234567890abcdef"


@pytest.fixture
def api_key_headers(mock_api_key: str) -> dict:
    """API Key headers for requests"""
    return {"Authorization": f"Bearer {mock_api_key}"}


@pytest.fixture
async def mock_user():
    """Mocked user data"""
    return {
        "user_id": "test_user_123",
        "email": "test@example.com",
        "username": "test@example.com",
        "role": "admin",
        "domain_id": 1,
        "domain_name": "test.com"
    }


@pytest.fixture
async def mock_webhook_data():
    """Mocked data for creating a webhook"""
    return {
        "name": "Test Webhook",
        "url": "https://example.com/webhook",
        "events": ["delivered", "opened"],
        "secret_key": "test_secret_key",
        "is_active": True,
        "retry_count": 3,
        "timeout_seconds": 30
    }


@pytest.fixture
async def mock_email_data():
    """Mocked data for sending an email"""
    return {
        "to": "recipient@example.com",
        "subject": "Test Email",
        "body": "This is a test email",
        "from_email": "sender@example.com",
        "from_name": "Test Sender"
    }


@pytest.fixture
def mock_redis():
    """Redis mock for the tests"""
    mock = AsyncMock()
    mock.get = AsyncMock(return_value=None)
    mock.set = AsyncMock(return_value=True)
    mock.delete = AsyncMock(return_value=True)
    mock.keys = AsyncMock(return_value=[])
    mock.llen = AsyncMock(return_value=0)
    mock.lrange = AsyncMock(return_value=[])
    mock.brpop = AsyncMock(return_value=None)
    return mock


@pytest.fixture
def mock_db():
    """Database mock for the tests"""
    mock = AsyncMock()
    mock.fetch_one_async = AsyncMock(return_value=None)
    mock.fetch_all_async = AsyncMock(return_value=[])
    mock.execute_async = AsyncMock(return_value=None)
    return mock
