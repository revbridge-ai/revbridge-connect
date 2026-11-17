"""Demo traffic for the panel (LOCAL dev stack only; everything lands in Mailpit).

Part of `make demo` (tools/dev/demo.sh), which runs it inside the `api` container of a fresh
`make dev` stack, with the bootstrap JSON on stdin.

Creates a signed webhook to the dev receiver, sends a handful of campaigns and transactional
emails to synthetic recipients through the real pipeline (API, queue, milter, DKIM, Postfix), then
opens and clicks some of them through the tracking service the way a mail client would, so the
panel shows emails with a full journey. With tools/dev/demo.override.yml, Mailpit refuses the
`old.customer…` addresses and those sends bounce for real.
"""

from __future__ import annotations

import json
import random
import re
import secrets
import sys
import time

import httpx

API = "http://127.0.0.1:8001"
MAILPIT = "http://mailpit:8025"
TRACKING = "http://tracking:8002"
# a desktop browser that is not Apple Mail (Apple's prefetch is counted apart, as it should be)
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36")
CLIENT_IP = "203.0.113.20"  # RFC 5737 documentation address: no cloud range, so bot detection keeps the event

CAMPAIGNS = [
    ("Your order #10482 has shipped", "Your order is on its way. Track it any time.", 6),
    ("Welcome to Acme Store — here is 10% off", "Thanks for joining! Use WELCOME10 on your first order.", 18),
    ("Black Friday starts now: up to 40% off", "Our biggest sale of the year is live.", 40),
    ("Reset your password", "Use the link below to choose a new password.", 3),
    ("New arrivals you might like", "Fresh picks based on what you viewed.", 25),
]


def html_for(title: str, line: str) -> str:
    return (
        "<html><body style=\"font-family:Arial,sans-serif;color:#1f2937\">"
        f"<h2>{title}</h2><p>{line}</p>"
        "<p><a href=\"https://shop.example/offer?utm_source=email\" "
        "style=\"background:#111827;color:#fff;padding:10px 16px;border-radius:6px;text-decoration:none\">"
        "Open the store</a></p>"
        "<p><img src=\"https://example.org/logo.png\" alt=\"Acme\" width=\"120\"></p>"
        "</body></html>"
    )


def main() -> int:
    boot = json.load(sys.stdin if sys.argv[1] == "-" else open(sys.argv[1]))
    auth = {"Authorization": f"Bearer {boot['api_key']}"}
    # a signed webhook to the dev receiver, so the Webhooks page shows real deliveries
    httpx.post(
        f"{API}/webhooks",
        json={
            "name": "Order system",
            "url": "http://webhook-sink:8099/acme/email-events",
            "events": ["delivered", "bounced", "opened", "clicked", "complaint", "unsubscribed", "test"],
            "secret_key": secrets.token_hex(24),
            "domain_name": boot["domain"],
            "domain_id": boot["domain_id"],
        },
        headers=auth,
        timeout=30,
    ).raise_for_status()
    sent = 0
    for subject, line, n in CAMPAIGNS:
        to = [f"buyer{random.randint(1000, 99999)}@example.org" for _ in range(n)]
        if n >= 25:  # a stale address in a big list: Mailpit (demo.override.yml) refuses "old." addresses
            to[-1] = f"old.customer{random.randint(100, 999)}@example.org"
        payload = {
            "personalizations": [{"to": [{"address": addr}]} for addr in to],
            "from": {"name": "Acme Store", "address": f"hello@{boot['domain']}"},
            "subject": subject,
            "content": [
                {"type": "text/plain", "value": f"{line}\nhttps://shop.example/offer"},
                {"type": "text/html", "value": html_for(subject, line)},
            ],
        }
        r = httpx.post(f"{API}/send", json=payload, headers=auth, timeout=60)
        r.raise_for_status()
        sent += n
        time.sleep(2)
    print(f"sent {sent} emails; waiting for delivery")
    time.sleep(25)

    msgs = httpx.get(f"{MAILPIT}/api/v1/messages", params={"limit": 200}, timeout=15).json()["messages"]
    hdrs = {"User-Agent": BROWSER_UA, "X-Forwarded-For": CLIENT_IP}
    opens = clicks = 0
    time.sleep(6)  # opens right after delivery are ignored by the open gate
    for m in msgs:
        if random.random() > 0.45:
            continue
        full = httpx.get(f"{MAILPIT}/api/v1/message/{m['ID']}", timeout=10).json()
        html = full.get("HTML", "")
        pixel = re.search(r'src="https?://[^/"]+(/o/[^"]+\.png)"', html)
        link = re.search(r'href="https?://[^/"]+(/c/[^"]+)"', html)
        if pixel:
            httpx.get(TRACKING + pixel.group(1), headers=hdrs, timeout=10)
            opens += 1
        if link and random.random() < 0.3:
            httpx.get(TRACKING + link.group(1), headers=hdrs, timeout=10, follow_redirects=False)
            clicks += 1
    print(json.dumps({"sent": sent, "opened": opens, "clicked": clicks}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
