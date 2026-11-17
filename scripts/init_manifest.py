#!/usr/bin/env python3
"""Writes the installation manifest OUTSIDE the checkout, so `git pull` never conflicts with it.

  scripts/init_manifest.py --ip 203.0.113.10 --domain acme.com --email you@acme.com

--domain is the domain your email is sent from (the one in `From:`). The installation gets its own
subdomain under it (mail.acme.com by default; change it with --install-domain), and the hostnames
mta1., api., t., smtp., bounce., img. and panel. under that. --email receives the Let's Encrypt
notices and the DMARC reports and is the panel login by default: use a mailbox you read.

Prints the path of the manifest. Use it with `CONNECT_TOML=<path> scripts/deploy.sh root@ip`.
Standard library only (Python 3.11+).
"""
from __future__ import annotations

import argparse
import ipaddress
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOSTS = {"api": "api", "tracking": "t", "submission": "smtp", "bounce": "bounce", "images": "img", "panel": "panel"}


def build(template: str, install_domain: str, ip: str, email: str) -> str:
    """The template with [install] and [endpoints] filled in; comments and the rest are kept."""
    values = {"install": {"domain": install_domain, "hostname": f"mta1.{install_domain}",
                          "public_ip": ip, "admin_email": email},
              "endpoints": {key: f"{label}.{install_domain}" for key, label in HOSTS.items()}}
    out, section = [], ""
    for line in template.splitlines():
        header = re.match(r"\s*\[([a-z_]+)\]", line)
        if header:
            section = header.group(1)
        m = re.match(r'^(\s*)([a-z_]+)(\s*=\s*)"[^"]*"(.*)$', line)
        if m and m.group(2) in values.get(section, {}):
            line = f'{m.group(1)}{m.group(2)}{m.group(3)}"{values[section][m.group(2)]}"{m.group(4)}'
        out.append(line)
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ip", required=True, help="public IPv4 of the VPS")
    ap.add_argument("--domain", required=True, help="the domain your email is sent from (From:)")
    ap.add_argument("--email", required=True, help="a mailbox you read: Let's Encrypt, DMARC reports, panel login")
    ap.add_argument("--install-domain", help="zone of the installation hostnames (default: mail.<domain>)")
    ap.add_argument("--out", help="where to write (default: ~/.config/revbridge-connect/connect-<install-domain>.toml)")
    ap.add_argument("--force", action="store_true", help="overwrite an existing manifest")
    args = ap.parse_args(argv)

    try:
        if ipaddress.ip_address(args.ip).version != 4:
            raise ValueError
    except ValueError:
        raise SystemExit(f"--ip must be an IPv4 address, got {args.ip!r}") from None
    domain = args.domain.strip().lower().rstrip(".")
    install_domain = (args.install_domain or f"mail.{domain}").strip().lower().rstrip(".")
    for name in (domain, install_domain):
        if not re.fullmatch(r"([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}", name):
            raise SystemExit(f"{name!r} does not look like a domain name")
    if "@" not in args.email or " " in args.email:
        raise SystemExit(f"--email must be an address, got {args.email!r}")

    out = Path(args.out).expanduser() if args.out else \
        Path.home() / ".config" / "revbridge-connect" / f"connect-{install_domain}.toml"
    if out.exists() and not args.force:
        raise SystemExit(f"{out} already exists: pass --force to overwrite it")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build((ROOT / "connect.toml").read_text(), install_domain, args.ip, args.email))
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
