"""Tests for domain_auth_audit (envelope, DKIM parser, IP coverage), without network."""

import base64
import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "domain_auth_audit.py"
spec = importlib.util.spec_from_file_location("domain_auth_audit", SCRIPT)
daa = importlib.util.module_from_spec(spec)
sys.modules["domain_auth_audit"] = daa
spec.loader.exec_module(daa)


class TestConfigEnvelopeDomain:
    """Mirrors the mail API's _spf_aligned_envelope(). It is NOT the envelope on the wire: it
    is only compared with the milter's to report a disagreement."""

    def test_uses_envelope_prefix_over_smtp_host(self):
        assert daa._config_envelope_domain(
            "news.shop.example", "rbm148776", "smtp.example.net"
        ) == "rbm148776.news.shop.example"

    def test_falls_back_to_second_label_of_smtp_host(self):
        assert daa._config_envelope_domain(
            "news.shop.example", None, "smtp.rbm533320.example.net"
        ) == "rbm533320.news.shop.example"

    def test_no_rbm_anywhere_keeps_from_domain(self):
        assert daa._config_envelope_domain(
            "brand.example", None, "smtp.example.net") == "brand.example"

    def test_from_already_in_envelope_subdomain_is_not_doubled(self):
        assert daa._config_envelope_domain(
            "rbm148776.shop.example", "rbm148776", "smtp.example.net"
        ) == "rbm148776.shop.example"


class TestEnvelopeDomain:
    """The actual envelope comes from virtual_domains (the milter), not from email_configs:
    an UPDATE of envelope_prefix alone does not change it."""

    def test_virtual_domains_wins_over_envelope_prefix(self):
        row = {
            "domain": "news.shop.example",
            "verp_domain": "rbm741531.news.shop.example",
            "envelope_prefix": "rbm148776",          # disagrees on purpose
            "smtp_host": "smtp.example.net",
        }
        assert daa._envelope_domain(row) == "rbm741531.news.shop.example"

    def test_falls_back_to_config_when_no_rbm_row(self):
        row = {
            "domain": "news.shop.example",
            "verp_domain": None,
            "envelope_prefix": None,
            "smtp_host": "smtp.rbm533320.example.net",
        }
        assert daa._envelope_domain(row) == "rbm533320.news.shop.example"

    def test_empty_string_verp_is_treated_as_absent(self):
        row = {
            "domain": "brand.example",
            "verp_domain": "   ",
            "envelope_prefix": None,
            "smtp_host": "smtp.example.net",
        }
        assert daa._envelope_domain(row) == "brand.example"


class TestPublishedP:
    def test_extracts_p_and_strips_whitespace(self):
        txt = ["v=DKIM1; h=sha256; k=rsa; p=AAAB BBB CCC"]
        assert daa._published_p(txt) == "AAABBBBCCC"

    def test_ignores_unrelated_txt_at_same_name(self):
        txt = ["google-site-verification=xyz", "v=DKIM1; k=rsa; p=ZZZ"]
        assert daa._published_p(txt) == "ZZZ"

    def test_two_dkim_records_is_ambiguous_not_first_wins(self):
        # Selector with 2 TXT records: a verifier cannot tell which one to use -> broken.
        txt = ["v=DKIM1; k=rsa; p=AAA", "v=DKIM1; k=rsa; p=BBB"]
        assert daa._published_p(txt) is None

    def test_no_dkim_record(self):
        assert daa._published_p(["google-site-verification=xyz"]) is None


class TestPublicKeyFromPem:
    def test_derives_spki_matching_the_signing_key(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()
        expected = base64.b64encode(
            key.public_key().public_bytes(
                encoding=serialization.Encoding.DER,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        ).decode()
        assert daa._public_key_from_pem(pem) == expected

    def test_garbage_pem_returns_none_instead_of_raising(self):
        assert daa._public_key_from_pem("not a key") is None


class TestRelayCoverage:
    """A smarthost's A record is its SUBMISSION endpoint (often a load balancer outside the
    provider's sending ranges), not the egress IP: comparing only with it would fail a
    correct envelope."""

    def _res(self, a_records, provider_spf):
        # test double: only the two DNS functions _relay_coverage uses
        import domain_auth_audit as m

        class R: ...
        orig_a, orig_spf = m._a_records, m._spf_authorized_ips
        m._a_records = lambda res, name: a_records
        m._spf_authorized_ips = lambda res, d, depth=0: provider_spf
        return m, R(), (orig_a, orig_spf)

    def _restore(self, m, orig):
        m._a_records, m._spf_authorized_ips = orig

    def test_direct_relay_covered_by_a_record(self):
        m, res, orig = self._res({"192.0.2.10"}, (False, set()))
        try:
            assert m._relay_coverage(res, "node2.outbound.example.com",
                                     {"192.0.2.10"}) == "covered"
        finally:
            self._restore(m, orig)

    def test_smarthost_covered_by_provider_spf_intersection(self):
        # A record (submission load balancer) NOT covered, but the envelope includes the
        # same ranges as the provider's SPF -> covered.
        m, res, orig = self._res({"198.51.100.64"}, (True, {"203.0.113.0/24"}))
        try:
            assert m._relay_coverage(res, "smtp.provider.example",
                                     {"203.0.113.0/24", "192.0.2.4"}) == "covered"
        finally:
            self._restore(m, orig)

    def test_uncovered_when_neither_matches(self):
        m, res, orig = self._res({"198.51.100.64"}, (True, {"203.0.113.0/24"}))
        try:
            assert m._relay_coverage(res, "smtp.provider.example", {"192.0.2.4"}) == "uncovered"
        finally:
            self._restore(m, orig)

    def test_unknown_when_no_dns_signal_at_all(self):
        m, res, orig = self._res(set(), (False, set()))
        try:
            assert m._relay_coverage(res, "smtp.example.com", {"192.0.2.4"}) == "unknown"
        finally:
            self._restore(m, orig)


class TestRegistrableTail:
    def test_smarthost(self):
        import domain_auth_audit as m
        assert m._registrable_tail("smtp.provider.example") == "provider.example"

    def test_deep_subdomain(self):
        import domain_auth_audit as m
        assert m._registrable_tail("node2.outbound.example.com") == "example.com"


class TestIpCovered:
    def test_exact_ip(self):
        assert daa._ip_covered("192.0.2.10", {"192.0.2.10", "192.0.2.4"})

    def test_cidr_range(self):
        assert daa._ip_covered("192.0.2.77", {"192.0.2.0/24"})

    def test_uncovered_relay_ip(self):
        # SPF lists only the own nodes, but the message leaves through another relay.
        assert not daa._ip_covered("198.51.100.95", {"192.0.2.10", "192.0.2.11"})

    def test_malformed_term_is_skipped_not_fatal(self):
        assert daa._ip_covered("192.0.2.4", {"not-a-cidr", "192.0.2.4"})
