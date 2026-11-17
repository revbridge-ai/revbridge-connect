"""
Explicit domain_id on POST /email-configs.

While a SECOND domain is being provisioned, the key's email channel may still
point to the previous domain. With `domain_id` in the body, the target of the
config is explicit, and it is validated against primary ∪ api_key_domains ∪ legacy.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from mail_api.app.api.routers.email_config_router import create_email_config
from mail_api.models.email_config_models import EmailConfigCreate


def _payload(**kw):
    return EmailConfigCreate(name="Email Config", smtp_host="smtp.example.com", **kw)


def _user(channel_domain=87, legacy_domain=91):
    return {
        "user_id": "test-user",
        "api_key_id": "ak_test",
        "domain_id": legacy_domain,
        "channels": {"email": {"enabled": True, "config": {"domain_id": channel_domain}}},
    }


@pytest.mark.asyncio
async def test_default_target_is_primary_channel_domain():
    with patch(
        "mail_api.app.api.routers.email_config_router.email_config_service.create_config",
        new=AsyncMock(return_value={"ok": True}),
    ) as create:
        await create_email_config(_payload(), current_user=_user())
    assert create.call_args.kwargs["domain_id"] == 87


@pytest.mark.asyncio
async def test_explicit_legacy_domain_allowed():
    # Provisioning scenario: PUT /auth/api-keys/me has just set the legacy
    # domain to the NEW domain; the channel still points to the old one. An
    # explicit domain_id equal to the legacy one must pass without looking up
    # extra api_key_domains.
    with patch(
        "mail_api.app.api.routers.email_config_router.get_authorized_domain_ids",
        new=AsyncMock(return_value={87}),
    ), patch(
        "mail_api.app.api.routers.email_config_router.email_config_service.create_config",
        new=AsyncMock(return_value={"ok": True}),
    ) as create:
        await create_email_config(_payload(domain_id=91), current_user=_user())
    assert create.call_args.kwargs["domain_id"] == 91


@pytest.mark.asyncio
async def test_explicit_extra_authorized_domain_allowed():
    with patch(
        "mail_api.app.api.routers.email_config_router.get_authorized_domain_ids",
        new=AsyncMock(return_value={87, 120}),
    ), patch(
        "mail_api.app.api.routers.email_config_router.email_config_service.create_config",
        new=AsyncMock(return_value={"ok": True}),
    ) as create:
        await create_email_config(_payload(domain_id=120), current_user=_user(legacy_domain=None))
    assert create.call_args.kwargs["domain_id"] == 120


@pytest.mark.asyncio
async def test_explicit_unauthorized_domain_403():
    with patch(
        "mail_api.app.api.routers.email_config_router.get_authorized_domain_ids",
        new=AsyncMock(return_value={87}),
    ):
        with pytest.raises(HTTPException) as exc:
            await create_email_config(_payload(domain_id=999), current_user=_user(legacy_domain=None))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_explicit_equal_to_primary_skips_authorization_lookup():
    with patch(
        "mail_api.app.api.routers.email_config_router.get_authorized_domain_ids",
        new=AsyncMock(side_effect=AssertionError("should not look it up")),
    ), patch(
        "mail_api.app.api.routers.email_config_router.email_config_service.create_config",
        new=AsyncMock(return_value={"ok": True}),
    ) as create:
        await create_email_config(_payload(domain_id=87), current_user=_user())
    assert create.call_args.kwargs["domain_id"] == 87
