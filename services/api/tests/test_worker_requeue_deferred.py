"""
Tests that the worker never drops a recoverable email in silence.

TEMPORARY send failures (4xx deferral, disconnect, rate limit) are re-raised so
the _process_email_queue() loop re-queues them with retry and keeps the payload;
PERMANENT failures stay terminal. All behind the WORKER_REQUEUE_DEFERRED kill
switch (default True).
"""

import smtplib
from unittest.mock import AsyncMock

import pytest


def _email_data() -> dict:
    return {
        "message_id": "test-msg-001",
        "domain_name": "test.example.com",
        "mail_data": {
            "from": {"name": "T", "address": "t@test.example.com"},
            "to": {"address": "user@example.org"},
            "subject": "hi",
            "content": [{"type": "text/html", "value": "<p>hi</p>"}],
        },
        "api_key": "k",
    }


@pytest.fixture(autouse=True)
def _stub_config_cache(monkeypatch):
    # Avoids depending on Redis for the config pre-fetch inside _process_single_email.
    import mail_api.common.email_config_cache as cc

    monkeypatch.setattr(
        cc, "get_email_config_for_domain_name_from_redis", AsyncMock(return_value=None)
    )


@pytest.fixture(autouse=True)
def _stub_send_ledger(monkeypatch):
    # Avoids depending on MySQL for the ledger state transitions during the
    # _process_single_email tests (mark_accepted/mark_failed).
    import mail_api.services.send_ledger as sl

    for fn in ("admit", "claim", "mark_accepted", "mark_failed",
               "mark_requeued", "mark_deadletter"):
        monkeypatch.setattr(sl, fn, AsyncMock(return_value=None))


@pytest.mark.asyncio
async def test_deferred_send_reraises_for_requeue(monkeypatch):
    """A temporary failure (451) must be RE-RAISED so the loop re-queues it."""
    import mail_api.common.config as config
    from mail_api.workers.email_processor import EmailProcessor

    monkeypatch.setattr(config, "WORKER_REQUEUE_DEFERRED", True)

    processor = EmailProcessor()
    processor._send_with_fallback = AsyncMock(
        side_effect=smtplib.SMTPResponseException(451, b"try again later")
    )

    with pytest.raises(smtplib.SMTPResponseException) as exc:
        await processor._process_single_email(_email_data())
    assert exc.value.smtp_code == 451


@pytest.mark.asyncio
async def test_permanent_send_does_not_reraise(monkeypatch):
    """A permanent failure (550) is terminal: it is NOT re-raised (returns normally)."""
    import mail_api.common.config as config
    from mail_api.workers.email_processor import EmailProcessor

    monkeypatch.setattr(config, "WORKER_REQUEUE_DEFERRED", True)

    processor = EmailProcessor()
    processor._send_with_fallback = AsyncMock(
        side_effect=smtplib.SMTPResponseException(550, b"user unknown")
    )

    # Must not raise.
    await processor._process_single_email(_email_data())


@pytest.mark.asyncio
async def test_kill_switch_off_keeps_old_behavior(monkeypatch):
    """With the flag OFF, a deferral is terminal."""
    import mail_api.common.config as config
    from mail_api.workers.email_processor import EmailProcessor

    monkeypatch.setattr(config, "WORKER_REQUEUE_DEFERRED", False)

    processor = EmailProcessor()
    processor._send_with_fallback = AsyncMock(
        side_effect=smtplib.SMTPResponseException(451, b"try again later")
    )

    # Flag OFF -> not re-raised, even for a deferral.
    await processor._process_single_email(_email_data())
