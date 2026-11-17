"""
Event outbox of the milter and the events-updater (table kafka_outbox; the name is historical).

Builds normalized events with connect_lib.messaging and writes one row per event to
kafka_outbox, which the webhook dispatcher reads. It does not talk to any Kafka broker.
The public interface is kept for compatibility with existing code.

Supports distributed tracing with OpenTelemetry.
"""

import json
import logging
import os
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from .db import get_conn


def _sanitize_surrogates(obj: Any) -> Any:
    """
    Recursively remove invalid surrogate characters from strings.

    Surrogates (e.g. \\udce9) can appear in badly encoded emails
    and make MySQL fail when inserting JSON.

    Args:
        obj: Object to sanitize (dict, list, str, etc.)

    Returns:
        The object with surrogates replaced by the replacement character
    """
    if isinstance(obj, str):
        # Encode with errors='replace' and decode back to drop surrogates
        return obj.encode('utf-8', errors='replace').decode('utf-8')
    elif isinstance(obj, dict):
        return {k: _sanitize_surrogates(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_sanitize_surrogates(item) for item in obj]
    elif isinstance(obj, tuple):
        return tuple(_sanitize_surrogates(item) for item in obj)
    else:
        return obj

# connect_lib.messaging imports
from connect_lib.messaging import (
    CampaignInfo,
    ChannelInfo,
    ChannelType,
    ContactInfo,
    EngagementAction,
    EngagementMetadata,
    Environment,
    IdentifierType,
    OutboxMessage,
    ProviderType,
    TenantInfo,
    UnifiedEngagementEvent,
    create_event_id,
)

# OpenTelemetry tracing (optional)
try:
    from opentelemetry import trace
    OTEL_AVAILABLE = True
except ImportError:
    OTEL_AVAILABLE = False


class KafkaEventType(StrEnum):
    """Email event types written to the outbox (name kept for compatibility)"""

    SENT = "sent"
    DELIVERED = "delivered"
    BOUNCED = "bounced"
    SOFT_BOUNCED = "soft_bounced"
    HARD_BOUNCED = "hard_bounced"
    DEFERRED = "deferred"
    SUPPRESSED = "suppressed"


# KafkaEventType -> EngagementAction mapping
EVENT_TYPE_TO_ACTION: dict[str, EngagementAction] = {
    "sent": EngagementAction.SENT,
    "delivered": EngagementAction.DELIVERED,
    "bounced": EngagementAction.BOUNCED,
    "soft_bounced": EngagementAction.SOFT_BOUNCED,
    "hard_bounced": EngagementAction.HARD_BOUNCED,
    "deferred": EngagementAction.DEFERRED,
    "suppressed": EngagementAction.SUPPRESSED,
}


def _get_current_trace_context() -> dict[str, str] | None:
    """
    Get the current OpenTelemetry trace context, for propagation.

    Returns:
        Dict with trace_id and span_id, or None if unavailable
    """
    if not OTEL_AVAILABLE:
        return None

    try:
        current_span = trace.get_current_span()
        if current_span is None:
            return None

        span_context = current_span.get_span_context()
        if span_context and span_context.trace_id:
            return {
                "trace_id": format(span_context.trace_id, '032x'),
                "span_id": format(span_context.span_id, '016x'),
            }
        return None
    except Exception as e:
        logging.getLogger(__name__).warning(f"[OTEL] Error getting the trace context: {e}")
        return None


def _create_unified_event(
    event_type: KafkaEventType,
    message_id: str,
    rcpt: str,
    domain_name: str,
    payload_data: dict[str, Any],
    trace_context: dict[str, str] | None = None,
) -> UnifiedEngagementEvent:
    """
    Build a normalized UnifiedEngagementEvent for email.

    Args:
        event_type: Event type (sent, delivered, bounced, etc.)
        message_id: Message ID
        rcpt: Recipient address
        domain_name: Sending domain
        payload_data: Additional event data
        trace_context: Trace context for propagation

    Returns:
        Normalized UnifiedEngagementEvent
    """
    # Map event_type to EngagementAction
    action = EVENT_TYPE_TO_ACTION.get(event_type.value, EngagementAction.SENT)

    # Email-specific fields from the payload
    subject = payload_data.get("subject")
    sender_email = payload_data.get("sender_email")
    queue_id = payload_data.get("queue_id")

    # Error/bounce information
    error_code = payload_data.get("bounce_code")
    error_message = payload_data.get("bounce_reason") or payload_data.get("server_response")

    # campaign_id: passed through from the sender's X-Connect-Campaign header,
    # when the caller puts it in the payload.
    campaign_id = payload_data.get("campaign_id")
    campaign_info = (
        CampaignInfo(id=campaign_id, message_id=message_id)
        if campaign_id else None
    )

    # Provider data includes the trace context, when available
    provider_data = {
        "domain_name": domain_name,
        **{k: v for k, v in payload_data.items() if k not in ("subject", "sender_email", "queue_id", "bounce_code", "bounce_reason")},
    }
    if trace_context:
        provider_data["_trace_context"] = trace_context

    # Build the normalized event
    return UnifiedEngagementEvent(
        event_id=create_event_id(),
        event_type="engagement",
        timestamp=datetime.now(UTC),
        channel=ChannelInfo(
            type=ChannelType.EMAIL,
            provider=ProviderType.POSTFIX,
            provider_event_id=message_id,
        ),
        action=action,
        contact=ContactInfo(
            identifier=rcpt or "",
            identifier_type=IdentifierType.EMAIL,
        ),
        campaign=campaign_info,  # campaign_id from the X-Connect-Campaign header (if any)
        metadata=EngagementMetadata(
            subject=subject,
            sender_email=sender_email,
            queue_id=queue_id,
            domain_name=domain_name,
            error_code=error_code,
            error_message=error_message,
            provider_data=provider_data,
        ),
        tenant=TenantInfo(
            id=domain_name or "default",
            environment=Environment.PRODUCTION,
        ),
        raw_payload=payload_data,
    )


class KafkaClient:
    """Client that writes normalized events to the outbox table"""

    def __init__(self):
        self.logger = logging.getLogger(__name__)
        # CONNECT_EVENT_OUTBOX turns on writing events to the outbox; the webhook
        # dispatcher (mail_api.webhook_dispatch) reads them
        self.enabled = os.getenv("CONNECT_EVENT_OUTBOX", "false").strip().lower() in ("1", "true", "yes", "on")

    def _insert_into_outbox(
        self,
        event: UnifiedEngagementEvent,
        event_type: KafkaEventType,
        rcpt: str | None,
        domain_name: str,
    ) -> int | None:
        """
        Insert a normalized event into the outbox table.

        Args:
            event: Normalized event
            event_type: Event type (for schema compatibility)
            rcpt: Recipient address
            domain_name: Domain name

        Returns:
            int or None: ID of the inserted row, or None on error
        """
        try:
            if not self.enabled:
                self.logger.debug(
                    f"Event outbox disabled - event {event_type} not written for {event.channel.provider_event_id}"
                )
                return None

            # Convert the event to the outbox format
            outbox_message = OutboxMessage.from_event(event, include_raw_payload=True)

            # Remove invalid surrogates from the payload before serializing
            sanitized_payload = _sanitize_surrogates(outbox_message.payload)

            # Serialize the payload
            payload_json = json.dumps(sanitized_payload, default=str)

            # Insert into the outbox
            sql = """
                INSERT INTO kafka_outbox
                (event_type, message_id, rcpt, domain_name, payload, status)
                VALUES (%s, %s, %s, %s, %s, 'pending')
            """
            params = (
                event_type.value,
                event.channel.provider_event_id,
                rcpt,
                domain_name,
                payload_json,
            )

            outbox_id = self._insert_with_guard(
                sql=sql,
                params=params,
                event_type=event_type,
                provider_event_id=event.channel.provider_event_id,
                rcpt=rcpt,
            )
            return outbox_id

        except Exception as e:
            self.logger.error(
                f"Error writing event to the outbox - Event: {event_type}, Message ID: {event.channel.provider_event_id}, Error: {e}",
                exc_info=True,
            )
            return None

    def _insert_with_guard(
        self,
        *,
        sql: str,
        params: tuple,
        event_type: KafkaEventType,
        provider_event_id: str,
        rcpt: str | None,
    ) -> int | None:
        """INSERT into kafka_outbox with a commit-failure guard.

        `cur.lastrowid` is known before `conn.commit()`, so logging success
        right after the INSERT could report a row that a failed commit never
        persisted (an auto-increment ID used up with no row behind it).

        This guard:
          1. Wraps the commit in its own try/except to surface errors.
          2. Verifies persistence with a SELECT-after-INSERT in a fresh
             cursor on the same connection (sees the just-committed row,
             or proves it didn't land).
          3. Retries ONCE in a fresh transaction if the row vanished —
             covers transient deadlocks. Anything beyond is logged loud.

        Returns the persisted outbox_id, or None on permanent failure.
        """
        attempt = 0
        last_error: str | None = None
        while attempt < 2:
            attempt += 1
            try:
                with get_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(sql, params)
                        outbox_id = cur.lastrowid
                    try:
                        conn.commit()
                    except Exception as commit_err:
                        # Surface commit-time failures.
                        self.logger.error(
                            "[OUTBOX_COMMIT_FAIL] commit failed for event_type=%s "
                            "provider_event_id=%s rcpt=%s lastrowid=%s err=%s",
                            event_type, provider_event_id, rcpt, outbox_id, commit_err,
                            exc_info=True,
                        )
                        last_error = f"commit: {commit_err}"
                        continue  # retry

                    # Verify the row actually persisted. Same connection,
                    # post-commit, so this either sees the row or proves
                    # the INSERT was rolled back somewhere.
                    with conn.cursor() as verify_cur:
                        verify_cur.execute(
                            "SELECT id FROM kafka_outbox WHERE id = %s",
                            (outbox_id,),
                        )
                        verify_row = verify_cur.fetchone()
                    if verify_row is None:
                        # Auto-increment ID used up but the row did not land.
                        # Log it loudly.
                        self.logger.error(
                            "[OUTBOX_GHOST] INSERT logged success but SELECT-after-INSERT "
                            "found NO row. lastrowid=%s event_type=%s "
                            "provider_event_id=%s rcpt=%s payload_size=%d (attempt %d/2)",
                            outbox_id, event_type, provider_event_id, rcpt,
                            len(params[4]) if len(params) > 4 else 0, attempt,
                        )
                        last_error = f"ghost row at id={outbox_id}"
                        continue  # retry

                # Success path.
                self.logger.debug(
                    "Normalized event %s written to the outbox (ID: %s) for %s",
                    event_type, outbox_id, provider_event_id,
                )
                return outbox_id
            except Exception as e:
                self.logger.warning(
                    "[OUTBOX_INSERT_FAIL] attempt %d/2 raised: %s "
                    "(event_type=%s provider_event_id=%s)",
                    attempt, e, event_type, provider_event_id,
                    exc_info=True,
                )
                last_error = str(e)

        self.logger.error(
            "[OUTBOX_PERMANENT_FAIL] gave up after 2 attempts. event_type=%s "
            "provider_event_id=%s rcpt=%s last_error=%s",
            event_type, provider_event_id, rcpt, last_error,
        )
        return None

    def queue_kafka_event(
        self,
        event_type: KafkaEventType,
        message_id: str,
        rcpt: str | None,
        domain_name: str,
        payload_data: dict[str, Any],
        trace_context: dict[str, str] | None = None,
    ) -> bool:
        """
        Write a normalized event to the outbox.

        Args:
            event_type: Event type (sent, delivered, bounced, etc.)
            message_id: Message ID
            rcpt: Recipient address
            domain_name: Domain name
            payload_data: Payload data
            trace_context: Trace context for propagation (optional; when None, the current one is used)

        Returns:
            bool: True if the event was written
        """
        try:
            # Use the current trace context when none is given
            if trace_context is None:
                trace_context = _get_current_trace_context()

            # Build the normalized event with connect_lib.messaging
            event = _create_unified_event(
                event_type=event_type,
                message_id=message_id,
                rcpt=rcpt or "",
                domain_name=domain_name,
                payload_data=payload_data,
                trace_context=trace_context,
            )

            if trace_context:
                self.logger.debug(
                    f"[OTEL] Trace context added to the outbox event: trace_id={trace_context.get('trace_id')}"
                )

            outbox_id = self._insert_into_outbox(
                event=event,
                event_type=event_type,
                rcpt=rcpt,
                domain_name=domain_name,
            )

            if outbox_id:
                self.logger.info(
                    f"log_type=audit action=mail_event_outbox_enqueued "
                    f"event_type={event_type.value} message_id={message_id} "
                    f"recipient={rcpt} domain_name={domain_name} "
                    f"outbox_id={outbox_id} event_id={event.event_id}"
                )

            return outbox_id is not None

        except Exception as e:
            self.logger.error(
                f"Error writing event to the outbox - Event: {event_type}, Message ID: {message_id}, RCPT: {rcpt}, Error: {e}",
                exc_info=True,
            )
            return False

    def queue_email_sent(
        self,
        message_id: str,
        rcpt: str,
        domain_name: str,
        subject: str = None,
        sender_email: str = None,
        queue_id: str = None,
        trace_context: dict[str, str] | None = None,
        **extra_data,
    ) -> bool:
        """Write an email-sent event to the outbox.

        Args:
            message_id: Message ID
            rcpt: Recipient address
            domain_name: Domain name
            subject: Email subject
            sender_email: Sender address
            queue_id: Postfix queue ID
            trace_context: Trace context for propagation (optional)
            **extra_data: Extra payload data

        Returns:
            bool: True if the event was written
        """
        self.logger.debug(f"Processing SENT event for {message_id}")

        payload_data = {
            "subject": subject,
            "sender_email": sender_email,
            "queue_id": queue_id,
            "domain_name": domain_name,
            "recipient_count": 1,
            **extra_data,
        }

        return self.queue_kafka_event(
            event_type=KafkaEventType.SENT,
            message_id=message_id,
            rcpt=rcpt,
            domain_name=domain_name,
            payload_data=payload_data,
            trace_context=trace_context,
        )

    def queue_email_delivered(
        self,
        message_id: str,
        rcpt: str,
        domain_name: str,
        delivery_time: datetime = None,
        server_response: str = None,
        trace_context: dict[str, str] | None = None,
        **extra_data,
    ) -> bool:
        """Write an email-delivered event to the outbox."""
        self.logger.debug(f"Processing DELIVERED event for {message_id}")

        # Convert delivery_time to an ISO string if it is a datetime
        delivery_time_str = None
        if delivery_time:
            if isinstance(delivery_time, datetime):
                delivery_time_str = delivery_time.isoformat()
            elif isinstance(delivery_time, str):
                delivery_time_str = delivery_time

        payload_data = {
            "delivery_time": delivery_time_str,
            "server_response": server_response,
            **extra_data,
        }

        return self.queue_kafka_event(
            event_type=KafkaEventType.DELIVERED,
            message_id=message_id,
            rcpt=rcpt,
            domain_name=domain_name,
            payload_data=payload_data,
            trace_context=trace_context,
        )

    def queue_email_bounced(
        self,
        message_id: str,
        rcpt: str,
        domain_name: str,
        bounce_type: str,
        bounce_reason: str = None,
        bounce_code: str = None,
        server_response: str = None,
        trace_context: dict[str, str] | None = None,
        **extra_data,
    ) -> bool:
        """Write an email-bounced event to the outbox."""
        # Pick the event type from bounce_type
        if bounce_type in ["hard", "hard_bounce"]:
            event_type = KafkaEventType.HARD_BOUNCED
        elif bounce_type in ["soft", "soft_bounce"]:
            event_type = KafkaEventType.SOFT_BOUNCED
        else:
            event_type = KafkaEventType.BOUNCED

        payload_data = {
            "bounce_type": bounce_type,
            "bounce_reason": bounce_reason,
            "bounce_code": bounce_code,
            "server_response": server_response,
            **extra_data,
        }

        return self.queue_kafka_event(
            event_type=event_type,
            message_id=message_id,
            rcpt=rcpt,
            domain_name=domain_name,
            payload_data=payload_data,
            trace_context=trace_context,
        )

    def queue_email_suppressed(
        self,
        message_id: str,
        rcpt: str,
        domain_name: str,
        suppression_type: str,
        suppression_reason: str = None,
        **extra_data,
    ) -> bool:
        """Write an email-suppressed event to the outbox."""
        payload_data = {
            "suppression_type": suppression_type,
            "suppression_reason": suppression_reason,
            **extra_data,
        }

        return self.queue_kafka_event(
            event_type=KafkaEventType.SUPPRESSED,
            message_id=message_id,
            rcpt=rcpt,
            domain_name=domain_name,
            payload_data=payload_data,
        )

    def queue_email_deferred(
        self,
        message_id: str,
        rcpt: str,
        domain_name: str,
        dsn: str = None,
        trace_context: dict[str, str] | None = None,
        **extra_data,
    ) -> bool:
        """Write an email-deferred event to the outbox."""
        payload_data = {
            "dsn": dsn,
            **extra_data,
        }

        return self.queue_kafka_event(
            event_type=KafkaEventType.DEFERRED,
            message_id=message_id,
            rcpt=rcpt,
            domain_name=domain_name,
            payload_data=payload_data,
            trace_context=trace_context,
        )


# Global client instance
kafka_client = KafkaClient()
