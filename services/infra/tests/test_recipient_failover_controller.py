"""Tests for the pure decision of recipient_failover_controller (recipient domain -> another relay)."""

import importlib.util
import os
import sys

_spec = importlib.util.spec_from_file_location(
    "recipient_failover_controller",
    os.path.join(os.path.dirname(__file__), "..", "scripts", "recipient_failover_controller.py"),
)
rf = importlib.util.module_from_spec(_spec)
sys.modules["recipient_failover_controller"] = rf
_spec.loader.exec_module(rf)


def test_insufficient_volume_never_fails_over():
    # below MIN_EVENTS (20) there is no statistical signal
    d = rf.decide(deferred=5, bounced=5, sent=0, block=10)
    assert d["failing"] is False and d["reason"] == "insufficient"


def test_reputation_block_fails_over():
    # yahoo-like: a high 5.7.x/4.7.x block rate -> another (warmer) IP helps
    d = rf.decide(deferred=874, bounced=3, sent=279, block=874)
    assert d["failing"] is True and d["reason"] == "block"


def test_sustained_deferral_fails_over():
    # comcast-like: 100% sustained deferral (greylisting/rate limit) -> another IP helps
    d = rf.decide(deferred=59, bounced=0, sent=0, block=0)
    assert d["failing"] is True and d["reason"] == "defer"


def test_bad_address_bounces_do_NOT_fail_over():
    # generic bounces (5.1.1/5.4.x), 0 blocks, few deferrals
    # -> no other relay would deliver them either; NO failover.
    d = rf.decide(deferred=0, bounced=69, sent=0, block=0)
    assert d["failing"] is False and d["reason"] == "healthy"


def test_gmail_low_block_stays_on_pool():
    # gmail: high volume, 3.8% blocks and 3.1% deferrals -> do NOT move the whole base
    d = rf.decide(deferred=585, bounced=824, sent=17413, block=720)
    assert d["failing"] is False and d["reason"] == "healthy"
    assert d["block_pct"] < rf.BLOCK_PCT and d["defer_pct"] < rf.DEFER_PCT
