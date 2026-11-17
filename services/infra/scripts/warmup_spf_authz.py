#!/usr/bin/env python3
"""Fills `warmup:spf_authz:{domain}`: which pool IPs the SPF record of each envelope domain
(`rbm%`) authorizes to send.

It is the source of the warm-up splitter's SPF GATE: the milter (warmup/loader.spf_authorized)
only routes a message through a pool IP if that IP is in this set. Without this job the gate
is always False (the splitter never routes), which is safe but keeps the IPs cold.
It runs periodically; the splitter turns itself on per sender as the sender's SPF record
starts to include the pool IPs.

Env: SPFA_DRY_RUN=true (default) logs (spfa) without writing to Redis.
     SPFA_TTL_SEC (default 86400), SPFA_DOMAIN_LIKE (default 'rbm%').
The milter reads the set; this job only writes it. Idempotent (replace).
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

DRY_RUN = os.getenv("SPFA_DRY_RUN", "true").lower() in ("1", "true", "yes", "on")
TTL = int(os.getenv("SPFA_TTL_SEC", "86400"))
DOMAIN_LIKE = os.getenv("SPFA_DOMAIN_LIKE", "rbm%")


def emit(event: str, **kw) -> None:
    print(json.dumps({"event": event, **kw}, default=str), flush=True)


def _dns_query(name: str, rtype: str) -> list[str]:
    """Real dns_query (dnspython) for the SPF checker. Failure -> empty list."""
    import dns.resolver
    try:
        ans = dns.resolver.resolve(name, rtype, lifetime=5.0)
    except Exception:
        return []
    out: list[str] = []
    for r in ans:
        if rtype == "TXT":
            strings = getattr(r, "strings", None)
            if strings:
                out.append(b"".join(strings).decode("utf-8", "ignore"))
            else:
                out.append(r.to_text().strip('"'))
        elif rtype == "MX":
            out.append(str(r.exchange).rstrip("."))
        else:
            out.append(r.to_text())
    return out


def main() -> int:
    from mail_infra.infrastructure.database import get_connection
    from mail_infra.warmup.spf import authorized_pool_ips

    with get_connection() as cnx:
        cur = cnx.cursor(dictionary=True)
        cur.execute("SELECT ip FROM poolset_state WHERE in_poolset=1 AND dnsbl_listed=0")
        pool_ips = [row["ip"] for row in cur.fetchall() if row.get("ip")]
        cur.execute(
            "SELECT name FROM revbridge_connect.virtual_domains WHERE name LIKE %s",
            (DOMAIN_LIKE,),
        )
        domains = [row["name"].lower().strip() for row in cur.fetchall() if row.get("name")]

    if not pool_ips:
        emit("no_pool_ips")
        return 0

    r = None
    if not DRY_RUN:
        import redis as redislib
        r = redislib.from_url(os.environ["REDIS_URL"])

    total_authz = 0
    for dom in domains:
        authz = authorized_pool_ips(dom, pool_ips, _dns_query)
        emit("spfa", domain=dom, authorized=sorted(authz), n=len(authz))
        total_authz += len(authz)
        if not DRY_RUN and r is not None:
            key = f"warmup:spf_authz:{dom}"
            pipe = r.pipeline()
            pipe.delete(key)
            if authz:
                pipe.sadd(key, *authz)
                pipe.expire(key, TTL)
            pipe.execute()

    emit("done", domains=len(domains), pool_ips=len(pool_ips),
         total_authz=total_authz, dry_run=DRY_RUN)
    return 0


if __name__ == "__main__":
    sys.exit(main())
