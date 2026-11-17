#!/usr/bin/env python3
"""domain_auth_audit.py — audits the published authentication of every sending domain (READ-ONLY).

A domain can be fully "provisioned" and still send with NO aligned identifier, and nothing on
the sending path complains: SMTP accepts the message, send_ledger marks it sent, dashboards
stay green, and the message lands in the recipient's spam folder (for example, when a
per-domain DKIM key was generated during provisioning but never published).

For every active email_config it checks the three facts that sending actually depends on:

  DKIM   — the key OpenDKIM signs with is `dkim_keys.private_key_pem` (the Lua KeyTable looks
           it up inline in MySQL by cluster_id+selector+dkim_domain). Here the public key is
           DERIVED from that PEM and compared with what DNS returns for
           `<selector>._domainkey.<domain>`, following CNAMEs, which is how the delegation is
           built. "The record exists" is NOT a check: a record can exist and point to the
           wrong key.

  SPF    — the Return-Path is not the From domain: `_spf_aligned_envelope()` changes the
           MAIL FROM to `<local>@<rbm>.<domain>`, where <rbm> comes from
           `email_configs.envelope_prefix` (fallback: the 2nd label of smtp_host). SPF is
           looked up on THAT subdomain. Having no SPF on `<domain>` is normal and expected;
           what breaks delivery is a missing envelope subdomain.

  MX     — the same envelope subdomain receives the VERP bounces. Without an MX every bounce
           is lost and suppression never learns.

  RELAY  — SPF is useless if it does not authorize the relay the message actually leaves
           through. The relay is read from the database (the domain's zone_relays row, or
           else relays.is_default=1) and SPF is resolved by EXPANDING the includes, never by
           substring: an `include:` that does not cover the relay looks right in a grep and
           fails the recipient's check.

Prints one JSON line per domain (`domain_auth_health`), in the same format as group_health.py,
so a log pipeline can turn it into metrics and alerts. It fixes nothing and never writes to
the database.

Env:
  AUDIT_ONLY_BROKEN   "1" (default) prints only what is broken; "0" prints everything
  AUDIT_DNS_TIMEOUT   timeout per DNS query, in seconds (default: 5)
  AUDIT_RESOLVERS     comma-separated resolvers (default: 1.1.1.1,8.8.8.8)
  DB_*                MySQL connection (same as mail_infra)
"""
from __future__ import annotations

import base64
import json
import logging
import os
import sys

os.environ.setdefault("OTEL_SDK_DISABLED", "true")

ONLY_BROKEN = os.getenv("AUDIT_ONLY_BROKEN", "1") == "1"
DNS_TIMEOUT = float(os.getenv("AUDIT_DNS_TIMEOUT", "5"))
RESOLVERS = [r.strip() for r in os.getenv("AUDIT_RESOLVERS", "1.1.1.1,8.8.8.8").split(",") if r.strip()]
# RFC 7208 §4.6.4 allows 10 lookups; stopping earlier avoids loops in malformed includes.
SPF_MAX_DEPTH = 5

LOG = logging.getLogger("domain_auth_audit")


# --------------------------------------------------------------------------- DNS
def _resolver():
    import dns.resolver

    r = dns.resolver.Resolver(configure=False)
    r.nameservers = RESOLVERS
    r.timeout = DNS_TIMEOUT
    r.lifetime = DNS_TIMEOUT
    return r


def _txt(res, name: str) -> list[str]:
    """TXT records of `name`, joining the strings of each record (DKIM exceeds 255)."""
    import dns.resolver

    try:
        answer = res.resolve(name, "TXT")
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers):
        return []
    except Exception as exc:  # timeout etc.: indistinguishable from "does not exist", so assert nothing
        LOG.warning("TXT %s: %s", name, exc)
        return []
    out = []
    for rdata in answer:
        out.append(b"".join(rdata.strings).decode("utf-8", "replace"))
    return out


def _has_mx(res, name: str) -> bool:
    import dns.resolver

    try:
        return len(res.resolve(name, "MX")) > 0
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers):
        return False
    except Exception as exc:
        LOG.warning("MX %s: %s", name, exc)
        return False


def _a_records(res, name: str) -> set[str]:
    import dns.resolver

    try:
        return {r.address for r in res.resolve(name, "A")}
    except Exception:
        return set()


def _spf_authorized_ips(res, domain: str, depth: int = 0) -> tuple[bool, set[str]]:
    """(has_spf, authorized_ips): expands include:/redirect= recursively.

    Only literal ip4/ip6 terms are collected: those can be checked deterministically
    against the relay's IP. `a`/`mx`/`ptr` are ignored on purpose: covering them would
    require resolving the context of each one and would produce false "covered" results.
    """
    if depth > SPF_MAX_DEPTH:
        return True, set()

    spf = next((t for t in _txt(res, domain) if t.lower().startswith("v=spf1")), None)
    if spf is None:
        return False, set()

    ips: set[str] = set()
    for term in spf.split():
        low = term.lower()
        if low.startswith("ip4:") or low.startswith("ip6:"):
            ips.add(term.split(":", 1)[1].strip())
        elif low.startswith("include:"):
            _, sub = _spf_authorized_ips(res, term.split(":", 1)[1].strip(), depth + 1)
            ips |= sub
        elif low.startswith("redirect="):
            _, sub = _spf_authorized_ips(res, term.split("=", 1)[1].strip(), depth + 1)
            ips |= sub
    return True, ips


def _ip_covered(ip: str, authorized: set[str]) -> bool:
    """`ip` falls within some ip4/ip6 term (CIDR accepted)."""
    import ipaddress

    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for term in authorized:
        try:
            if addr in ipaddress.ip_network(term, strict=False):
                return True
        except ValueError:
            continue
    return False


def _registrable_tail(host: str) -> str:
    """Approximation of the registrable domain of smtp_host (the last 2 labels).

    Good enough for typical relay hostnames (smtp.provider.example -> provider.example,
    node1.outbound.example.com -> example.com); no Public Suffix List is needed here.
    """
    parts = host.rstrip(".").split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _relay_coverage(res, relay_host: str, authorized: set[str]) -> str:
    """"covered"/"uncovered"/"unknown": does the envelope's SPF authorize the relay's EGRESS?

    Two modes, because "the relay's IP" is ambiguous:
    - Direct relay (an own node): the A record of smtp_host IS the IP that delivers at the
      far end, so it only has to be covered.
    - Smarthost (a provider's relay): the A record is just the SUBMISSION endpoint (a load
      balancer), and delivery leaves from the ranges published in the PROVIDER's SPF. The
      right check is the intersection between the envelope's terms and the provider's;
      comparing with the A record would fail a perfectly correct envelope (a false
      "uncovered").
    """
    relay_ips = _a_records(res, relay_host)
    if relay_ips and any(_ip_covered(ip, authorized) for ip in relay_ips):
        return "covered"

    provider = _registrable_tail(relay_host)
    has_provider_spf, provider_terms = _spf_authorized_ips(res, provider)
    if has_provider_spf and provider_terms and provider_terms & authorized:
        return "covered"

    if not relay_ips and not has_provider_spf:
        return "unknown"
    return "uncovered"


# --------------------------------------------------------------------------- DKIM
def _public_key_from_pem(pem: str) -> str | None:
    """`p=` of the DKIM TXT record, derived from the SAME private key OpenDKIM uses."""
    from cryptography.hazmat.primitives import serialization

    try:
        key = serialization.load_pem_private_key(pem.encode(), password=None)
        der = key.public_key().public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        return base64.b64encode(der).decode()
    except Exception as exc:
        LOG.warning("could not derive the public key: %s", exc)
        return None


def _published_p(txts: list[str]) -> str | None:
    """Published `p=`. More than one DKIM TXT record at the same name is ambiguous, so it is treated as broken."""
    dkim = [t for t in txts if "p=" in t and "k=rsa" in t.lower().replace(" ", "")]
    if len(dkim) != 1:
        return None
    for tag in dkim[0].split(";"):
        tag = tag.strip()
        if tag.startswith("p="):
            return tag[2:].strip().replace(" ", "")
    return None


# --------------------------------------------------------------------------- DB
def _configs(cnx) -> list[dict]:
    """Active email_configs + domain + DKIM identity/key + effective relay + actual VERP domain."""
    cur = cnx.cursor(dictionary=True)
    cur.execute(
        """
        SELECT vd.name                AS domain,
               ec.envelope_prefix     AS envelope_prefix,
               ec.smtp_host           AS smtp_host,
               -- SAME lookup as the milter (get_rbm_domain_for_api_key): this is where the
               -- actual Return-Path comes from, and it is ALPHABETICAL: if there are two rbm
               -- rows for the same domain, the milter uses this one and ignores the other.
               -- The NOT LIKE mirrors the envelope rule: only a DIRECT child (1 label).
               -- Without it the `%` crosses dots and a PARENT domain inherits a child's rbm.
               -- Keep both sides in sync, or the audit reports false positives/negatives
               -- about the envelope the milter actually uses.
               (SELECT rbm.name FROM virtual_domains rbm
                 WHERE rbm.name LIKE 'rbm%'
                   AND rbm.name LIKE CONCAT('%.', vd.name)
                   AND rbm.name NOT LIKE CONCAT('%.%.', vd.name)
                 ORDER BY rbm.name ASC LIMIT 1) AS verp_domain,
               di.selector            AS selector,
               di.dkim_domain         AS dkim_domain,
               dk.private_key_pem     AS private_key_pem,
               COALESCE(zr_relay.smtp_host, def_relay.smtp_host) AS relay_host,
               COALESCE(zr_relay.name,      def_relay.name)      AS relay_name
        FROM email_configs ec
        JOIN virtual_domains vd ON vd.id = ec.domain_id
        LEFT JOIN dkim_identities di
               ON di.from_domain = vd.name AND di.cluster_id = 'default'
        LEFT JOIN dkim_keys dk
               ON dk.cluster_id = di.cluster_id
              AND dk.selector   = di.selector
              AND dk.dkim_domain = di.dkim_domain
        LEFT JOIN zone_relays zr ON zr.zone_domain = vd.name
        LEFT JOIN relays zr_relay ON zr_relay.id = zr.relay_id
        LEFT JOIN relays def_relay ON def_relay.is_default = 1
        WHERE ec.is_active = 1
        ORDER BY vd.name
        """
    )
    return list(cur.fetchall() or [])


def _config_envelope_domain(domain: str, envelope_prefix: str | None, smtp_host: str | None) -> str:
    """What the mail API's `_spf_aligned_envelope()` would produce.

    This is NOT the envelope that goes on the wire: the milter overrides it later with
    `chgfrom()`. It is only used to detect a disagreement between the two sources.
    """
    prefix = (envelope_prefix or "").strip()
    if not prefix:
        parts = (smtp_host or "").split(".")
        if len(parts) >= 3:
            prefix = parts[1]  # smtp.rbm123456.example.com -> rbm123456
    if not prefix.startswith("rbm") or domain.startswith(prefix + "."):
        return domain
    return f"{prefix}.{domain}"


def _envelope_domain(row: dict) -> str:
    """The Return-Path domain that ACTUALLY goes out: this is where SPF is looked up.

    The source is `virtual_domains` (the `rbm%.<domain>` row), because that is where the
    milter builds the VERP address `bounces+<rcpt>@<...>` in eom() and applies it with
    `chgfrom()`. The `email_configs.envelope_prefix` column does NOT decide here: changing it
    does not change the envelope, because the milter does. Falls back to the mail API's
    derivation only when there is no rbm row.
    """
    verp = (row.get("verp_domain") or "").strip()
    if verp:
        return verp
    return _config_envelope_domain(
        row["domain"], row.get("envelope_prefix"), row.get("smtp_host")
    )


# --------------------------------------------------------------------------- main
def main() -> int:
    # JSON on stdout, like group_health.py: plain-text logs on stderr would be read as
    # errors by log collectors and raise false alerts on every run.
    from connect_lib.logging import setup_logging
    setup_logging(service_name="mail-infra-domain-auth-audit", json_format=True)
    from mail_infra.infrastructure.database import get_connection

    res = _resolver()
    broken = 0
    total = 0

    with get_connection() as cnx:
        rows = _configs(cnx)

    for row in rows:
        total += 1
        domain = row["domain"]
        selector = row["selector"]
        reasons: list[str] = []

        # ---- DKIM: is the published key the one that signs?
        if not selector or not row["dkim_domain"]:
            dkim = "no_identity"
            reasons.append("no dkim_identities row: nothing signs this domain")
        elif not row["private_key_pem"]:
            dkim = "no_key"
            reasons.append(f"dkim_identities points to {selector}.{row['dkim_domain']}, which has no key")
        else:
            expected = _public_key_from_pem(row["private_key_pem"])
            published = _published_p(_txt(res, f"{selector}._domainkey.{domain}"))
            if expected is None:
                dkim = "unreadable_key"
                reasons.append("private_key_pem is unreadable")
            elif published is None:
                dkim = "missing"
                reasons.append(f"{selector}._domainkey.{domain} has no single DKIM TXT record")
            elif published != expected:
                dkim = "mismatch"
                reasons.append(
                    f"{selector}._domainkey.{domain} publishes a key that is NOT the signing key"
                )
            else:
                dkim = "ok"

        # ---- SPF/MX: on the envelope domain (the milter's), not on the From domain
        env_domain = _envelope_domain(row)
        if not row.get("verp_domain"):
            reasons.append(
                "no rbm row in virtual_domains: the milter builds no VERP address for this domain"
            )
        else:
            # Deliberately redundant guard: the query above already excludes grandchildren
            # (NOT LIKE '%.%.<domain>'), like the milter. If this branch fires, the two sides
            # are out of sync and a domain may be using another domain's envelope.
            if env_domain.count(".") != domain.count(".") + 1:
                reasons.append(
                    f"VERP {env_domain} is not rbm<id>.{domain}: the milter matches by suffix "
                    f"and picked another domain's rbm row"
                )
        # Do the two rbm sources disagree? Reported, but NOT counted as broken: the milter's
        # value is the one that applies, and if its envelope is healthy the message
        # authenticates. Counting it as broken would flag most domains and turn the alert
        # into noise. The field warns that changing envelope_prefix does not change what goes
        # on the wire.
        cfg_env = _config_envelope_domain(
            domain, row.get("envelope_prefix"), row.get("smtp_host")
        )
        envelope_prefix_stale = bool(row.get("verp_domain")) and cfg_env != env_domain

        has_spf, authorized = _spf_authorized_ips(res, env_domain)
        spf = "ok" if has_spf else "missing"
        if not has_spf:
            reasons.append(f"envelope {env_domain} has no SPF: the Return-Path does not authenticate")

        mx = "ok" if _has_mx(res, env_domain) else "missing"
        if mx == "missing":
            reasons.append(f"envelope {env_domain} has no MX: VERP bounces are lost")

        # ---- does SPF cover the relay the message actually leaves through?
        relay_host = row["relay_host"]
        if not has_spf or not relay_host:
            relay = "unknown"
        else:
            relay = _relay_coverage(res, relay_host, authorized)
            if relay == "uncovered":
                reasons.append(
                    f"SPF of {env_domain} does not authorize relay {row['relay_name']} ({relay_host})"
                )

        status = "ok" if not reasons else "broken"
        if status == "broken":
            broken += 1
        elif ONLY_BROKEN and not envelope_prefix_stale:
            # `stale` is not a failure, but it would disappear when filtering by broken only,
            # and it is what warns that `envelope_prefix` misrepresents the real envelope.
            continue

        print(json.dumps({
            "event": "domain_auth_health", "domain": domain, "status": status,
            "selector": selector, "dkim": dkim,
            "envelope_domain": env_domain,
            "envelope_source": "milter" if row.get("verp_domain") else "email_configs",
            "envelope_prefix_stale": envelope_prefix_stale,
            "envelope_prefix_config": cfg_env if envelope_prefix_stale else None,
            "spf": spf, "mx": mx,
            "relay": relay, "relay_name": row["relay_name"], "relay_host": relay_host,
            "reasons": reasons,
        }, ensure_ascii=False), flush=True)

        if status == "broken":
            LOG.warning("%s: %s", domain, "; ".join(reasons))

    LOG.info("domain_auth_audit done; domains=%d broken=%d", total, broken)
    return 0


if __name__ == "__main__":
    sys.exit(main())
