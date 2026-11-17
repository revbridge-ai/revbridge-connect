"""
Queue fairness: a token (permit) with no item behind it must not circulate forever.

Rule: a token that finds no item dies (it is not pushed back to e:queue). An item
without a token (a pod died between the BRPOP of the token and the RPOP of the item)
is swept by a synthetic permit when the BRPOP times out; the lazy SREM of the
registry re-checks the queue so a customer that still has an item is never
unregistered. Retries pushed with LPUSH (end of the list) are always reached.

In-memory Redis (no server): only the commands the worker loop uses.
"""

import json
import logging
from unittest.mock import AsyncMock

import pytest

TOKEN = '{"_t": 1}'
QUEUE = "e:queue"
REGISTRY = "e:q:customers"


class FakeRedis:
    """In-memory lists and sets with the Redis semantics of what the worker calls."""

    def __init__(self, processor=None, max_empty_polls=2, max_polls=60):
        self.lists: dict[str, list[str]] = {}
        self.sets: dict[str, set[str]] = {}
        self.processor = processor
        self.max_empty_polls = max_empty_polls
        self.max_polls = max_polls
        self.polls = 0
        self.empty_polls = 0

    async def ping(self):
        return True

    async def rpush(self, key, *values):
        self.lists.setdefault(key, []).extend(values)
        return len(self.lists[key])

    async def lpush(self, key, *values):
        lst = self.lists.setdefault(key, [])
        for v in values:
            lst.insert(0, v)
        return len(lst)

    async def rpop(self, key):
        lst = self.lists.get(key)
        return lst.pop() if lst else None

    async def llen(self, key):
        return len(self.lists.get(key, []))

    async def smembers(self, key):
        return set(self.sets.get(key, set()))

    async def sadd(self, key, *members):
        s = self.sets.setdefault(key, set())
        before = len(s)
        s.update(members)
        return len(s) - before

    async def srem(self, key, *members):
        s = self.sets.get(key, set())
        removed = len(s & set(members))
        s.difference_update(members)
        return removed

    async def brpop(self, key, timeout=0):
        # Stops the worker loop after N empty polls in a row (queue drained)
        # or at an absolute cap (a token circulating forever = the failure mode).
        self.polls += 1
        item = await self.rpop(key)
        if item is None:
            self.empty_polls += 1
        else:
            self.empty_polls = 0
        if self.empty_polls >= self.max_empty_polls or self.polls >= self.max_polls:
            self.processor.running = False
        return None if item is None else (key, item)


def _item(message_id: str) -> str:
    return json.dumps(
        {
            "message_id": message_id,
            "domain_name": "updates.example.com",
            "mail_data": {"to": {"address": f"{message_id}@example.com"}},
        }
    )


def _enqueue(fake: FakeRedis, cid: str, message_id: str, *, token: bool = True):
    """Same order as the producer (email_service._enqueue): item → registry → token."""
    fake.lists.setdefault(f"e:q:c:{cid}", []).append(_item(message_id))
    fake.sets.setdefault(REGISTRY, set()).add(cid)
    if token:
        fake.lists.setdefault(QUEUE, []).append(TOKEN)


@pytest.fixture
def worker(monkeypatch):
    """EmailProcessor with in-memory Redis and mocked ledger and send."""
    import mail_api.services.send_ledger as sl
    import mail_api.workers.email_processor as ep

    processor = ep.EmailProcessor()
    fake = FakeRedis(processor)
    monkeypatch.setattr(ep, "redis_client", fake)

    sent: list[str] = []
    zombies: set[str] = set()

    async def _claim(message_id, *args, **kwargs):
        return message_id not in zombies

    async def _send(email):
        sent.append(email["message_id"])

    monkeypatch.setattr(sl, "claim", AsyncMock(side_effect=_claim))
    processor._process_scheduled_emails = AsyncMock()
    processor._process_single_email = AsyncMock(side_effect=_send)
    processor.running = True
    return processor, fake, sent, zombies


@pytest.mark.asyncio
async def test_surplus_token_dies_instead_of_circulating(worker):
    processor, fake, sent, _ = worker
    fake.lists[QUEUE] = [TOKEN] * 3  # tokens with no item at all (leftovers)

    await processor._process_email_queue()

    assert fake.lists[QUEUE] == []
    assert fake.empty_polls >= 2, "token went back to e:queue instead of dying"
    assert sent == []


@pytest.mark.asyncio
async def test_tokens_serve_items_round_robin(worker):
    processor, fake, sent, _ = worker
    _enqueue(fake, "cust-a", "a1")
    _enqueue(fake, "cust-a", "a2")
    _enqueue(fake, "cust-b", "b1")

    await processor._process_email_queue()

    assert sorted(sent) == ["a1", "a2", "b1"]
    assert fake.lists[QUEUE] == []
    assert fake.lists["e:q:c:cust-a"] == [] and fake.lists["e:q:c:cust-b"] == []
    assert fake.sets[REGISTRY] == set()


@pytest.mark.asyncio
async def test_zombie_skip_leaves_no_eternal_token(worker):
    """A zombie (ledger already terminal) is skipped under the SAME permit: the zombie's
    token is left over. That surplus must die, not circulate."""
    processor, fake, sent, zombies = worker
    zombies.add("zombie")
    _enqueue(fake, "cust-a", "real")
    _enqueue(fake, "cust-a", "zombie")  # RPOP takes this one first

    await processor._process_email_queue()

    assert sent == ["real"]
    assert fake.lists[QUEUE] == []
    assert fake.empty_polls >= 2, "the surplus from the zombie skip kept circulating"


@pytest.mark.asyncio
async def test_idle_sweep_serves_item_without_token(worker):
    """An item registered without a token (the pod died between the BRPOP of the token
    and the RPOP of the item): the synthetic permit of the BRPOP timeout delivers it."""
    processor, fake, sent, _ = worker
    _enqueue(fake, "cust-a", "orphan", token=False)

    await processor._process_email_queue()

    assert sent == ["orphan"]
    assert fake.lists["e:q:c:cust-a"] == []


@pytest.mark.asyncio
async def test_retry_behind_surplus_tokens_is_reached(worker):
    """A retry goes to the end of the list via LPUSH; it must still be reached when
    surplus tokens are ahead of it."""
    processor, fake, sent, _ = worker
    fake.lists[QUEUE] = [_item("old-retry")] + [TOKEN] * 5

    await processor._process_email_queue()

    assert sent == ["old-retry"]
    assert fake.lists[QUEUE] == []


@pytest.mark.asyncio
async def test_lazy_srem_race_keeps_customer_registered(worker):
    """The producer does RPUSH+SADD between the empty RPOP and the worker's SREM: the SREM
    must not leave the customer out of the registry while it has an item in the queue."""
    processor, fake, _, _ = worker
    fake.sets[REGISTRY] = {"cust-a"}
    original_rpop = fake.rpop

    async def rpop_with_producer_in_between(key):
        result = await original_rpop(key)
        if key == "e:q:c:cust-a" and result is None:
            _enqueue(fake, "cust-a", "just-arrived")
        return result

    fake.rpop = rpop_with_producer_in_between

    assert await processor._rr_pop_customer_item() is None
    assert "cust-a" in fake.sets[REGISTRY]
    assert fake.lists["e:q:c:cust-a"] == [_item("just-arrived")]


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("rediss://default:s3cr3t@valkey.example.com:21233/0", "rediss://valkey.example.com:21233/0"),
        ("redis://:s3cr3t@192.0.2.5:6379/0", "redis://192.0.2.5:6379/0"),
        ("redis://127.0.0.1:6379/0", "redis://127.0.0.1:6379/0"),
        ("redis://u:p@[::1]:6379", "redis://[::1]:6379"),
        ("", ""),
    ],
)
def test_redact_url(url, expected):
    from mail_api.workers.email_processor import _redact_url

    assert _redact_url(url) == expected


@pytest.mark.asyncio
async def test_startup_log_has_no_redis_password(worker, monkeypatch, caplog):
    import mail_api.common.config as config

    processor, _, _, _ = worker
    monkeypatch.setattr(
        config, "REDIS_URL", "rediss://default:s3cr3t@valkey.example.com:21233/0"
    )
    processor._migrate_legacy_keys = AsyncMock()
    processor._process_email_queue = AsyncMock()

    with caplog.at_level(logging.INFO, logger="mail_api.workers.email_processor"):
        await processor.start()

    assert "s3cr3t" not in caplog.text
    assert "valkey.example.com:21233" in caplog.text
