"""
Per-domain volume governor at the sending point (domain ramp-up).

Email reputation lives on the sending_domain: a sudden jump in volume means
throttling/spam at the mailbox providers. The volume ramp controller
(services/infra) keeps the daily quota per domain in `domain_volume_ramp`; this
module is the ENFORCEMENT in `/send`, the last point every send goes through
(campaigns, transactional mail, direct API calls).

Core rule: **excess is never dropped, it is DEFERRED** to tomorrow's window through
the existing scheduling mechanism (scheduled_at → ZSET `scheduled:{domain}`) while
tomorrow's quota has room. 429 (with Retry-After) when tomorrow is full as well, or
for extreme abuse (>10x the quota in one request): the caller sends the rest later.
The caller sees `scheduled_count` in the response.

Exemptions: `domain_volume_ramp.exempt=1` (purely transactional domains) is NEVER
held. A domain without a row in the table (not governed) also passes: the
bootstrap belongs to the controller, not to this module.

Transactional reserve: "bulk" requests (> VOLUME_BULK_THRESHOLD recipients) only
see (100 - VOLUME_TXN_RESERVE_PCT)% of the quota; small requests use the full
quota, so a large campaign does not starve password resets.

Consumption lives in Redis (`volume:sent:{key}:{YYYYMMDD}`, mirroring the milter's
`warmup:sent:{ip}`), fail-open: Redis/MySQL down → the send goes through.
Deferring reserves the TARGET day (INCR on tomorrow's key), so deferred traffic
has priority on tomorrow's quota.

Modes (VOLUME_GOVERNOR_MODE): "off" (no lookup) | "dryrun" (measures and logs
what it would have deferred, holds nothing) | "enforce" (really defers).

See docs/deliverability.md.
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from mail_api.common.config import (
    VOLUME_BULK_THRESHOLD,
    VOLUME_DEFER_UTC_HOUR,
    VOLUME_GOVERNOR_MODE,
    VOLUME_HARD_REJECT_FACTOR,
    VOLUME_QUOTA_CACHE_TTL_SEC,
    VOLUME_SENT_KEY_PREFIX,
    VOLUME_SENT_TTL_SEC,
    VOLUME_TXN_RESERVE_PCT,
)
from mail_api.common.db import fetch_all_async
from mail_api.common.redis_client import redis_client

logger = logging.getLogger(__name__)

MODE_OFF = "off"
MODE_DRYRUN = "dryrun"
MODE_ENFORCE = "enforce"

# In-process cache of the governed rows (the table is small: ~1 row per active
# domain). Short TTL: the quota changes at most once an hour (controller cron).
_cache: dict = {"ts": 0.0, "rows": None}


@dataclass
class GovernorDecision:
    """Result of evaluating a send request."""

    mode: str
    total: int
    admit: int
    defer: int = 0
    reject: bool = False
    governed_key: str | None = None  # key in domain_volume_ramp (may be the parent)
    exempt: bool = False
    quota: int = 0
    consumed: int = 0
    defer_until_iso: str | None = None
    would_defer: int = 0  # in dryrun: what enforce would have deferred
    # recipients that still fit in tomorrow's quota (set only when part is deferred): a
    # deferral larger than this is refused with 429, so a big first campaign cannot land
    # on tomorrow all at once
    tomorrow_room: int | None = None
    extra: dict = field(default_factory=dict)


def match_governed(domain_name: str, rows: list[dict]) -> dict | None:
    """Matches the send's domain to the governed key, exact or parent.

    If the send's key is a SUBDOMAIN of a governed key, it is normalized to the
    parent (e.g. a send admitted as `rbm1001.mail.example.com` consumes the quota of
    `mail.example.com`), which avoids two curves sharing the same health events.
    The most specific key wins.
    """
    d = (domain_name or "").strip().lower()
    if not d:
        return None
    best = None
    for r in rows:
        k = (r.get("domain_name") or "").lower()
        if d == k or d.endswith("." + k):
            if best is None or len(k) > len(best["domain_name"].lower()):
                best = r
    return best


def decide_admission(*, mode: str, quota: int, consumed: int, total: int,
                     bulk: bool, exempt: bool) -> dict:
    """Pure (testable) decision: how much is admitted today, how much is deferred, or reject.

    action: pass | defer | reject. In dryrun the action taken is always pass,
    but would_defer carries what enforce would have held.
    """
    if mode == MODE_OFF or exempt or quota <= 0:
        return {"action": "pass", "admit": total, "defer": 0, "would_defer": 0}

    effective = int(quota * (1 - VOLUME_TXN_RESERVE_PCT / 100.0)) if bulk else quota
    remaining = max(0, effective - consumed)
    over = max(0, total - remaining)

    if mode == MODE_DRYRUN:
        return {"action": "pass", "admit": total, "defer": 0, "would_defer": over}

    # enforce
    if total > quota * VOLUME_HARD_REJECT_FACTOR:
        return {"action": "reject", "admit": 0, "defer": 0, "would_defer": total}
    if over == 0:
        return {"action": "pass", "admit": total, "defer": 0, "would_defer": 0}
    return {"action": "defer", "admit": total - over, "defer": over,
            "would_defer": over}


def room_on_day(*, quota: int, consumed: int, bulk: bool) -> int:
    """Recipients that still fit in a day's quota (bulk requests see the part without the
    transactional reserve, the same rule as decide_admission)."""
    effective = int(quota * (1 - VOLUME_TXN_RESERVE_PCT / 100.0)) if bulk else quota
    return max(0, effective - consumed)


def seconds_until_next_day(now: datetime | None = None) -> int:
    """Seconds until the next UTC midnight, when the daily counters roll over (Retry-After)."""
    now = now or datetime.now(UTC)
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((tomorrow - now).total_seconds()))


def split_personalizations(personalizations: list, admit_n: int) -> tuple[list, list]:
    """Splits at a personalization boundary (conservative: the one that would cross
    the limit goes WHOLE to the deferred part; admitting part of a personalization
    would break the caller's "1 personalization = 1 logical target" semantics)."""
    admitted, deferred, count = [], [], 0
    for p in personalizations:
        n = len(p.to)
        if count + n <= admit_n:
            admitted.append(p)
            count += n
        else:
            deferred.append(p)
    return admitted, deferred


def _day_key(governed_key: str, offset_days: int = 0) -> str:
    day = (datetime.now(UTC) + timedelta(days=offset_days)).strftime("%Y%m%d")
    return f"{VOLUME_SENT_KEY_PREFIX}{governed_key}:{day}"


def defer_until() -> str:
    """Start of tomorrow's window (UTC ISO) for the deferred part's scheduled_at."""
    tomorrow = datetime.now(UTC) + timedelta(days=1)
    return tomorrow.replace(hour=VOLUME_DEFER_UTC_HOUR, minute=0, second=0,
                            microsecond=0).isoformat()


async def _governed_rows() -> list[dict]:
    now = time.monotonic()
    if _cache["rows"] is not None and now - _cache["ts"] < VOLUME_QUOTA_CACHE_TTL_SEC:
        return _cache["rows"]
    try:
        rows = await fetch_all_async(
            "SELECT domain_name, state, exempt, daily_quota, last_admitted_24h, "
            "last_bounce_rate, last_block_rate, updated_at FROM domain_volume_ramp",
            (),
        )
        _cache["rows"] = rows or []
        _cache["ts"] = now
    except Exception as e:
        logger.error(
            "log_type=ops action=volume_governor_db_failed error=%s "
            "(fail-open: using the stale or empty cache)", e,
        )
        if _cache["rows"] is None:
            return []
    return _cache["rows"]


async def _consumed_today(governed_key: str, offset_days: int = 0) -> int:
    try:
        v = await redis_client.get(_day_key(governed_key, offset_days))
        return int(v) if v else 0
    except Exception as e:
        logger.error(
            "log_type=ops action=volume_governor_redis_failed error=%s "
            "(fail-open: consumed=0)", e,
        )
        return 0


async def register_admission(governed_key: str, n: int, offset_days: int = 0) -> None:
    """INCR of the consumption (best-effort). offset_days=1 reserves tomorrow's
    quota for the deferred traffic."""
    if n <= 0 or not governed_key:
        return
    try:
        key = _day_key(governed_key, offset_days)
        await redis_client.incrby(key, n)
        await redis_client.expire(key, VOLUME_SENT_TTL_SEC)
    except Exception as e:
        logger.error(
            "log_type=ops action=volume_governor_incr_failed key=%s n=%d error=%s",
            governed_key, n, e,
        )


async def allowance(domain_name: str) -> dict:
    """Snapshot of the domain's quota for the /volume/allowance endpoint."""
    row = match_governed(domain_name, await _governed_rows())
    if row is None:
        return {"domain": domain_name, "governed": False,
                "mode": VOLUME_GOVERNOR_MODE}
    governed_key = row["domain_name"]
    quota = int(row.get("daily_quota") or 0)
    consumed = await _consumed_today(governed_key)
    return {
        "domain": domain_name,
        "governed": True,
        "governed_key": governed_key,
        "state": row.get("state"),
        "exempt": bool(row.get("exempt")),
        "mode": VOLUME_GOVERNOR_MODE,
        "daily_quota": quota,
        "consumed_today": consumed,
        "remaining_today": max(0, quota - consumed),
    }


# UI states (plain wording, as shown in the panel):
#   scaling  = governed and active (curve rising or stable)
#   cooldown = braked (health brake; recovers by itself)
#   learning = cold (re-warm / mandatory warm-up)
#   unmanaged = no row in the governor (no limit applied)
_UI_STATE = {"active": "scaling", "braked": "cooldown", "cold": "learning"}

# Mirror of the volume ramp controller's parameters (services/infra/scripts/
# volume_ramp_controller.py: the REAL decision is the controller's; here we only
# project what it would do). Keep them in sync: if they diverge, the panel promises
# an increase that does not happen. The projection must follow the controller's
# utilization gate, or a domain at 63% of its quota would "grow tomorrow" forever.
_RAMP_FACTOR = 1.3
_MAX_STEP_FACTOR = 2.0
_RAMP_UTIL_PCT = 80.0
_QUOTA_MAX = 500_000
_YELLOW_BLOCK_PCT = 3.0
_YELLOW_BOUNCE_PCT = 8.0


def project_next_quota(
    *,
    state: str,
    quota: int,
    admitted_24h: int,
    bounce_rate: float = 0.0,
    block_rate: float = 0.0,
) -> int:
    """Tomorrow's quota IF the curve keeps going as it is now.

    Mirrors the controller's `ramp_decide`: the quota only grows when the domain
    actually reaches the limit (>= 80% of the quota in the last 24h) AND health is
    green. Underused, braked or cold → unchanged (returns the current quota).

    The ramp limits itself by construction: after a 1.3x step, utilization drops
    to ~77%, below the gate, so the next step requires more real volume.
    """
    if state != "active" or quota <= 0:
        return quota
    # Yellow holds the ramp. Without the event counts here (only the rates are in
    # the row), the projection is conservative on purpose: when in doubt, it does
    # not promise growth.
    if block_rate >= _YELLOW_BLOCK_PCT or bounce_rate >= _YELLOW_BOUNCE_PCT:
        return quota
    if admitted_24h < quota * _RAMP_UTIL_PCT / 100.0:
        return quota
    return max(
        quota,
        min(int(quota * _RAMP_FACTOR), int(admitted_24h * _MAX_STEP_FACTOR), _QUOTA_MAX),
    )


async def domain_health_entry(domain_name: str, rows: list[dict]) -> dict:
    """Health entry of ONE domain for the panel's sending health block."""
    row = match_governed(domain_name, rows)
    if row is None:
        return {"domain": domain_name, "governed": False, "ui_state": "unmanaged",
                "mode": VOLUME_GOVERNOR_MODE}
    governed_key = row["domain_name"]
    quota = int(row.get("daily_quota") or 0)
    consumed = await _consumed_today(governed_key)
    state = row.get("state") or "active"
    # Tomorrow's projection uses the SAME rule as the controller (see project_next_quota).
    # `last_admitted_24h` is the window the controller actually observed, not today's
    # consumption, which resets at midnight and would project a false drop.
    admitted_24h = int(row.get("last_admitted_24h") or 0)
    bounce_rate = float(row.get("last_bounce_rate") or 0)
    block_rate = float(row.get("last_block_rate") or 0)
    projected = project_next_quota(
        state=state,
        quota=quota,
        admitted_24h=admitted_24h,
        bounce_rate=bounce_rate,
        block_rate=block_rate,
    )
    return {
        "domain": domain_name,
        "governed": True,
        "governed_key": governed_key,
        "state": state,
        "ui_state": _UI_STATE.get(state, state),
        "exempt": bool(row.get("exempt")),
        "mode": VOLUME_GOVERNOR_MODE,
        "daily_quota": quota,
        "consumed_today": consumed,
        "remaining_today": max(0, quota - consumed),
        "projected_next_quota": projected,
        # volume of the 24h window the controller observed (basis of the projection);
        # != consumed_today, which is the counter of the current day
        "admitted_24h": admitted_24h,
        "bounce_rate": bounce_rate,
        "block_rate": block_rate,
        "updated_at": str(row.get("updated_at") or ""),
    }


async def health_overview(domain_names: list[str]) -> dict:
    """Aggregated (multi-domain) sending health block of the client."""
    rows = await _governed_rows()
    domains = [await domain_health_entry(d, rows) for d in domain_names]
    return {
        "domains": domains,
        "total_daily_capacity": sum(d.get("daily_quota", 0) for d in domains),
        "mode": VOLUME_GOVERNOR_MODE,
    }


async def quota_history(domain_name: str, days: int = 30) -> list[dict]:
    """Time series of the curve (the "Sent vs domain limit" chart).

    Source: domain_volume_ramp_log, every step (init/ramp/brake/resume/decay/
    cool/rewarm) with the detail JSON written by the controller.
    """
    import json as _json
    row = match_governed(domain_name, await _governed_rows())
    if row is None:
        return []
    try:
        logs = await fetch_all_async(
            "SELECT action, detail, created_at FROM domain_volume_ramp_log "
            "WHERE domain_name = %s AND created_at >= NOW() - INTERVAL %s DAY "
            "ORDER BY created_at ASC",
            (row["domain_name"], min(int(days), 90)),
        )
    except Exception as e:
        logger.error("log_type=ops action=volume_history_failed error=%s", e)
        return []
    out = []
    for r in logs or []:
        try:
            detail = _json.loads(r.get("detail") or "{}")
        except ValueError:
            detail = {}
        out.append({
            "ts": str(r.get("created_at")),
            "action": r.get("action"),
            "quota": detail.get("quota"),
            "admitted_24h": detail.get("admitted_24h"),
            "bounce_rate": detail.get("bounce_rate"),
            "block_rate": detail.get("block_rate"),
        })
    return out


async def evaluate(domain_name: str, total_recipients: int) -> GovernorDecision:
    """Evaluates a send request. Never raises (fail-open)."""
    mode = VOLUME_GOVERNOR_MODE
    if mode not in (MODE_DRYRUN, MODE_ENFORCE) or total_recipients <= 0:
        return GovernorDecision(mode=MODE_OFF, total=total_recipients,
                                admit=total_recipients)

    row = match_governed(domain_name, await _governed_rows())
    if row is None:
        return GovernorDecision(mode=mode, total=total_recipients,
                                admit=total_recipients)

    governed_key = row["domain_name"]
    exempt = bool(row.get("exempt"))
    quota = int(row.get("daily_quota") or 0)
    consumed = await _consumed_today(governed_key)
    bulk = total_recipients > VOLUME_BULK_THRESHOLD

    d = decide_admission(mode=mode, quota=quota, consumed=consumed,
                         total=total_recipients, bulk=bulk, exempt=exempt)
    tomorrow_room = None
    if d["action"] == "defer":
        tomorrow_room = room_on_day(quota=quota, bulk=bulk,
                                    consumed=await _consumed_today(governed_key, 1))
    return GovernorDecision(
        mode=mode, total=total_recipients, admit=d["admit"], defer=d["defer"],
        reject=(d["action"] == "reject"), governed_key=governed_key,
        exempt=exempt, quota=quota, consumed=consumed,
        defer_until_iso=defer_until() if d["defer"] else None,
        would_defer=d["would_defer"], tomorrow_room=tomorrow_room, extra={"bulk": bulk},
    )
