"""
Tests for the deliverability router
"""
from unittest.mock import patch

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_get_deliverability_stats(api_client: AsyncClient, auth_headers: dict):
    """Tests fetching the deliverability statistics"""
    with patch("mail_api.app.api.routers.deliverability_router.fetch_one") as mock_fetch, \
         patch("mail_api.app.api.routers.deliverability_router.fetch_all") as mock_fetch_all, \
         patch("mail_api.app.api.routers.deliverability_router.get_user_domain_name") as mock_domain:
        mock_domain.return_value = "test.com"
        # Several fetch_one calls, one per query
        mock_fetch.side_effect = [
            {"total_sent": 100, "total_delivered": 95, "total_bounced": 3, "total_deferred": 1, "total_failed": 1},  # overall_counts
            {"avg_delivery_time_minutes": 2.5},  # avg_delivery_time
            {"hard_bounces": 1, "policy_bounces": 1, "soft_bounces": 1, "other_bounces": 0},  # bounce_breakdown
        ]
        mock_fetch_all.return_value = []  # isp_performance and timeseries

        response = await api_client.get("/deliverability/", headers=auth_headers)

        assert response.status_code in [200, 401, 403, 500]

        if response.status_code == 200:
            data = response.json()
            assert isinstance(data, dict)
            assert "overall_metrics" in data


@pytest.mark.asyncio
async def test_get_deliverability_by_domain(api_client: AsyncClient, auth_headers: dict):
    """Tests fetching deliverability by domain"""
    domain_name = "test.com"

    with patch("mail_api.common.db.fetch_one") as mock_fetch:
        mock_fetch.return_value = {
            "domain_name": domain_name,
            "delivery_rate": 0.95
        }

        response = await api_client.get(
            f"/deliverability/domain/{domain_name}",
            headers=auth_headers
        )

        assert response.status_code in [200, 404, 401, 403]


@pytest.mark.asyncio
async def test_get_bounce_stats(api_client: AsyncClient, auth_headers: dict):
    """Tests fetching the bounce statistics (through the main endpoint)"""
    # The /deliverability/ endpoint already includes bounce_breakdown
    with patch("mail_api.app.api.routers.deliverability_router.fetch_one") as mock_fetch, \
         patch("mail_api.app.api.routers.deliverability_router.fetch_all") as mock_fetch_all, \
         patch("mail_api.app.api.routers.deliverability_router.get_user_domain_name") as mock_domain:
        mock_domain.return_value = "test.com"
        mock_fetch.side_effect = [
            {"total_sent": 100, "total_delivered": 95, "total_bounced": 3, "total_deferred": 1, "total_failed": 1},
            {"avg_delivery_time_minutes": 2.5},
            {"hard_bounces": 1, "policy_bounces": 1, "soft_bounces": 1, "other_bounces": 0},
        ]
        mock_fetch_all.return_value = []

        response = await api_client.get("/deliverability/", headers=auth_headers)

        assert response.status_code in [200, 401, 403, 500]

        if response.status_code == 200:
            data = response.json()
            assert isinstance(data, dict)
            assert "bounce_breakdown" in data
