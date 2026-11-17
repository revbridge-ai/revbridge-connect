"""Tests for the pure decision of the warm-up controller + budget gate."""

import importlib.util
import os
import sys

_spec = importlib.util.spec_from_file_location(
    "warmup_controller",
    os.path.join(os.path.dirname(__file__), "..", "scripts", "warmup_controller.py"),
)
wc = importlib.util.module_from_spec(_spec)
sys.modules["warmup_controller"] = wc
_spec.loader.exec_module(wc)


def d(**kw):
    base = dict(state="warming", bounce_rate=0.0, block_rate=0.0,
                sent_window=1000, sent_24h=0, budget=5000, consecutive_green=0)
    base.update(kw)
    return wc.decide(**base)


# --- brake (red health) ---
def test_brake_on_high_block_rate():
    r = d(block_rate=20, block_events=8)
    assert r["action"] == "brake" and r["state"] == "braked"


def test_brake_on_high_bounce_rate():
    r = d(bounce_rate=40, bounce_events=50)
    assert r["action"] == "brake"


def test_brake_requires_min_events_anti_noise():
    assert d(block_rate=50, block_events=2, sent_window=4)["action"] != "brake"
    assert d(block_rate=50, block_events=6, sent_window=10)["action"] == "brake"


def test_brake_from_overflow_too():
    r = d(state="overflow", block_rate=15, block_events=9, sent_24h=9000)
    assert r["action"] == "brake"


# --- budget gate (overflow to another route) ---
def test_overflow_when_stressed_and_over_budget():
    # yellow (stressed) + demand above the ceiling -> the excess moves away
    r = d(state="warming", sent_24h=5200, budget=5000, block_rate=5)
    assert r["action"] == "overflow" and r["state"] == "overflow"


def test_green_over_budget_ramps_not_overflow():
    # HEALTHY node above the ceiling: keeps using its own IP, the budget follows (no overflow)
    r = d(state="warming", sent_24h=9158, budget=364, block_rate=0.7)
    assert r["action"] == "ramp" and r["budget"] > 9158


def test_no_overflow_below_budget_ramps():
    r = d(state="warming", sent_24h=1000, budget=5000, block_rate=0)
    assert r["action"] == "ramp" and r["budget"] > 5000


def test_overflow_holds_until_volume_drops():
    # still close to the budget -> stays on the other route
    r = d(state="overflow", sent_24h=4800, budget=5000)
    assert r["action"] == "hold" and r["state"] == "overflow"


def test_overflow_restores_when_volume_drops_below_margin():
    # the 24 h volume dropped below budget*0.85 -> back to the node
    r = d(state="overflow", sent_24h=4000, budget=5000)
    assert r["action"] == "restore" and r["state"] == "warming"


# --- ramp and yellow ---
def test_yellow_holds_budget_flat():
    r = d(block_rate=5, budget=5000, sent_24h=100)
    assert r["action"] == "hold" and r["budget"] == 5000


def test_ramp_respects_max():
    assert d(budget=240000, sent_24h=0)["budget"] <= wc.BUDGET_MAX


def test_insufficient_sample_does_not_move():
    r = d(sent_window=10, budget=3000, sent_24h=100)
    assert r["action"] == "insufficient" and r["budget"] == 3000


# --- recovery from the brake (real green) ---
def test_braked_needs_consecutive_green_to_restore():
    r1 = d(state="braked", consecutive_green=0)
    assert r1["action"] == "hold" and r1["consecutive_green"] == 1
    r2 = d(state="braked", consecutive_green=wc.GREEN_TO_RESTORE - 1)
    assert r2["action"] == "restore" and r2["budget"] == wc.BUDGET_START


def test_braked_without_traffic_does_not_self_restore():
    r = d(state="braked", sent_window=0, consecutive_green=2)
    assert r["action"] == "insufficient" and r["consecutive_green"] == 0


def test_braked_canary_reopens_after_cooldown():
    # no traffic, but the cooldown is over and it is not parked (budget>0) -> a trickle
    r = d(state="braked", sent_window=0, budget=5000, brake_cooldown_over=True)
    assert r["action"] == "restore" and r["budget"] == wc.BUDGET_START


def test_braked_hard_parked_never_canaries():
    # budget=0 = the operator parked it for good: it never reopens on its own
    r = d(state="braked", sent_window=0, budget=0, brake_cooldown_over=True)
    assert r["action"] == "insufficient"


def test_braked_hard_parked_ignores_green_traffic():
    # budget=0 + real green traffic (e.g. draining a backlog) does NOT restore the node
    r = d(state="braked", sent_window=2000, budget=0, block_rate=0, consecutive_green=5)
    assert r["action"] == "insufficient" and r["consecutive_green"] == 0
