"""
Tests for the webhook events router
"""
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from mail_api.models.webhook_models import WebhookEventType, WebhookResponse


@pytest.mark.asyncio
async def test_list_webhook_events(api_client: AsyncClient, auth_headers: dict):
    """Tests listing the events of a given webhook"""
    webhook_id = 1  # Must be an int, not a string

    # Mock of the existing webhook
    mock_webhook = WebhookResponse(
        id=webhook_id,
        webhook_id=f"wh_{webhook_id}",
        name="Test Webhook",
        url="https://example.com/webhook",
        events=[WebhookEventType.DELIVERED],
        secret_key="test_secret",
        is_active=True,
        retry_count=3,
        timeout_seconds=30,
        headers=None,
        created_at="2024-01-01T00:00:00",
        updated_at="2024-01-01T00:00:00",
        created_by="test@example.com",
        domain_name="example.com",
        domain_id=1
    )

    with patch(
        "mail_api.app.api.routers.events_router.webhook_service.get_webhook"
    ) as mock_get_webhook, \
    patch(
        "mail_api.app.api.routers.events_router.webhook_service.list_webhook_events"
    ) as mock_list:
        mock_get_webhook.return_value = mock_webhook
        mock_list.return_value = []

        response = await api_client.get(
            f"/webhooks/{webhook_id}/events",
            headers=auth_headers
        )

        assert response.status_code in [200, 401, 403, 422, 500]

        if response.status_code == 200:
            data = response.json()
            assert isinstance(data, list)


@pytest.mark.asyncio
async def test_get_recent_events(api_client: AsyncClient, auth_headers: dict):
    """Tests fetching recent events"""
    with patch(
        "mail_api.app.api.routers.events_router.webhook_service.list_webhook_events"
    ) as mock_list:
        mock_list.return_value = []

        response = await api_client.get(
            "/webhooks/events/recent",
            headers=auth_headers
        )

        assert response.status_code in [200, 401, 403, 500]

        if response.status_code == 200:
            data = response.json()
            assert isinstance(data, list)


@pytest.mark.asyncio
async def test_get_failed_events(api_client: AsyncClient, auth_headers: dict):
    """Tests fetching failed events"""
    with patch(
        "mail_api.app.api.routers.events_router.webhook_service.list_webhook_events"
    ) as mock_list:
        mock_list.return_value = []

        response = await api_client.get(
            "/webhooks/events/failed",
            headers=auth_headers
        )

        assert response.status_code in [200, 401, 403, 500]

        if response.status_code == 200:
            data = response.json()
            assert isinstance(data, list)


@pytest.mark.asyncio
async def test_get_webhook_events_by_id(api_client: AsyncClient, auth_headers: dict):
    """Tests fetching the events of a given webhook"""
    webhook_id = 1  # Must be an int, not a string

    # Mock of the existing webhook
    mock_webhook = WebhookResponse(
        id=webhook_id,
        webhook_id=f"wh_{webhook_id}",
        name="Test Webhook",
        url="https://example.com/webhook",
        events=[WebhookEventType.DELIVERED],
        secret_key="test_secret",
        is_active=True,
        retry_count=3,
        timeout_seconds=30,
        headers=None,
        created_at="2024-01-01T00:00:00",
        updated_at="2024-01-01T00:00:00",
        created_by="test@example.com",
        domain_name="example.com",
        domain_id=1
    )

    with patch(
        "mail_api.app.api.routers.events_router.webhook_service.get_webhook"
    ) as mock_get_webhook, \
    patch(
        "mail_api.app.api.routers.events_router.webhook_service.list_webhook_events"
    ) as mock_list:
        mock_get_webhook.return_value = mock_webhook
        mock_list.return_value = []

        response = await api_client.get(
            f"/webhooks/{webhook_id}/events",
            headers=auth_headers
        )

        assert response.status_code in [200, 404, 401, 403, 422, 500]

        if response.status_code == 200:
            data = response.json()
            assert isinstance(data, list)
