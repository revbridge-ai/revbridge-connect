"""Tests for the pure per-IP ramp decision (warm-up curve + brake)."""

import importlib.util
import os
import sys

_spec = importlib.util.spec_from_file_location(
    "ip_warmup_controller",
    os.path.join(os.path.dirname(__file__), "..", "scripts", "ip_warmup_controller.py"),
)
iw = importlib.util.module_from_spec(_spec)
sys.modules["ip_warmup_controller"] = iw
_spec.loader.exec_module(iw)


def d(**kw):
    base = dict(state="warming", dnsbl_listed=False, cap=iw.CAP_START, sent=1000,
                bounce_rate=0.0, block_rate=0.0, consecutive_clean=0)
    base.update(kw)
    return iw.ramp_decide(**base)


def test_cold_starts():
    r = d(state="cold")
    assert r["action"] == "start" and r["state"] == "warming" and r["cap"] == iw.CAP_START


def test_green_ramps_up():
    r = d(state="warming", cap=100, block_rate=0, sent=1000)
    assert r["action"] == "ramp" and r["cap"] > 100 and r["state"] == "warming"


def test_ramp_promotes_at_full():
    r = d(state="warming", cap=iw.CAP_FULL, sent=1000)
    assert r["action"] == "promote" and r["state"] == "warm" and r["cap"] == iw.CAP_FULL


def test_red_block_pauses():
    r = d(state="warming", block_rate=20, block_events=10)
    assert r["action"] == "pause" and r["state"] == "paused" and r["cap"] == iw.CAP_START


def test_red_bounce_pauses():
    r = d(state="warming", bounce_rate=30, bounce_events=50)
    assert r["action"] == "pause"


def test_dnsbl_pauses():
    r = d(state="warming", dnsbl_listed=True)
    assert r["action"] == "pause" and r["state"] == "paused"


def test_warm_ip_pauses_if_lists():
    # a warm IP that gets listed goes back to warm-up
    r = d(state="warm", cap=iw.CAP_FULL, dnsbl_listed=True)
    assert r["action"] == "pause" and r["state"] == "paused"


def test_red_needs_min_events():
    # a high block rate with few events does NOT pause
    r = d(state="warming", block_rate=50, block_events=2, sent=3)
    assert r["action"] != "pause"


def test_paused_resumes_when_clean():
    r = d(state="paused", dnsbl_listed=False, consecutive_clean=iw.RESUME_CLEAN - 1)
    assert r["action"] == "resume" and r["state"] == "warming"


def test_paused_holds_while_listed():
    r = d(state="paused", dnsbl_listed=True, consecutive_clean=5)
    assert r["action"] == "hold" and r["state"] == "paused"


def test_low_sent_holds():
    r = d(state="warming", sent=5, cap=100)
    assert r["action"] == "hold" and r["cap"] == 100


def test_warm_stays_warm_when_clean():
    r = d(state="warm", cap=iw.CAP_FULL, block_rate=0, sent=5000)
    assert r["action"] == "keep" and r["state"] == "warm"


# --------------------------------------------------------------------------- #
# Cadence gate (1 step / ramp_interval_h): the job runs hourly; without the gate
# the cap would grow 1.3x PER HOUR.
# --------------------------------------------------------------------------- #
def test_ramp_gated_within_interval():
    d = iw.ramp_decide(state="warming", dnsbl_listed=False, cap=65, sent=100,
                        bounce_rate=0.0, block_rate=0.0, consecutive_clean=3,
                        hours_since_ramp=2.0, ramp_interval_h=24)
    assert d["action"] == "hold"
    assert d["cap"] == 65                      # the cap does NOT grow within the interval


def test_ramp_allowed_after_interval():
    d = iw.ramp_decide(state="warming", dnsbl_listed=False, cap=65, sent=100,
                        bounce_rate=0.0, block_rate=0.0, consecutive_clean=3,
                        hours_since_ramp=25.0, ramp_interval_h=24)
    assert d["action"] == "ramp"
    assert d["cap"] == int(65 * 1.3)


def test_ramp_allowed_without_history():
    # None = no step history (e.g. the first run after a deploy) -> may ramp
    d = iw.ramp_decide(state="warming", dnsbl_listed=False, cap=65, sent=100,
                        bounce_rate=0.0, block_rate=0.0, consecutive_clean=3,
                        hours_since_ramp=None)
    assert d["action"] == "ramp"


def test_red_pause_not_gated():
    # the RED brake ignores the gate: it pauses immediately, even right after a ramp.
    # sent is above MIN_RATE_SENT because with a small sample the rate is not a signal and
    # the rate brake does not fire; this test is about the absence of the GATE, not about
    # the sample floor.
    d = iw.ramp_decide(state="warming", dnsbl_listed=False, cap=200,
                        sent=iw.MIN_RATE_SENT + 100,
                        bounce_rate=0.0, block_rate=50.0, consecutive_clean=5,
                        block_events=50, bounce_events=0,
                        hours_since_ramp=0.5, ramp_interval_h=24)
    assert d["action"] == "pause"


# --------------------------------------------------------------------------- #
# Hysteresis + sample floor.
#
# A block rate that hovers right at the threshold must not flip an IP between pause and
# resume every hour, and a resume must not reset the cap or the step clock. These tests
# pin the three rules (dead band, sample floor, halved cap); reverting any of them brings
# the oscillation back.
# --------------------------------------------------------------------------- #
def test_rate_on_a_small_sample_does_not_pause():
    """A small sample with a high rate does NOT pause: below the floor the rate measures
    the denominator, not the reputation."""
    # hours_since_ramp=2 holds the step, isolating the one question of this test:
    # a small sample with a high rate must NOT pause.
    d = iw.ramp_decide(state="warming", dnsbl_listed=False, cap=200,
                       sent=iw.MIN_RATE_SENT - 1, bounce_rate=0.0, block_rate=18.0,
                       consecutive_clean=2, block_events=50, bounce_events=0,
                       hours_since_ramp=2.0, ramp_interval_h=24)
    assert d["action"] == "hold"
    assert d["cap"] == 200                    # neither pauses nor throws away the curve


def test_rate_on_a_sufficient_sample_pauses():
    d = iw.ramp_decide(state="warming", dnsbl_listed=False, cap=200,
                       sent=iw.MIN_RATE_SENT, bounce_rate=0.0, block_rate=18.0,
                       consecutive_clean=2, block_events=50, bounce_events=0)
    assert d["action"] == "pause"


def test_dnsbl_pauses_even_on_a_small_sample():
    """A listing is a fact, not a statistic: it does not go through the sample floor."""
    d = iw.ramp_decide(state="warming", dnsbl_listed=True, cap=200, sent=1,
                       bounce_rate=0.0, block_rate=0.0, consecutive_clean=0)
    assert d["action"] == "pause"


def test_pause_halves_the_cap_instead_of_zeroing():
    """Resetting the cap would throw away weeks of the curve on every oscillation."""
    d = iw.ramp_decide(state="warming", dnsbl_listed=False, cap=1000, sent=1000,
                       bounce_rate=0.0, block_rate=50.0, consecutive_clean=5,
                       block_events=50, bounce_events=0)
    assert d["action"] == "pause"
    assert d["cap"] == 500


def test_pause_never_goes_below_the_initial_cap():
    d = iw.ramp_decide(state="warming", dnsbl_listed=False, cap=iw.CAP_START,
                       sent=1000, bounce_rate=0.0, block_rate=50.0,
                       consecutive_clean=0, block_events=50, bounce_events=0)
    assert d["cap"] == iw.CAP_START


def test_paused_does_not_resume_in_the_dead_band():
    """4% is below the pause threshold (5) but above the resume threshold (3): this is
    exactly the band where an hourly pause<->resume would start."""
    d = iw.ramp_decide(state="paused", dnsbl_listed=False, cap=300, sent=1000,
                       bounce_rate=0.0, block_rate=4.0, consecutive_clean=5)
    assert d["action"] == "hold"
    assert d["state"] == "paused"


def test_paused_needs_consecutive_clean_runs():
    d = iw.ramp_decide(state="paused", dnsbl_listed=False, cap=300, sent=1000,
                       bounce_rate=0.0, block_rate=0.5,
                       consecutive_clean=iw.RESUME_CLEAN - 2)
    assert d["action"] == "hold"


def test_paused_resumes_clean_and_keeps_the_cap():
    d = iw.ramp_decide(state="paused", dnsbl_listed=False, cap=300, sent=1000,
                       bounce_rate=0.0, block_rate=0.5,
                       consecutive_clean=iw.RESUME_CLEAN - 1)
    assert d["action"] == "resume"
    assert d["cap"] == 300                    # does NOT go back to CAP_START


def test_streak_resets_when_leaving_the_clean_band():
    d = iw.ramp_decide(state="paused", dnsbl_listed=False, cap=300, sent=1000,
                       bounce_rate=0.0, block_rate=4.0, consecutive_clean=9)
    assert d["consecutive_clean"] == 0
