"""Tests for the installation helpers: scripts/dns_records.py and scripts/init_manifest.py.

No network, no DNS, no server: `dig` and the Cloudflare API are replaced by fakes.
"""
from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dns = _load("dns_records")
init = _load("init_manifest")

MANIFEST = {
    "install": {"hostname": "mta1.mail.acme.com", "public_ip": "203.0.113.10"},
    "endpoints": {"api": "api.mail.acme.com", "tracking": "t.mail.acme.com", "submission": "smtp.mail.acme.com",
                  "bounce": "bounce.mail.acme.com", "images": "img.mail.acme.com", "panel": "panel.mail.acme.com"},
}
BOOTSTRAP = {"dns": {
    "installation_zone": [
        {"type": "A", "name": "mta1.mail.acme.com", "value": "<NODE-IP>"},
        {"type": "MX", "name": "mta1.mail.acme.com", "value": "10 mta1.mail.acme.com"},
        {"type": "TXT", "name": "mta1.mail.acme.com", "value": "v=spf1 ip4:<NODE-IP> -all"},
        {"type": "TXT", "name": "s1._domainkey.mta1.mail.acme.com", "value": "v=DKIM1; k=rsa; p=" + "A" * 400},
        {"type": "PTR", "name": "203.0.113.10", "value": "mta1.mail.acme.com"},
    ],
    "customer_domain": [
        {"type": "CNAME", "name": "rbm7.acme.com", "value": "mta1.mail.acme.com"},
        {"type": "TXT", "name": "_dmarc.acme.com", "value": "v=DMARC1; p=none; rua=mailto:me@acme.com"},
    ],
}}


def test_find_zone_walks_up_to_the_first_name_with_name_servers():
    answers = {"mta1.mail.acme.com": [], "mail.acme.com": [], "acme.com": ["ada.ns.cloudflare.com.", "bob.ns.cloudflare.com."]}
    zone, ns = dns.find_zone("mta1.mail.acme.com", lambda a: answers.get(a[1], []))
    assert zone == "acme.com"
    assert ns == ["ada.ns.cloudflare.com", "bob.ns.cloudflare.com"]


def test_find_zone_stops_at_a_delegated_subdomain():
    answers = {"mail.acme.com": ["ns1.digitalocean.com."], "acme.com": ["ada.ns.cloudflare.com."]}
    zone, _ = dns.find_zone("mta1.mail.acme.com", lambda a: answers.get(a[1], []))
    assert zone == "mail.acme.com"


def test_detect_provider_by_name_server():
    assert dns.detect_provider(["ada.ns.cloudflare.com"])[0] == "Cloudflare"
    assert dns.detect_provider(["ns-123.awsdns-45.org"])[0] == "Amazon Route 53"
    assert dns.detect_provider(["ns1.example-dns.net"])[0] == "your DNS provider"


def test_collect_puts_the_vps_ip_where_the_bootstrap_left_a_placeholder():
    plan = dns.collect(MANIFEST, BOOTSTRAP)
    recs = {(r["type"], r["name"]): r["value"] for r in plan["records"]}
    assert recs[("A", "mta1.mail.acme.com")] == "203.0.113.10"
    assert recs[("TXT", "mta1.mail.acme.com")] == "v=spf1 ip4:203.0.113.10 -all"
    assert not any("<NODE-IP>" in r["value"] or "<NODE-IP>" in r["name"] for r in plan["records"])
    assert len([r for r in plan["records"] if r["type"] == "A" and r["name"] == "mta1.mail.acme.com"]) == 1
    assert not any(r["type"] == "PTR" for r in plan["records"])  # the PTR is not a zone record
    assert plan["ptr"] == {"ip": "203.0.113.10", "value": "mta1.mail.acme.com"}


def test_collect_before_the_bootstrap_has_only_the_hosts():
    plan = dns.collect(MANIFEST, None)
    assert {r["type"] for r in plan["records"]} == {"A"}
    assert len(plan["records"]) == 7


def test_relative_names():
    assert dns.relative("mta1.mail.acme.com", "acme.com") == "mta1.mail"
    assert dns.relative("acme.com", "acme.com") == "@"
    assert dns.relative("MTA1.mail.acme.com.", "acme.com") == "mta1.mail"


def test_zone_file_splits_long_txt_and_ends_targets_with_a_dot():
    text = dns.zone_file(dns.collect(MANIFEST, BOOTSTRAP)["records"], "acme.com")
    assert "$ORIGIN acme.com." in text
    dkim = [ln for ln in text.splitlines() if ln.startswith("s1._domainkey.mta1.mail")][0]
    assert dkim.count('" "') == 1  # 408 characters: two quoted strings of at most 255
    assert "\tCNAME\tmta1.mail.acme.com." in text
    assert "\tMX\t10 mta1.mail.acme.com." in text


def test_matches_ignores_case_and_trailing_dots_for_names():
    assert dns.matches({"type": "CNAME", "value": "mta1.mail.acme.com"}, ["MTA1.mail.acme.com."])
    assert dns.matches({"type": "MX", "value": "10 mta1.mail.acme.com"}, ["10 mta1.mail.acme.com."])
    assert not dns.matches({"type": "MX", "value": "10 mta1.mail.acme.com"}, ["20 mta1.mail.acme.com."])
    assert dns.matches({"type": "A", "value": "203.0.113.10"}, ["203.0.113.10"])


def test_long_txt_from_dig_is_joined_back():
    rec = {"type": "TXT", "name": "x.acme.com", "value": "v=DKIM1; p=" + "B" * 300}
    chunks = '"' + rec["value"][:255] + '" "' + rec["value"][255:] + '"'
    assert dns.matches(rec, dns.lookup_record(rec, None, lambda a: [chunks]))


def test_plan_changes_creates_skips_and_reports_conflicts_without_touching_anything():
    records = [
        {"type": "A", "name": "api.mail.acme.com", "value": "203.0.113.10"},
        {"type": "A", "name": "t.mail.acme.com", "value": "203.0.113.10"},
        {"type": "TXT", "name": "mta1.mail.acme.com", "value": "v=spf1 ip4:203.0.113.10 -all"},
        {"type": "CNAME", "name": "rbm7.acme.com", "value": "mta1.mail.acme.com"},
        {"type": "TXT", "name": "_dmarc.acme.com", "value": "v=DMARC1; p=none"},
    ]
    existing = [
        {"type": "A", "name": "api.mail.acme.com", "content": "203.0.113.10"},                  # same
        {"type": "A", "name": "t.mail.acme.com", "content": "198.51.100.9"},                    # different A
        {"type": "TXT", "name": "mta1.mail.acme.com", "content": "v=spf1 include:other -all"},  # other SPF
        {"type": "A", "name": "rbm7.acme.com", "content": "198.51.100.9"},                      # CNAME vs A
    ]
    out = dns.plan_changes(records, existing)
    assert [r["name"] for r in out["same"]] == ["api.mail.acme.com"]
    assert sorted(r["name"] for r, _ in out["conflict"]) == ["mta1.mail.acme.com", "rbm7.acme.com", "t.mail.acme.com"]
    assert [r["name"] for r in out["create"]] == ["_dmarc.acme.com"]


def test_already_different_flags_only_names_that_answer_with_another_value():
    recs = [{"type": "TXT", "name": "_dmarc.acme.com", "value": "v=DMARC1; p=none"},
            {"type": "A", "name": "api.mail.acme.com", "value": "203.0.113.10"},
            {"type": "A", "name": "t.mail.acme.com", "value": "203.0.113.10"}]
    answers = {"_dmarc.acme.com": ['"v=DMARC1; p=quarantine"'], "api.mail.acme.com": ["203.0.113.10"]}
    out = dns.already_different(recs, lambda a: answers.get(a[-1], []))
    assert [r["name"] for r, _ in out] == ["_dmarc.acme.com"]


def test_cloudflare_records_are_never_proxied():
    body = dns.cloudflare_body({"type": "A", "name": "api.mail.acme.com", "value": "203.0.113.10"})
    assert body["proxied"] is False
    mx = dns.cloudflare_body({"type": "MX", "name": "mta1.mail.acme.com", "value": "10 mta1.mail.acme.com."})
    assert mx["priority"] == 10 and mx["content"] == "mta1.mail.acme.com" and "proxied" not in mx


def test_group_by_zone_separates_a_sending_domain_in_another_zone():
    zones = {"mail.acme.com": [], "acme.com": ["ns.cloudflare.com."], "other.org": ["ns1.digitalocean.com."]}
    recs = [{"type": "A", "name": "mta1.mail.acme.com", "value": "1.2.3.4"},
            {"type": "CNAME", "name": "rbm7.other.org", "value": "mta1.mail.acme.com"}]
    grouped = dns.group_by_zone(recs, ("acme.com", ["ns.cloudflare.com"]), lambda a: zones.get(a[1], []))
    assert sorted(grouped) == ["acme.com", "other.org"]
    assert [r["name"] for r in grouped["other.org"][1]] == ["rbm7.other.org"]


def test_init_manifest_fills_install_and_endpoints_and_keeps_the_rest():
    text = init.build((ROOT / "connect.toml").read_text(), "mail.acme.com", "203.0.113.10", "me@acme.com")
    data = tomllib.loads(text)
    assert data["install"]["domain"] == "mail.acme.com"
    assert data["install"]["hostname"] == "mta1.mail.acme.com"
    assert data["install"]["public_ip"] == "203.0.113.10"
    assert data["install"]["admin_email"] == "me@acme.com"
    assert data["endpoints"] == {"api": "api.mail.acme.com", "tracking": "t.mail.acme.com",
                                 "submission": "smtp.mail.acme.com", "bounce": "bounce.mail.acme.com",
                                 "images": "img.mail.acme.com", "panel": "panel.mail.acme.com"}
    assert data["mysql"]["user"] == "connect"      # sections it does not own are untouched
    assert "# RevBridge Connect" in text           # comments are kept
    assert "example.com" not in text.split("[mysql]")[0]


def test_init_manifest_rejects_bad_input():
    for argv in (["--ip", "not-an-ip", "--domain", "acme.com", "--email", "a@b.co"],
                 ["--ip", "203.0.113.10", "--domain", "acme", "--email", "a@b.co"],
                 ["--ip", "203.0.113.10", "--domain", "acme.com", "--email", "nobody"]):
        try:
            init.main(argv + ["--out", "/nonexistent/never-written.toml"])
        except SystemExit as exc:
            assert exc.code not in (0, None)
        else:
            raise AssertionError(f"accepted {argv}")


def test_newest_picks_the_most_recent_file():
    import os
    import tempfile
    home = os.environ.get("HOME")
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / ".config" / "revbridge-connect"
        cfg.mkdir(parents=True)
        old, new = cfg / "bootstrap-a.com.json", cfg / "bootstrap-b.com.json"
        old.write_text("{}")
        new.write_text("{}")
        os.utime(old, (1, 1))
        os.environ["HOME"] = tmp
        try:
            assert dns.newest("bootstrap-*.json") == str(new)
            assert dns.newest("connect-*.toml") is None
        finally:
            os.environ["HOME"] = home
