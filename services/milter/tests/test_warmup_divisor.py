"""Tests for the decision core of the warmup divisor (pure)."""
from mail_milter.warmup.divisor import (
    WarmupCandidate,
    is_ms_family,
    is_tolerant,
    select_warmup_relay,
)


def _cand(ip="203.0.113.119", host="pool-02.mta.example.com", rid=18,
          budget=100, spf_ok=True, healthy=True):
    return WarmupCandidate(
        ip=ip, relay_host=host, relay_id=rid,
        remaining_budget=budget, spf_ok=spf_ok, healthy=healthy,
    )


# --------------------------------------------------------------------------- #
# Recipient eligibility
# --------------------------------------------------------------------------- #
def test_ms_family_detection():
    assert is_ms_family("outlook.com")
    assert is_ms_family("Hotmail.COM.BR")   # case-normalized
    assert is_ms_family("live.com")
    assert not is_ms_family("gmail.com")


def test_tolerant_detection():
    assert is_tolerant("gmail.com")
    assert is_tolerant("YAHOO.COM.BR")
    assert not is_tolerant("outlook.com")
    assert not is_tolerant("corp.example.com")


# --------------------------------------------------------------------------- #
# select_warmup_relay - rules
# --------------------------------------------------------------------------- #
def test_routes_when_all_conditions_pass():
    d = select_warmup_relay(
        env_from_domain="rbm268811.shop.example.com",
        rcpt_domain="gmail.com",
        candidates=[_cand()],
    )
    assert d.route is True
    assert d.ip == "203.0.113.119"
    assert d.reason == "warmup_routed"


def test_microsoft_recipient_never_warms():
    d = select_warmup_relay(
        env_from_domain="rbm1.x.com.br", rcpt_domain="outlook.com",
        candidates=[_cand()],
    )
    assert d.route is False
    assert d.reason == "rcpt_microsoft"


def test_non_tolerant_recipient_skipped():
    d = select_warmup_relay(
        env_from_domain="rbm1.x.com.br", rcpt_domain="company.example.com",
        candidates=[_cand()],
    )
    assert d.route is False
    assert d.reason == "rcpt_not_tolerant"


def test_spf_gate_blocks_unauthorized_ip():
    # The IP exists and has budget, but is NOT in the envelope-from's SPF -> no routing.
    d = select_warmup_relay(
        env_from_domain="rbm999.newsender.example.com", rcpt_domain="gmail.com",
        candidates=[_cand(spf_ok=False)],
    )
    assert d.route is False
    assert d.reason == "spf_not_authorized"


def test_cap_exhausted_overflows_to_default():
    d = select_warmup_relay(
        env_from_domain="rbm1.x.com.br", rcpt_domain="gmail.com",
        candidates=[_cand(budget=0)],
    )
    assert d.route is False
    assert d.reason == "cap_exhausted"


def test_unhealthy_ip_not_used():
    d = select_warmup_relay(
        env_from_domain="rbm1.x.com.br", rcpt_domain="gmail.com",
        candidates=[_cand(healthy=False)],
    )
    assert d.route is False
    assert d.reason == "no_healthy_ip"


def test_no_candidates():
    d = select_warmup_relay(
        env_from_domain="rbm1.x.com.br", rcpt_domain="gmail.com", candidates=[],
    )
    assert d.route is False
    assert d.reason == "no_warming_ip"


def test_no_env_from():
    d = select_warmup_relay(
        env_from_domain="", rcpt_domain="gmail.com", candidates=[_cand()],
    )
    assert d.route is False
    assert d.reason == "no_env_from"


def test_picks_ip_with_most_remaining_budget():
    # Spreads the load: picks the IP least used today (largest remaining budget).
    cands = [
        _cand(ip="192.0.2.1", host="pool-02.mta.example.com", rid=18, budget=10),
        _cand(ip="192.0.2.2", host="pool-03.mta.example.com", rid=19, budget=90),
        _cand(ip="192.0.2.3", host="pool-04.mta.example.com", rid=20, budget=50),
    ]
    d = select_warmup_relay(
        env_from_domain="rbm1.x.com.br", rcpt_domain="gmail.com", candidates=cands,
    )
    assert d.ip == "192.0.2.2"
    assert d.relay_id == 19


def test_only_spf_ok_and_healthy_and_budget_are_eligible():
    cands = [
        _cand(ip="192.0.2.1", budget=999, spf_ok=False),   # no SPF
        _cand(ip="192.0.2.2", budget=999, healthy=False),  # unhealthy
        _cand(ip="192.0.2.3", budget=0),                   # no budget
        _cand(ip="192.0.2.4", budget=5),                   # OK (the only eligible one)
    ]
    d = select_warmup_relay(
        env_from_domain="rbm1.x.com.br", rcpt_domain="gmail.com", candidates=cands,
    )
    assert d.route is True
    assert d.ip == "192.0.2.4"
