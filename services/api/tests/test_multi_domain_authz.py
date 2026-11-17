"""Unit tests for multi-domain sender authorization (api_key_domains).

Covers `get_authorized_domain_ids` (primary ∪ extra domains) and the multi-domain
behavior of `validate_authorized_sender`: one API Key may send from any of its
authorized domains. Without extra domains, only the primary domain is authorized.
"""
from unittest.mock import AsyncMock, patch

import mail_api.shared.domain_validation as dv
import pytest


@pytest.mark.asyncio
async def test_get_authorized_domain_ids_unions_primary_and_extra():
    cu = {"domain_id": 5, "api_key_id": "ak_x"}
    with patch.object(dv, "get_user_domain_id", return_value=5), \
         patch.object(dv, "_get_api_key_extra_domains_cached", new=AsyncMock(return_value={7, 8})):
        assert await dv.get_authorized_domain_ids(cu) == {5, 7, 8}


@pytest.mark.asyncio
async def test_get_authorized_domain_ids_primary_only_when_no_extra():
    cu = {"domain_id": 5, "api_key_id": "ak_x"}
    with patch.object(dv, "get_user_domain_id", return_value=5), \
         patch.object(dv, "_get_api_key_extra_domains_cached", new=AsyncMock(return_value=set())):
        assert await dv.get_authorized_domain_ids(cu) == {5}


@pytest.mark.asyncio
async def test_send_from_extra_authorized_domain_is_allowed():
    """Key primary=1, extra={2}; sending from domain 2 -> allowed."""
    cu = {"domain_id": 1, "api_key_id": "ak_test"}
    with patch.object(dv, "is_email_channel_enabled", return_value=True), \
         patch.object(dv, "get_authorized_domain_ids", new=AsyncMock(return_value={1, 2})), \
         patch.object(dv, "sender_belongs_to_api_key_domain",
                      new=AsyncMock(side_effect=lambda email, did: did == 2)):
        assert await dv.validate_authorized_sender("noreply@brand2.com", cu) is True


@pytest.mark.asyncio
async def test_send_from_primary_domain_still_allowed():
    cu = {"domain_id": 1, "api_key_id": "ak_test"}
    with patch.object(dv, "is_email_channel_enabled", return_value=True), \
         patch.object(dv, "get_authorized_domain_ids", new=AsyncMock(return_value={1})), \
         patch.object(dv, "sender_belongs_to_api_key_domain",
                      new=AsyncMock(side_effect=lambda email, did: did == 1)):
        assert await dv.validate_authorized_sender("noreply@brand1.com", cu) is True


@pytest.mark.asyncio
async def test_send_from_unauthorized_domain_is_denied():
    """No extra domains; sender belongs to none and is not an approved sender -> 403."""
    cu = {"domain_id": 1, "api_key_id": "ak_test"}
    with patch.object(dv, "is_email_channel_enabled", return_value=True), \
         patch.object(dv, "get_authorized_domain_ids", new=AsyncMock(return_value={1})), \
         patch.object(dv, "sender_belongs_to_api_key_domain", new=AsyncMock(return_value=False)), \
         patch("mail_api.common.approved_sender_cache.get_approved_sender_info",
               new=AsyncMock(return_value=None)):
        assert await dv.validate_authorized_sender("noreply@unrelated.com", cu) is False
