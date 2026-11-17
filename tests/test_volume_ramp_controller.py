"""Tests for the pure decision of the per-domain volume governor.

Covers the curve (ramp gated by utilization and cadence), the immediate red brake, the
inactivity decay and the cold/re-warm cycle.
"""

import importlib.util
import os

_SCRIPT = os.path.join(os.path.dirname(__file__), "..", "services", "infra",
                       "scripts", "volume_ramp_controller.py")
_spec = importlib.util.spec_from_file_location("volume_ramp_controller", _SCRIPT)
vrc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vrc)


def _decide(**kw):
    base = dict(state="active", quota=1000, admitted_24h=0,
                bounce_rate=0.0, block_rate=0.0,
                bounce_events=0, block_events=0,
                idle_days=0, hours_since_step=None)
    base.update(kw)
    return vrc.ramp_decide(**base)


def test_ramp_green_with_usage():
    d = _decide(admitted_24h=900, hours_since_step=25)
    assert d["action"] == "ramp"
    assert d["quota"] == 1300  # 1000 * 1.3


def test_ramp_without_usage_holds():
    # used only 50% of the quota: no reason to go up
    d = _decide(admitted_24h=500, hours_since_step=25)
    assert d["action"] == "hold"
    assert d["quota"] == 1000


def test_cadence_gate_holds_the_step():
    # the job runs hourly; one step only every VR_RAMP_INTERVAL_H
    d = _decide(admitted_24h=1000, hours_since_step=2)
    assert d["action"] == "hold"


def test_step_capped_at_2x_the_real_volume(monkeypatch):
    # with an aggressive RAMP_FACTOR, the ceiling becomes 2x the admitted volume (doubling rule)
    monkeypatch.setattr(vrc, "RAMP_FACTOR", 5.0)
    d = _decide(admitted_24h=900, hours_since_step=25)
    assert d["action"] == "ramp"
    assert d["quota"] == 1800  # min(5000, 900*2)


def test_red_brakes_immediately_even_when_gated():
    d = _decide(admitted_24h=900, hours_since_step=1,
                block_rate=6.0, block_events=50)
    assert d["action"] == "brake"
    assert d["state"] == "braked"
    assert d["quota"] == 500  # half


def test_red_needs_a_minimum_sample():
    # 6% blocks but only 3 events: does not brake (VR_RED_MIN_EVENTS=20)
    d = _decide(admitted_24h=900, block_rate=6.0, block_events=3,
                hours_since_step=25)
    assert d["action"] == "ramp"


def test_yellow_holds_without_braking():
    d = _decide(admitted_24h=900, bounce_rate=9.0, bounce_events=50,
                hours_since_step=25)
    assert d["action"] == "hold"
    assert d["state"] == "active"


def test_braked_resumes_when_the_window_is_clean():
    d = _decide(state="braked", admitted_24h=100)
    assert d["action"] == "resume"
    assert d["state"] == "active"
    assert d["quota"] == 1000  # keeps the braked quota; the ramp restarts from here


def test_brake_never_below_the_floor():
    d = _decide(quota=300, block_rate=6.0, block_events=50)
    assert d["action"] == "brake"
    assert d["quota"] == vrc.QUOTA_MIN


def test_short_idle_holds():
    d = _decide(admitted_24h=0, idle_days=3)
    assert d["action"] == "hold"
    assert d["quota"] == 1000


def test_medium_idle_decays():
    d = _decide(admitted_24h=0, idle_days=10, hours_since_step=25)
    assert d["action"] == "decay"
    assert d["quota"] == 900  # 1000 * 0.9


def test_long_idle_cools_down():
    d = _decide(admitted_24h=0, idle_days=31)
    assert d["action"] == "cool"
    assert d["state"] == "cold"
    assert d["quota"] == vrc.COLD_QUOTA


def test_cold_sending_again_rewarms():
    d = _decide(state="cold", quota=vrc.COLD_QUOTA, admitted_24h=50)
    assert d["action"] == "rewarm"
    assert d["state"] == "active"
    assert d["quota"] == vrc.COLD_QUOTA  # starts again from the bottom of the ladder, grows over the cycles


def test_absolute_ceiling():
    d = _decide(quota=vrc.QUOTA_MAX, admitted_24h=vrc.QUOTA_MAX,
                hours_since_step=25)
    assert d["action"] == "hold"
    assert d["quota"] == vrc.QUOTA_MAX


# --- init_quota: the bootstrap sees the real capacity -------------------------
# A 14-day window is blind to a sender that paused: a high-volume domain that stopped for a
# few weeks would be created at the 200 floor and, with enforcement on, its return would be
# throttled for weeks. The long window fixes that, but giving the FULL peak to a domain idle
# for weeks would be the opposite of a ramp-up, hence the rebuilt decay.

def test_active_domain_starts_with_headroom_over_the_peak():
    assert vrc.init_quota(10_000, idle_days=0) == 12_000  # 1.2x


def test_small_domain_does_not_drop_below_the_floor():
    assert vrc.init_quota(1, idle_days=0) == vrc.QUOTA_MIN


def test_short_pause_is_not_penalized():
    # the decay only starts after IDLE_DECAY_START_D idle days
    assert vrc.init_quota(10_000, idle_days=7) == 12_000


def test_paused_high_volume_sender_neither_floor_nor_full_peak():
    # a 140,000/day peak, idle for 29 days when the governor first sees it
    q = vrc.init_quota(140_000, idle_days=29)
    assert q > vrc.QUOTA_MIN * 50, "must not start at the floor: it would throttle the return"
    assert q < int(140_000 * vrc.INIT_HEADROOM), "nor hand the full peak to a sender that stopped"
    # ~0.9^22 of the peak with headroom
    assert 10_000 < q < 25_000


def test_long_decay_still_respects_the_floor():
    assert vrc.init_quota(5_000, idle_days=365) == vrc.QUOTA_MIN


def test_global_ceiling_respected():
    assert vrc.init_quota(10_000_000, idle_days=0) == vrc.QUOTA_MAX
