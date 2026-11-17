"""Tests for the idempotency contract of POST /dkim/keys.

When the DKIM key already exists, the service must still return ``public_key`` and
``dns_record_*``. With null values, a claim workflow would publish a TXT record whose content
is ``None``, the DNS provider would reject it, and the domain would be left without a DKIM
record while signing with a key nobody can verify (``dkim=permerror`` at the recipient).

No Docker/MySQL: database access is replaced by a fake.
"""
from __future__ import annotations

import pytest
from mail_api.models.dkim_models import DkimKeyResponse
from mail_api.services import dkim_key_service as svc_mod
from mail_api.services.dkim_key_service import (
    DkimKeyService,
    generate_dkim_keypair,
    public_key_from_private_pem,
)


def _response(key_id: int = 1) -> DkimKeyResponse:
    """Row as the service builds it: no private material, as in a real deployment."""
    from datetime import datetime

    agora = datetime(2026, 7, 31, 12, 0, 0)
    return DkimKeyResponse(
        id=key_id,
        cluster_id="default",
        selector="rbds1",
        dkim_domain="news.shop.example",
        private_key_path="/etc/opendkim/keys/news.shop.example/rbds1.private",
        created_at=agora,
        updated_at=agora,
    )


@pytest.fixture
def pem_and_pub() -> tuple[str, str]:
    return generate_dkim_keypair(key_size=2048)


@pytest.mark.asyncio
async def test_existing_key_returns_the_dns_record(monkeypatch, pem_and_pub):
    """With a PEM in the database, the response carries the TXT record ready to publish."""
    pem, pub_esperada = pem_and_pub

    async def fake_fetch_one(sql, params):
        assert "private_key_pem" in sql
        return {"private_key_pem": pem}

    monkeypatch.setattr(svc_mod, "fetch_one_async", fake_fetch_one)

    resultado = await DkimKeyService()._with_dns_record(_response())

    assert resultado.public_key == pub_esperada
    assert resultado.dns_record_name == "rbds1._domainkey.news.shop.example"
    assert resultado.dns_record_value == f"v=DKIM1; k=rsa; p={pub_esperada}"
    # never "None" in the TXT content: a DNS provider rejects exactly that
    assert "None" not in resultado.dns_record_value


@pytest.mark.asyncio
async def test_txt_format_matches_creation(monkeypatch, pem_and_pub):
    """Re-provisioning must produce a TXT record byte-for-byte equal to the first creation."""
    pem, pub = pem_and_pub

    async def fake_fetch_one(sql, params):
        return {"private_key_pem": pem}

    monkeypatch.setattr(svc_mod, "fetch_one_async", fake_fetch_one)

    na_repeticao = (await DkimKeyService()._with_dns_record(_response())).dns_record_value
    na_criacao = f"v=DKIM1; k=rsa; p={pub}"
    assert na_repeticao == na_criacao


@pytest.mark.asyncio
async def test_no_pem_in_database_does_not_invent_a_record(monkeypatch):
    """Legacy row (private_key_path only): better to return no DNS record than a wrong one."""

    async def fake_fetch_one(sql, params):
        return {"private_key_pem": None}

    monkeypatch.setattr(svc_mod, "fetch_one_async", fake_fetch_one)

    resultado = await DkimKeyService()._with_dns_record(_response())

    assert resultado.public_key is None
    assert resultado.dns_record_value is None


@pytest.mark.asyncio
async def test_corrupted_pem_does_not_break_the_claim(monkeypatch, caplog):
    """Invalid PEM in the database: logs and continues without DNS instead of raising."""

    async def fake_fetch_one(sql, params):
        return {"private_key_pem": "-----BEGIN RSA PRIVATE KEY-----\ngarbage\n-----END RSA PRIVATE KEY-----\n"}

    monkeypatch.setattr(svc_mod, "fetch_one_async", fake_fetch_one)

    resultado = await DkimKeyService()._with_dns_record(_response())

    assert resultado.public_key is None
    assert "private_key_pem" in caplog.text


@pytest.mark.asyncio
async def test_generated_key_matches_the_derived_one(pem_and_pub):
    """The public key returned at creation is the same one derived from the stored private key.

    This is what guarantees that the published TXT record matches the signing key.
    """
    pem, pub = pem_and_pub
    assert public_key_from_private_pem(pem) == pub
    assert pem.startswith("-----BEGIN RSA PRIVATE KEY-----")  # PKCS#1, like opendkim-genkey
