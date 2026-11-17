"""Fixtures of the tracking service tests."""
import os
from collections.abc import AsyncGenerator, Generator

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from mail_tracking.app.tracking.app import app as tracking_app

os.environ.setdefault("TESTING", "true")
os.environ.setdefault("JWT_SECRET", "test-secret-key-for-testing-only")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
# Redis DB 15 for tests
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")


@pytest.fixture
async def tracking_client() -> AsyncGenerator[AsyncClient, None]:
    """Asynchronous HTTP client for the tracking service."""
    async with AsyncClient(
        transport=ASGITransport(app=tracking_app),
        base_url="http://test",
        timeout=30.0,
    ) as client:
        yield client


@pytest.fixture
def tracking_test_client() -> Generator[TestClient, None, None]:
    """Synchronous HTTP client for the tracking service (simple tests)."""
    with TestClient(app=tracking_app) as client:
        yield client
