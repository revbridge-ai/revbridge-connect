"""Unit tests for mail_api email idempotency helpers.

No real Redis: a fake async client simulates GET / SETNX(ex) / SETEX, plus a
failing client to prove fail-open.
"""
from __future__ import annotations

import json

import pytest
from mail_api.common import idempotency


class _FakeRedis:
    """Minimal async Redis: SETNX-with-TTL semantics via a dict."""

    def __init__(self):
        self.store: dict[str, str] = {}

    async def get(self, key):
        return self.store.get(key)

    async def set_nx(self, key, value, ttl):
        if key in self.store:
            return None  # already exists -> the claim lost
        self.store[key] = value
        return True

    async def setex(self, key, ttl, value):
        self.store[key] = value
        return True

    async def delete(self, *keys):
        for key in keys:
            self.store.pop(key, None)
        return len(keys)


class _BoomRedis:
    async def get(self, key):
        raise RuntimeError("redis down")

    async def set_nx(self, key, value, ttl):
        raise RuntimeError("redis down")

    async def setex(self, key, ttl, value):
        raise RuntimeError("redis down")

    async def delete(self, *keys):
        raise RuntimeError("redis down")


@pytest.mark.asyncio
async def test_get_returns_none_when_absent(monkeypatch):
    monkeypatch.setattr(idempotency, "redis_client", _FakeRedis())
    assert await idempotency.get_idempotent_response("k1") is None


@pytest.mark.asyncio
async def test_claim_then_get_roundtrip(monkeypatch):
    monkeypatch.setattr(idempotency, "redis_client", _FakeRedis())
    resp = {"message_id": "m1", "status": "queued", "recipients_count": 3}
    assert await idempotency.claim_idempotency_key("camp:u:d", resp) is True
    cached = await idempotency.get_idempotent_response("camp:u:d")
    assert cached["message_id"] == "m1"
    assert cached["recipients_count"] == 3


@pytest.mark.asyncio
async def test_second_claim_loses_race(monkeypatch):
    monkeypatch.setattr(idempotency, "redis_client", _FakeRedis())
    assert await idempotency.claim_idempotency_key("k", {"message_id": "first"}) is True
    # same key -> the second claim loses (SETNX returns None)
    assert await idempotency.claim_idempotency_key("k", {"message_id": "second"}) is False
    # and the winner (first) is the one that stays
    assert (await idempotency.get_idempotent_response("k"))["message_id"] == "first"


@pytest.mark.asyncio
async def test_store_overwrites(monkeypatch):
    monkeypatch.setattr(idempotency, "redis_client", _FakeRedis())
    await idempotency.claim_idempotency_key("k", {"message_id": "m", "recipients_count": 0})
    await idempotency.store_idempotent_response("k", {"message_id": "m", "recipients_count": 50})
    assert (await idempotency.get_idempotent_response("k"))["recipients_count"] == 50


@pytest.mark.asyncio
async def test_fail_open_on_redis_error(monkeypatch):
    monkeypatch.setattr(idempotency, "redis_client", _BoomRedis())
    # the claim fails open (True) so sending is never blocked; get returns None
    assert await idempotency.claim_idempotency_key("k", {"message_id": "m"}) is True
    assert await idempotency.get_idempotent_response("k") is None


@pytest.mark.asyncio
async def test_no_key_is_noop(monkeypatch):
    monkeypatch.setattr(idempotency, "redis_client", _FakeRedis())
    assert await idempotency.get_idempotent_response("") is None
    assert await idempotency.claim_idempotency_key("", {"message_id": "m"}) is True


@pytest.mark.asyncio
async def test_release_frees_key_for_retry(monkeypatch):
    """Enqueue failure after the claim: release deletes the key -> the retry claims again and
    re-enqueues (otherwise the retry would get the 'queued' response of a lost message)."""
    monkeypatch.setattr(idempotency, "redis_client", _FakeRedis())
    assert await idempotency.claim_idempotency_key("k", {"message_id": "lost"}) is True
    await idempotency.release_idempotency_key("k")
    assert await idempotency.get_idempotent_response("k") is None
    # the retry can claim again
    assert await idempotency.claim_idempotency_key("k", {"message_id": "retry"}) is True
    assert (await idempotency.get_idempotent_response("k"))["message_id"] == "retry"


@pytest.mark.asyncio
async def test_release_is_best_effort(monkeypatch):
    monkeypatch.setattr(idempotency, "redis_client", _BoomRedis())
    # a Redis error does not propagate
    await idempotency.release_idempotency_key("k")


@pytest.mark.asyncio
async def test_release_no_key_is_noop(monkeypatch):
    monkeypatch.setattr(idempotency, "redis_client", _FakeRedis())
    await idempotency.release_idempotency_key("")


@pytest.mark.asyncio
async def test_value_is_json_serialized(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(idempotency, "redis_client", fake)
    await idempotency.claim_idempotency_key("k", {"message_id": "m"})
    # stored as JSON under the right prefix
    raw = fake.store[f"{idempotency.IDEMPOTENCY_PREFIX}k"]
    assert json.loads(raw)["message_id"] == "m"
