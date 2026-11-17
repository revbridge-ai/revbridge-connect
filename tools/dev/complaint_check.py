"""Spam-complaint test (runs INSIDE the `api` container). Local Docker only.

    python /tmp/complaint_check.py prepare /tmp/bootstrap.json          # -> JSON describing the send
    python /tmp/complaint_check.py verify  /tmp/bootstrap.json '<json>'  # after arf_inject.py
"""

import json
import sys
import time
from datetime import date, timedelta

import httpx

API, SINK, MAILPIT = "http://127.0.0.1:8001", "http://webhook-sink:8099", "http://mailpit:8025"
c = httpx.Client(timeout=30)
boot = json.load(open(sys.argv[2]))
PASS = FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if ok else (PASS, FAIL + 1)
    print(("OK    " if ok else "FAIL  ") + name + (f" — {detail}" if detail else ""))


def jwt() -> dict:
    r = c.post(f"{API}/auth/login", json={"email": boot["panel"]["login"], "password": boot["panel"]["password"]})
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def send(rcpt: str, subject: str) -> httpx.Response:
    return c.post(f"{API}/send", headers={"Authorization": f"Bearer {boot['api_key']}"}, json={
        "personalizations": [{"to": [{"address": rcpt}]}],
        "from": {"name": "Example Shop", "address": f"contact@{boot['domain']}"},
        "subject": subject, "content": [{"type": "text/plain", "value": "hi"}],
    })


if sys.argv[1] == "prepare":
    run = int(time.time())
    h = jwt()
    hook = c.post(f"{API}/webhooks", headers=h, json={"name": f"complaints {run}", "url": f"{SINK}/c", "events": ["complaint"],
                                                       "service_type": "email"}).json()
    rcpt = f"complainer{run}@example.net"
    subject = f"complaint-{run}"
    mid = send(rcpt, subject).json()["message_id"]
    for _ in range(40):  # wait until it reaches Mailpit (the complaint points to a real send)
        if any(m.get("Subject") == subject for m in c.get(f"{MAILPIT}/api/v1/messages").json().get("messages", [])):
            break
        time.sleep(1)
    print(json.dumps({"message_id": mid, "rcpt": rcpt, "rp": boot["return_path"], "webhook_id": hook.get("id"),
                      "forged_rcpt": f"forged{run}@example.net"}))
    sys.exit(0)

info = json.loads(sys.argv[3])
h = jwt()
sup, mine, end = [], [], time.time() + 40  # the ARF goes through Postfix -> pipe -> events-updater
while time.time() < end and not mine:
    sup = c.get(f"{API}/suppressions", headers=h, params={"page": 1, "limit": 100, "suppression_type": "complaint"}).json().get("suppressions", [])
    mine = [s for s in sup if s["email"] == info["rcpt"]]
    if not mine:
        time.sleep(2)
check("ARF becomes a 'complaint' suppression of the recipient", len(mine) == 1 and mine[0].get("suppression_type") == "complaint",
      json.dumps(mine[:1], default=str)[:160])
check("forged ARF (no matching send) is ignored", not any(s["email"] == info["forged_rcpt"] for s in sup))
hook = None
for _ in range(30):
    for r in c.get(f"{SINK}/received", params={"path": "/c"}).json():
        b = json.loads(r["body"])
        if b.get("message_id") == info["message_id"] and b.get("event_type") == "complaint":
            hook = b
    if hook:
        break
    time.sleep(1)
check("'complaint' webhook delivered", hook is not None)
if hook:
    check("complaint payload", hook.get("complaint_type") == "abuse" and hook.get("source_ip") == "203.0.113.25"
          and hook.get("recipient_email") == info["rcpt"] and hook.get("complaint_feedback") == "ExampleFBL/1.0",
          json.dumps({k: hook.get(k) for k in ("complaint_type", "complaint_feedback", "source_ip", "recipient_email")}))
rng = {"start_date": (date.today() - timedelta(days=2)).isoformat(), "end_date": (date.today() + timedelta(days=2)).isoformat()}
s = c.get(f"{API}/stats/dashboard", headers=h, params=rng).json().get("data", {}).get("summary", {})
check("dashboard counts the complaint", (s.get("complaints") or 0) >= 1 and (s.get("spam_rate") or 0) > 0,
      f"complaints={s.get('complaints')} rate={s.get('spam_rate')}%")
r = send(info["rcpt"], "after-the-complaint")
check("a new send to the recipient who complained is blocked", r.status_code == 200 and r.json().get("status") == "suppressed",
      f"{r.status_code} {r.text[:80]}")
d = c.get(f"{API}/domains/{boot['domain']}/dns-info", headers=h).json()
check("dns-info shows the FBL address", d.get("fbl_address") == f"bounces+fbl@{info['rp']}", str(d.get("fbl_address")))
print(f"\n{PASS}/{PASS + FAIL} passed")
sys.exit(1 if FAIL else 0)
