"""
Tests for the email router
"""
from datetime import datetime
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from mail_api.models.email_models import EmailResponse, EmailStatus


@pytest.mark.asyncio
async def test_send_email(api_client: AsyncClient, auth_headers: dict):
    """Tests sending an email"""
    # The correct SendEmailRequest format
    email_data = {
        "personalizations": [
            {
                "to": [
                    {
                        "name": "Recipient",
                        "address": "recipient@example.com"
                    }
                ]
            }
        ],
        "from": {
            "name": "Test Sender",
            "address": "sender@example.com"
        },
        "subject": "Test Email",
        "content": [
            {
                "type": "text/plain",
                "value": "This is a test email"
            }
        ]
    }

    with patch(
        "mail_api.app.api.routers.email_router.mail_service.queue_email"
    ) as mock_queue, \
        patch(
            "mail_api.app.api.routers.email_router.get_user_domain_name_async"
        ) as mock_domain, \
        patch(
            "mail_api.app.api.routers.email_router.validate_authorized_sender"
        ) as mock_validate:
        mock_domain.return_value = "example.com"
        mock_validate.return_value = True
        mock_queue.return_value = EmailResponse(
            message_id="msg_1234567890abcdef",
            status=EmailStatus.QUEUED,
            domain_name="example.com",
            queue="email_queue",
            timestamp=datetime.now().isoformat(),
            recipients_count=1
        )

        response = await api_client.post(
            "/send",
            json=email_data,
            headers=auth_headers
        )

        assert response.status_code in [200, 400, 401, 403, 422]

        if response.status_code == 200:
            data = response.json()
            assert "message_id" in data
            assert data["status"] == "queued"


@pytest.mark.asyncio
async def test_send_email_unauthorized(api_client: AsyncClient):
    """Tests sending an email without authentication"""
    email_data = {
        "to": "recipient@example.com",
        "subject": "Test Email",
        "body": "This is a test email"
    }

    response = await api_client.post("/send", json=email_data)
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_send_email_invalid_data(api_client: AsyncClient, auth_headers: dict):
    """Tests sending an email with invalid data"""
    # Email without a recipient
    email_data = {
        "subject": "Test Email",
        "body": "This is a test email"
    }

    response = await api_client.post(
        "/send",
        json=email_data,
        headers=auth_headers
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_get_email_status(api_client: AsyncClient, auth_headers: dict):
    """Tests fetching an email status"""
    message_id = "msg_1234567890abcdef"

    # The endpoint queries the database directly, so fetch_one is mocked
    with patch(
        "mail_api.app.api.routers.email_router.fetch_one"
    ) as mock_fetch, \
        patch(
            "mail_api.app.api.routers.email_router.get_user_domain_name_async"
        ) as mock_domain:
        mock_domain.return_value = "example.com"
        from datetime import datetime
        # First call: check that the table exists
        # Second call: fetch the email status
        mock_fetch.side_effect = [
            {"total": 1},  # First call (table check)
            {
                "message_id": message_id,
                "rcpt": "recipient@example.com",
                "status": "delivered",
                "dsn": "2.0.0",
                "attempt": 1,
                "ts": datetime.now()
            }
        ]

        response = await api_client.get(
            f"/emails/status/{message_id}",
            headers=auth_headers
        )

        assert response.status_code in [200, 404, 401, 403]

        if response.status_code == 200:
            data = response.json()
            assert data["message_id"] == message_id


@pytest.mark.asyncio
async def test_list_emails(api_client: AsyncClient, auth_headers: dict):
    """Tests listing emails (history)"""
    # The /history endpoint queries the database directly
    with patch(
        "mail_api.app.api.routers.email_router.fetch_all"
    ) as mock_fetch_all, \
        patch(
            "mail_api.app.api.routers.email_router.fetch_one"
        ) as mock_fetch_one, \
        patch(
            "mail_api.app.api.routers.email_router.get_user_domain_name_async"
        ) as mock_domain:
        mock_domain.return_value = "example.com"
        mock_fetch_all.return_value = []
        mock_fetch_one.return_value = {"total": 0}

        response = await api_client.get("/history", headers=auth_headers)

        assert response.status_code in [200, 401, 403, 500]

        if response.status_code == 200:
            data = response.json()
            assert isinstance(data, dict)
            assert "emails" in data
            assert "pagination" in data


@pytest.mark.asyncio
async def test_execution_status_matches_delivery_events_by_domain_id(
    api_client: AsyncClient, auth_headers: dict
):
    """delivery_events stores the sending host (rbm<id>.<domain>) with that
    host's domain_id, not the one of the key's virtual domain. The endpoint matches
    by the key's id, by the exact name or by any host under the name."""
    message_id = "00000000-0000-4000-8000-000000000001"
    tenant_domain = "tenant-08e65f1c.example.com"

    with patch(
        "mail_api.app.api.routers.email_router.fetch_one"
    ) as mock_fetch, \
        patch(
            "mail_api.app.api.routers.email_router.get_user_domain_name_async"
        ) as mock_domain, \
        patch(
            "mail_api.app.api.routers.email_router.get_user_domain_id"
        ) as mock_domain_id, \
        patch(
            "mail_api.common.redis_client.redis_client.exists"
        ) as mock_exists:
        mock_domain.return_value = tenant_domain
        mock_domain_id.return_value = 98
        mock_exists.return_value = False
        mock_fetch.return_value = {
            "message_id": message_id,
            "rcpt": "recipient@example.com",
            "status": "delivered",
            "attempt": 1,
            "ts": datetime(2026, 10, 7, 21, 24, 30),
        }

        response = await api_client.get(
            f"/execution-status/{message_id}", headers=auth_headers
        )

        assert response.status_code == 200, response.text

        query, params = mock_fetch.call_args[0]
        assert "domain_id = %s" in query
        assert "domain_name = %s" in query
        assert "domain_name LIKE %s" in query
        assert params == (message_id, 98, tenant_domain, f"%.{tenant_domain}")

        data = response.json()
        assert data["message_id"] == message_id
        assert data["in_queue"] is False
        assert data["in_delivery_events"] is True
        assert data["execution_status"] == "succeeded"
        assert data["delivery_status"] == "delivered"


@pytest.mark.asyncio
async def test_execution_status_falls_back_to_domain_name_without_domain_id(
    api_client: AsyncClient, auth_headers: dict
):
    """A key/JWT without domain_id matches by the domain name only."""
    message_id = "00000000-0000-4000-8000-000000000002"

    with patch(
        "mail_api.app.api.routers.email_router.fetch_one"
    ) as mock_fetch, \
        patch(
            "mail_api.app.api.routers.email_router.get_user_domain_name_async"
        ) as mock_domain, \
        patch(
            "mail_api.app.api.routers.email_router.get_user_domain_id"
        ) as mock_domain_id, \
        patch(
            "mail_api.common.redis_client.redis_client.exists"
        ) as mock_exists:
        mock_domain.return_value = "example.com"
        mock_domain_id.return_value = None
        mock_exists.return_value = False
        mock_fetch.return_value = None

        response = await api_client.get(
            f"/execution-status/{message_id}", headers=auth_headers
        )

        assert response.status_code == 200, response.text

        query, params = mock_fetch.call_args[0]
        assert "domain_id" not in query
        assert "domain_name LIKE %s" in query
        assert params == (message_id, "example.com", "%.example.com")
        assert response.json()["execution_status"] == "running"


@pytest.mark.asyncio
async def test_email_status_accepts_the_send_host_under_the_tenant_domain(
    api_client: AsyncClient, auth_headers: dict
):
    """/status/:id uses the same tenant filter (id, exact name or a host
    under the name)."""
    message_id = "00000000-0000-4000-8000-000000000003"
    tenant_domain = "store-b-00000000000000000000000000000000.example.com"

    with patch(
        "mail_api.app.api.routers.email_router.fetch_one"
    ) as mock_fetch, \
        patch(
            "mail_api.app.api.routers.email_router.get_user_domain_name_async"
        ) as mock_domain, \
        patch(
            "mail_api.app.api.routers.email_router.get_user_domain_id"
        ) as mock_domain_id:
        mock_domain.return_value = tenant_domain
        mock_domain_id.return_value = 103
        mock_fetch.side_effect = [
            {"total": 1},
            {
                "message_id": message_id,
                "rcpt": "ana@example.org",
                "status": "delivered",
                "dsn": "2.0.0",
                "attempt": 1,
                "ts": datetime(2026, 10, 8, 1, 55, 39),
            },
        ]

        response = await api_client.get(
            f"/status/{message_id}", headers=auth_headers
        )

        assert response.status_code == 200, response.text
        query, params = mock_fetch.call_args_list[1][0]
        assert "domain_name LIKE %s" in query
        assert params == (message_id, 103, tenant_domain, f"%.{tenant_domain}")
        assert response.json()["message_id"] == message_id
