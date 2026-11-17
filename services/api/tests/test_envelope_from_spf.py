"""Tests for the SPF-aligned envelope-from (Return-Path).

Covers `_spf_aligned_envelope`: derives the envelope on the sending subdomain
rbm<id>.<from-domain> (which has SPF) from the smtp_host, keeping the same
mailbox and without changing the visible From. Returns None when it cannot be
derived safely (the caller keeps the default behavior = the envelope comes from
the From).
"""

import pytest
from mail_api.services.email_service import _spf_aligned_envelope


def test_derives_the_spf_subdomain_from_the_smtp_host():
    # From without SPF -> envelope on the subdomain that has SPF.
    assert (
        _spf_aligned_envelope(
            "contact@updates.shop.example", "smtp.rbm1001.example.net"
        )
        == "contact@rbm1001.updates.shop.example"
    )


def test_keeps_the_mailbox_and_only_swaps_the_domain():
    assert (
        _spf_aligned_envelope("sales@store.example", "smtp.rbm999.example.net")
        == "sales@rbm999.store.example"
    )


def test_none_when_from_is_already_on_the_subdomain():
    # From is already the subdomain with SPF -> nothing to do (avoids rbm.rbm.<domain>).
    assert (
        _spf_aligned_envelope(
            "contact@rbm1001.updates.shop.example",
            "smtp.rbm1001.example.net",
        )
        is None
    )


@pytest.mark.parametrize(
    "from_address,smtp_host",
    [
        (None, "smtp.rbm1001.example.net"),  # no from
        ("no-at-sign", "smtp.rbm1001.example.net"),  # invalid from
        ("@domain.example", "smtp.rbm1001.example.net"),  # no local part
        ("a@b.com", None),  # no smtp_host
        ("a@b.com", "localhost"),  # smtp_host without a derivable prefix
        ("a@b.com", "smtp.relay.example.net"),  # 2nd label is not rbm<id>
        ("a@b.com", "rbm1001.example.net"),  # no 'smtp.' in front -> 2nd label != rbm
    ],
)
def test_none_when_it_cannot_be_derived(from_address, smtp_host):
    assert _spf_aligned_envelope(from_address, smtp_host) is None
