"""Shows the error stored in send_ledger for a message_id (local testing, read only).

    python /tmp/ledger_error.py <message_id>
"""

import json
import sys

from mail_shared.db.mysql_sync import fetch_all

rows = fetch_all(
    "SELECT state, attempt, max_attempts, last_error, last_smtp_code, worker_id, created_at, terminal_at "
    "FROM send_ledger WHERE message_id = %s",
    (sys.argv[1],),
)
print(json.dumps(rows, default=str, ensure_ascii=False, indent=1))
