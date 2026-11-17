"""
Format of the kafka_outbox table rows.

The services store every event as a 'pending' row in kafka_outbox (the name is historical);
in RevBridge Connect the webhook-dispatcher consumes them.
"""

from .models import OutboxMessage, OutboxStatus

__all__ = [
    "OutboxMessage",
    "OutboxStatus",
]
