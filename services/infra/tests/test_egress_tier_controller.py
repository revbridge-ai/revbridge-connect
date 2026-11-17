"""Tests for the tier controller's pure decision (upgrade/downgrade by volume)."""

import importlib.util
import os
import sys

_spec = importlib.util.spec_from_file_location(
    "egress_tier_controller",
    os.path.join(os.path.dirname(__file__), "..", "scripts", "egress_tier_controller.py"),
)
tc = importlib.util.module_from_spec(_spec)
sys.modules["egress_tier_controller"] = tc  # dataclasses resolve types through sys.modules
_spec.loader.exec_module(tc)


def test_upgrade_when_shared_and_volume_above_threshold():
    assert tc.decide(
        bound_dedicated=False, avg_up=3500, avg_down=3000, up_daily=3000, down_daily=500
    ) == "upgrade"


def test_no_upgrade_below_threshold():
    assert tc.decide(
        bound_dedicated=False, avg_up=2999, avg_down=2500, up_daily=3000, down_daily=500
    ) is None


def test_downgrade_when_dedicated_and_volume_below_floor():
    assert tc.decide(
        bound_dedicated=True, avg_up=400, avg_down=499, up_daily=3000, down_daily=500
    ) == "downgrade"


def test_hold_when_dedicated_and_volume_healthy():
    # Dedicated, with volume between the floor and the ceiling: stays where it is (no flapping).
    assert tc.decide(
        bound_dedicated=True, avg_up=1000, avg_down=1200, up_daily=3000, down_daily=500
    ) is None


def test_hysteresis_zone_between_thresholds_never_moves():
    # Shared zone with volume between down and up: does not go up (nor down, it is already shared).
    assert tc.decide(
        bound_dedicated=False, avg_up=1500, avg_down=1500, up_daily=3000, down_daily=500
    ) is None


def test_quarantine_fires_on_high_policy_reject_ratio():
    # 2,140 5.7.x rejections vs 1,455 delivered: far above the 30% ratio
    assert tc.should_quarantine(rejected=2140, delivered=1455, min_events=100, max_ratio=0.3)


def test_quarantine_needs_minimum_volume():
    # Nearly idle node: 10 rejections take nothing down, even at a 100% ratio.
    assert not tc.should_quarantine(rejected=10, delivered=0, min_events=100, max_ratio=0.3)


def test_quarantine_ignores_healthy_pool_with_noise():
    # 5% of 5.7.x on a healthy node (occasional spam-folder policy) keeps it in service.
    assert not tc.should_quarantine(rejected=120, delivered=2300, min_events=100, max_ratio=0.3)


def test_upgrade_uses_short_window_downgrade_uses_long():
    # A recent burst (high avg7) upgrades even with a low long-term average...
    assert tc.decide(
        bound_dedicated=False, avg_up=5000, avg_down=100, up_daily=3000, down_daily=500
    ) == "upgrade"
    # ...and the downgrade looks at the LONG average: a 7-day dip does not move the zone
    # while the 14-day average stays above the floor.
    assert tc.decide(
        bound_dedicated=True, avg_up=100, avg_down=800, up_daily=3000, down_daily=500
    ) is None
