"""Tests for the pure logic of the volume governor (the ramp-up enforcement on /send).

Covers decide_admission (pass/defer/reject, dryrun, transactional reserve, exempt),
match_governed (exact, subdomain->parent, the most specific key wins) and
split_personalizations (conservative boundary).
"""

from types import SimpleNamespace

from mail_api.services import volume_governor as vg


def _d(**kw):
    base = dict(mode=vg.MODE_ENFORCE, quota=1000, consumed=0, total=100,
                bulk=False, exempt=False)
    base.update(kw)
    return vg.decide_admission(**base)


# --- decide_admission --------------------------------------------------------

def test_within_quota_passes():
    d = _d(total=500)
    assert d["action"] == "pass"
    assert d["admit"] == 500


def test_excess_is_deferred_never_dropped():
    d = _d(consumed=800, total=500)
    assert d["action"] == "defer"
    assert d["admit"] == 200
    assert d["defer"] == 300
    assert d["admit"] + d["defer"] == 500


def test_dryrun_admits_everything_and_measures():
    d = _d(mode=vg.MODE_DRYRUN, consumed=800, total=500)
    assert d["action"] == "pass"
    assert d["admit"] == 500
    assert d["would_defer"] == 300


def test_exempt_is_never_held():
    d = _d(exempt=True, consumed=99999, total=50000)
    assert d["action"] == "pass"
    assert d["admit"] == 50000


def test_off_passes_everything():
    d = _d(mode=vg.MODE_OFF, consumed=99999, total=50000)
    assert d["action"] == "pass"


def test_zero_quota_does_not_govern():
    # quota 0 = a row without a curve yet; do not hold (the bootstrap belongs to the controller)
    d = _d(quota=0, total=500)
    assert d["action"] == "pass"


def test_transactional_reserve_for_bulk():
    # bulk sees 80% of the quota (20% reserved); a small request uses the full quota
    d = _d(bulk=True, consumed=800, total=100)
    assert d["action"] == "defer"  # effective=800, remaining=0
    assert d["defer"] == 100
    d2 = _d(bulk=False, consumed=800, total=100)
    assert d2["action"] == "pass"  # transactional uses the full quota


def test_reject_only_extreme_abuse():
    d = _d(total=10001)  # > 10x quota
    assert d["action"] == "reject"
    d2 = _d(total=10000)  # exactly 10x: defers, does not reject
    assert d2["action"] == "defer"


# --- match_governed ----------------------------------------------------------

ROWS = [
    {"domain_name": "news.shop.example", "exempt": 0, "daily_quota": 100},
    {"domain_name": "rbm183008.m.travel.example", "exempt": 1, "daily_quota": 200},
]


def test_exact_match():
    assert vg.match_governed("rbm183008.m.travel.example", ROWS)["exempt"] == 1


def test_subdomain_match_normalizes_to_the_parent():
    # the envelope subdomain consumes the parent's quota
    r = vg.match_governed("rbm533320.news.shop.example", ROWS)
    assert r["domain_name"] == "news.shop.example"


def test_most_specific_match_wins():
    rows = ROWS + [{"domain_name": "shop.example", "daily_quota": 1}]
    r = vg.match_governed("x.news.shop.example", rows)
    assert r["domain_name"] == "news.shop.example"


def test_no_match_is_not_governed():
    assert vg.match_governed("other-domain.example", ROWS) is None


# --- split_personalizations --------------------------------------------------

def _p(n):
    return SimpleNamespace(to=[SimpleNamespace(address=f"u{i}@x.com") for i in range(n)])


def test_split_at_the_boundary():
    ps = [_p(3), _p(3), _p(3)]
    admitted, deferred = vg.split_personalizations(ps, 6)
    assert sum(len(p.to) for p in admitted) == 6
    assert sum(len(p.to) for p in deferred) == 3


def test_conservative_split_keeps_a_personalization_whole():
    ps = [_p(3), _p(3)]
    admitted, deferred = vg.split_personalizations(ps, 4)
    # the second one would cross the limit: it goes to the deferred part as a whole
    assert sum(len(p.to) for p in admitted) == 3
    assert sum(len(p.to) for p in deferred) == 3


def test_split_nothing_fits():
    ps = [_p(5)]
    admitted, deferred = vg.split_personalizations(ps, 0)
    assert admitted == []
    assert len(deferred) == 1


# --- project_next_quota (the "tomorrow" projection shown on screen) -----------
# The projection must mirror the controller's ramp_decide, including its utilization gate:
# an underused domain must not be promised a 1.3x increase every day.

def _proj(**kw):
    base = dict(state="active", quota=1000, admitted_24h=1000,
                bounce_rate=0.0, block_rate=0.0)
    base.update(kw)
    return vg.project_next_quota(**base)


def test_projection_underused_keeps_the_quota():
    # 63,300 admitted for a quota of 100,000 = 63.3%: below the 80% gate
    assert _proj(quota=100_000, admitted_24h=63_300) == 100_000


def test_projection_at_the_80pct_boundary_grows():
    # 33,040 of 40,000 = 82.6%: above the gate
    assert _proj(quota=40_000, admitted_24h=33_040) == 52_000  # 1.3x


def test_projection_just_below_the_gate_does_not_grow():
    assert _proj(quota=1000, admitted_24h=799) == 1000
    assert _proj(quota=1000, admitted_24h=800) == 1300


def test_projection_capped_at_2x_the_real_volume():
    # inflated quota + low volume that still passes the gate: 2x volume < 1.3x quota
    assert _proj(quota=1000, admitted_24h=900) == 1300  # 1.3x=1300 < 2x=1800
    assert _proj(quota=100, admitted_24h=120) == 130


def test_projection_yellow_health_holds():
    assert _proj(quota=1000, admitted_24h=1000, block_rate=3.0) == 1000
    assert _proj(quota=1000, admitted_24h=1000, bounce_rate=8.0) == 1000
    # green right at the boundary still grows
    assert _proj(quota=1000, admitted_24h=1000, bounce_rate=7.9) == 1300


def test_projection_braked_or_cold_never_grows():
    assert _proj(state="braked", quota=1000, admitted_24h=5000) == 1000
    assert _proj(state="cold", quota=500, admitted_24h=5000) == 500


def test_projection_respects_the_global_ceiling():
    assert _proj(quota=490_000, admitted_24h=490_000) == 500_000


def test_projection_never_shrinks():
    # ramping down belongs to the controller (decay/brake), not to the projection
    assert _proj(quota=1000, admitted_24h=0) == 1000


# --- tomorrow's room (a deferral must fit the next day) -----------------------

def test_room_on_day_uses_the_bulk_share():
    # bulk requests only see the quota minus the transactional reserve (20% by default)
    assert vg.room_on_day(quota=500, consumed=0, bulk=True) == 400
    assert vg.room_on_day(quota=500, consumed=0, bulk=False) == 500


def test_room_on_day_never_negative():
    assert vg.room_on_day(quota=500, consumed=450, bulk=True) == 0
    assert vg.room_on_day(quota=500, consumed=100, bulk=False) == 400


def test_retry_after_points_at_the_next_utc_midnight():
    from datetime import UTC, datetime

    assert vg.seconds_until_next_day(datetime(2026, 11, 9, 23, 0, tzinfo=UTC)) == 3600
    assert vg.seconds_until_next_day(datetime(2026, 11, 9, 0, 0, tzinfo=UTC)) == 86400
