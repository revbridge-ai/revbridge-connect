"""Panel + API test through the panel's Caddy (like a browser does). Runs INSIDE the `api` container.
Two customers (A and B) to prove they are isolated from each other. Local Docker only.

    python /tmp/panel_test.py /tmp/bootstrap.json /tmp/bootstrap_b.json
"""

import base64
import json
import re
import sys
import time
from datetime import date, timedelta

import httpx

BASE = "http://panel-dev:8088"
API = BASE + "/api"
SEND = "http://127.0.0.1:8001/send"

A = json.load(open(sys.argv[1]))
B = json.load(open(sys.argv[2]))
DA, DB = A["domain"], B["domain"]
PASS = FAIL = 0
RUN = int(time.time())
c = httpx.Client(timeout=30)


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"OK    {name}" + (f" — {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"FAIL  {name}" + (f" — {detail}" if detail else ""))


def login(boot: dict) -> dict:
    r = c.post(f"{API}/auth/login", json={"email": boot["panel"]["login"], "password": boot["panel"]["password"]})
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def in_family(d: str, dom: str) -> bool:
    return d == dom or d.endswith("." + dom)


def send(api_key: str, subject: str) -> httpx.Response:
    return c.post(SEND, headers={"Authorization": f"Bearer {api_key}"}, json={
        "personalizations": [{"to": [{"address": f"panel{int(time.time())}@example.net"}]}],
        "from": {"name": "Example Shop", "address": f"contact@{DA}"},
        "subject": subject,
        "content": [{"type": "text/plain", "value": "test"}],
    })


# wide window, in UTC (the server stores UTC)
rng = {"start_date": (date.today() - timedelta(days=2)).isoformat(), "end_date": (date.today() + timedelta(days=2)).isoformat()}


def wait_status(h: dict, subject: str, timeout: int = 60) -> str:
    """Waits until the send with this subject reaches a final state in the report (delivered/failed/bounced)."""
    end = time.time() + timeout
    while time.time() < end:
        r = c.get(f"{API}/stats/messages", headers=h, params={"page": 1, "limit": 10, "search": subject, **rng})
        ms = r.json().get("messages", []) if r.status_code == 200 else []
        st = next((m.get("delivery_status") for m in ms if m.get("subject") == subject), None)
        if st in ("delivered", "failed", "bounced"):
            return st
        time.sleep(2)
    return "timeout"


# ───────────── panel files and Caddy rules ─────────────
r = c.get(BASE + "/")
check("GET / returns the panel", r.status_code == 200 and "text/html" in r.headers.get("content-type", ""), str(r.status_code))
check("X-Frame-Options DENY header", r.headers.get("x-frame-options") == "DENY")
check("index.html without long caching", r.headers.get("cache-control") == "no-cache", r.headers.get("cache-control", ""))
r2 = c.get(BASE + "/emails")
check("panel route (/emails) falls back to index.html", r2.status_code == 200 and r2.text == r.text)
asset = re.search(r'(/assets/[^"\']+\.js)', r.text)
if asset:
    r3 = c.get(BASE + asset.group(1))
    check("hashed asset: immutable cache", r3.status_code == 200 and "immutable" in r3.headers.get("cache-control", ""),
          asset.group(1))
else:
    check("index.html references /assets/*.js", False)
check("/api/metrics blocked on the panel host", c.get(API + "/metrics").status_code == 404)

hA, hB = login(A), login(B)
tokA = hA["Authorization"].split()[1]
payload = json.loads(base64.urlsafe_b64decode(tokA.split(".")[1] + "=="))
ttl_min = (payload.get("exp", 0) - time.time()) / 60
check("panel JWT expires (12 h)", 700 < ttl_min <= 721, f"{ttl_min:.0f} min")

# ───────────── domain DNS ─────────────
r = c.get(f"{API}/domains/{DA}/dns-info", headers=hA)
recs = r.json().get("dns_records", []) if r.status_code == 200 else []
names = [x["name"] for x in recs]
check("dns-info of the customer's own domain", r.status_code == 200 and len(recs) == 4, ", ".join(names))
check("dns-info matches the bootstrap",
      sorted((x["type"], x["name"], x["value"]) for x in recs)
      == sorted((x["type"], x["name"], x["value"]) for x in A["dns"]["customer_domain"]))
check("dns-info of another customer: 404", c.get(f"{API}/domains/{DB}/dns-info", headers=hA).status_code == 404)

# ───────────── DKIM ─────────────
r = c.get(f"{API}/dkim/keys", headers=hA)
keys = r.json() if r.status_code == 200 else []
check("DKIM: lists only the keys of the customer's own domain",
      r.status_code == 200 and keys and all(in_family(k["dkim_domain"], DA) for k in keys),
      ", ".join(f"{k['selector']}@{k['dkim_domain']}" for k in keys))
check("DKIM: list includes the TXT value", all((k.get("dns_record_value") or "").startswith("v=DKIM1") for k in keys))
kb = (c.get(f"{API}/dkim/keys", headers=hB).json() or [{}])[0]
check("DKIM: reading another customer's key: 404", c.get(f"{API}/dkim/keys/{kb.get('id')}", headers=hA).status_code == 404)
check("DKIM: deleting another customer's key: 404", c.delete(f"{API}/dkim/keys/{kb.get('id')}", headers=hA).status_code == 404)
check("DKIM: B's key is still there", c.get(f"{API}/dkim/keys/{kb.get('id')}", headers=hB).status_code == 200)
r = c.post(f"{API}/dkim/keys", headers=hA, json={"cluster_id": "default", "selector": "x1", "dkim_domain": DB,
                                                 "generate_key": True})
check("DKIM: creating a key on another customer's domain: 403", r.status_code == 403, str(r.status_code))

# ───────────── suppressions ─────────────
r = c.post(f"{API}/suppressions", headers=hA, json={"email": "manual-a@example.net", "reason": "customer request",
                                                   "domain_name": DB, "suppression_type": "manual"})
check("manual suppression: created", r.status_code in (200, 201), f"{r.status_code} {r.text[:80]}")
c.post(f"{API}/suppressions", headers=hB, json={"email": "manual-b@example.net", "reason": "b"})
la = c.get(f"{API}/suppressions", headers=hA, params={"page": 1, "limit": 100}).json().get("suppressions", [])
lb = c.get(f"{API}/suppressions", headers=hB, params={"page": 1, "limit": 100}).json().get("suppressions", [])
mine = next((s for s in la if s["email"] == "manual-a@example.net"), None)
check("manual suppression stored on the token's domain (not the one sent)", bool(mine) and mine.get("domain_name") == DA,
      str(mine and mine.get("domain_name")))
check("list includes the type", bool(mine) and mine.get("suppression_type") == "manual")
check("A's list includes unsubscribes/bounces of the Return-Path (rbm<n>.domain)",
      any(s.get("suppression_type") in ("unsubscribe", "bounce") for s in la),
      ", ".join(sorted({f"{s.get('suppression_type')}@{s.get('domain_name')}" for s in la})))
check("A's list does not include B's suppression", not any(s["email"] == "manual-b@example.net" for s in la))
sb = next((s for s in lb if s["email"] == "manual-b@example.net"), {})
check("deleting another customer's suppression: 404", c.delete(f"{API}/suppressions/{sb.get('id')}", headers=hA).status_code == 404)
check("deleting one's own suppression", c.delete(f"{API}/suppressions/{mine and mine['id']}", headers=hA).status_code == 200)

# ───────────── webhooks ─────────────
wa = c.post(f"{API}/webhooks", headers=hA, json={"name": f"hook A {RUN}", "url": f"https://example.org/hook-a-{RUN}",
                                                "events": ["delivered"], "service_type": "email",
                                                "secret_key": "whsec_local_test_0123456"})
wb = c.post(f"{API}/webhooks", headers=hB, json={"name": f"hook B {RUN}", "url": f"https://example.org/hook-b-{RUN}",
                                                "events": ["delivered"], "service_type": "email"})
check("webhook: created", wa.status_code in (200, 201) and wb.status_code in (200, 201), f"{wa.status_code}/{wb.status_code}")
wa, wb = wa.json(), wb.json()
lst = c.get(f"{API}/webhooks", headers=hA, params={"service_type": "email"}).json()
ids = [w["id"] for w in lst]
check("webhook: lists only the customer's own", wa["id"] in ids and wb["id"] not in ids, str(ids))
mine_w = next((w for w in lst if w["id"] == wa["id"]), {})
check("webhook: secret masked in the list", mine_w.get("secret_key") == "whse***"
      and all(w.get("secret_key") in (None, "") or w["secret_key"].endswith("***") for w in lst),
      str([w.get("secret_key") for w in lst]))
r = c.get(f"{API}/webhooks/{wa['id']}/events", headers=hA)
check("webhook: the owner reads its events", r.status_code == 200, str(r.status_code))
check("webhook: testing another customer's webhook: 404",
      c.post(f"{API}/webhooks/{wb['id']}/test", headers=hA, json={"webhook_id": wb["id"]}).status_code == 404)
check("webhook: another customer's events: 404", c.get(f"{API}/webhooks/{wb['id']}/events", headers=hA).status_code == 404)
check("webhook: another customer's logs: 404", c.get(f"{API}/webhooks/{wb['id']}/logs", headers=hA).status_code == 404)
c.post(f"{API}/webhooks/{wb['id']}/test", headers=hB, json={"webhook_id": wb["id"]})  # creates an event for B
time.sleep(2)
r = c.get(f"{API}/webhooks/events/recent", headers=hA)
evs = r.json() if r.status_code == 200 else None
check("webhook: recent events only from the customer's own webhooks",
      evs is not None and all(e["webhook_id"] in ids for e in evs), f"{r.status_code} n={len(evs or [])}")

# ───────────── email settings ─────────────
r = c.get(f"{API}/email-configs", headers=hA)
check("email-configs: lists the customer's own", r.status_code == 200 and r.json() and all(x["domain_id"] == A["domain_id"] for x in r.json()))
r = c.get(f"{API}/email-configs", headers=hA, params={"domain_id": B["domain_id"]})
check("email-configs of another customer (?domain_id=): 403", r.status_code == 403, str(r.status_code))

# ───────────── API keys ─────────────
r = c.post(f"{API}/api-keys", headers=hA, json={"name": "panel-test", "permissions": ["write:email", "read:email", "read:stat"]})
check("key: created", r.status_code in (200, 201), str(r.status_code))
key = r.json()
ch = c.post(f"{API}/api-keys/{key['api_key_id']}/channels", headers=hA,
            json={"channel": "email", "config": {"domain_id": A["domain_id"], "include_subdomains": True}, "is_active": True})
check("key: email channel enabled", ch.status_code in (200, 201), f"{ch.status_code} {ch.text[:100]}")
subj = f"panel-new-key-{RUN}"
r = send(key["api_key"], subj)
check("new key: the API accepts the send", r.status_code == 200 and r.json().get("status") == "queued", f"{r.status_code} {r.text[:80]}")
# the queue-processor injects into the MTA with SMTP AUTH (user = api_key_id, password = the key): revoke
# only after the delivery, otherwise a 535 would come from the revocation and prove nothing
st = wait_status(hA, subj)
check("new key: email delivered (SMTP AUTH with a key created in the panel)", st == "delivered", st)
r = c.delete(f"{API}/api-keys/{key['id']}", headers=hA)
check("key: revoked", r.status_code in (200, 204), str(r.status_code))
time.sleep(35)
r = send(key["api_key"], "panel-revoked-key")
check("revoked key stops sending within 30 s", r.status_code in (401, 403), str(r.status_code))

# ───────────── reports ─────────────
s = c.get(f"{API}/stats/dashboard", headers=hA, params=rng).json().get("data", {}).get("summary", {})
check("dashboard: unsubscribes are counted", s.get("unsubscribes", 0) >= 1 and s.get("unsubscribe_rate", 0) > 0,
      f"unsub={s.get('unsubscribes')} rate={s.get('unsubscribe_rate')}% spam={s.get('spam_rate')}%")
mb = c.get(f"{API}/stats/messages", headers=hB, params={"page": 1, "limit": 50, **rng}).json()
check("messages: B does not see A's sends", (mb.get("pagination", {}).get("total", mb.get("total", 0)) or 0) == 0,
      json.dumps(mb)[:120])

print(f"\n{PASS}/{PASS + FAIL} passed")
sys.exit(1 if FAIL else 0)
