"""Unit tests for the queue's fairness round-robin (e:q:c:{cid} + tokens)."""
from __future__ import annotations

import json

import pytest
from mail_api.workers import email_processor as ep_mod
from mail_api.workers.email_processor import EmailProcessor

PREFIX = "e:q:c:"
REGISTRY = "e:q:customers"


class _FakeRedis:
    def __init__(self):
        self.lists: dict[str, list[str]] = {}
        self.sets: dict[str, set[str]] = {}

    async def rpush(self, key, *values):
        self.lists.setdefault(key, []).extend(values)

    async def rpop(self, key):
        lst = self.lists.get(key)
        return lst.pop() if lst else None  # same end as BRPOP (the tail)

    async def smembers(self, key):
        return set(self.sets.get(key, set()))

    async def sadd(self, key, *members):
        self.sets.setdefault(key, set()).update(members)

    async def srem(self, key, *members):
        self.sets.get(key, set()).difference_update(members)

    async def llen(self, key):
        return len(self.lists.get(key, []))


def _proc():
    proc = EmailProcessor.__new__(EmailProcessor)  # without __init__ (MailService is heavy)
    proc._rr_index = 0
    proc._rr_current = None
    proc._rr_served = 0
    return proc


@pytest.fixture
def redis(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(ep_mod, "redis_client", fake)
    return fake


async def _seed(redis, cid, items):
    for it in items:
        await redis.rpush(f"{PREFIX}{cid}", it)
    await redis.sadd(REGISTRY, cid)


@pytest.mark.asyncio
async def test_round_robin_interleaves_customers(redis, monkeypatch):
    """A large campaign (A, 6 items) does NOT monopolize the queue: with quantum=2, B (2 items)
    gets its turn after at most 2 of A's items."""
    from mail_api.common import config
    monkeypatch.setattr(config, "QUEUE_FAIRNESS_QUANTUM", 2)
    await _seed(redis, "A", ["a1", "a2", "a3", "a4", "a5", "a6"])
    await _seed(redis, "B", ["b1", "b2"])
    proc = _proc()
    pops = [await proc._rr_pop_customer_item() for _ in range(8)]
    assert sorted(pops) == ["a1", "a2", "a3", "a4", "a5", "a6", "b1", "b2"]
    # B starts after at most one quantum (2 items) of A
    first_b = min(i for i, p in enumerate(pops) if p.startswith("b"))
    assert first_b <= 2
    # turns respect the quantum: never 3+ consecutive items of the same customer
    for i in range(len(pops) - 2):
        trio = {p[0] for p in pops[i:i + 3]}
        if len(trio) == 1 and pops[i][0] == "a":
            # 3 As in a row are only allowed once B is empty
            assert i >= first_b + 2


@pytest.mark.asyncio
async def test_quantum_keeps_customer_sticky(redis, monkeypatch):
    """Within the quantum the worker does NOT switch customers: that is what keeps the SMTP
    connection reusable (the pool is keyed by customer)."""
    from mail_api.common import config
    monkeypatch.setattr(config, "QUEUE_FAIRNESS_QUANTUM", 3)
    await _seed(redis, "A", ["a1", "a2", "a3", "a4"])
    await _seed(redis, "B", ["b1", "b2", "b3"])
    proc = _proc()
    pops = [await proc._rr_pop_customer_item() for _ in range(6)]
    # the first 3 from the same customer (a whole turn), then it switches
    assert len({p[0] for p in pops[:3]}) == 1
    assert pops[3][0] != pops[0][0]


@pytest.mark.asyncio
async def test_empty_registry_returns_none(redis):
    proc = _proc()
    assert await proc._rr_pop_customer_item() is None


@pytest.mark.asyncio
async def test_empty_customer_is_lazily_removed(redis):
    await _seed(redis, "A", ["a1"])
    await redis.sadd(REGISTRY, "vazio")  # a registered customer with no items
    proc = _proc()
    got = {await proc._rr_pop_customer_item() for _ in range(2)}
    assert "a1" in got
    # 'vazio' was swept from the registry in the pass where it returned None
    assert "vazio" not in redis.sets[REGISTRY]


@pytest.mark.asyncio
async def test_all_empty_returns_none_and_cleans(redis):
    await redis.sadd(REGISTRY, "x", "y")
    proc = _proc()
    assert await proc._rr_pop_customer_item() is None
    assert redis.sets[REGISTRY] == set()


# ---------------------------------------------------------------------------
# Permit x discard: a dedupe-skipped item must NOT consume the token
# (otherwise terminal leftovers steal tokens and single sends wait, orphaned, until
# another send brings a new token)
# ---------------------------------------------------------------------------

QUEUE = "e:queue"
TOKEN = json.dumps({"_t": 1})


def _item(mid: str) -> str:
    return json.dumps({
        "message_id": mid,
        "domain_name": "d.example.com",
        "mail_data": {"to": {"address": f"{mid}@x.com"}},
    })


async def _run_worker(proc, redis, monkeypatch, claim_ok, max_brpop=20):
    """Runs _process_email_queue with fake claim/send/brpop until the queues are empty."""
    from mail_api.services import send_ledger

    processed: list[str] = []

    async def fake_claim(message_id, rcpt, domain, worker_id, lease):
        return claim_ok(message_id)

    async def fake_process(email):
        processed.append(email["message_id"])

    async def noop_scheduled():
        return None

    calls = {"n": 0}

    async def fake_brpop(key, timeout=None):
        calls["n"] += 1
        if calls["n"] >= max_brpop:
            proc.running = False
            return None
        lst = redis.lists.get(key)
        if lst:
            return (key, lst.pop())
        proc.running = False
        return None

    monkeypatch.setattr(send_ledger, "claim", fake_claim)
    redis.brpop = fake_brpop
    proc._process_single_email = fake_process
    proc._process_scheduled_emails = noop_scheduled
    proc.running = True
    proc.worker_id = "w-test"
    await proc._process_email_queue()
    return processed


@pytest.mark.asyncio
async def test_dedupe_skip_does_not_consume_the_permit(redis, monkeypatch):
    """Leftovers (claim=False) at the head of the round-robin are discarded under the SAME
    token and the real item is sent, without waiting for another send's token."""
    # rpop takes from the tail: seed in reverse order so the leftovers come out first
    await _seed(redis, "A", [_item("real"), _item("z2"), _item("z1")])
    await redis.rpush(QUEUE, TOKEN)
    proc = _proc()
    processed = await _run_worker(proc, redis, monkeypatch, lambda mid: mid == "real")
    assert processed == ["real"]
    assert redis.lists.get(QUEUE, []) == []  # permit consumed by the real send
    assert redis.lists.get(f"{PREFIX}A", []) == []  # leftovers flushed along the way


@pytest.mark.asyncio
async def test_token_with_only_zombies_does_not_become_surplus(redis, monkeypatch):
    """If the permit only found leftovers and the queues are empty, the token is NOT given
    back (otherwise it would become surplus: BRPOP->None->RPUSH in an endless ping-pong)."""
    await _seed(redis, "A", [_item("z1")])
    await redis.rpush(QUEUE, TOKEN)
    proc = _proc()
    processed = await _run_worker(proc, redis, monkeypatch, lambda mid: False)
    assert processed == []
    assert redis.lists.get(QUEUE, []) == []


@pytest.mark.asyncio
async def test_token_without_an_item_is_dropped(redis, monkeypatch):
    """A token with no item in any client queue is surplus and dies: the producer writes
    item -> registry -> token, so no item is on its way. Giving it back to e:queue would
    create an eternal token (BRPOP -> None -> RPUSH) that buries the retries."""
    await redis.rpush(QUEUE, TOKEN)
    proc = _proc()
    processed = await _run_worker(
        proc, redis, monkeypatch, lambda mid: True, max_brpop=2
    )
    assert processed == []
    assert redis.lists.get(QUEUE, []) == []
