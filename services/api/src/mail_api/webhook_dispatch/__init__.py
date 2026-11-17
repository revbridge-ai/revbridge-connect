"""RevBridge Connect webhook dispatcher.

Events come from the `kafka_outbox` table (the event JSON written by the other services) and
from the `w:<domain>:*` Redis keys (test events sent by the API, and retries). What the
receiver gets (body, headers, signature) is described in dispatcher.py.
"""
