"""RevBridge Connect: DNS records of a sending domain (for the panel's Domains page).

GET /domains/{domain}/dns-info -> domain, virtual_domain_name, zone_name, zone_status,
zone_target and dns_records[]: the records the CLIENT creates in its own domain:
  rbm<n>.<domain>                  CNAME <zone target>               (Return-Path/VERP + SPF)
  rbds1<n>._domainkey.<domain>     CNAME s1._domainkey.<target>      (DKIM)
  rbds2<n>._domainkey.<domain>     CNAME s2._domainkey.<target>      (DKIM, rotation)
  _dmarc.<domain>                  TXT   v=DMARC1; p=none; ...
Only the domain owner (same family as the token's domain) can query it.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from mail_shared.db.mysql_sync import fetch_one_async
from connect_lib.auth.hybrid import get_current_user_hybrid

router = APIRouter(prefix="/domains", tags=["domains"])


def _owns(domain: str, current_user: dict) -> bool:
    from mail_api.shared.domain_validation import get_user_domain_name

    mine = (get_user_domain_name(current_user) or "").strip().lower()
    d = domain.strip().lower()
    return bool(mine) and (d == mine or d.endswith("." + mine))


@router.get("/{domain}/dns-info", summary="DNS records of the sending domain")
async def dns_info(domain: str, current_user: dict = Depends(get_current_user_hybrid)):
    domain = domain.strip().lower()
    if not _owns(domain, current_user):
        raise HTTPException(status_code=404, detail="Domain not found")
    zone = await fetch_one_async("SELECT zone_name, domain, subdomain_lb, subdomain_customer, status "
                                 "FROM zones WHERE zone_name = %s", ("connect",))
    if not zone:
        raise HTTPException(status_code=404, detail="Installation zone not configured (run the bootstrap)")
    target = f"{zone['subdomain_lb']}.{zone['domain']}"
    prefix = zone["subdomain_customer"]
    vd = await fetch_one_async("SELECT name FROM virtual_domains WHERE name = %s", (f"{prefix}.{domain}",))
    records = [
        {"type": "CNAME", "name": f"{prefix}.{domain}", "value": target, "ttl": "3600"},
        {"type": "CNAME", "name": f"{prefix.replace('rbm', 'rbds1')}._domainkey.{domain}",
         "value": f"s1._domainkey.{target}", "ttl": "3600"},
        {"type": "CNAME", "name": f"{prefix.replace('rbm', 'rbds2')}._domainkey.{domain}",
         "value": f"s2._domainkey.{target}", "ttl": "3600"},
        {"type": "TXT", "name": f"_dmarc.{domain}",
         "value": f"v=DMARC1; p=none; rua=mailto:postmaster@{zone['domain']}; aspf=r; adkim=r", "ttl": "3600"},
    ]
    return {
        "domain": domain,
        "virtual_domain_name": vd["name"] if vd else f"{prefix}.{domain}",
        "zone_name": zone["zone_name"],
        "zone_status": zone.get("status"),
        "zone_target": target,
        "dns_records": records,
        # address to register with the mailbox providers' complaint feedback loops (FBL)
        "fbl_address": f"bounces+fbl@{prefix}.{domain}",
    }
