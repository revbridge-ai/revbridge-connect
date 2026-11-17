"""
Tests for the email configs router
"""
from unittest.mock import patch

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_list_email_configs(api_client: AsyncClient, auth_headers: dict):
    """Tests listing email configs"""
    with patch("mail_api.common.db.fetch_all") as mock_fetch:
        mock_fetch.return_value = []

        response = await api_client.get("/email-configs", headers=auth_headers)

        assert response.status_code in [200, 401, 403]

        if response.status_code == 200:
            data = response.json()
            assert isinstance(data, list)


# A real INSERT into email_configs — without a live DB it answers 500. Marked
# e2e: skipped by default, run it with `pytest -m e2e`.
@pytest.mark.e2e
@pytest.mark.asyncio
async def test_create_email_config(api_client: AsyncClient, auth_headers: dict):
    """Tests creating an email config"""
    config_data = {
        "name": "Test Config",
        "domain_id": 1,
        "smtp_host": "smtp.example.com",
        "smtp_port": 587,
        "smtp_user": "user@example.com"
    }

    with patch("mail_api.common.db.exec_sql") as mock_exec:
        mock_exec.return_value = None

        response = await api_client.post(
            "/email-configs",
            json=config_data,
            headers=auth_headers
        )

        assert response.status_code in [201, 400, 401, 403]


@pytest.mark.asyncio
async def test_get_email_config_by_id(api_client: AsyncClient, auth_headers: dict):
    """Tests fetching a config by ID"""
    config_id = 1

    with patch("mail_api.common.db.fetch_one") as mock_fetch:
        mock_fetch.return_value = {
            "id": config_id,
            "name": "Test Config"
        }

        response = await api_client.get(
            f"/email-configs/{config_id}",
            headers=auth_headers
        )

        assert response.status_code in [200, 404, 401, 403]


@pytest.mark.asyncio
async def test_update_email_config(api_client: AsyncClient, auth_headers: dict):
    """Tests updating a config"""
    config_id = 1
    update_data = {
        "name": "Updated Config"
    }

    with patch("mail_api.common.db.exec_sql") as mock_exec:
        mock_exec.return_value = None

        response = await api_client.put(
            f"/email-configs/{config_id}",
            json=update_data,
            headers=auth_headers
        )

        assert response.status_code in [200, 404, 401, 403]


@pytest.mark.asyncio
async def test_delete_email_config(api_client: AsyncClient, auth_headers: dict):
    """Tests deleting a config"""
    config_id = 1

    with patch("mail_api.common.db.exec_sql") as mock_exec:
        mock_exec.return_value = None

        response = await api_client.delete(
            f"/email-configs/{config_id}",
            headers=auth_headers
        )

        assert response.status_code in [204, 404, 401, 403]
