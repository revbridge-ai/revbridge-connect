"""
Tests for the API Service health check endpoints
"""
import pytest
from httpx import AsyncClient


# Calls the real /health, which checks DB/Redis — without live infra it answers 503.
# Marked e2e: skipped by default (addopts -m 'not e2e'), run it with `pytest -m e2e`.
@pytest.mark.e2e
@pytest.mark.asyncio
async def test_api_health_check(api_client: AsyncClient):
    """Tests the API health check endpoint"""
    response = await api_client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["service"] == "api-service"
    assert "timestamp" in data

    assert "timestamp" in data
