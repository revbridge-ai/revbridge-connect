"""Local end-to-end test: sends one email through the API and follows its status.

Runs INSIDE the `api` container (talks to the API on 127.0.0.1:8001), using the output of
scripts/bootstrap_tenant.py:

    docker compose exec api python tools/dev/send_test.py /tmp/bootstrap.json to@example.net

Local testing only (the dev override delivers to Mailpit, never to the internet).
"""

from __future__ import annotations

import json
import sys
import time

import httpx

API = "http://127.0.0.1:8001"


def main() -> int:
    boot = json.load(open(sys.argv[1]))
    to = sys.argv[2] if len(sys.argv) > 2 else "recipient@example.net"
    headers = {"Authorization": f"Bearer {boot['api_key']}"}
    domain = boot["domain"]
    payload = {
        "personalizations": [{"to": [{"name": "Test Recipient", "address": to}]}],
        "from": {"name": "Example Shop", "address": f"contact@{domain}"},
        "subject": "RevBridge Connect test",
        "content": [
            {"type": "text/plain", "value": "Hello! This is a Connect test. https://example.org/offer"},
            {"type": "text/html", "value": (
                "<html><body><p>Hello! This is a <b>Connect</b> test.</p>"
                "<p><a href=\"https://example.org/offer?utm_source=test\">See the offer</a></p>"
                "<p><img src=\"https://example.org/logo.png\" alt=\"logo\"></p>"
                "</body></html>"
            )},
        ],
    }
    r = httpx.post(f"{API}/send", json=payload, headers=headers, timeout=30)
    print("POST /send ->", r.status_code)
    print(r.text[:800])
    if r.status_code >= 300:
        return 1
    body = r.json()
    message_id = body.get("message_id") or (body.get("messages") or [{}])[0].get("message_id")
    if not message_id:
        return 0
    for _ in range(20):
        time.sleep(2)
        s = httpx.get(f"{API}/status/{message_id}", headers=headers, timeout=10)
        print("GET /status ->", s.status_code, s.text[:300])
        if s.status_code == 200 and any(k in s.text for k in ("sent", "delivered", "failed", "bounced")):
            break
    return 0


if __name__ == "__main__":
    sys.exit(main())
