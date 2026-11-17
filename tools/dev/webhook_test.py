"""End-to-end webhook test (runs INSIDE the `api` container). Local Docker only.

    python /tmp/webhook_test.py /tmp/bootstrap.json /tmp/bootstrap_b.json

Sends real mail (Mailpit), triggers an open, a click and an unsubscribe, and checks on the
receiver (webhook-sink) what the dispatcher delivered: body, headers, signature, isolation
between customers, retries and the SSRF guard.
"""

import hashlib
import hmac
import json
import re
import sys
import time

import httpx

API, TRACKING, MAILPIT, SINK = "http://127.0.0.1:8001", "http://tracking:8002", "http://mailpit:8025", "http://webhook-sink:8099"
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")
CLIENT_IP = "203.0.113.20"  # RFC 5737 documentation address (outside every cloud range)
EVENTS = ["sent", "delivered", "opened", "clicked", "unsubscribed", "bounced", "hard_bounced", "complaint", "deferred"]
RUN = int(time.time())
A, B = json.load(open(sys.argv[1])), json.load(open(sys.argv[2]))
c = httpx.Client(timeout=30)
PASS = FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if ok else (PASS, FAIL + 1)
    print(("OK    " if ok else "FAIL  ") + name + (f" — {detail}" if detail else ""))


def jwt(boot: dict) -> dict:
    r = c.post(f"{API}/auth/login", json={"email": boot["panel"]["login"], "password": boot["panel"]["password"]})
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def create_hook(h: dict, path: str, events=EVENTS, secret: str | None = None, **extra) -> httpx.Response:
    body = {"name": f"test {path} {RUN}", "url": f"{SINK}{path}", "events": events, "service_type": "email", **extra}
    if secret:
        body["secret_key"] = secret
    return c.post(f"{API}/webhooks", headers=h, json=body)


def send(boot: dict, subject: str, rcpt: str | None = None) -> str:
    r = c.post(f"{API}/send", headers={"Authorization": f"Bearer {boot['api_key']}"}, json={
        "personalizations": [{"to": [{"name": "Recipient", "address": rcpt or f"hook{RUN}@example.net"}]}],
        "from": {"name": "Example Shop", "address": f"contact@{boot['domain']}"},
        "subject": subject,
        "content": [{"type": "text/plain", "value": "hi"},
                    {"type": "text/html", "value": '<html><body><p>Hi</p><a href="https://example.org/offer">View</a></body></html>'}],
    })
    r.raise_for_status()
    return r.json()["message_id"]


def received(path: str) -> list[dict]:
    return c.get(f"{SINK}/received", params={"path": path}).json()


def wait_event(path: str, message_id: str, event: str, timeout: int = 60) -> dict | None:
    end = time.time() + timeout
    while time.time() < end:
        for r in received(path):
            body = json.loads(r["body"])
            if body.get("message_id") == message_id and body.get("event_type") == event:
                return {**r, "json": body}
        time.sleep(1)
    return None


def mailpit_message(subject: str, timeout: int = 40) -> dict | None:
    end = time.time() + timeout
    while time.time() < end:
        for m in c.get(f"{MAILPIT}/api/v1/messages").json().get("messages", []):
            if m.get("Subject") == subject:
                return c.get(f"{MAILPIT}/api/v1/message/{m['ID']}").json()
        time.sleep(1)
    return None


c.post(f"{SINK}/reset")
hA, hB = jwt(A), jwt(B)

# ───────────── registration ─────────────
for bad in ("http://db:3306/x", "http://169.254.169.254/latest/meta-data", "http://127.0.0.1:8001/metrics", "ftp://example.org/x"):
    r = c.post(f"{API}/webhooks", headers=hA, json={"name": f"ssrf {RUN} {bad}", "url": bad, "events": ["delivered"],
                                                     "service_type": "email"})
    check(f"registration rejects an internal URL ({bad})", r.status_code in (400, 422), str(r.status_code))
r = create_hook(hA, "/a/intruder", domain_id=B["domain_id"])
check("registration on another customer's domain: 403", r.status_code == 403, str(r.status_code))
r = create_hook(hA, "/a/name", domain_name=B["domain"])
check("domain_name in the body is ignored (the token's domain is kept)", r.status_code in (200, 201) and r.json().get("domain_name") == A["domain"],
      f"{r.status_code} {r.json().get('domain_name') if r.status_code < 300 else r.text[:80]}")
SECRET = f"whsec_test_{RUN}"
wa = create_hook(hA, "/a", secret=SECRET)
wb = create_hook(hB, "/b", secret=f"other_{RUN}")
check("webhooks of A and B created", wa.status_code in (200, 201) and wb.status_code in (200, 201), f"{wa.status_code}/{wb.status_code}")

# ───────────── delivery, signature and headers ─────────────
subject = f"webhook-{RUN}"
mid = send(A, subject)
ev = wait_event("/a", mid, "delivered")
check("delivered reaches A's webhook", ev is not None)
if ev:
    h = {k.lower(): v for k, v in ev["headers"].items()}
    expected = "sha256=" + hmac.new(SECRET.encode(), ev["body"].encode(), hashlib.sha256).hexdigest()
    check("X-Webhook-Signature matches", h.get("x-webhook-signature") == expected)
    check("sender headers", h.get("user-agent") == "RevBridge-Webhook/1.0" and h.get("x-webhook-event") == "delivered"
          and h.get("x-webhook-message-id") == mid and h.get("x-webhook-worker") and h.get("x-webhook-timestamp", "").isdigit(),
          json.dumps({k: h.get(k) for k in ("user-agent", "x-webhook-event", "x-webhook-worker")}))
    b = ev["json"]
    check("payload: recipient, domain and domain_id of A", b.get("recipient_email") == f"hook{RUN}@example.net"
          and b.get("domain_id") == A["domain_id"] and A["domain"] in (b.get("domain_name") or ""),
          json.dumps({k: b.get(k) for k in ("recipient_email", "domain_name", "domain_id")}))
    check("payload: message_headers with the subject", (b.get("message_headers") or {}).get("Subject") == subject,
          json.dumps(b.get("message_headers")))

# ───────────── open, click, unsubscribe ─────────────
msg = mailpit_message(subject)
html = (msg or {}).get("HTML") or ""
pixel = re.search(r'src="https?://[^/"]+(/o/[^"]+\.png)"', html)
click = re.search(r'href="https?://[^/"]+(/c/[^"]+)"', html)
hdrs = {"User-Agent": BROWSER_UA, "X-Forwarded-For": CLIENT_IP}
if pixel:
    c.get(TRACKING + pixel.group(1), headers=hdrs)
if click:
    c.get(TRACKING + click.group(1), headers=hdrs, follow_redirects=False)
ev = wait_event("/a", mid, "opened", timeout=40)
check("opened reaches the webhook", ev is not None)
ev = wait_event("/a", mid, "clicked", timeout=40)
check("clicked arrives (tracking emits 'click')", ev is not None)
if ev:
    check("clicked carries clicked_url", ev["json"].get("clicked_url") == "https://example.org/offer", str(ev["json"].get("clicked_url")))
lu = ""
for hdr in (msg or {}).get("Headers", {}).get("List-Unsubscribe", []) if msg else []:
    lu = hdr
if not lu and msg:
    raw = c.get(f"{MAILPIT}/api/v1/message/{msg['ID']}/headers").json()
    lu = (raw.get("List-Unsubscribe") or [""])[0]
u = re.search(r"<https?://[^/>]+(/u/[^>]+)>", lu or "")
if u:
    c.post(TRACKING + u.group(1), data={"List-Unsubscribe": "One-Click"}, headers=hdrs)
ev = wait_event("/a", mid, "unsubscribed", timeout=40)
check("unsubscribed reaches the webhook", ev is not None)

# ───────────── isolation ─────────────
time.sleep(3)
leaked = [r for r in received("/b") if json.loads(r["body"]).get("message_id") == mid]
check("B's webhook receives nothing from A", not leaked, f"{len(leaked)} event(s)")

# ───────────── retries (5xx) and permanent rejection (4xx) ─────────────
c.patch(f"{API}/webhooks/{wa.json()['id']}/toggle", headers=hA, params={"is_active": False})
FAILP, BADP = f"/a/{RUN}/fail", f"/a/{RUN}/bad"  # unique paths: earlier runs leave active webhooks behind
fail = create_hook(hA, FAILP, events=["delivered"], secret=SECRET, retry_count=3).json()
bad = create_hook(hA, BADP, events=["delivered"], secret=SECRET, retry_count=3).json()
# another recipient: the first one unsubscribed above, so sending to it is blocked
mid2 = send(A, f"webhook-retry-{RUN}", rcpt=f"retry{RUN}@example.net")
end = time.time() + 60
while time.time() < end and len([r for r in received(FAILP) if json.loads(r["body"]).get("message_id") == mid2]) < 3:
    time.sleep(1)
tries = [r for r in received(FAILP) if json.loads(r["body"]).get("message_id") == mid2]
check("5xx: 3 attempts (the webhook's retry_count)", len(tries) == 3, f"{len(tries)} attempt(s)")
check("retries carry an identical body", len({r["body"] for r in tries}) == 1)
time.sleep(6)
tries = [r for r in received(FAILP) if json.loads(r["body"]).get("message_id") == mid2]
check("5xx: stops at the limit (no 4th attempt)", len(tries) == 3, f"{len(tries)}")
bads = [r for r in received(BADP) if json.loads(r["body"]).get("message_id") == mid2]
check("4xx: a single attempt", len(bads) == 1, f"{len(bads)}")
r = c.get(f"{API}/webhooks/{fail['id']}/logs", headers=hA)
check("attempt logs in the API", r.status_code == 200 and len(r.json()) >= 3, f"{r.status_code} n={len(r.json()) if r.status_code == 200 else '-'}")

# ───────────── metrics ─────────────
m, end = "", time.time() + 75  # the dispatcher publishes every 60 s
while time.time() < end and "connect_webhook_delivered_total" not in m:
    m = c.get(f"{API}/metrics").text
    time.sleep(3)
check("dispatcher metrics in /metrics", "connect_webhook_delivered_total" in m and "connect_webhook_outbox_pending" in m)

print(f"\n{PASS}/{PASS + FAIL} passed")
sys.exit(1 if FAIL else 0)
