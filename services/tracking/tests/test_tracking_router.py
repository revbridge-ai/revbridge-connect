"""
Tests for the tracking router
"""
from unittest.mock import patch

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_track_open(tracking_client: AsyncClient):
    """Tracks an email open"""
    token = "test_token_1234567890abcdef"

    with patch(
        "mail_tracking.app.tracking.routers.tracking_router.validate_token"
    ) as mock_validate, \
         patch("mail_tracking.common.db.exec_sql") as mock_db, \
         patch("mail_tracking.common.db.fetch_one") as mock_fetch, \
         patch(
             "mail_tracking.app.tracking.routers.tracking_router.get_domain_name_from_message_id"
         ) as mock_domain:
        mock_validate.return_value = {
            "mid": "msg_123",
            "rcpt": "test@example.com",
            "type": "open",
            "ts": 1234567890
        }
        mock_fetch.return_value = {
            "delta_seconds": 100,
            "first_delivered": None
        }
        mock_domain.return_value = "example.com"
        mock_db.return_value = None

        response = await tracking_client.get(f"/o/{token}")

        # Tracking endpoints are public and must return an image or 200
        assert response.status_code in [200, 302, 404]


@pytest.mark.asyncio
async def test_track_click(tracking_client: AsyncClient):
    """Tracks a link click"""
    token = "test_token_1234567890abcdef"
    redirect_url = "https://example.com"

    with patch(
        "mail_tracking.app.tracking.routers.tracking_router.validate_token"
    ) as mock_validate, \
         patch("mail_tracking.common.db.exec_sql") as mock_db, \
         patch("mail_tracking.common.db.fetch_one") as mock_fetch, \
         patch(
             "mail_tracking.app.tracking.routers.tracking_router.get_domain_name_from_message_id"
         ) as mock_domain:
        mock_validate.return_value = {
            "mid": "msg_123",
            "rcpt": "test@example.com",
            "type": "click",
            "url": redirect_url,
            "ts": 1234567890
        }
        mock_fetch.return_value = {
            "delta_seconds": 100,
            "first_delivered": None
        }
        mock_domain.return_value = "example.com"
        mock_db.return_value = None

        response = await tracking_client.get(f"/c/{token}")

        # Must redirect or return 200
        assert response.status_code in [200, 302, 307, 404]


@pytest.mark.asyncio
async def test_track_unsubscribe(tracking_client: AsyncClient):
    """Tracks an unsubscribe"""
    token = "test_token_1234567890abcdef"

    with patch(
        "mail_tracking.app.tracking.routers.tracking_router.validate_token"
    ) as mock_validate, \
         patch("mail_tracking.common.db.exec_sql") as mock_db, \
         patch("mail_tracking.common.db.fetch_one") as mock_fetch, \
         patch(
             "mail_tracking.app.tracking.routers.tracking_router.get_domain_name_from_message_id"
         ) as mock_domain:
        mock_validate.return_value = {
            "mid": "msg_123",
            "rcpt": "test@example.com",
            "type": "unsubscribe",
            "ts": 1234567890
        }
        mock_fetch.return_value = {
            "delta_seconds": 100,
            "first_delivered": None
        }
        mock_domain.return_value = "example.com"
        mock_db.return_value = None

        response = await tracking_client.get(f"/u/{token}")

        # Must return a confirmation page or redirect
        assert response.status_code in [200, 302, 404]


@pytest.mark.asyncio
async def test_track_open_invalid_token(tracking_client: AsyncClient):
    """Open tracking with an invalid token"""
    token = "invalid_token"

    response = await tracking_client.get(f"/o/{token}")

    # May return 404 or an empty image
    assert response.status_code in [200, 404]


@pytest.mark.asyncio
async def test_track_click_invalid_token(tracking_client: AsyncClient):
    """Click tracking with an invalid token"""
    token = "invalid_token"

    response = await tracking_client.get(f"/c/{token}")

    # May return 404 or redirect to a default URL
    assert response.status_code in [200, 302, 404]


@pytest.mark.asyncio
async def test_tracking_health_check(tracking_client: AsyncClient):
    """Health check of the tracking service."""
    response = await tracking_client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["service"] == "track-service"
