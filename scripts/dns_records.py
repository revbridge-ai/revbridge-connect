#!/usr/bin/env python3
"""All the DNS records of an installation, in one list, for the right provider.

Runs on YOUR machine (Python 3.11+, standard library only, needs `dig`). It reads the manifest
(connect.toml) and the output of the bootstrap (`scripts/first_customer.sh` saves it), finds the
zone your domain lives in and who hosts it, and then:

  plan    prints every record in one table and writes a zone file you can import
  check   asks the zone's own name servers (and a public resolver) whether each record is there
  apply   creates the missing records at Cloudflare through its API (a dry run unless --apply)

  scripts/dns_records.py plan  --bootstrap ~/.config/revbridge-connect/bootstrap-acme.com.json
  scripts/dns_records.py check --bootstrap ...
  CLOUDFLARE_API_TOKEN=... scripts/dns_records.py apply --apply     (without --apply: a dry run)

check and apply use the newest manifest and bootstrap in ~/.config/revbridge-connect/ unless you pass them.

Only public data is read from the bootstrap file (its `dns` part). The API key and the panel
password in it are never read or printed. `apply` never changes or deletes a record that already
exists: when one conflicts it is listed so you can decide. The PTR (reverse DNS) can only be set
in your VPS provider's panel; it is listed apart.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

TTL = 300

# NS host fragment -> (provider, where to add the records)
PROVIDERS = [
    ("ns.cloudflare.com", "Cloudflare", "dash.cloudflare.com > your domain > DNS > Records (or Import)"),
    ("awsdns", "Amazon Route 53", "console.aws.amazon.com/route53 > Hosted zones > your domain"),
    ("googledomains.com", "Google Cloud DNS / Squarespace", "your DNS panel at Squarespace Domains or Cloud DNS"),
    ("ns-cloud", "Google Cloud DNS", "console.cloud.google.com > Network services > Cloud DNS"),
    ("domaincontrol.com", "GoDaddy", "dcc.godaddy.com > your domain > DNS"),
    ("registrar-servers.com", "Namecheap", "Domain List > Manage > Advanced DNS"),
    ("digitalocean.com", "DigitalOcean", "cloud.digitalocean.com > Networking > Domains"),
    ("hetzner", "Hetzner DNS", "dns.hetzner.com > your zone"),
    ("ovh.net", "OVHcloud", "OVH Manager > Domains > DNS zone"),
    ("contabo", "Contabo DNS", "my.contabo.com > DNS Zone Management"),
    ("azure-dns", "Azure DNS", "portal.azure.com > DNS zones"),
    ("vercel-dns", "Vercel", "vercel.com > Domains > your domain > DNS records"),
    ("gandi.net", "Gandi", "admin.gandi.net > Domains > your domain > DNS records"),
    ("nsone.net", "NS1", "your NS1 portal"),
    ("dnsimple.com", "DNSimple", "dnsimple.com > your domain > DNS"),
    ("registro.br", "Registro.br", "registro.br > your domain > DNS (or the DNS host you pointed it to)"),
    ("uol.com.br", "UOL Host", "your UOL Host panel > DNS"),
    ("locaweb", "Locaweb", "your Locaweb panel > DNS"),
]

# where each VPS provider has the reverse-DNS setting (keyed by a fragment of its RDAP/whois name)
PTR_HELP = [
    ("contabo", "Contabo: my.contabo.com > Network > Reverse DNS (rDNS) > the IP"),
    ("hetzner", "Hetzner: Cloud Console > the server > Networking > the IP > Edit reverse DNS"),
    ("digitalocean", "DigitalOcean: the droplet's name IS its PTR: rename the droplet to the hostname"),
    ("vultr", "Vultr: the server > Settings > IPv4 > Reverse DNS"),
    ("linode", "Linode/Akamai: the Linode > Network > Edit RDNS"),
    ("ovh", "OVHcloud: Bare Metal Cloud > IP > the IP > Reverse DNS"),
    ("amazon", "AWS: Elastic IPs need the 'Request to remove/update reverse DNS' form"),
    ("google", "Google Cloud: set it on the static external IP (Compute Engine > VPC > IP addresses)"),
]


# ─── names ──────────────────────────────────────────────────────────────────────────────────
def norm(name: str) -> str:
    return name.strip().rstrip(".").lower()


def relative(fqdn: str, apex: str) -> str:
    fqdn, apex = norm(fqdn), norm(apex)
    if fqdn == apex:
        return "@"
    if fqdn.endswith("." + apex):
        return fqdn[: -(len(apex) + 1)]
    return fqdn


def split_txt(value: str) -> list[str]:
    """A TXT string holds at most 255 bytes: longer values are several quoted strings."""
    return [value[i:i + 255] for i in range(0, len(value), 255)] or [""]


# ─── the records ────────────────────────────────────────────────────────────────────────────
def collect(manifest: dict, bootstrap: dict | None) -> dict:
    """Every record of the installation: {'records': [...], 'ptr': {...} | None}.

    Each record is {type, name (fqdn), value, group, why}. The hosts of the manifest come first
    (they only need the VPS IP); the DKIM keys and the customer's records come from the bootstrap.
    """
    install, endpoints = manifest["install"], manifest.get("endpoints", {})
    ip = install.get("public_ip", "")
    hostname = norm(install["hostname"])
    records: list[dict] = []

    def add(rtype: str, name: str, value: str, group: str, why: str) -> None:
        name = norm(name)
        if ip:  # an old bootstrap may carry the placeholder instead of the node IP
            name, value = name.replace("<NODE-IP>", ip), value.replace("<NODE-IP>", ip)
        if any(r["type"] == rtype and r["name"] == name and r["value"] == value for r in records):
            return
        records.append({"type": rtype, "name": name, "value": value, "group": group, "why": why})

    hosts = [(hostname, "mail server: HELO name and reverse DNS")]
    labels = {"api": "HTTP API", "tracking": "open pixel, click and unsubscribe links",
              "submission": "SMTP submission (587/465)", "bounce": "bounces (MX target)",
              "images": "image proxy", "panel": "control panel"}
    for key, why in labels.items():
        if endpoints.get(key):
            hosts.append((norm(endpoints[key]), why))
    for host, why in hosts:
        add("A", host, ip or "<VPS-IP>", "server", why)

    ptr = {"ip": ip or "<VPS-IP>", "value": hostname}
    if bootstrap:
        dns = bootstrap.get("dns", bootstrap)
        for r in dns.get("installation_zone", []):
            if r["type"] == "PTR":
                continue
            add(r["type"], r["name"], r["value"], "mail server",
                {"MX": "bounces and replies reach the server", "TXT": "SPF / DKIM public key of the server"}
                .get(r["type"], "mail server"))
        for r in dns.get("customer_domain", []):
            add(r["type"], r["name"], r["value"], "sending domain",
                {"CNAME": "points the domain's DKIM / return-path at the server", "TXT": "DMARC policy and reports"}
                .get(r["type"], "sending domain"))
    return {"records": records, "ptr": ptr}


# ─── DNS lookups (dig) ──────────────────────────────────────────────────────────────────────
def dig(args: list[str]) -> list[str]:
    try:
        out = subprocess.run(["dig", "+short", "+time=4", "+tries=1", *args], capture_output=True, text=True,
                             timeout=20).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [line.strip() for line in out.splitlines() if line.strip() and not line.startswith(";")]


def find_zone(domain: str, lookup=dig) -> tuple[str, list[str]]:
    """The zone apex of `domain`: the closest name, going up, that has its own NS records."""
    labels = norm(domain).split(".")
    for i in range(len(labels) - 1):
        candidate = ".".join(labels[i:])
        ns = [norm(n) for n in lookup(["NS", candidate])]
        if ns:
            return candidate, ns
    return ".".join(labels[-2:]), []


def detect_provider(ns_hosts: list[str]) -> tuple[str, str]:
    joined = " ".join(ns_hosts)
    for fragment, name, where in PROVIDERS:
        if fragment in joined:
            return name, where
    return "your DNS provider", "its DNS panel"


def ptr_help(ip: str, rdap=None) -> str:
    """Where to set the reverse DNS, from the IP's owner (RDAP). Falls back to a generic hint."""
    text = ""
    try:
        fetch = rdap or (lambda u: urllib.request.urlopen(u, timeout=6).read())
        text = json.dumps(json.loads(fetch(f"https://rdap.org/ip/{ip}"))).lower()
    except Exception:  # noqa: BLE001 - best effort
        pass
    for fragment, hint in PTR_HELP:
        if fragment in text:
            return hint
    return "your VPS provider's panel (look for 'reverse DNS' or 'rDNS' on the server or its IP)"


# ─── outputs ────────────────────────────────────────────────────────────────────────────────
def zone_file(records: list[dict], apex: str) -> str:
    lines = [f"; Connect: records for {apex}. Import this file at your DNS provider, or add them one by one.",
             f"$ORIGIN {norm(apex)}.", f"$TTL {TTL}"]
    for r in records:
        name = relative(r["name"], apex)
        if r["type"] == "TXT":
            body = " ".join(f'"{chunk}"' for chunk in split_txt(r["value"]))
        elif r["type"] in ("CNAME", "MX"):
            target = r["value"].split()[-1]
            prefix = r["value"].split()[0] + " " if r["type"] == "MX" else ""
            body = f"{prefix}{norm(target)}."
        else:
            body = r["value"]
        lines.append(f"{name}\t{TTL}\tIN\t{r['type']}\t{body}")
    return "\n".join(lines) + "\n"


def long_values(records: list[dict], apex: str) -> str:
    """The values the table shortened, whole, for copying."""
    return "\n".join(f"{r['type']} {relative(r['name'], apex)}\n{r['value']}\n"
                     for r in records if len(r["value"]) > 70)


def render_table(records: list[dict], apex: str) -> str:
    rows = [("Type", "Name", "Value")]
    for r in records:
        value = r["value"] if len(r["value"]) <= 70 else r["value"][:67] + "... (full value below)"
        rows.append((r["type"], relative(r["name"], apex), value))
    widths = [max(len(row[i]) for row in rows) for i in range(3)]
    out = []
    for n, row in enumerate(rows):
        out.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))
        if n == 0:
            out.append("  ".join("-" * w for w in widths))
    return "\n".join(out)


# ─── check ──────────────────────────────────────────────────────────────────────────────────
def _txt_values(lines: list[str]) -> list[str]:
    """dig prints a long TXT as several quoted strings: join them back into one value."""
    return ["".join(line.split('"')[1::2]) if '"' in line else line for line in lines]


def lookup_record(rec: dict, server: str | None = None, lookup=dig) -> list[str]:
    args = [rec["type"], rec["name"]]
    if server:
        args.insert(0, f"@{server}")
    found = lookup(args)
    return _txt_values(found) if rec["type"] == "TXT" else found


def _canon(rtype: str, value: str) -> str:
    if rtype == "CNAME":
        return norm(value)
    if rtype == "MX":
        prio, _, host = value.partition(" ")
        return f"{prio} {norm(host)}"
    return value.strip()


def matches(rec: dict, found: list[str]) -> bool:
    return _canon(rec["type"], rec["value"]) in {_canon(rec["type"], f) for f in found}


def check(plan: dict, apex: str, ns_hosts: list[str], lookup=dig) -> list[dict]:
    """Per record: whether the zone's own name server has it, and whether a public resolver does."""
    auth = ns_hosts[0] if ns_hosts else None
    results = []
    for rec in plan["records"]:
        at_auth = matches(rec, lookup_record(rec, auth, lookup)) if auth else False
        public = matches(rec, lookup_record(rec, "1.1.1.1", lookup))
        results.append({**rec, "published": at_auth, "propagated": public})
    ptr = plan["ptr"]
    got = [norm(x) for x in lookup(["-x", ptr["ip"]])] if not ptr["ip"].startswith("<") else []
    results.append({"type": "PTR", "name": ptr["ip"], "value": ptr["value"], "group": "server",
                    "why": "reverse DNS, set in the VPS provider's panel",
                    "published": norm(ptr["value"]) in got, "propagated": norm(ptr["value"]) in got})
    return results


def already_different(records: list[dict], lookup=dig) -> list[tuple[dict, list[str]]]:
    """Records whose name already answers with another value (a conflict you must resolve by hand)."""
    out = []
    for rec in records:
        found = lookup_record(rec, "1.1.1.1", lookup)
        if found and not matches(rec, found):
            out.append((rec, found))
    return out


# ─── Cloudflare ─────────────────────────────────────────────────────────────────────────────
class Cloudflare:
    base = "https://api.cloudflare.com/client/v4"

    def __init__(self, token: str):
        self._token = token

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(self.base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {self._token}",
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            try:
                detail = "; ".join(e.get("message", "") for e in json.loads(exc.read()).get("errors", []))
            except Exception:  # noqa: BLE001
                detail = ""
            raise SystemExit(f"Cloudflare answered {exc.code} on {method} {path}: {detail or 'no detail'}") from None

    def zone_id(self, apex: str) -> str:
        res = self.call("GET", f"/zones?name={apex}").get("result", [])
        if not res:
            raise SystemExit(f"Cloudflare has no zone called {apex} for this token. "
                             "Create the token with 'Zone > DNS > Edit' and 'Zone > Zone > Read' on that zone.")
        return res[0]["id"]

    def records(self, zone: str) -> list[dict]:
        out, page = [], 1
        while True:
            data = self.call("GET", f"/zones/{zone}/dns_records?per_page=100&page={page}")
            out += data.get("result", [])
            if page >= data.get("result_info", {}).get("total_pages", 1):
                return out
            page += 1


def cloudflare_body(rec: dict) -> dict:
    body = {"type": rec["type"], "name": rec["name"], "ttl": TTL}
    if rec["type"] == "MX":
        prio, host = rec["value"].split()
        body.update(content=norm(host), priority=int(prio))
    elif rec["type"] == "CNAME":
        body.update(content=norm(rec["value"]))
    else:
        body.update(content=rec["value"])
    if rec["type"] in ("A", "CNAME"):
        body["proxied"] = False  # mail and certificates need the real IP: never the orange cloud
    return body


def plan_changes(records: list[dict], existing: list[dict]) -> dict:
    """Split the wanted records into create / already there / conflict. Never edits or deletes."""
    create, same, conflict = [], [], []
    for rec in records:
        at_name = [e for e in existing if norm(e["name"]) == rec["name"]]
        if rec["type"] == "TXT":
            kind = rec["value"].split(";")[0].split(" ")[0]  # v=spf1 / v=DKIM1 / v=DMARC1
            if any(e["type"] == "TXT" and e["content"].strip('"') == rec["value"] for e in at_name):
                same.append(rec)
            elif any(e["type"] == "TXT" and e["content"].strip('"').startswith(kind) for e in at_name):
                conflict.append((rec, "a different " + kind + " record is already there"))
            elif any(e["type"] == "CNAME" for e in at_name):
                conflict.append((rec, "a CNAME is already there"))
            else:
                create.append(rec)
            continue
        same_type = [e for e in at_name if e["type"] == rec["type"]]
        if rec["type"] == "MX":
            want = norm(rec["value"].split()[-1])
            if any(norm(e["content"]) == want for e in same_type):
                same.append(rec)
            else:
                create.append(rec)
            continue
        if rec["type"] == "A" and any(e["content"] == rec["value"] for e in same_type):
            same.append(rec)
        elif rec["type"] == "CNAME" and any(norm(e["content"]) == norm(rec["value"]) for e in same_type):
            same.append(rec)
        elif at_name and (rec["type"] == "CNAME" or same_type):
            conflict.append((rec, "a different " + (same_type[0]["type"] if same_type else at_name[0]["type"])
                             + " record is already there"))
        else:
            create.append(rec)
    return {"create": create, "same": same, "conflict": conflict}


def group_by_zone(records: list[dict], home: tuple[str, list[str]], lookup=dig) -> dict:
    """{zone apex: (its name servers, [records])}. The sending domain can live in another zone."""
    zones: dict = {home[0]: (home[1], [])}
    seen: dict[str, tuple[str, list[str]]] = {}
    for rec in records:
        parent = rec["name"].split(".", 1)[1] if rec["name"].count(".") > 1 else rec["name"]
        if parent not in seen:
            seen[parent] = find_zone(rec["name"], lookup)
        z, zns = seen[parent]
        zones.setdefault(z, (zns, []))[1].append(rec)
    return zones


# ─── command line ───────────────────────────────────────────────────────────────────────────
def newest(pattern: str) -> str | None:
    """The most recently written file in ~/.config/revbridge-connect (what the other scripts saved)."""
    found = sorted(Path("~/.config/revbridge-connect").expanduser().glob(pattern),
                   key=lambda f: f.stat().st_mtime, reverse=True)
    return str(found[0]) if found else None


def load(args) -> tuple[dict, dict | None]:
    manifest_path = Path(args.manifest).expanduser()
    if not manifest_path.exists():
        raise SystemExit(f"manifest not found: {manifest_path}")
    manifest = tomllib.loads(manifest_path.read_text())
    bootstrap = None
    if args.bootstrap:
        bootstrap = json.loads(Path(args.bootstrap).expanduser().read_text())
        bootstrap = {"dns": bootstrap["dns"]}  # nothing but the public records is kept
    return manifest, bootstrap


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["plan", "check", "apply"])
    ap.add_argument("--manifest", default=os.environ.get("CONNECT_TOML") or newest("connect-*.toml") or "connect.toml",
                    help="default: $CONNECT_TOML, else the newest manifest init_manifest.py wrote")
    ap.add_argument("--bootstrap", help="the JSON saved by scripts/first_customer.sh (check and apply default to the newest one)")
    ap.add_argument("--out", default="~/.config/revbridge-connect", help="where `plan` writes the zone file")
    ap.add_argument("--provider", default="cloudflare", choices=["cloudflare"])
    ap.add_argument("--apply", action="store_true", help="apply: really create the records (default: dry run)")
    args = ap.parse_args(argv)

    if not args.bootstrap and args.command in ("check", "apply"):
        args.bootstrap = newest("bootstrap-*.json")
    manifest, bootstrap = load(args)
    plan = collect(manifest, bootstrap)
    apex, ns = find_zone(norm(manifest["install"]["hostname"]))
    zones = group_by_zone(plan["records"], (apex, ns))
    provider, where = detect_provider(ns)

    if args.command == "plan":
        out_dir = Path(args.out).expanduser()
        out_dir.mkdir(parents=True, exist_ok=True)
        missing = bootstrap is None
        print(f"Your domain's DNS is hosted at: {provider}  (zone {apex})")
        print(f"Add the records at: {where}\n")
        for z, (zns, recs) in sorted(zones.items()):
            if not recs:
                continue
            if len(zones) > 1:
                zp, zw = detect_provider(zns)
                print(f"Zone {z} ({zp}):")
            print(render_table(recs, z))
            full = long_values(recs, z)
            if full:
                print("\nFull values (copy them whole):\n" + full)
            path = out_dir / f"dns-{z}.zone"
            path.write_text(zone_file(recs, z))
            os.chmod(path, 0o600)
            print(f"\nZone file to import: {path}\n")
            for rec, found in already_different(recs):
                print(f"ATTENTION: {rec['type']} {relative(rec['name'], z)} already exists with another value "
                      f"({'; '.join(found)[:80]}). Importing or applying will not overwrite it: decide which one stays.")
        print(f"Reverse DNS (PTR), in your VPS provider's panel: {plan['ptr']['ip']}  ->  {plan['ptr']['value']}")
        print(f"  {ptr_help(plan['ptr']['ip'])}")
        if missing:
            print("\nThe DKIM keys and the customer's records are not here yet: run "
                  "scripts/first_customer.sh and plan again.")
        if provider == "Cloudflare":
            print("\nAutomatic: with a Cloudflare API token (Zone > DNS > Edit on this zone), run it yourself:\n"
                  "  CLOUDFLARE_API_TOKEN=... scripts/dns_records.py apply            (dry run: lists what it would create)\n"
                  "  CLOUDFLARE_API_TOKEN=... scripts/dns_records.py apply --apply    (creates them)")
        return 0

    if args.command == "check":
        results = check(plan, apex, ns)
        bad = 0
        for r in results:
            state = "ok" if r["propagated"] else ("published, not propagated yet" if r["published"] else "MISSING")
            bad += 0 if r["propagated"] else 1
            print(f"  {state:<30} {r['type']:<5} {relative(r['name'], apex)}")
        print(f"\n{len(results) - bad} of {len(results)} records are visible to public resolvers.")
        return 0 if not bad else 1

    # apply
    token = os.environ.get("CLOUDFLARE_API_TOKEN", "")
    if not token:
        raise SystemExit("set CLOUDFLARE_API_TOKEN in your own shell first (it is not read from anywhere else). "
                         "Do not paste it into a chat.")
    cf = Cloudflare(token)
    total = {"create": 0, "same": 0, "conflict": 0}
    for z, (zns, recs) in sorted(zones.items()):
        if not recs:
            continue
        zid = cf.zone_id(z)
        changes = plan_changes(recs, cf.records(zid))
        for rec in changes["same"]:
            print(f"  already there  {rec['type']:<5} {relative(rec['name'], z)}")
        for rec, why in changes["conflict"]:
            print(f"  CONFLICT       {rec['type']:<5} {relative(rec['name'], z)}: {why} (left untouched)")
        for rec in changes["create"]:
            verb = "creating" if args.apply else "would create"
            print(f"  {verb:<14} {rec['type']:<5} {relative(rec['name'], z)}")
            if args.apply:
                cf.call("POST", f"/zones/{zid}/dns_records", cloudflare_body(rec))
        for k in total:
            total[k] += len(changes[k])
    print(f"\n{total['create']} to create, {total['same']} already there, {total['conflict']} conflicts."
          + ("" if args.apply else "  Dry run: add --apply to create them."))
    return 1 if total["conflict"] else 0


if __name__ == "__main__":
    sys.exit(main())
