"""Local end-to-end test of RevBridge Connect, run inside the `api` container.

    docker compose exec api python /tmp/connect_e2e.py /tmp/bootstrap.json

Covers: sending (headers, DKIM, List-Unsubscribe, image proxy), the email list (MySQL),
panel login (/auth) and keys (/api-keys), one-click unsubscribe (POST /u), suppression
blocking a resend, SSRF blocked by the image proxy, the new domain's daily limit, a
scheduled send being released, and /metrics.
Prints OK/FAIL per check. Local testing only (mail is delivered to Mailpit).
"""

from __future__ import annotations

import json
import re
import sys
import time
from datetime import UTC, datetime, timedelta

import httpx

API, TRACKING, MAILPIT, IMG = "http://127.0.0.1:8001", "http://tracking:8002", "http://mailpit:8025", "http://image-proxy:8080"
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(("OK    " if ok else "FAIL  ") + name + (f" — {detail}" if detail else ""), flush=True)


def latest_message(subject: str, timeout: int = 40) -> dict | None:
    for _ in range(timeout // 2):
        msgs = httpx.get(f"{MAILPIT}/api/v1/messages", timeout=10).json().get("messages", [])
        for m in msgs:
            if m.get("Subject") == subject:
                return m
        time.sleep(2)
    return None


def send(auth: dict, domain: str, to: str, subject: str, scheduled_at: str | None = None) -> httpx.Response:
    payload = {
        "personalizations": [{"to": [{"name": "Recipient", "address": to}]}],
        "from": {"name": "Example Shop", "address": f"contact@{domain}"},
        "subject": subject,
        "content": [
            {"type": "text/plain", "value": "Test https://example.org/a"},
            {"type": "text/html", "value": (
                '<html><body style="background:#ffffff"><p>Hi</p>'
                '<a href="https://example.org/offer">View</a>'
                '<img src="https://example.org/logo.png" alt="logo"></body></html>')},
        ],
    }
    if scheduled_at:
        payload["scheduled_at"] = scheduled_at
    return httpx.post(f"{API}/send", json=payload, headers=auth, timeout=30)


def main() -> int:
    boot = json.load(open(sys.argv[1]))
    auth = {"Authorization": f"Bearer {boot['api_key']}"}
    domain = boot["domain"]
    stamp = str(int(time.time()))
    rcpt = f"person{stamp}@example.net"

    # 1. send + headers
    subject = f"E2E {stamp}"
    r = send(auth, domain, rcpt, subject)
    check("POST /send 200", r.status_code == 200, r.text[:160])
    msg_id = r.json().get("message_id") if r.status_code == 200 else None
    m = latest_message(subject)
    check("arrived in Mailpit", m is not None)
    if m:
        h = httpx.get(f"{MAILPIT}/api/v1/message/{m['ID']}/headers", timeout=10).json()
        full = httpx.get(f"{MAILPIT}/api/v1/message/{m['ID']}", timeout=10).json()
        html = full.get("HTML", "")
        check("DKIM-Signature d=" + domain, any(f"d={domain}" in v for v in h.get("Dkim-Signature", [])))
        lu = (h.get("List-Unsubscribe") or [""])[0]
        check("List-Unsubscribe present", lu.startswith("<http"), lu[:90])
        check("List-Unsubscribe-Post one-click", (h.get("List-Unsubscribe-Post") or [""])[0] == "List-Unsubscribe=One-Click")
        check("Return-Path VERP", "@rbm" in (h.get("Return-Path") or [""])[0])
        check("no RevBridge-branded header", not any("revbridge" in k.lower() for k in h),
              ", ".join(k for k in h if "revbridge" in k.lower()))
        check("link rewritten to /c/", "/c/" in html)
        check("pixel /o/ injected", "/o/" in html)
        check("<img> wrapped by the image proxy", "/p?url=https%3A%2F%2Fexample.org%2Flogo.png" in html,
              (re.search(r'<img src="([^"]+)"', html) or [None, ""])[1][:100])

        # 2. one-click unsubscribe (RFC 8058): POST to the URL from the header
        unsub_path = re.sub(r"^<https?://[^/]+", "", lu).rstrip(">")
        u = httpx.post(TRACKING + unsub_path, content="List-Unsubscribe=One-Click",
                       headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=15)
        check("POST /u one-click", u.status_code in (200, 202, 204), str(u.status_code))

    # 3. a resend to the recipient who unsubscribed must be blocked (SUPPRESSION_CHECK_MODE=enforce)
    time.sleep(2)
    r2 = send(auth, domain, rcpt, subject + " resend")
    body = r2.text
    suppressed = r2.status_code != 200 or '"recipients_count":0' in body.replace(" ", "")
    check("resend blocked by the suppression", suppressed, f"{r2.status_code} {body[:160]}")

    # 4. email list (MySQL) and headers
    time.sleep(8)
    lst = httpx.get(f"{API}/stats/messages", params={"search": rcpt}, headers=auth, timeout=15).json()
    msgs = lst.get("messages", [])
    mine = next((x for x in msgs if x.get("subject") == subject), None)
    check("GET /stats/messages lists the send", mine is not None,
          f"total={lst.get('pagination', {}).get('total')} source={lst.get('source')} "
          f"status={mine and mine.get('delivery_status')} journey={mine and mine.get('journey')}")
    if msg_id:
        hd = httpx.get(f"{API}/stats/message/headers", params={"message_id": msg_id}, headers=auth, timeout=15)
        check("GET /stats/message/headers", hd.status_code == 200 and hd.json().get("subject") == subject, str(hd.status_code))
        one = httpx.get(f"{API}/stats/message/headers", params={"message_id": msg_id, "rcpt": rcpt},
                        headers=auth, timeout=15)
        check("headers of one copy show only its recipient",
              one.status_code == 200 and one.json().get("headers", {}).get("to") == rcpt, one.text[:160])

    # 5. panel login and keys
    login = httpx.post(f"{API}/auth/login", json={"email": boot["panel"]["login"], "password": sys.argv[2] if len(sys.argv) > 2 else ""}, timeout=15)
    ok_login = login.status_code == 200 and "access_token" in login.text
    check("POST /auth/login", ok_login, f"{login.status_code} {login.text[:120]}")
    if ok_login:
        jwt = {"Authorization": f"Bearer {login.json()['access_token']}"}
        keys = httpx.get(f"{API}/api-keys", headers=jwt, timeout=15)
        check("GET /api-keys with JWT", keys.status_code == 200, f"{keys.status_code} {keys.text[:120]}")
        dash = httpx.get(f"{API}/stats/dashboard", headers=jwt, timeout=15)
        check("GET /stats/dashboard with JWT", dash.status_code == 200, str(dash.status_code))

    # 6. image proxy: unsigned URLs are refused, and a signed URL to an internal host is blocked
    target = "http://api:8001/health"
    unsigned = httpx.get(f"{IMG}/p", params={"url": target}, timeout=15)
    check("image proxy refuses an unsigned URL", unsigned.status_code == 403, str(unsigned.status_code))
    from mail_api.common.image_proxy import sign_proxy_url

    ssrf = httpx.get(f"{IMG}/p", params={"url": target, "bg": "FFFFFF", "s": sign_proxy_url(target, "FFFFFF")}, timeout=15)
    check("image proxy blocks an internal target (signed URL)", ssrf.status_code in (400, 403)
          and "assinatura" not in ssrf.text and "signature" not in ssrf.text, f"{ssrf.status_code} {ssrf.text[:80]}")

    # 7. a new domain is under the volume governor from its first send
    allow = httpx.get(f"{API}/volume/allowance", headers=auth, timeout=15)
    aj = allow.json() if allow.status_code == 200 else {}
    check("new domain starts with a daily limit", aj.get("governed") is True
          and aj.get("daily_quota") == (boot.get("daily_limit") or 500), allow.text[:160])

    # 8. a scheduled send is released by the queue processors and delivered
    later = f"E2E scheduled {stamp}"
    when = (datetime.now(UTC) + timedelta(seconds=5)).isoformat()
    rs = send(auth, domain, f"later{stamp}@example.net", later, scheduled_at=when)
    check("POST /send with scheduled_at", rs.status_code == 200, rs.text[:160])
    check("scheduled send is delivered after its time", latest_message(later, timeout=90) is not None)

    # 9. /metrics
    met = httpx.get(f"{API}/metrics", timeout=15)
    check("GET /metrics responds", met.status_code == 200, f"{met.status_code} {len(met.text)} bytes")

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed" + (f"; failed: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
