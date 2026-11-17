"""Conservative SPF authorization checker for the warm-up splitter's gate.

`spf_authorizes_ip(domain, ip, dns_query)` answers: does the SPF record of `domain` authorize
`ip` to send? It covers the mechanisms that actually authorize sending IPs: ip4, ip6, a, mx,
include, redirect. It ignores all/ptr/exists/exp/macros. WHEN IN DOUBT it returns False
(DNS failure, lookup budget exhausted, unsupported mechanism). It is a safety GATE: an IP
that is not authorized is simply not used by the splitter for that domain, so the message
keeps its normal route (this never causes an SPF fail).

`dns_query(name, rtype)` is injectable (tests run without network). It returns a list of strings:
- TXT -> concatenated TXT records; A/AAAA -> IPs; MX -> hostnames.
A budget of 10 DNS lookups (RFC 7208) is shared across the include/redirect recursion.
"""
from __future__ import annotations

import ipaddress
from collections.abc import Callable

DnsQuery = Callable[[str, str], list[str]]
_MAX_LOOKUPS = 10


def _ip_in(ip_obj, spec: str) -> bool:
    try:
        net = ipaddress.ip_network(spec, strict=False)
        return ip_obj.version == net.version and ip_obj in net
    except ValueError:
        return False


def _spf_record(domain: str, dns_query: DnsQuery) -> str | None:
    try:
        for txt in dns_query(domain, "TXT") or []:
            t = txt.strip().strip('"')
            if t.lower().startswith("v=spf1"):
                return t
    except Exception:
        return None
    return None


def _authorizes(domain: str, ip_obj, dns_query: DnsQuery, budget: list[int]) -> bool:
    if budget[0] <= 0:
        return False
    rec = _spf_record(domain, dns_query)
    if not rec:
        return False

    redirect: str | None = None
    for term in rec.split()[1:]:
        t = term.lstrip("+")  # only PASS counts; -/~/? do not AUTHORIZE
        low = t.lower()

        if low.startswith(("-", "~", "?")):
            continue
        if low.startswith("ip4:") or low.startswith("ip6:"):
            if _ip_in(ip_obj, t.split(":", 1)[1]):
                return True
        elif low == "a" or low.startswith("a:") or low.startswith("a/"):
            budget[0] -= 1
            if budget[0] < 0:
                return False
            target = domain
            cidr = ""
            body = t[2:] if low.startswith("a:") else t[1:]
            if body.startswith(":"):
                body = body[1:]
            if "/" in body:
                host_part, cidr = body.split("/", 1)
                if host_part:
                    target = host_part
            elif body:
                target = body
            try:
                for a in dns_query(target, "A") or []:
                    spec = f"{a}/{cidr}" if cidr else a
                    if _ip_in(ip_obj, spec):
                        return True
            except Exception:
                pass
        elif low == "mx" or low.startswith("mx:"):
            budget[0] -= 1
            if budget[0] < 0:
                return False
            target = t.split(":", 1)[1] if ":" in t else domain
            try:
                for mx in dns_query(target, "MX") or []:
                    budget[0] -= 1
                    if budget[0] < 0:
                        return False
                    for a in dns_query(mx, "A") or []:
                        if _ip_in(ip_obj, a):
                            return True
            except Exception:
                pass
        elif low.startswith("include:"):
            budget[0] -= 1
            if budget[0] < 0:
                return False
            if _authorizes(t.split(":", 1)[1], ip_obj, dns_query, budget):
                return True
        elif low.startswith("redirect="):
            redirect = t.split("=", 1)[1]
        # all / ptr / exists / exp / macros -> ignored (conservative)

    if redirect:
        budget[0] -= 1
        if budget[0] >= 0 and _authorizes(redirect, ip_obj, dns_query, budget):
            return True
    return False


def spf_authorizes_ip(domain: str, ip: str, dns_query: DnsQuery) -> bool:
    """True if the SPF record of `domain` authorizes `ip` (PASS). Conservative: False when in doubt."""
    if not domain or not ip:
        return False
    try:
        ip_obj = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return _authorizes(domain.lower().rstrip("."), ip_obj, dns_query, [_MAX_LOOKUPS])


def authorized_pool_ips(domain: str, pool_ips: list[str], dns_query: DnsQuery) -> set[str]:
    """Subset of pool_ips that the SPF record of `domain` authorizes."""
    return {ip for ip in pool_ips if spf_authorizes_ip(domain, ip, dns_query)}
