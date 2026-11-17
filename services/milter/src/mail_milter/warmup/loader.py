"""I/O loader of the divisor: builds the WarmupCandidate list from the real state.

Sources:
- `ip_warmup` (state, per-IP daily_cap, moved by the ip_warmup controller/ramp)
- `poolset_state` (health: in_poolset, dnsbl_listed, fcrdns_ok)
- `relays` (relay_id by pool_name)
- Redis: per-IP sent-today counter (cap) and per-domain SPF authorization set.
- `egress_tier_plan` mode='manual' (a zone whose egress the operator PINNED to a
  relay): the divisor never routes the envelope of those zones, even with SPF ok.

The milter hot path is synchronous and per message, so:
- the list of warming IPs is read from the DB with a short TTL cache (it changes
  slowly: the ramp runs periodically), not on every message;
- SPF is NOT resolved per message: a Redis authorization set
  (`warmup:spf_authz:{domain}`), filled outside the hot path, is checked
  instead. Missing => spf_ok=False (conservative: no routing without confirmed SPF).

The pure part (`build_candidates`) takes injected callables (spf/budget), so it
is testable without DB/Redis.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from mail_milter.warmup.divisor import WarmupCandidate

# Per-process TTL cache of the warming IP list.
_CACHE_TTL_SEC = 60
_cache: dict[str, Any] = {"at": 0.0, "ips": []}
_pinned_cache: dict[str, Any] = {"at": 0.0, "zones": None}

# The X-Warmup-Route header (FILTER) takes precedence over zone_relays, so a zone
# pinned by hand to a relay must be honored HERE; otherwise the pin would only last
# until the zone's SPF includes the pool IPs (spf_authz enables the divisor per sender).
_PINNED_SQL = "SELECT zone_domain FROM egress_tier_plan WHERE mode = 'manual'"

_WARMING_SQL = """
    SELECT w.ip, w.pool_name, w.daily_cap, w.relay_host,
           p.in_poolset, p.dnsbl_listed, p.fcrdns_ok,
           r.id AS relay_id
    FROM ip_warmup w
    JOIN poolset_state p ON p.ip = w.ip
    LEFT JOIN relays r ON r.name = w.pool_name
    WHERE w.state IN ('warming', 'warm') AND w.daily_cap > 0
"""


def _today(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y%m%d")


def load_warming_ips(fetch_all: Callable[..., list[dict]], *, ttl: int = _CACHE_TTL_SEC,
                     _clock: Callable[[], float] = time.monotonic) -> list[dict]:
    """Warming IPs plus health, with a per-process TTL cache. `healthy` already resolved."""
    now = _clock()
    if now - _cache["at"] < ttl and _cache["ips"]:
        return _cache["ips"]
    rows = fetch_all(_WARMING_SQL) or []
    ips = [
        {
            "ip": r["ip"],
            "relay_host": r["relay_host"],
            "relay_id": int(r["relay_id"]) if r.get("relay_id") is not None else None,
            "daily_cap": int(r["daily_cap"] or 0),
            "healthy": bool(r.get("in_poolset")) and not bool(r.get("dnsbl_listed"))
            and bool(r.get("fcrdns_ok")),
        }
        for r in rows
        if r.get("relay_id") is not None
    ]
    _cache["at"], _cache["ips"] = now, ips
    return ips


def load_pinned_zones(fetch_all: Callable[..., list[dict]], *, ttl: int = _CACHE_TTL_SEC,
                      _clock: Callable[[], float] = time.monotonic) -> frozenset[str]:
    """Zones with pinned egress (egress_tier_plan mode='manual'), with a TTL cache.
    A DB error propagates: the caller does not route (fail-safe: stays on the zone's relay)."""
    now = _clock()
    if _pinned_cache["zones"] is not None and now - _pinned_cache["at"] < ttl:
        return _pinned_cache["zones"]
    rows = fetch_all(_PINNED_SQL) or []
    zones = frozenset(
        str(r["zone_domain"]).strip().lower().rstrip(".")
        for r in rows if r.get("zone_domain")
    )
    _pinned_cache["at"], _pinned_cache["zones"] = now, zones
    return zones


def is_pinned(env_from_domain: str | None, zones: frozenset[str]) -> bool:
    """The envelope is the zone or one of its subdomains (rbm<id>.<zone>). Matches by
    label (`x.foo` does not match `foo`), stricter than the LIKE of relay_by_zone."""
    d = (env_from_domain or "").strip().lower().rstrip(".")
    return bool(d) and any(d == z or d.endswith("." + z) for z in zones)


def sent_today(ip: str, redis: Any, now: datetime | None = None) -> int:
    if redis is None:
        return 0
    try:
        v = redis.get(f"warmup:sent:{ip}:{_today(now)}")
        return int(v) if v else 0
    except Exception:
        return 0


def spf_authorized(env_from_domain: str, ip: str, redis: Any) -> bool:
    """Is the IP in the SPF of the envelope-from domain? Reads the Redis set filled
    outside the hot path (`warmup:spf_authz:{domain}`). Missing/error => False."""
    if redis is None or not env_from_domain:
        return False
    try:
        return bool(redis.sismember(f"warmup:spf_authz:{env_from_domain.lower()}", ip))
    except Exception:
        return False


def mark_routed(ip: str, redis: Any, now: datetime | None = None, ttl: int = 172800) -> int:
    """Increment the IP's sent-today counter (cap enforcement). Atomic (INCR); sets
    a 2-day TTL on the first write of the day. Returns the new total (0 on error or
    without Redis: never breaks the send)."""
    if redis is None or not ip:
        return 0
    try:
        key = f"warmup:sent:{ip}:{_today(now)}"
        n = redis.incr(key)
        if n == 1:
            redis.expire(key, ttl)
        return int(n)
    except Exception:
        return 0


def build_candidates(
    env_from_domain: str,
    warming_ips: list[dict],
    *,
    spf_fn: Callable[[str, str], bool],
    budget_fn: Callable[[str, int], int],
) -> list[WarmupCandidate]:
    """Pure over the injected callables: builds the WarmupCandidate list."""
    out: list[WarmupCandidate] = []
    for w in warming_ips:
        out.append(WarmupCandidate(
            ip=w["ip"],
            relay_host=w["relay_host"],
            relay_id=w["relay_id"],
            remaining_budget=budget_fn(w["ip"], w["daily_cap"]),
            spf_ok=spf_fn(env_from_domain, w["ip"]),
            healthy=w["healthy"],
        ))
    return out


def evaluate(
    env_from_domain: str,
    rcpt_domain: str,
    *,
    fetch_all: Callable[..., list[dict]],
    redis: Any,
    now: datetime | None = None,
):
    """Full glue: load IPs (cached) -> candidates (real SPF/budget) -> decision."""
    from mail_milter.warmup.divisor import WarmupDecision, select_warmup_relay

    if is_pinned(env_from_domain, load_pinned_zones(fetch_all)):
        return WarmupDecision(False, None, None, None, "zone_pinned")

    warming = load_warming_ips(fetch_all)
    candidates = build_candidates(
        env_from_domain, warming,
        spf_fn=lambda d, ip: spf_authorized(d, ip, redis),
        budget_fn=lambda ip, cap: cap - sent_today(ip, redis, now),
    )
    return select_warmup_relay(
        env_from_domain=env_from_domain,
        rcpt_domain=rcpt_domain,
        candidates=candidates,
    )


def evaluate_recipients(
    env_from_domain: str,
    rcpt_domains: list[str],
    *,
    fetch_all: Callable[..., list[dict]],
    redis: Any,
    now: datetime | None = None,
) -> dict:
    """Evaluate per DISTINCT destination domain (deduplicated, order preserved).
    Returns {rcpt_domain: WarmupDecision}."""
    uniq = list(dict.fromkeys(d for d in rcpt_domains if d))
    return {
        d: evaluate(env_from_domain, d, fetch_all=fetch_all, redis=redis, now=now)
        for d in uniq
    }


def _reset_cache_for_tests() -> None:
    _cache["at"], _cache["ips"] = 0.0, []
    _pinned_cache["at"], _pinned_cache["zones"] = 0.0, None
