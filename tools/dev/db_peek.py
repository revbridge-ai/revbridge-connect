"""Shows the latest rows of the sending tables (read only, local testing).
Usage, inside the `api` container: python tools/dev/db_peek.py [n]
"""

import sys

from mail_shared.db.mysql_sync import fetch_all

n = int(sys.argv[1]) if len(sys.argv) > 1 else 3
QUERIES = {
    "send_ledger": "SELECT message_id, rcpt, domain_name, domain_id, customer_id, state, attempt, created_at FROM send_ledger ORDER BY id DESC LIMIT %s",
    "deliveries": "SELECT message_id, rcpt, domain_name, domain_id, status, first_seen, last_update FROM deliveries ORDER BY id DESC LIMIT %s",
    "delivery_events": "SELECT message_id, rcpt, domain_name, domain_id, queue_id, status, dsn, host, ts FROM delivery_events ORDER BY id DESC LIMIT %s",
    "engagement_events": "SELECT message_id, rcpt, type, url, ip_address, open_source, open_status, is_bot, bot_reason, domain_id, ts FROM engagement_events ORDER BY id DESC LIMIT %s",
    "suppressions": "SELECT * FROM suppressions ORDER BY id DESC LIMIT %s",
}
for name, sql in QUERIES.items():
    try:
        rows = fetch_all(sql, (n,))
    except Exception as e:  # the table may not exist
        print(f"== {name}: error {e}")
        continue
    print(f"== {name}: {len(rows)} row(s)")
    for r in rows:
        print("  ", {k: (str(v)[:60] if v is not None else None) for k, v in r.items()})
