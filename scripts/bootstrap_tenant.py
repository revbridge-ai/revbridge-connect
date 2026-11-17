"""Creates (or reuses) the installation's zone and a tenant that is ready to send.

Runs INSIDE the `api` container (it uses Connect's own database and libraries):

    docker compose exec api python scripts/bootstrap_tenant.py \
        --domain shop.example --customer "Example Shop" --admin-email you@shop.example

Zone model:
  - The installation is one zone: the target `<hostname label>.<installation domain>` holds
    A/MX/SPF and the DKIM keys s1/s2. Adding nodes only changes the zone's DNS.
  - Each customer domain only creates CNAMEs pointing to the zone:
      rbm<n>.<domain>                   -> <zone target>               (Return-Path/VERP + SPF)
      rbds1<n>._domainkey.<domain>      -> s1._domainkey.<zone target> (DKIM)
      rbds2<n>._domainkey.<domain>      -> s2._domainkey.<zone target> (DKIM, rotation)
    plus the DMARC TXT record.
Idempotent: running it again reuses the zone, customer, domain and user. It always creates a
NEW API key (the key only appears in the output, once).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import sys
import uuid

from mail_shared.db.mysql_sync import fetch_one, get_conn
from connect_lib.user.utils import get_password_hash

from mail_api.services.dkim_key_service import generate_dkim_keypair, public_key_from_private_pem

CLUSTER_ID = "default"  # the same CLUSTER_ID as OpenDKIM's Lua functions (keytable/signingtable)


def insert(sql: str, params: tuple) -> int:
    """Runs an INSERT and returns the generated id (get_conn is a context manager that closes/returns the connection to the pool)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            conn.commit()
            return int(cur.lastrowid or 0)


def run(sql: str, params: tuple) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            conn.commit()


# ───────────────────────── installation zone ─────────────────────────
def ensure_zone(install_domain: str, hostname: str) -> dict:
    label = hostname.split(".", 1)[0]
    zone = fetch_one("SELECT * FROM zones WHERE zone_name = %s", ("connect",))
    if not zone:
        prefix = f"rbm{secrets.randbelow(900000) + 100000}"
        insert(
            "INSERT INTO zones (provider, zone_name, domain, subdomain_lb, subdomain_customer, status) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            ("connect", "connect", install_domain, label, prefix, "active"),
        )
        zone = fetch_one("SELECT * FROM zones WHERE zone_name = %s", ("connect",))
    target = f"{zone['subdomain_lb']}.{zone['domain']}"
    # the control node is also a sending node: relay 'direct' (direct delivery from its own IP).
    # node_health checks that IP against the blocklists and, if it is listed, moves the traffic to another node.
    if not fetch_one("SELECT id FROM relays WHERE name = %s", (hostname,)):
        insert(
            "INSERT INTO relays (name, smtp_host, smtp_port, tls_level, is_default) VALUES (%s, %s, %s, %s, 0)",
            (hostname, "direct", 25, "may"),
        )
    keys = {}
    for sel in ("s1", "s2"):
        row = fetch_one(
            "SELECT private_key_pem FROM dkim_keys WHERE cluster_id = %s AND selector = %s AND dkim_domain = %s",
            (CLUSTER_ID, sel, target),
        )
        if not row:
            pem, _pub = generate_dkim_keypair(2048)
            insert(
                "INSERT INTO dkim_keys (cluster_id, selector, dkim_domain, private_key_path, private_key_pem) "
                "VALUES (%s, %s, %s, %s, %s)",
                (CLUSTER_ID, sel, target, "inline", pem),
            )
            row = {"private_key_pem": pem}
        keys[sel] = row["private_key_pem"]
    return {"zone": zone, "target": target, "keys": keys}


# ───────────────────────── tenant ─────────────────────────
def ensure_customer(name: str, contact: str, zone_id: int) -> dict:
    cust = fetch_one("SELECT id, client_id FROM customers WHERE name = %s", (name,))
    if not cust:
        client_id = str(uuid.uuid4())
        cid = insert(
            "INSERT INTO customers (name, client_id, contact_email, is_active, status, channels_enabled) "
            "VALUES (%s, %s, %s, 1, 'active', %s)",
            (name, client_id, contact, json.dumps(["email"])),
        )
        cust = {"id": cid, "client_id": client_id}
    link = fetch_one("SELECT id FROM customer_zones WHERE customer_id = %s AND zone_id = %s", (cust["id"], zone_id))
    if not link:
        insert("INSERT INTO customer_zones (customer_id, zone_id, status) VALUES (%s, %s, 'active')", (cust["id"], zone_id))
    return cust


def ensure_domain(name: str) -> int:
    row = fetch_one("SELECT id FROM virtual_domains WHERE name = %s", (name,))
    if row:
        return int(row["id"])
    return insert("INSERT INTO virtual_domains (name) VALUES (%s)", (name,))


# A new sending domain starts at the bottom of the warm-up ladder. The volume ramp controller
# raises the limit 1.3x per day of clean delivery once the domain uses it (docs/deliverability.md).
NEW_DOMAIN_DAILY_LIMIT = 500


def ensure_volume_limit(domain: str, daily_limit: int) -> None:
    """Puts a new domain under the volume governor before its first send.

    Without a row, the governor lets everything through until the hourly controller sees
    traffic, so the first campaign of a fresh domain and IP would go out at full speed.
    A domain that already has a row keeps it.
    """
    if fetch_one("SELECT 1 AS found FROM domain_volume_ramp WHERE domain_name = %s", (domain,)):
        return
    run("INSERT INTO domain_volume_ramp (domain_name, state, daily_quota) VALUES (%s, 'active', %s)",
        (domain, daily_limit))
    run("INSERT INTO domain_volume_ramp_log (domain_name, action, detail) VALUES (%s, 'init', %s)",
        (domain, json.dumps({"quota": daily_limit, "reason": "new_domain"})))


# The queue injects into the MTA's INTERNAL submission port (2587: SASL with the API key as the
# password, checked by Dovecot against the api_keys table; the tracking and DKIM milters run there).
# The port only exists on the compose internal network; the public 587 keeps the anti-abuse limits.
SMTP_HOST = "mta"
SMTP_PORT = int(os.getenv("QUEUE_SUBMISSION_PORT", "2587"))


def ensure_email_config(domain_id: int, domain: str, prefix: str, tracking_host: str, created_by: str) -> None:
    row = fetch_one("SELECT id FROM email_configs WHERE domain_id = %s AND is_default = 1 AND is_active = 1", (domain_id,))
    if row:
        run("UPDATE email_configs SET smtp_host = %s, smtp_port = %s, tracking_domain = %s WHERE id = %s",
            (SMTP_HOST, SMTP_PORT, tracking_host, row["id"]))
        return
    insert(
        "INSERT INTO email_configs (config_id, domain_id, name, smtp_host, smtp_port, smtp_secure, envelope_prefix, "
        "tracking_enabled, inject_pixel, rewrite_links, tracking_domain, default_from_email, is_active, is_default, created_by) "
        "VALUES (%s, %s, %s, %s, %s, 0, %s, 1, 1, 1, %s, %s, 1, 1, %s)",
        (str(uuid.uuid4()), domain_id, f"Connect {domain}", SMTP_HOST, SMTP_PORT, prefix, tracking_host,
         f"contato@{domain}", created_by),
    )


def ensure_dkim(domain: str, prefix: str, zone_keys: dict) -> list[str]:
    selectors = []
    for zone_sel in ("s1", "s2"):
        sel = prefix.replace("rbm", f"rbds{zone_sel[-1]}")
        selectors.append(sel)
        row = fetch_one(
            "SELECT id FROM dkim_keys WHERE cluster_id = %s AND selector = %s AND dkim_domain = %s",
            (CLUSTER_ID, sel, domain),
        )
        if not row:
            insert(
                "INSERT INTO dkim_keys (cluster_id, selector, dkim_domain, private_key_path, private_key_pem) "
                "VALUES (%s, %s, %s, %s, %s)",
                (CLUSTER_ID, sel, domain, "inline", zone_keys[zone_sel]),
            )
    ident = fetch_one(
        "SELECT id FROM dkim_identities WHERE cluster_id = %s AND from_domain = %s", (CLUSTER_ID, domain)
    )
    if not ident:
        insert(
            "INSERT INTO dkim_identities (cluster_id, from_domain, wildcard_subdomains, selector, dkim_domain) "
            "VALUES (%s, %s, 1, %s, %s)",
            (CLUSTER_ID, domain, selectors[0], domain),
        )
    return selectors


def ensure_admin(email: str, password: str, domain_id: int, customer_id: int) -> tuple[str, bool]:
    row = fetch_one("SELECT user_id FROM users WHERE email = %s", (email,))
    if row:
        return row["user_id"], False
    user_id = str(uuid.uuid4())
    insert(
        "INSERT INTO users (user_id, username, email, password_hash, domain_id, customer_id, role, is_active) "
        "VALUES (%s, %s, %s, %s, %s, %s, 'admin', 1)",
        (user_id, email, email, get_password_hash(password), domain_id, customer_id),
    )
    return user_id, True


async def create_api_key(user_id: str, client_id: str, domain_id: int, name: str) -> dict:
    from connect_lib.api_key.models import ApiKeyCreate, ApiKeyPermission, ChannelCreate, ChannelType

    from mail_api.services.api_key_service import api_key_service

    perms = [ApiKeyPermission.READ_EMAIL, ApiKeyPermission.WRITE_EMAIL, ApiKeyPermission.READ_STAT,
             ApiKeyPermission.READ_WEBHOOK, ApiKeyPermission.WRITE_WEBHOOK,
             ApiKeyPermission.READ_SUPPRESSION, ApiKeyPermission.WRITE_SUPPRESSION]
    resp = await api_key_service.create_api_key_with_channels(
        ApiKeyCreate(name=name, permissions=perms),
        user_id=user_id,
        client_id=client_id,
        channels=[ChannelCreate(channel=ChannelType.EMAIL, config={"domain_id": domain_id, "include_subdomains": True})],
    )
    # authorizes the domain on the key (validate_authorized_sender reads api_key_domains)
    run("INSERT IGNORE INTO api_key_domains (api_key_id, domain_id, include_subdomains) VALUES (%s, %s, 1)",
        (resp.id, domain_id))
    return {"api_key_id": resp.api_key_id, "api_key": resp.api_key}


def dns_records(domain: str, prefix: str, selectors: list[str], target: str, zone_keys: dict,
                install_domain: str, node_ips: list[str]) -> dict:
    ips = " ".join(f"ip4:{ip}" for ip in node_ips) or "ip4:<NODE-IP>"
    zone = [
        {"type": "A", "name": target, "value": ", ".join(node_ips) or "<NODE-IP>"},
        {"type": "MX", "name": target, "value": f"10 {target}"},
        {"type": "TXT", "name": target, "value": f"v=spf1 {ips} -all"},
        {"type": "TXT", "name": f"s1._domainkey.{target}",
         "value": f"v=DKIM1; k=rsa; p={public_key_from_private_pem(zone_keys['s1'])}"},
        {"type": "TXT", "name": f"s2._domainkey.{target}",
         "value": f"v=DKIM1; k=rsa; p={public_key_from_private_pem(zone_keys['s2'])}"},
        {"type": "PTR", "name": ", ".join(node_ips) or "<NODE-IP>", "value": target,
         "note": "reverse DNS of the IP, set in the VPS provider's panel"},
    ]
    customer = [
        {"type": "CNAME", "name": f"{prefix}.{domain}", "value": target},
        {"type": "CNAME", "name": f"{selectors[0]}._domainkey.{domain}", "value": f"s1._domainkey.{target}"},
        {"type": "CNAME", "name": f"{selectors[1]}._domainkey.{domain}", "value": f"s2._domainkey.{target}"},
        {"type": "TXT", "name": f"_dmarc.{domain}",
         # aggregate reports go to the operator's address (install.admin_email), which receives mail
         "value": f"v=DMARC1; p=none; rua=mailto:{os.environ.get('CADDY_ADMIN_EMAIL') or 'postmaster@' + install_domain}; aspf=r; adkim=r"},
    ]
    return {"installation_zone": zone, "customer_domain": customer}


def main() -> int:
    ap = argparse.ArgumentParser(description="Creates the installation zone and a RevBridge Connect tenant")
    ap.add_argument("--domain", required=True, help="the customer's sending domain (From)")
    ap.add_argument("--customer", required=True, help="customer name")
    ap.add_argument("--admin-email", required=True, help="panel login")
    ap.add_argument("--admin-password", help="panel password (generated if omitted)")
    ap.add_argument("--key-name", default="default")
    ap.add_argument("--daily-limit", type=int, default=NEW_DOMAIN_DAILY_LIMIT,
                    help=f"starting daily send limit of a new domain (default {NEW_DOMAIN_DAILY_LIMIT}; 0 = none)")
    args = ap.parse_args()

    install_domain = os.environ.get("DOMAIN", "example.com")
    hostname = os.environ.get("MYHOSTNAME", f"mta1.{install_domain}")
    tracking_host = os.environ.get("TRACK_BASE", "").replace("https://", "").replace("http://", "").strip("/")
    node_ips = [ip for ip in os.environ.get("CONNECT_NODE_IPS", "").split(",") if ip]
    if not node_ips and os.environ.get("CONNECT_CONTROL_IP"):
        node_ips = [os.environ["CONNECT_CONTROL_IP"]]  # the control node's public IP (install.public_ip)
    domain = args.domain.strip().lower()

    z = ensure_zone(install_domain, hostname)
    zone, target = z["zone"], z["target"]
    prefix = zone["subdomain_customer"]

    cust = ensure_customer(args.customer, args.admin_email, int(zone["id"]))
    domain_id = ensure_domain(domain)
    if args.daily_limit > 0:
        ensure_volume_limit(domain, args.daily_limit)
    ensure_domain(f"{prefix}.{domain}")  # Return-Path (VERP) domain, checked by the milter
    ensure_email_config(domain_id, domain, prefix, tracking_host, args.admin_email)
    selectors = ensure_dkim(domain, prefix, z["keys"])

    password = args.admin_password or secrets.token_urlsafe(12)
    user_id, created = ensure_admin(args.admin_email, password, domain_id, int(cust["id"]))
    key = asyncio.run(create_api_key(user_id, cust["client_id"], domain_id, args.key_name))

    out = {
        "client_id": cust["client_id"],
        "domain": domain,
        "domain_id": domain_id,
        "return_path": f"{prefix}.{domain}",
        # register this address with the mailbox providers' complaint (FBL) programs
        "fbl_address": f"bounces+fbl@{prefix}.{domain}",
        "panel": {"login": args.admin_email, "password": password if created else "(user already existed; password unchanged)"},
        "api_key_id": key["api_key_id"],
        "api_key": key["api_key"],
        "daily_limit": args.daily_limit or None,
        "dns": dns_records(domain, prefix, selectors, target, z["keys"], install_domain, node_ips),
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
