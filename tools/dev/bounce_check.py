"""Helpers of the asynchronous-bounce test (run INSIDE the `api` container). Local tests only.

    python /tmp/bounce_check.py send <bootstrap.json> <recipient>   # sends one email, prints its message_id
    python /tmp/bounce_check.py suppressed <recipient>               # prints hard | none

tools/dev/run_e2e.sh sends a real email, then injects two DSNs on the MX (tools/dev/bounce_test.py):
one quoting that message (must suppress the recipient) and one forged, for a message that was
never sent (must be ignored).
"""

from __future__ import annotations

import json
import sys
import time

import httpx
from mail_shared.db.mysql_sync import fetch_one

API = "http://127.0.0.1:8001"


def send(boot: dict, rcpt: str) -> None:
    r = httpx.post(f"{API}/send", headers={"Authorization": f"Bearer {boot['api_key']}"}, json={
        "personalizations": [{"to": [{"address": rcpt}]}],
        "from": {"name": "Shop", "address": f"news@{boot['domain']}"},
        "subject": "Bounce test",
        "content": [{"type": "text/plain", "value": "bounce test"}],
    }, timeout=30)
    r.raise_for_status()
    message_id = r.json()["message_id"]
    # wait until the MTA accepted it (the DSN only makes sense for a message that left)
    for _ in range(60):
        row = fetch_one("SELECT state FROM send_ledger WHERE message_id = %s AND rcpt = %s", (message_id, rcpt))
        if row and row["state"] in ("smtp_accepted", "delivered"):
            break
        time.sleep(1)
    print(json.dumps({"message_id": message_id, "rcpt": rcpt}))


def suppressed(rcpt: str) -> None:
    for _ in range(20):
        row = fetch_one(
            "SELECT suppression_type FROM suppressions WHERE email = %s AND is_active = 1 LIMIT 1", (rcpt.lower(),)
        )
        if row:
            print(row["suppression_type"])
            return
        time.sleep(1)
    print("none")


if __name__ == "__main__":
    if sys.argv[1] == "send":
        send(json.load(open(sys.argv[2])), sys.argv[3])
    elif sys.argv[1] == "suppressed":
        suppressed(sys.argv[2])
