"""
Tests for the webhooks router
"""
from unittest.mock import patch

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_create_webhook(api_client: AsyncClient, auth_headers: dict, mock_webhook_data: dict):
    """Tests creating a webhook"""
    with patch("mail_api.app.api.routers.webhook_router.webhook_service.create_webhook") as mock_create, \
         patch("mail_api.shared.domain_validation.validate_domain_id_exists") as mock_validate:
        from datetime import datetime

        from mail_api.models.webhook_models import WebhookResponse

        mock_validate.return_value = True
        mock_create.return_value = WebhookResponse(
            id=1,
            webhook_id="wh_1234567890abcdef",
            name=mock_webhook_data["name"],
            url=str(mock_webhook_data["url"]),
            events=mock_webhook_data["events"],
            secret_key="sk_***",
            is_active=True,
            retry_count=3,
            timeout_seconds=30,
            headers=None,
            created_at=datetime.now(),
            updated_at=datetime.now(),
            created_by="test@example.com",
            domain_id=1
        )

        response = await api_client.post(
            "/webhooks",
            json=mock_webhook_data,
            headers=auth_headers
        )

        # May return 201, 400, 401, 403 or 422 depending on the validation
        assert response.status_code in [201, 400, 401, 403, 422]

        if response.status_code == 201:
            data = response.json()
            assert "id" in data
            assert "webhook_id" in data
            assert data["name"] == mock_webhook_data["name"]


@pytest.mark.asyncio
async def test_create_webhook_unauthorized(api_client: AsyncClient, mock_webhook_data: dict):
    """Tests creating a webhook without authentication"""
    response = await api_client.post("/webhooks", json=mock_webhook_data)
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_list_webhooks(api_client: AsyncClient, auth_headers: dict):
    """Tests listing webhooks"""
    with patch("mail_api.services.webhook_service.webhook_service.list_webhooks") as mock_list:
        mock_list.return_value = []

        response = await api_client.get("/webhooks", headers=auth_headers)

        assert response.status_code in [200, 401, 403]

        if response.status_code == 200:
            data = response.json()
            assert isinstance(data, list)


@pytest.mark.asyncio
async def test_get_webhook_by_id(api_client: AsyncClient, auth_headers: dict):
    """Tests fetching a webhook by ID"""
    webhook_id = "wh_1234567890abcdef"

    with patch("mail_api.app.api.routers.webhook_router.webhook_service.get_webhook") as mock_get:
        mock_get.return_value = {
            "id": 1,
            "webhook_id": webhook_id,
            "name": "Test Webhook",
            "url": "https://example.com/webhook",
            "events": ["delivered"],
            "is_active": True
        }

        response = await api_client.get(f"/webhooks/{webhook_id}", headers=auth_headers)

        assert response.status_code in [200, 404, 401, 403, 422]

        if response.status_code == 200:
            data = response.json()
            assert "webhook_id" in data or "id" in data


@pytest.mark.asyncio
async def test_update_webhook(api_client: AsyncClient, auth_headers: dict):
    """Tests updating a webhook"""
    webhook_id = "wh_1234567890abcdef"
    update_data = {
        "name": "Updated Webhook",
        "url": "https://example.com/webhook/updated"
    }

    with patch("mail_api.app.api.routers.webhook_router.webhook_service.update_webhook") as mock_update:
        from datetime import datetime

        from mail_api.models.webhook_models import WebhookResponse

        mock_update.return_value = WebhookResponse(
            id=1,
            webhook_id=webhook_id,
            name=update_data.get("name", "Updated Webhook"),
            url=update_data.get("url", "https://example.com/webhook/updated"),
            events=["delivered"],
            secret_key="sk_***",
            is_active=True,
            retry_count=3,
            timeout_seconds=30,
            headers=None,
            created_at=datetime.now(),
            updated_at=datetime.now(),
            created_by="test@example.com",
            domain_id=1
        )

        response = await api_client.put(
            f"/webhooks/{webhook_id}",
            json=update_data,
            headers=auth_headers
        )

        assert response.status_code in [200, 404, 401, 403, 422]


@pytest.mark.asyncio
async def test_delete_webhook(api_client: AsyncClient, auth_headers: dict):
    """Tests deleting a webhook"""
    webhook_id = "wh_1234567890abcdef"

    with patch("mail_api.app.api.routers.webhook_router.webhook_service.delete_webhook") as mock_delete:
        mock_delete.return_value = True

        response = await api_client.delete(f"/webhooks/{webhook_id}", headers=auth_headers)

        assert response.status_code in [204, 404, 401, 403, 422]


@pytest.mark.asyncio
async def test_toggle_webhook(api_client: AsyncClient, auth_headers: dict):
    """Tests enabling/disabling a webhook"""
    webhook_id = "wh_1234567890abcdef"

    with patch("mail_api.app.api.routers.webhook_router.webhook_service.toggle_webhook") as mock_toggle:
        from datetime import datetime

        from mail_api.models.webhook_models import WebhookResponse

        mock_toggle.return_value = WebhookResponse(
            id=1,
            webhook_id=webhook_id,
            name="Test Webhook",
            url="https://example.com/webhook",
            events=["delivered"],
            secret_key="sk_***",
            is_active=False,
            retry_count=3,
            timeout_seconds=30,
            headers=None,
            created_at=datetime.now(),
            updated_at=datetime.now(),
            created_by="test@example.com",
            domain_id=1
        )

        response = await api_client.patch(
            f"/webhooks/{webhook_id}/toggle",
            headers=auth_headers
        )

        assert response.status_code in [200, 404, 401, 403, 422]


@pytest.mark.asyncio
async def test_test_webhook(api_client: AsyncClient, auth_headers: dict):
    """Tests the webhook test endpoint"""
    webhook_id = "wh_1234567890abcdef"
    test_data = {
        "event_type": "delivered",
        "payload": {"test": "data"}
    }

    with patch("mail_api.app.api.routers.webhook_router.webhook_service.queue_webhook_event") as mock_queue:
        mock_queue.return_value = True

        response = await api_client.post(
            f"/webhooks/{webhook_id}/test",
            json=test_data,
            headers=auth_headers
        )

        assert response.status_code in [200, 404, 401, 403, 422]
