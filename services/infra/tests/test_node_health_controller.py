"""Tests for the pure decision of node_health_controller (RevBridge Connect): blocklist -> quarantine -> back."""

import importlib.util
import os
import socket
import sys

_spec = importlib.util.spec_from_file_location(
    "node_health_controller",
    os.path.join(os.path.dirname(__file__), "..", "scripts", "node_health_controller.py"),
)
nh = importlib.util.module_from_spec(_spec)
sys.modules["node_health_controller"] = nh
_spec.loader.exec_module(nh)


# ───────────── decide(): state machine ─────────────

def test_healthy_clean_stays_healthy():
    assert nh.decide("HEALTHY", False, 0) == ("HEALTHY", 1, "ok")


def test_listed_from_healthy_quarantines_once():
    assert nh.decide("HEALTHY", True, 7) == ("LISTED", 0, "quarantine")
    # already quarantined and still listed: hold, do not move again
    assert nh.decide("LISTED", True, 0) == ("LISTED", 0, "hold")


def test_release_needs_consecutive_clean_checks():
    n = nh.CLEAN_TO_RELEASE
    state, streak = "LISTED", 0
    for _ in range(n - 1):
        state, streak, action = nh.decide(state, False, streak)
        assert (state, action) == ("LISTED", "hold")
    state, streak, action = nh.decide(state, False, streak)
    assert (state, streak, action) == ("HEALTHY", n, "release")


def test_relisting_resets_the_clean_streak():
    state, streak, _ = nh.decide("LISTED", False, 0)
    state, streak, _ = nh.decide(state, True, streak)
    assert (state, streak) == ("LISTED", 0)


def test_disabled_never_changes():
    assert nh.decide("DISABLED", True, 2) == ("DISABLED", 2, "hold")
    assert nh.decide("DISABLED", False, 2) == ("DISABLED", 2, "hold")


def test_new_node_without_state_listed_quarantines():
    # a node with no connect_node_state row enters the controller as HEALTHY
    assert nh.decide("HEALTHY", True, 0)[2] == "quarantine"


# ───────────── dnsbl_listings(): reading the answers ─────────────

def _fake_dns(answers):
    def resolve(name):
        for suffix, ans in answers.items():
            if name.endswith(suffix):
                if isinstance(ans, Exception):
                    raise ans
                return ans
        raise socket.gaierror("NXDOMAIN")
    return resolve


def test_nxdomain_everywhere_is_clean(monkeypatch):
    monkeypatch.setattr(nh, "DNSBL_ZONES", ["zen.spamhaus.org", "bl.spamcop.net"])
    monkeypatch.setattr(nh.socket, "gethostbyname", _fake_dns({}))
    assert nh.dnsbl_listings("8.8.4.4") == ([], [])


def test_127_answer_is_a_listing_and_query_is_reversed(monkeypatch):
    seen = []

    def resolve(name):
        seen.append(name)
        if name.endswith("bl.spamcop.net"):
            return "127.0.0.2"
        raise socket.gaierror("NXDOMAIN")

    monkeypatch.setattr(nh, "DNSBL_ZONES", ["zen.spamhaus.org", "bl.spamcop.net"])
    monkeypatch.setattr(nh.socket, "gethostbyname", resolve)
    listed, unknown = nh.dnsbl_listings("8.8.4.4")
    assert listed == ["bl.spamcop.net=127.0.0.2"] and unknown == []
    assert "4.4.8.8.zen.spamhaus.org" in seen


def test_spamhaus_refusal_is_not_a_listing(monkeypatch):
    # public resolver: Spamhaus answers 127.255.255.254 (query refused), which is not a listing
    monkeypatch.setattr(nh, "DNSBL_ZONES", ["zen.spamhaus.org"])
    monkeypatch.setattr(nh.socket, "gethostbyname", _fake_dns({"zen.spamhaus.org": "127.255.255.254"}))
    assert nh.dnsbl_listings("8.8.4.4") == ([], ["zen.spamhaus.org"])


def test_resolver_error_is_unknown(monkeypatch):
    monkeypatch.setattr(nh, "DNSBL_ZONES", ["psbl.surriel.com"])
    monkeypatch.setattr(nh.socket, "gethostbyname", _fake_dns({"psbl.surriel.com": OSError("timeout")}))
    assert nh.dnsbl_listings("8.8.4.4") == ([], ["psbl.surriel.com"])


def test_private_ip_is_never_queried(monkeypatch):
    def boom(_):
        raise AssertionError("must not query a DNSBL for a private IP")

    monkeypatch.setattr(nh.socket, "gethostbyname", boom)
    assert nh.dnsbl_listings("172.18.0.3") == ([], [])


def test_force_listed_only_for_tests(monkeypatch):
    monkeypatch.setattr(nh, "FORCE_LISTED", {"172.18.0.3"})
    assert nh.dnsbl_listings("172.18.0.3") == (["test:NH_FORCE_LISTED"], [])


# ───────────── node_ip(): where the node's IP comes from ─────────────

def test_control_node_uses_control_ip(monkeypatch):
    monkeypatch.setattr(nh, "CONTROL_IP", "203.0.113.10")
    assert nh.node_ip({"smtp_host": "direct"}) == "203.0.113.10"
    monkeypatch.setattr(nh, "CONTROL_IP", "")
    assert nh.node_ip({"smtp_host": "direct"}) is None


def test_node_with_literal_ip():
    assert nh.node_ip({"smtp_host": "198.51.100.7"}) == "198.51.100.7"
