"""Synthetic demo history for the panel (LOCAL dev stack only).

Part of `make demo` (tools/dev/demo.sh), which runs it inside the `api` container after
`demo_traffic.py`, with the bootstrap JSON on stdin.

Writes `days` (default 40) of made-up deliveries, opens, clicks, unsubscribes and suppressions
for the bootstrapped domain, with a growth trend and weekly seasonality, so the Overview chart and
rates look like an installation in use. Recipients are @example.org/@example.net addresses; nothing
is sent. Never run this against a real installation.
"""

from __future__ import annotations

import json
import random
import sys
import uuid
from datetime import datetime, timedelta, timezone

from mail_shared.db.mysql_sync import fetch_one, get_conn

random.seed(42)

HARD_BOUNCE = "550 5.1.1 The email account that you tried to reach does not exist"


def main() -> int:
    boot = json.load(sys.stdin if sys.argv[1] == "-" else open(sys.argv[1]))
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 40
    # the domain the delivery pipeline records for this tenant (its return-path domain)
    row = fetch_one("SELECT domain_name FROM deliveries ORDER BY id DESC LIMIT 1")
    domain_name = row["domain_name"] if row else boot["domain"]
    domain_id = int(boot["domain_id"])
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    deliveries, events, suppressions = [], [], []
    for back in range(days, -1, -1):
        day = today - timedelta(days=back)
        trend = 700 + (days - back) * 30  # a growing list
        weekday_factor = 0.55 if day.weekday() >= 5 else 1.0
        volume = int(trend * weekday_factor * random.uniform(0.9, 1.1))
        # today: only up to now
        last_second = int((now - day).total_seconds()) - 60 if back == 0 else 21 * 3600
        first_second = min(8 * 3600, max(last_second - 3600, 0))
        for _ in range(volume):
            mid = str(uuid.uuid4())
            rcpt = f"customer{random.randint(1, 90000)}@example.{random.choice(['org', 'net'])}"
            sent_at = day + timedelta(seconds=random.randint(first_second, max(last_second, first_second)))
            r = random.random()
            status = "delivered" if r < 0.986 else ("bounced" if r < 0.993 else "deferred")
            deliveries.append((mid, rcpt, domain_name, domain_id, status, sent_at, sent_at, sent_at))
            if status == "bounced":
                suppressions.append((rcpt, domain_name, "bounce", HARD_BOUNCE, "hard", None, mid, sent_at))
                continue
            if status != "delivered":
                continue
            later = min(sent_at + timedelta(minutes=random.randint(2, 600)), now)
            if random.random() < 0.34:  # human open
                src = random.choice(["gmail_proxy", "gmail_proxy", "direct", "other_proxy"])
                events.append((mid, rcpt, "open", None, uuid.uuid4().hex, "203.0.113.20", "Mozilla/5.0", src,
                               "confirmed_human", 80, 0, None, domain_id, later))
                if random.random() < 0.13:  # a click among the openers
                    events.append((mid, rcpt, "click", "https://shop.example/offer", uuid.uuid4().hex,
                                   "203.0.113.20", "Mozilla/5.0", None, None, None, 0, None, domain_id, later))
                if random.random() < 0.004:  # asked to leave
                    events.append((mid, rcpt, "unsubscribe", None, uuid.uuid4().hex, "203.0.113.20",
                                   "Mozilla/5.0", None, None, None, 0, None, domain_id, later))
                    suppressions.append((rcpt, domain_name, "unsubscribe", "List-Unsubscribe one-click",
                                         None, None, mid, later))
            elif random.random() < 0.18:  # Apple Mail Privacy Protection prefetch: kept, but not counted
                events.append((mid, rcpt, "open", None, uuid.uuid4().hex, "17.58.0.10", "Mozilla/5.0", "apple_mpp",
                               "proxy_only", 10, 1, "apple_mpp", domain_id, sent_at + timedelta(seconds=30)))
            if random.random() < 0.0002:  # a spam report through a feedback loop
                suppressions.append((rcpt, domain_name, "complaint", "Feedback loop report (ARF)", None,
                                     "abuse", mid, later))
    suppressions.append(("former.client@example.net", domain_name, "manual", "Asked by phone to stop all email",
                         None, None, None, now - timedelta(days=3)))

    with get_conn() as conn:
        with conn.cursor() as cur:
            for i in range(0, len(deliveries), 2000):
                cur.executemany(
                    "INSERT IGNORE INTO deliveries (message_id, rcpt, domain_name, domain_id, status, last_update, "
                    "first_seen, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    deliveries[i : i + 2000],
                )
            for i in range(0, len(events), 2000):
                cur.executemany(
                    "INSERT INTO engagement_events (message_id, rcpt, type, url, token, ip_address, user_agent, "
                    "open_source, open_status, open_confidence_score, is_bot, bot_reason, domain_id, ts) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    events[i : i + 2000],
                )
            cur.executemany(
                "INSERT IGNORE INTO suppressions (email, domain_name, suppression_type, reason, bounce_type, "
                "complaint_feedback_type, message_id, suppressed_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                suppressions,
            )
        conn.commit()
    print(json.dumps({"domain_name": domain_name, "deliveries": len(deliveries), "engagement_events": len(events),
                      "suppressions": len(suppressions)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
