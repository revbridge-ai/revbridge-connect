"""Tests for the splitter's conservative SPF authorization checker."""
from mail_infra.warmup.spf import authorized_pool_ips, spf_authorizes_ip


def make_dns(zone):
    """zone: {(name, rtype): [records]}. Fake dns_query."""
    def q(name, rtype):
        return zone.get((name.lower().rstrip("."), rtype), [])
    return q


POOL = "192.0.2.119"


def test_ip4_direct_authorizes():
    dns = make_dns({("rbm1.shop.example", "TXT"): [f'v=spf1 ip4:{POOL} -all']})
    assert spf_authorizes_ip("rbm1.shop.example", POOL, dns) is True


def test_ip4_cidr_authorizes():
    dns = make_dns({("d.com", "TXT"): ['v=spf1 ip4:192.0.2.0/24 ~all']})
    assert spf_authorizes_ip("d.com", POOL, dns) is True


def test_ip_not_in_spf_is_false():
    dns = make_dns({("d.com", "TXT"): ['v=spf1 ip4:198.51.100.0/24 -all']})
    assert spf_authorizes_ip("d.com", POOL, dns) is False


def test_no_spf_record_false():
    dns = make_dns({("d.com", "TXT"): ['some other txt']})
    assert spf_authorizes_ip("d.com", POOL, dns) is False


def test_dns_failure_false():
    def boom(name, rtype):
        raise RuntimeError("dns down")
    assert spf_authorizes_ip("d.com", POOL, boom) is False


def test_include_recurses():
    dns = make_dns({
        ("d.com", "TXT"): ['v=spf1 include:_spf.provider.com -all'],
        ("_spf.provider.com", "TXT"): [f'v=spf1 ip4:{POOL} -all'],
    })
    assert spf_authorizes_ip("d.com", POOL, dns) is True


def test_redirect_followed():
    dns = make_dns({
        ("d.com", "TXT"): ['v=spf1 redirect=_spf.d.com'],
        ("_spf.d.com", "TXT"): [f'v=spf1 ip4:{POOL} ~all'],
    })
    assert spf_authorizes_ip("d.com", POOL, dns) is True


def test_a_mechanism():
    dns = make_dns({
        ("d.com", "TXT"): ['v=spf1 a -all'],
        ("d.com", "A"): [POOL],
    })
    assert spf_authorizes_ip("d.com", POOL, dns) is True


def test_mx_mechanism():
    dns = make_dns({
        ("d.com", "TXT"): ['v=spf1 mx -all'],
        ("d.com", "MX"): ["mail.d.com"],
        ("mail.d.com", "A"): [POOL],
    })
    assert spf_authorizes_ip("d.com", POOL, dns) is True


def test_softfail_prefixed_ip_does_not_authorize():
    # ~ip4 / -ip4 are not PASS -> they do not authorize.
    dns = make_dns({("d.com", "TXT"): [f'v=spf1 -ip4:{POOL} ~all']})
    assert spf_authorizes_ip("d.com", POOL, dns) is False


def test_lookup_budget_cap_is_conservative():
    # A self-referencing chain of includes -> exhausts the budget -> False (safe).
    zone = {("d.com", "TXT"): ['v=spf1 include:d1.com -all']}
    for i in range(1, 20):
        zone[(f"d{i}.com", "TXT")] = [f'v=spf1 include:d{i+1}.com -all']
    assert spf_authorizes_ip("d.com", POOL, make_dns(zone)) is False


def test_authorized_pool_ips_filters():
    dns = make_dns({("d.com", "TXT"): ['v=spf1 ip4:192.0.2.119 ip4:198.51.100.4 -all']})
    got = authorized_pool_ips("d.com", ["192.0.2.119", "203.0.113.9", "198.51.100.4"], dns)
    assert got == {"192.0.2.119", "198.51.100.4"}
