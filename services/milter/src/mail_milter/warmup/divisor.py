"""Decision core of the warmup divisor (pure, testable).

Per message, decides whether the send should leave through a pool IP in warmup
(instead of the default relay), to warm the IP up with real, controlled and
SPF-safe volume. It does NO I/O: it receives the candidates already loaded and
vetted by the caller (DB/Redis/SPF) and picks one. That keeps the rule testable
and deterministic.

Rules (ALL must pass to route through a warming IP):
1. The recipient is NOT in the Microsoft family. Microsoft mailboxes are strict
   with new IPs, so warmup never targets them (defense in depth: that traffic
   stays on the default relay, or on whatever recipient_relays routes it to).
2. The recipient is in the tolerant allowlist (the Gmail/Yahoo family by
   default): providers that accept a new IP with small volume without heavy
   penalties.
3. There is at least one candidate IP (built by the caller) that, at once:
   a. has budget left for the day (remaining_budget > 0);
   b. is AUTHORIZED in the SPF of the envelope-from domain (spf_ok); otherwise
      the send fails SPF, which is WORSE than not warming up;
   c. is healthy in the poolset (healthy: in_poolset and not on a DNSBL).

No candidate passes -> no route (the default relay is used). The poolset
controller REMOVES a toxic IP; this module only PUSHES the fraction. The
"fraction" comes from each IP's daily_cap: once the caps are full, the excess
goes to the default relay, with no explicit probability.
"""
from __future__ import annotations

from dataclasses import dataclass

# Microsoft family (Outlook/Hotmail/Live/etc.): never warm up against it.
MS_FAMILY: frozenset[str] = frozenset({
    "hotmail.com", "hotmail.com.br", "hotmail.co.uk", "hotmail.es", "hotmail.fr",
    "outlook.com", "outlook.com.br", "outlook.es", "outlook.fr",
    "live.com", "live.com.br", "live.co.uk",
    "msn.com", "msn.com.br",
    "passport.com", "windowslive.com",
})

# Providers tolerant of new IPs (warmup). Starts conservative with the two
# largest mailbox providers that tolerate a small ramp.
TOLERANT_PROVIDERS: frozenset[str] = frozenset({
    "gmail.com", "googlemail.com",
    "yahoo.com", "yahoo.com.br", "yahoo.com.ar", "yahoo.com.mx",
    "yahoo.es", "yahoo.co.uk", "ymail.com", "rocketmail.com",
})


@dataclass(frozen=True)
class WarmupCandidate:
    """A pool IP that is a candidate for warmup traffic (already vetted by the caller)."""
    ip: str
    relay_host: str          # the relay's smtp_host (e.g. pool-02.mta.example.com)
    relay_id: int
    remaining_budget: int    # daily_cap - sent today (Redis counter)
    spf_ok: bool             # is the IP in the SPF of the envelope-from domain?
    healthy: bool            # in_poolset, not on a DNSBL and FCrDNS ok


@dataclass(frozen=True)
class WarmupDecision:
    route: bool                 # True = route through a warming IP; False = default relay
    ip: str | None
    relay_host: str | None
    relay_id: int | None
    reason: str                 # readable reason (metrics/telemetry)


def _norm(domain: str | None) -> str:
    return (domain or "").strip().lower().rstrip(".")


def is_ms_family(rcpt_domain: str | None, ms_family: frozenset[str] = MS_FAMILY) -> bool:
    return _norm(rcpt_domain) in ms_family


def is_tolerant(rcpt_domain: str | None, tolerant: frozenset[str] = TOLERANT_PROVIDERS) -> bool:
    return _norm(rcpt_domain) in tolerant


def select_warmup_relay(
    *,
    env_from_domain: str | None,
    rcpt_domain: str | None,
    candidates: list[WarmupCandidate],
    tolerant: frozenset[str] = TOLERANT_PROVIDERS,
    ms_family: frozenset[str] = MS_FAMILY,
) -> WarmupDecision:
    """Pick the warmup relay (or none) for (envelope-from, recipient).

    Deterministic: among the eligible candidates, picks the one with the
    LARGEST remaining_budget (favors the IP least used today and spreads the
    load), breaking ties by IP for stable tests.
    """
    env_dom = _norm(env_from_domain)
    if not env_dom:
        return WarmupDecision(False, None, None, None, "no_env_from")

    if is_ms_family(rcpt_domain, ms_family):
        return WarmupDecision(False, None, None, None, "rcpt_microsoft")

    if not is_tolerant(rcpt_domain, tolerant):
        return WarmupDecision(False, None, None, None, "rcpt_not_tolerant")

    eligible = [
        c for c in candidates
        if c.healthy and c.spf_ok and c.remaining_budget > 0
    ]
    if not eligible:
        # Tell the reasons apart for telemetry (why none passed).
        if not candidates:
            reason = "no_warming_ip"
        elif not any(c.spf_ok for c in candidates):
            reason = "spf_not_authorized"
        elif not any(c.remaining_budget > 0 for c in candidates):
            reason = "cap_exhausted"
        else:
            reason = "no_healthy_ip"
        return WarmupDecision(False, None, None, None, reason)

    best = max(eligible, key=lambda c: (c.remaining_budget, c.ip))
    return WarmupDecision(
        route=True,
        ip=best.ip,
        relay_host=best.relay_host,
        relay_id=best.relay_id,
        reason="warmup_routed",
    )
