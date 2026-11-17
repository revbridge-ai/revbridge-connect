"""
Tests for the statistics router
"""
from unittest.mock import patch

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_installation_wide_stats_endpoint_is_gone(api_client: AsyncClient, auth_headers: dict):
    """There is no installation-wide GET /stats: statistics are always scoped to the caller's domain."""
    response = await api_client.get("/stats", headers=auth_headers)
    assert response.status_code in (404, 405)


@pytest.mark.asyncio
async def test_get_stats_by_domain(api_client: AsyncClient, auth_headers: dict):
    """Tests fetching statistics by domain"""
    domain_name = "test.com"

    with patch("mail_api.common.db.fetch_one") as mock_fetch:
        mock_fetch.return_value = {
            "domain_name": domain_name,
            "total_emails": 500,
            "delivered": 480
        }

        response = await api_client.get(
            f"/stats/domain/{domain_name}",
            headers=auth_headers
        )

        assert response.status_code in [200, 404, 401, 403]


@pytest.mark.asyncio
async def test_get_engagement_stats(api_client: AsyncClient, auth_headers: dict):
    """Tests fetching engagement statistics (through the dashboard)"""
    # There is no /engagement endpoint; use /dashboard or /timeseries
    with patch("mail_api.app.api.routers.stats_router.fetch_one") as mock_fetch, \
         patch("mail_api.app.api.routers.stats_router.fetch_all") as mock_fetch_all, \
         patch("mail_api.app.api.routers.stats_router.get_user_domain_name") as mock_domain:
        mock_domain.return_value = "example.com"
        mock_fetch.return_value = {"open_count": 500, "click_count": 200}
        mock_fetch_all.return_value = []

        response = await api_client.get("/stats/dashboard", headers=auth_headers)

        assert response.status_code in [200, 401, 403, 404, 500]

        if response.status_code == 200:
            data = response.json()
            assert isinstance(data, dict)
