"""A click can arrive before the events updater has recorded the delivery (link scanners click
seconds after delivery), so the deliveries row does not exist yet. The v3 token carries
domain_name, which is used as a fallback before giving up."""

import importlib

import pytest

# The routers package __init__ exports the APIRouter object under the same name as the
# module; importing by the full path gets the MODULE (where fetch_one lives).
tracking_router = importlib.import_module(
    "mail_tracking.app.tracking.routers.tracking_router"
)


@pytest.fixture
def no_delivery_rows(monkeypatch):
    """Simulates that window: no row in deliveries/delivery_events yet."""
    monkeypatch.setattr(tracking_router, "fetch_one", lambda *_a, **_k: None)


def test_domain_id_uses_token_domain_name_fallback(no_delivery_rows, monkeypatch):
    from mail_tracking.shared import domain_validation

    monkeypatch.setattr(
        domain_validation,
        "get_domain_id_from_name",
        lambda name: 37 if name == "news.shop.example" else None,
    )
    domain_id = tracking_router.get_domain_id_from_message_id(
        "00000000-0000-4000-8000-000000000002",
        fallback_domain_name="news.shop.example",
    )
    assert domain_id == 37


def test_domain_id_still_raises_without_any_fallback(no_delivery_rows, monkeypatch):
    from mail_tracking.shared import domain_validation

    monkeypatch.setattr(domain_validation, "get_domain_id_from_name", lambda _n: None)
    monkeypatch.delenv("DOMAIN_NAME", raising=False)
    with pytest.raises(ValueError, match="domain_id"):
        tracking_router.get_domain_id_from_message_id("uuid-without-row")


def test_domain_name_uses_token_fallback_before_env(no_delivery_rows, monkeypatch):
    monkeypatch.setenv("DOMAIN_NAME", "env-default.example.com")
    name = tracking_router.get_domain_name_from_message_id(
        "uuid-without-row", fallback_domain_name="m.travel.example"
    )
    assert name == "m.travel.example"


def test_domain_name_env_fallback_preserved(no_delivery_rows, monkeypatch):
    monkeypatch.setenv("DOMAIN_NAME", "env-default.example.com")
    name = tracking_router.get_domain_name_from_message_id("uuid-without-row")
    assert name == "env-default.example.com"
