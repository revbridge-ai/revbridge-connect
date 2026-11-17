"""Tests for the divisor's I/O loader (DB/Redis fakes)."""
from datetime import UTC

from mail_milter.warmup import loader
from mail_milter.warmup.divisor import WarmupCandidate


class FakeRedis:
    def __init__(self, counters=None, spf=None):
        self._c = counters or {}                 # key -> value
        self._spf = spf or {}                    # domain -> set(ips)

    def get(self, key):
        return self._c.get(key)

    def sismember(self, key, member):
        dom = key.removeprefix("warmup:spf_authz:")
        return member in self._spf.get(dom, set())


ROWS = [
    {"ip": "192.0.2.1", "pool_name": "pool-02", "daily_cap": 100,
     "relay_host": "pool-02.mta.example.com", "in_poolset": 1,
     "dnsbl_listed": 0, "fcrdns_ok": 1, "relay_id": 18},
    # unhealthy (DNSBL) -> healthy False
    {"ip": "192.0.2.2", "pool_name": "pool-03", "daily_cap": 50,
     "relay_host": "pool-03.mta.example.com", "in_poolset": 1,
     "dnsbl_listed": 1, "fcrdns_ok": 1, "relay_id": 19},
    # no relay_id -> dropped
    {"ip": "192.0.2.3", "pool_name": "pool-x", "daily_cap": 10,
     "relay_host": "x", "in_poolset": 1, "dnsbl_listed": 0, "fcrdns_ok": 1,
     "relay_id": None},
]


def setup_function():
    loader._reset_cache_for_tests()


def test_load_warming_ips_maps_and_filters():
    ips = loader.load_warming_ips(lambda *a, **k: ROWS)
    assert len(ips) == 2                       # 192.0.2.3 dropped (relay_id None)
    a = next(i for i in ips if i["ip"] == "192.0.2.1")
    assert a["healthy"] is True and a["daily_cap"] == 100 and a["relay_id"] == 18
    b = next(i for i in ips if i["ip"] == "192.0.2.2")
    assert b["healthy"] is False               # DNSBL listed


def test_load_warming_ips_ttl_cache():
    calls = {"n": 0}

    def fa(*a, **k):
        calls["n"] += 1
        return ROWS

    t = {"v": 1000.0}
    loader.load_warming_ips(fa, ttl=60, _clock=lambda: t["v"])
    loader.load_warming_ips(fa, ttl=60, _clock=lambda: t["v"] + 30)   # within the TTL
    assert calls["n"] == 1
    loader.load_warming_ips(fa, ttl=60, _clock=lambda: t["v"] + 61)   # expired
    assert calls["n"] == 2


def test_sent_today_and_budget():
    from datetime import datetime
    now = datetime(2026, 7, 22, tzinfo=UTC)
    r = FakeRedis(counters={"warmup:sent:192.0.2.1:20260722": "40"})
    assert loader.sent_today("192.0.2.1", r, now) == 40
    assert loader.sent_today("192.0.2.9", r, now) == 0     # missing
    assert loader.sent_today("192.0.2.1", None, now) == 0  # no redis


def test_spf_authorized():
    r = FakeRedis(spf={"rbm1.shop.example.com": {"192.0.2.1"}})
    assert loader.spf_authorized("rbm1.shop.example.com", "192.0.2.1", r) is True
    assert loader.spf_authorized("rbm1.shop.example.com", "192.0.2.2", r) is False
    assert loader.spf_authorized("other.example.com", "192.0.2.1", r) is False
    assert loader.spf_authorized("x", "192.0.2.1", None) is False   # no redis


def test_build_candidates_pure():
    warming = [{"ip": "192.0.2.1", "relay_host": "h", "relay_id": 18,
                "daily_cap": 100, "healthy": True}]
    cands = loader.build_candidates(
        "dom", warming,
        spf_fn=lambda d, ip: True,
        budget_fn=lambda ip, cap: cap - 30,
    )
    assert cands == [WarmupCandidate("192.0.2.1", "h", 18, 70, True, True)]


def test_evaluate_end_to_end_routes():
    from datetime import datetime
    now = datetime(2026, 7, 22, tzinfo=UTC)
    r = FakeRedis(
        counters={"warmup:sent:192.0.2.1:20260722": "10"},        # 90 left
        spf={"rbm1.shop.example.com": {"192.0.2.1"}},
    )
    d = loader.evaluate(
        "rbm1.shop.example.com", "gmail.com",
        fetch_all=lambda *a, **k: ROWS, redis=r, now=now,
    )
    assert d.route is True and d.ip == "192.0.2.1"


def test_evaluate_blocks_without_spf():
    from datetime import datetime
    now = datetime(2026, 7, 22, tzinfo=UTC)
    r = FakeRedis(counters={}, spf={})     # no SPF authorization at all
    d = loader.evaluate(
        "rbm1.shop.example.com", "gmail.com",
        fetch_all=lambda *a, **k: ROWS, redis=r, now=now,
    )
    assert d.route is False and d.reason == "spf_not_authorized"


def _fetch_with_pins(pins):
    def fetch_all(sql, *a, **k):
        return [{"zone_domain": z} for z in pins] if "egress_tier_plan" in sql else ROWS
    return fetch_all


def test_evaluate_skips_pinned_zone_even_with_spf():
    from datetime import datetime
    now = datetime(2026, 10, 26, tzinfo=UTC)
    r = FakeRedis(spf={"rbm504978.mail.store.example": {"192.0.2.1"}})
    d = loader.evaluate(
        "rbm504978.mail.store.example", "gmail.com",
        fetch_all=_fetch_with_pins(["mail.store.example"]), redis=r, now=now,
    )
    assert d.route is False and d.reason == "zone_pinned"


def test_evaluate_pin_does_not_leak_to_other_zones():
    from datetime import datetime
    now = datetime(2026, 10, 26, tzinfo=UTC)
    r = FakeRedis(spf={"rbm1.shop.example.com": {"192.0.2.1"},
                       "rbm1.xmail.store.example": {"192.0.2.1"}})
    fetch = _fetch_with_pins(["mail.store.example"])
    assert loader.evaluate("rbm1.shop.example.com", "gmail.com",
                           fetch_all=fetch, redis=r, now=now).route is True
    # the suffix matches by label, not by substring
    assert loader.evaluate("rbm1.xmail.store.example", "gmail.com",
                           fetch_all=fetch, redis=r, now=now).route is True


def test_evaluate_pinned_lookup_error_propagates():
    import pytest

    def boom(sql, *a, **k):
        raise RuntimeError("db down")
    with pytest.raises(RuntimeError):
        loader.evaluate("rbm1.shop.example.com", "gmail.com",
                        fetch_all=boom, redis=FakeRedis())


def test_evaluate_recipients_dedups_and_maps():
    from datetime import datetime
    now = datetime(2026, 7, 22, tzinfo=UTC)
    r = FakeRedis(spf={"rbm1.shop.example.com": {"192.0.2.1"}})
    res = loader.evaluate_recipients(
        "rbm1.shop.example.com",
        ["gmail.com", "gmail.com", "outlook.com", ""],   # dedup + skips empty
        fetch_all=lambda *a, **k: ROWS, redis=r, now=now,
    )
    assert set(res.keys()) == {"gmail.com", "outlook.com"}
    assert res["gmail.com"].route is True
    assert res["outlook.com"].reason == "rcpt_microsoft"


class FakeIncrRedis:
    def __init__(self):
        self.store = {}
        self.ttls = {}
    def incr(self, key):
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]
    def expire(self, key, ttl):
        self.ttls[key] = ttl


def test_mark_routed_incrs_and_sets_ttl_once():
    from datetime import datetime
    now = datetime(2026, 7, 23, tzinfo=UTC)
    r = FakeIncrRedis()
    assert loader.mark_routed("192.0.2.1", r, now) == 1
    assert loader.mark_routed("192.0.2.1", r, now) == 2      # increments
    assert r.store["warmup:sent:192.0.2.1:20260723"] == 2
    assert r.ttls["warmup:sent:192.0.2.1:20260723"] == 172800  # TTL only on the 1st
    assert loader.mark_routed("192.0.2.1", None, now) == 0   # no redis: safe no-op
