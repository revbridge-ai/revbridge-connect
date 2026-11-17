"""
Email delivery events updater service

Processes Postfix delivery events and updates the database with the
delivery status. Works with the milter's tokens and Redis mappings to
attribute each event to its message.

Features:
- Processing of Postfix delivery events
- Integration with the milter's tokens and mappings
- Delivery status updates in the database
- Support for several input formats
"""

import asyncio
import concurrent.futures
import gzip
import json
import os
import re
import time
from datetime import datetime
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

# Configure observability (OTel) - must be before logging
from connect_lib.observability import instrument_fastapi, setup_observability

from mail_milter.common.config import (
    EVENTS_UPDATER_HOST,
    EVENTS_UPDATER_PORT,
    REDIS_TTL_SEC,
    setup_logging,
)
from mail_milter.common.db import exec_sql, fetch_all, fetch_one, get_conn
from mail_milter.common.email_config_cache import get_domain_id
from mail_milter.common.kafka_client import kafka_client
from mail_milter.common.redis_client import redis
from mail_milter.events_updater.async_bounce import (
    decode_verp_recipient,
    extract_dsn_diagnostic,
    extract_dsn_status,
    extract_original_message_ids,
)
from mail_milter.events_updater.feedback_report import parse_feedback_report

setup_observability(service_name="mail-events-service")

# JSON logging
logger = setup_logging("mail-events-service")

# Hostname suffix of the installation's OWN smarthosts/nodes: a hop to one of them is not
# the delivery to the recipient (that node logs the real delivery). Set by
# CONNECT_INTERNAL_RELAY_SUFFIX (the same value Vector uses); defaults to ".<DOMAIN>".
# Without either, ".invalid" (RFC 2606) never matches a real host.
_INSTALL_DOMAIN = (os.getenv("DOMAIN") or "").strip().lower()
_INTERNAL_RELAY_SUFFIX = (
    os.getenv("CONNECT_INTERNAL_RELAY_SUFFIX")
    or (f".{_INSTALL_DOMAIN}" if _INSTALL_DOMAIN else ".invalid")
).strip().lower()


def _is_internal_host(host: str | None) -> bool:
    h = (host or "").strip().lower().rstrip(".")
    return h.endswith(_INTERNAL_RELAY_SUFFIX) or h == _INTERNAL_RELAY_SUFFIX.lstrip(".")


def bounce_relay_host(log_line: str | None) -> str | None:
    """The hop that refused a message: the `relay=` host of the Postfix line.

    Not the syslog host of the event: that is the node that LOGGED the line, which is always
    one of this installation's own nodes, so it would make every bounce look internal.
    """
    match = re.search(r"relay=([^\s,\[]+)", log_line or "")
    return match.group(1) if match else None


def event_relay_host(event: dict) -> str | None:
    """`relay=` host of a structured event: Vector's grok field, else parsed from the line."""
    relay = event.get("relay_name") or event.get("relay")
    if relay:
        return relay
    for text in (event.get("status_msg"), event.get("message")):
        host = bounce_relay_host(text)
        if host:
            return host
    return None

# OTel tracer for business spans
from opentelemetry import trace
from opentelemetry.trace import StatusCode
from connect_lib.observability import create_span_from_remote_context, get_tracer

otel_tracer = get_tracer("mail-events-service")


def normalize_timestamp(timestamp_str: str) -> str:
    """
    Normalize a timestamp to a MySQL-compatible format

    Args:
        timestamp_str: Timestamp string (ISO 8601, Unix timestamp, etc.)

    Returns:
        str: Timestamp in MySQL format 'YYYY-MM-DD HH:MM:SS'
    """
    if not timestamp_str:
        return None

    try:
        # ISO 8601 with 'Z': drop the 'Z' and replace 'T' with a space
        if "T" in timestamp_str and timestamp_str.endswith("Z"):
            timestamp_str = timestamp_str.replace("T", " ").replace("Z", "")

        # Parse as a datetime
        if "T" in timestamp_str:
            # ISO 8601
            dt = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
        elif timestamp_str.isdigit():
            # Unix timestamp
            dt = datetime.fromtimestamp(int(timestamp_str))
        else:
            # Plain string
            dt = datetime.fromisoformat(timestamp_str)

        # Return in MySQL format
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    except Exception as e:
        logger.warning(f"Error normalizing timestamp '{timestamp_str}': {e}")
        # Fall back to the current time
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def normalize_timestamp_to_iso(timestamp_str: str) -> str:
    """
    Normalize a timestamp to ISO 8601 with microseconds

    Args:
        timestamp_str: Timestamp string (ISO 8601, Unix timestamp, etc.)

    Returns:
        str: Timestamp in ISO 8601 format 'YYYY-MM-DDTHH:MM:SS.ffffff'
    """
    if not timestamp_str:
        return None

    try:
        # Parse as a datetime
        if "T" in timestamp_str:
            # ISO 8601
            dt = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
        elif timestamp_str.isdigit():
            # Unix timestamp
            dt = datetime.fromtimestamp(int(timestamp_str))
        else:
            # Plain string (MySQL format)
            dt = datetime.fromisoformat(timestamp_str)

        # Return ISO 8601 with microseconds
        return dt.isoformat()

    except Exception as e:
        logger.warning(
            f"Error normalizing timestamp to ISO '{timestamp_str}': {e}")
        # Fall back to the current time
        return datetime.now().isoformat()


# Reputation rejection of the SENDING IP (DNSBL/RBL) that arrives without an enhanced
# code: `550 Email blocked by dnsbl.spfbl.net` becomes dsn=5.0.0 in Postfix. Do NOT use
# `listed` alone: "User not listed in public Exchange directory" is a genuinely invalid
# recipient. `barracudacentral` (the list), never `barracuda*`: many recipients' MX is
# `*.ess.barracudanetworks.com`, and the host name is not a reason.
_BLOCKLIST_RE = re.compile(
    r"\b(dnsbl|rbl|spfbl|spamhaus|spamcop|barracudacentral|sorbs|uceprotect|psbl|surriel|abusix)\b"
    r"|\b(black|block)[ -]?list"
    r"|\bblocked (using|by)\b"
    r"|\b(poor|bad|low|ip) reputation\b",
    re.IGNORECASE,
)
# Phrases that keep the bounce hard even when they mention a blacklist: the generic
# text "The account or domain may not exist, they may be blacklisted" (cPanel/Exim)
# covers a nonexistent address, and "on the recipient's blacklist" is the recipient
# blocking the sender. Suppressing is right in both cases.
_NOT_OUR_IP_RE = re.compile(
    r"\b(may|does) not exist\b|\buser unknown\b|\bunknown user\b"
    r"|\bno such (user|mailbox)\b|\brecipient'?s blacklist\b",
    re.IGNORECASE,
)


def _remote_reply(status_msg: str) -> str:
    """Only the remote MX's reply: what follows the last `said:`. The Postfix line
    also has `relay=<host>` and `host <host> said:`; the host name must not be read
    as the reason for the rejection."""
    _, sep, reply = status_msg.rpartition(" said:")
    return reply if sep else status_msg


def is_blocklist_rejection(status_msg: str) -> bool:
    reply = _remote_reply(status_msg or "")
    return bool(_BLOCKLIST_RE.search(reply)) and not _NOT_OUR_IP_RE.search(reply)


def classify_bounce_type(dsn: str, status_msg: str = "") -> str:
    """Classify the bounce type from the Postfix DSN.

    - ``4.x.x``  -> ``soft``     (temporary failure)
    - ``5.5.x``  -> ``protocol`` (SMTP protocol/syntax error: a problem on the
      sending path, NEVER the recipient's fault; does not suppress)
    - ``5.7.x``  -> ``policy``   (policy rejection: relay quota/limit, block,
      reputation). NOT an invalid recipient, and must NOT suppress.
    - ``5.x.x``  -> ``hard``     (invalid recipient / real permanent failure)
    - empty/unknown -> ``hard`` (no DSN: assumed permanent)
    - ``5.x.x``/empty whose ``status_msg`` (the MX reply) mentions a DNSBL/blocklist
      -> ``policy``: the MX refused the sending IP, not the address (for example
      ``550 Email blocked by dnsbl.spfbl.net`` is logged as 5.0.0), and the same
      address can still receive from other IPs.

    Mirrors the convention of the ``vw_bounce_breakdown`` view (5.7.% = policy_bounces,
    other 5.% = hard_bounces). Treating policy or protocol bounces as hard would
    suppress valid addresses.
    """
    d = (dsn or "").strip()
    if d.startswith("4."):
        return "soft"
    if d.startswith("5.5."):
        return "protocol"
    if d.startswith("5.7."):
        return "policy"
    if is_blocklist_rejection(status_msg):
        return "policy"
    return "hard"


def map_delivery_status(
    vector_status: str, dsn: str = "", current_status: str = None
) -> str:
    """
    Map the delivery status from the Postfix DSN

    Args:
        vector_status: Status received from Vector
        dsn: Postfix Delivery Status Notification code
        current_status: Current status in the database (if any)

    Returns:
        str: Status mapped from the DSN
    """
    # Normalize status and DSN
    normalized_status = vector_status.lower().strip()
    normalized_dsn = dsn.strip() if dsn else ""

    logger.debug(
        f"[STATUS_MAPPING] Vector status: '{vector_status}' | DSN: '{dsn}'")

    # DSN-based mapping (Postfix convention)
    if normalized_dsn:
        dsn_prefix = (
            normalized_dsn.split(
                ".")[0] if "." in normalized_dsn else normalized_dsn
        )

        if dsn_prefix == "2":
            # 2.x.x = successful delivery to the final recipient
            mapped_status = "delivered"
            logger.debug(
                f"[STATUS_MAPPING] DSN {dsn} means successful delivery -> status: 'delivered'"
            )

        elif dsn_prefix == "4":
            # 4.x.x = temporary failure
            mapped_status = "deferred"
            logger.debug(
                f"[STATUS_MAPPING] DSN {dsn} means temporary failure -> status: 'deferred'"
            )

        elif dsn_prefix == "5":
            # 5.x.x = permanent failure
            mapped_status = "bounced"
            logger.debug(
                f"[STATUS_MAPPING] DSN {dsn} means permanent failure -> status: 'bounced'"
            )

        else:
            # Unknown DSN: use the Vector status
            mapped_status = normalized_status
            logger.warning(
                f"[STATUS_MAPPING] DSN {dsn} not recognized, using the Vector status: '{mapped_status}'"
            )

    else:
        # No DSN: use the Vector status
        mapped_status = normalized_status
        logger.debug(
            f"[STATUS_MAPPING] No DSN, using the Vector status: '{mapped_status}'"
        )

    # Status transition checks (following how Postfix reports deliveries)
    if current_status:
        logger.debug(
            f"[STATUS_MAPPING] Current status in the database: '{current_status}'")

        # DSN-based transition rules
        if current_status == "sent" and mapped_status == "delivered":
            # Allow sent -> delivered (delivery confirmation)
            logger.debug(
                "[STATUS_MAPPING] Transition allowed: 'sent' -> 'delivered' (delivery confirmation)"
            )

        elif current_status == "sent" and mapped_status == "deferred":
            # Allow sent -> deferred (another attempt)
            logger.debug(
                "[STATUS_MAPPING] Transition allowed: 'sent' -> 'deferred' (another attempt)"
            )

        elif current_status == "deferred" and mapped_status == "delivered":
            # Allow deferred -> delivered (success after a retry)
            logger.debug(
                "[STATUS_MAPPING] Transition allowed: 'deferred' -> 'delivered' (success after a retry)"
            )

        elif current_status == "bounced" and mapped_status in [
            "sent",
            "deferred",
            "delivered",
        ]:
            # Never move away from bounced (permanent failure)
            logger.warning(
                f"[STATUS_MAPPING] WARNING: not changing 'bounced' to '{mapped_status}' - keeping 'bounced'"
            )
            return "bounced"

        # Log the transition
        if mapped_status != current_status:
            logger.debug(
                f"[STATUS_MAPPING] Status transition: '{current_status}' -> '{mapped_status}'"
            )
        else:
            logger.debug(
                f"[STATUS_MAPPING] Status unchanged: '{mapped_status}'")

    logger.debug(f"[STATUS_MAPPING] Final status: '{mapped_status}'")
    return mapped_status


def trigger_kafka_event(
    event_type: str,
    message_id: str,
    recipient_email: str,
    domain_name: str,
    trace_context: dict = None,
    **extra_data,
):
    """
    Write an event to the outbox (kafka_client)

    Args:
        event_type: Event type (sent, delivered, bounced, etc.)
        message_id: Message ID
        recipient_email: Recipient address
        domain_name: Domain name
        trace_context: Trace context for propagation (optional)
        **extra_data: Extra payload data
    """
    logger.debug(f"Writing outbox event {event_type} for {message_id}")

    # Build the kafka_client arguments for the event type
    kafka_fn = None
    kafka_kwargs = {}

    # Connect does not store message headers, so delivery events carry
    # campaign_id=None; the key stays in the payload (same format).
    campaign_id = None

    if event_type == "sent":
        kafka_fn = kafka_client.queue_email_sent
        kafka_kwargs = dict(
            message_id=message_id,
            rcpt=recipient_email,
            domain_name=domain_name,
            **extra_data,
        )
    elif event_type == "delivered":
        delivery_time = extra_data.pop("delivery_time", None)
        server_response = extra_data.pop("server_response", None)

        logger.debug(
            f"[KAFKA_DELIVERED] Writing DELIVERED outbox event, message_id: {message_id}, for {recipient_email}"
        )

        kafka_fn = kafka_client.queue_email_delivered
        kafka_kwargs = dict(
            message_id=message_id,
            rcpt=recipient_email,
            domain_name=domain_name,
            delivery_time=delivery_time,
            server_response=server_response,
            campaign_id=campaign_id,
            trace_context=trace_context,
            **extra_data,
        )
    elif event_type == "bounced":
        logger.debug(
            f"[KAFKA_BOUNCED] Writing BOUNCED outbox event for message_id: {message_id}"
        )

        bounce_reason = extra_data.get("dsn", "Bounce detected")
        kafka_fn = kafka_client.queue_email_bounced
        # Remove dsn from extra_data since it's already used in bounce_reason
        bounced_extra = {k: v for k, v in extra_data.items() if k != "dsn"}
        kafka_kwargs = dict(
            message_id=message_id,
            rcpt=recipient_email,
            domain_name=domain_name,
            bounce_type=classify_bounce_type(
                extra_data.get("dsn", ""), extra_data.get("status_msg", "")
            ),
            bounce_reason=bounce_reason,
            campaign_id=campaign_id,
            trace_context=trace_context,
            **bounced_extra,
        )
    elif event_type == "deferred":
        logger.debug(
            f"[KAFKA_DEFERRED] Writing DEFERRED outbox event for message_id: {message_id}"
        )

        bounce_reason = extra_data.get("dsn", "Temporary failure")
        kafka_fn = kafka_client.queue_email_deferred
        # Remove dsn from extra_data to avoid duplicate keyword argument
        deferred_extra = {k: v for k, v in extra_data.items() if k != "dsn"}
        kafka_kwargs = dict(
            message_id=message_id,
            rcpt=recipient_email,
            domain_name=domain_name,
            dsn=bounce_reason,
            campaign_id=campaign_id,
            trace_context=trace_context,
            **deferred_extra,
        )

    if kafka_fn is None:
        logger.debug(f"Event type '{event_type}' has no outbox mapping, skipping")
        return

    # Retry with backoff
    outbox_ok = False
    for attempt in range(1, 4):
        try:
            result = kafka_fn(**kafka_kwargs)
            if result:
                logger.debug(f"Outbox event {event_type} written for {message_id}")
                outbox_ok = True
                break
            else:
                logger.warning(
                    f"Outbox returned False (attempt {attempt}/3) event={event_type} message_id={message_id}"
                )
        except Exception as e:
            logger.error(
                f"Error writing outbox event {event_type} (attempt {attempt}/3): {e}"
            )
        if attempt < 3:
            time.sleep(attempt)  # backoff: 1s, 2s
    if not outbox_ok:
        logger.error(
            f"CRITICAL FAILURE: {event_type} NOT written to the outbox after 3 attempts! "
            f"message_id={message_id} rcpt={recipient_email}"
        )


ASYNC_BOUNCE_LOOKBACK_DAYS = int(os.getenv("ASYNC_BOUNCE_LOOKBACK_DAYS", "14"))


def bounce_matches_send(rcpt: str, rbm_domain: str, message_ids: list[str]) -> bool:
    """Does the asynchronous DSN refer to a real send of this installation?

    The bounce (VERP) address is public, so a DSN is only trusted when it matches a
    send: with the original message IDs in the DSN, the (message_id, recipient) pair
    must be in send_ledger. Without IDs (an MTA that does not return the original
    headers), a recent send from the domain to that recipient is required.
    """
    rcpt = (rcpt or "").strip().lower()
    if not rcpt:
        return False
    for mid in message_ids[:4]:
        if fetch_one(
            "SELECT 1 AS ok FROM send_ledger WHERE message_id = %s AND LOWER(rcpt) = %s LIMIT 1",
            (mid, rcpt),
        ):
            return True
    if message_ids:
        return False
    base_domain = rbm_domain.split(".", 1)[1] if "." in rbm_domain else rbm_domain
    return bool(
        fetch_one(
            "SELECT 1 AS ok FROM send_ledger WHERE LOWER(rcpt) = %s AND domain_name IN (%s, %s) "
            "AND admitted_at >= NOW() - INTERVAL %s DAY LIMIT 1",
            (rcpt, base_domain, rbm_domain, ASYNC_BOUNCE_LOOKBACK_DAYS),
        )
    )


def handle_feedback_report(arf: dict, envelope_rcpt: str = "") -> dict:
    """Spam complaint (ARF) -> 'complaint' suppression + 'complaint' webhook.

    Only accepts a report that points to a real send (message_id + recipient in
    send_ledger): the bounce address is public, so a report that matches no send
    is ignored.
    """
    rcpt = (arf.get("rcpt") or "").strip().lower()
    if not rcpt or not arf.get("message_ids"):
        logger.info("[FBL] report without a recipient or a message ID - ignored")
        return {"processed": False, "complaint": False, "reason": "arf_incomplete"}
    row = None
    for mid in arf["message_ids"][:4]:
        row = fetch_one(
            "SELECT message_id, domain_name FROM send_ledger WHERE message_id = %s AND LOWER(rcpt) = %s LIMIT 1",
            (mid, rcpt),
        )
        if row:
            break
    if not row:
        logger.warning(f"[FBL] complaint from {rcpt} with no matching send - ignored")
        return {"processed": False, "complaint": False, "reason": "unknown_message"}
    message_id = row["message_id"]
    base_domain = (row.get("domain_name") or arf.get("from_domain") or "").lower()
    verp = arf.get("verp_domain")
    if not verp and "@" in envelope_rcpt:
        dom = envelope_rcpt.rsplit("@", 1)[1].lower()
        verp = dom if dom.startswith("rbm") else None
    # Bounce and unsubscribe suppressions live on the VERP domain (rbm<n>.<domain>): same rule.
    # The report's VERP domain only counts if it belongs to the sending domain (never another one).
    if base_domain:
        domain_name = verp if verp and verp.endswith("." + base_domain) else base_domain
    else:
        domain_name = verp
    if not domain_name:
        return {"processed": False, "complaint": False, "reason": "no_domain"}
    feedback = arf.get("feedback_type") or "abuse"
    exec_sql(
        """
        INSERT INTO suppressions (email, domain_name, suppression_type, reason, expires_at, is_active)
        VALUES (%s, %s, 'complaint', %s, NULL, TRUE)
        ON DUPLICATE KEY UPDATE
            suppression_type = 'complaint',
            reason = VALUES(reason),
            expires_at = NULL,
            is_active = TRUE,
            suppressed_at = CURRENT_TIMESTAMP
        """,
        (rcpt, domain_name, f"Spam complaint (FBL {feedback}) - message_id: {message_id}"[:255]),
    )
    try:
        from mail_milter.common.webhook_client import WebhookEventType as _HookEvent, webhook_client

        webhook_client.queue_webhook_event(
            _HookEvent.COMPLAINT,
            message_id,
            {
                "complaint_type": feedback,
                "complaint_feedback": arf.get("user_agent"),
                "source_ip": arf.get("source_ip"),
                "arrival_date": arf.get("arrival_date"),
                "reported_domain": arf.get("reported_domain"),
            },
            domain_name=domain_name,
            recipient_email=rcpt,
        )
    except Exception as e:  # noqa: BLE001
        logger.error(f"[FBL] complaint webhook not queued: {e}")
    logger.info(f"[FBL] complaint recorded: rcpt={rcpt} domain={domain_name} message_id={message_id} type={feedback}")
    return {"processed": True, "complaint": True, "rcpt": rcpt, "domain_name": domain_name,
            "message_id": message_id, "feedback_type": feedback}


def apply_suppression_rules_for_bounce(
    rcpt: str, domain_name: str, bounce_type: str = "hard"
):
    """
    Suppress an address immediately on a HARD bounce

    Args:
        rcpt: Recipient address
        domain_name: Domain name
        bounce_type: Bounce type (hard/soft/policy), stored in the suppression
            for diagnostics. Only call this for hard bounces.
    """
    try:
        logger.info(
            f"[SUPPRESSION] Suppressing address after a bounce: {rcpt} on {domain_name}"
        )

        # Suppress IMMEDIATELY, with no further rules
        exec_sql(
            """
            INSERT INTO suppressions (email, domain_name, suppression_type, bounce_type, reason, expires_at, is_active)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                bounce_type = VALUES(bounce_type),
                reason = VALUES(reason),
                expires_at = VALUES(expires_at),
                is_active = TRUE,
                suppressed_at = CURRENT_TIMESTAMP
        """,
            (
                rcpt,
                domain_name,
                "bounce",
                bounce_type,
                "Automatic suppression - hard bounce detected",
                None,  # No expiry: permanent suppression
                True,
            ),
        )

        # Fetch the ID of the suppression just created
        suppression_result = fetch_one(
            """
            SELECT id, suppression_type, reason, expires_at
            FROM suppressions
            WHERE email = %s AND domain_name = %s AND is_active = TRUE
        """,
            (rcpt, domain_name),
        )

        if suppression_result:
            logger.info(
                f"[SUPPRESSION] Address {rcpt} suppressed automatically after a bounce"
            )

            # Record the suppression event
            exec_sql(
                """
                INSERT INTO suppression_events (suppression_id, email, domain_name, event_type, new_status, reason, user_id, ip_address)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
                (
                    suppression_result["id"],
                    rcpt,
                    domain_name,
                    "created",
                    True,
                    "Automatic suppression triggered by bounce",
                    "system",
                    "events_updater",
                ),
            )

            return {
                "suppressed": True,
                "suppression_type": suppression_result["suppression_type"],
                "reason": suppression_result["reason"],
                "expires_at": suppression_result["expires_at"],
            }
        else:
            logger.warning(
                f"[SUPPRESSION] Failed to create the suppression for {rcpt}")
            return {"suppressed": False, "error": "Failed to create suppression"}

    except Exception as e:
        logger.error(f"[SUPPRESSION] Error suppressing address {rcpt}: {e}")
        return {"suppressed": False, "error": str(e)}


app = FastAPI(
    title="Events Updater",
    description="Email delivery events updater",
    version="1.0.0",
)
instrument_fastapi(app)

# CORS, for frontend integration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # internal service, not reachable from the internet
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Dedicated, bounded executor for the heavy ingestion path (/postfix, /batch).
#
# Ingestion runs in its own pool so that large /postfix batches never occupy the
# event loop's default executor (min(32, cpu+4) threads), which stays free for the
# health checks and the light endpoints; otherwise /health/ready could queue behind
# ingestion and time out. The limit stays below the MySQL pool size (25,
# common/db.py) so ingestion cannot exhaust the pool.
INGEST_EXECUTOR_WORKERS = int(os.getenv("INGEST_EXECUTOR_WORKERS", "12"))
_ingest_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=INGEST_EXECUTOR_WORKERS,
    thread_name_prefix="ingest",
)

# ThreadPoolExecutor uses an UNBOUNDED internal queue (queue.SimpleQueue). When the
# POST /postfix and /batch rate exceeds the workers (e.g. a burst of Vector retries),
# that queue would grow without limit, holding each queued item's payload, _WorkItem
# and Future in memory.
#
# The semaphore is acquired BEFORE await request.body(): the excess waits in the
# event loop without holding the full body in memory. It also caps the in-flight
# submits at the number of workers, so the executor's internal queue never grows.
_ingest_semaphore = asyncio.Semaphore(INGEST_EXECUTOR_WORKERS)


async def _run_ingest(func, *args):
    """Run ingestion work in the dedicated executor, outside the loop's default pool."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_ingest_executor, func, *args)


async def _read_ingest_body(request: Request) -> bytes:
    # Vector's http sink with `compression = "gzip"` (config/vector/vector.toml) sets
    # Content-Encoding: gzip; uvicorn/Starlette do NOT decompress automatically, and
    # `payload.decode("utf-8")` would fail on the gzip magic bytes. Decompress here.
    payload = await request.body()
    encoding = request.headers.get("content-encoding", "").lower()
    if encoding == "gzip":
        try:
            payload = gzip.decompress(payload)
        except (OSError, EOFError) as e:
            # gzip raises BadGzipFile/OSError on bad header and EOFError on
            # truncated streams — both come back as 400 so Vector can retry.
            raise HTTPException(
                status_code=400,
                detail=f"invalid_gzip: {e!s}",
            ) from e
    return payload


# SQL for delivery operations
#
# Atomic deduplication gate. A separate "SELECT status, then UPSERT" is not atomic:
# two concurrent calls for the same (message_id, rcpt) (across replicas or across
# the _ingest_executor threads) could both read the old status, both conclude that
# the status changed and both enqueue the event in the outbox (a duplicate).
#
# So "did it change?" comes from the rowcount itself, serialized on the UNIQUE KEY:
#   1) UPDATE_DELIVERY_ON_CHANGE: moves an EXISTING row only if the status differs
#      (WHERE status <> new). rowcount==1 => real transition.
#   2) INSERT_DELIVERY_IF_ABSENT: if (1) matched nothing (no row, OR same status),
#      try to insert; ON DUPLICATE KEY is a no-op. rowcount==1 => first occurrence
#      (insert); rowcount==0 => it already existed with the same status (duplicate).
# No SELECT ... FOR UPDATE on a missing row (that would cause gap locks/deadlocks):
# the first-seen serialization happens on the INSERT's UNIQUE KEY.
UPDATE_DELIVERY_ON_CHANGE = """
UPDATE deliveries
SET status = %s,
    domain_id = %s,
    last_update = CURRENT_TIMESTAMP,
    attempt = CASE WHEN %s = 'deferred' THEN attempt + 1 ELSE attempt END
WHERE message_id = %s AND rcpt = %s AND status <> %s
"""

INSERT_DELIVERY_IF_ABSENT = """
INSERT INTO deliveries (message_id, rcpt, domain_name, domain_id, status, attempt, last_update, first_seen)
VALUES (%s, %s, %s, %s, %s, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
ON DUPLICATE KEY UPDATE message_id = message_id
"""

INSERT_EVENT = """
INSERT INTO delivery_events (message_id, rcpt, domain_name, domain_id, queue_id, status, dsn, attempt, host, service, timestamp, ts)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
ON DUPLICATE KEY UPDATE
    dsn      = VALUES(dsn),
    status   = VALUES(status),
    attempt  = VALUES(attempt),
    ts       = CURRENT_TIMESTAMP
"""


def upsert_delivery_returning_changed(
    message_id: str,
    rcpt: str,
    domain_name: str,
    domain_id,
    mapped_status: str,
) -> bool:
    """Delivery UPSERT that returns whether THIS call made a real status transition.

    It is the atomic deduplication gate for the outbox enqueue: concurrent calls
    for the same (message_id, rcpt) serialize on the UNIQUE KEY and only the winner
    gets ``True``, so only it enqueues the downstream event. Reading the current
    status first and comparing after the UPSERT would leave a window between read
    and write in which reprocessing could emit duplicates.

    Opens no explicit transaction: the pool is ``autocommit=True``, so each statement
    is atomic and the first-seen serialization happens on the INSERT's UNIQUE KEY,
    avoiding the gap lock/deadlock that a ``SELECT ... FOR UPDATE`` on a missing row
    would cause.

    Returns:
        True  -> real transition (an existing row changed status) OR first
                 occurrence (insert); the caller MUST enqueue in the outbox.
        False -> the row already existed with this same status (reprocessing); skip the enqueue.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            # (1) Move an existing row only if the status actually changed.
            cur.execute(
                UPDATE_DELIVERY_ON_CHANGE,
                (mapped_status, domain_id, mapped_status,
                 message_id, rcpt, mapped_status),
            )
            if cur.rowcount == 1:
                conn.commit()
                return True
            # (2) rowcount==0: no row OR same status. Insert or no-op;
            #     rowcount==1 => inserted (first seen); rowcount==0 => duplicate, same status.
            cur.execute(
                INSERT_DELIVERY_IF_ABSENT,
                (message_id, rcpt, domain_name, domain_id, mapped_status),
            )
            changed = cur.rowcount == 1
        conn.commit()
    return changed


# ---------------------------------------------------------------------------
# send_ledger reconciliation (durability).
# The ledger's REAL terminal point is the confirmed exit from the local Postfix
# queue, i.e. when the relay accepts the message (Postfix logs status=sent), NOT
# the 250 that Postfix returns at injection (that only means "queued", and a
# queued message can be lost if the container restarts). From 'sent' on, the
# message has left the Postfix queue, so it cannot be lost and the reaper must not
# re-inject it. 'delivered' (DSN 2.x.x) is an upgrade.
# Guarded by state IN (non-terminal): idempotent, never revives failed/deadletter
# nor marks twice. Best-effort / fail-open: it must NEVER break delivery
# processing (same approach as the ledger DAO).
# ---------------------------------------------------------------------------
MARK_LEDGER_DELIVERED = """
UPDATE send_ledger
SET state='delivered', delivered_at=NOW(), terminal_at=NOW(),
    lease_expires_at=NULL, next_attempt_at=NULL
WHERE message_id = %s AND rcpt = %s
  AND state IN ('queued','sending','smtp_accepted','deferred')
"""


def reconcile_send_ledger_delivered(message_id: str, rcpt: str) -> None:
    """Close the delivery in send_ledger when the relay confirms the exit
    (status=sent/delivered). Best-effort: an error here is only logged."""
    try:
        exec_sql(MARK_LEDGER_DELIVERED, (message_id, rcpt))
    except Exception as e:
        logger.warning(
            f"[LEDGER] reconcile delivered failed {message_id}->{rcpt}: {e}"
        )


# Bounce (DSN 5.x) = the message left the Postfix queue WITHOUT being delivered and
# will NOT be retried: terminal 'failed'. Marking it keeps bounced rows out of
# smtp_accepted, where the reaper would re-inject them (re-sending a bounce hurts
# reputation). Guarded by state IN (never revives a terminal row).
MARK_LEDGER_FAILED = """
UPDATE send_ledger
SET state='failed', terminal_at=NOW(), lease_expires_at=NULL,
    next_attempt_at=NULL, last_error=%s
WHERE message_id = %s AND rcpt = %s
  AND state IN ('queued','sending','smtp_accepted','deferred')
"""

# Deferred (DSN 4.x) = Postfix is ALIVE and retrying this message, so the reaper must
# leave it alone. Each deferred event PUSHES delivery_deadline forward (7200s, more
# than Postfix's maximal_backoff of 4000s): if the node dies with its queue, the
# events stop, the deadline expires and the reaper re-injects through the remaining
# nodes in about 2 hours.
LEDGER_DEFERRED_EXTEND_SEC = 7200
EXTEND_LEDGER_DEADLINE = """
UPDATE send_ledger
SET delivery_deadline = NOW() + INTERVAL %s SECOND
WHERE message_id = %s AND rcpt = %s AND state = 'smtp_accepted'
"""


def reconcile_send_ledger_failed(message_id: str, rcpt: str, dsn: str) -> None:
    """Mark a bounced message as terminal in the ledger. Best-effort."""
    try:
        exec_sql(MARK_LEDGER_FAILED, (f"bounced dsn={dsn}", message_id, rcpt))
    except Exception as e:
        logger.warning(
            f"[LEDGER] reconcile failed did not apply {message_id}->{rcpt}: {e}"
        )


def reconcile_send_ledger_deferred(message_id: str, rcpt: str) -> None:
    """Push delivery_deadline forward: Postfix keeps retrying. Best-effort."""
    try:
        exec_sql(EXTEND_LEDGER_DEADLINE, (LEDGER_DEFERRED_EXTEND_SEC, message_id, rcpt))
    except Exception as e:
        logger.warning(
            f"[LEDGER] reconcile deferred failed {message_id}->{rcpt}: {e}"
        )


def get_attempt_count(message_id: str, rcpt: str) -> int:
    """Get the number of attempts of one delivery"""
    try:
        result = fetch_one(
            "SELECT attempt FROM deliveries WHERE message_id = %s AND rcpt = %s",
            (message_id, rcpt),
        )
        return result["attempt"] if result else 1
    except Exception as e:
        logger.error(f"Error getting the attempt count: {e}")
        return 1


def extract_from_message(message_str: str) -> tuple[str | None, str | None]:
    """Extract queue_id and rcpt from a raw Postfix log line.

    Module-level (not a closure inside ingest_postfix()) so that the MGET
    prefetch can reuse the same extraction: most Vector events arrive as log
    lines in the `message` field rather than as structured fields, and without
    this extraction the prefetch would be empty.
    """
    if not message_str:
        return None, None

    extracted_queue_id = None
    extracted_rcpt = None

    # 5+ hex chars: Postfix queue IDs range from 5 to 15 characters depending on
    # PID/inode (short ones such as 7176A17 must match too).
    queue_match = re.search(r"\b([A-F0-9]{5,}):\s+to=", message_str)
    if queue_match:
        extracted_queue_id = queue_match.group(1)

    for pattern in (r"to=<([^>]+)>", r"to=([^\s,]+)"):
        rcpt_match = re.search(pattern, message_str, re.IGNORECASE)
        if rcpt_match:
            candidate = rcpt_match.group(1).strip()
            if "@" in candidate:
                extracted_rcpt = candidate
                break

    return extracted_queue_id, extracted_rcpt


def find_message_id_by_queue(
    queue_id: str,
    rcpt: str,
    cache: dict | None = None,
) -> str | None:
    """
    Look up the message_id in Redis by queue_id and recipient

    Args:
        queue_id: Postfix queue ID
        rcpt: Recipient address
        cache: Optional map `{redis_key: parsed_mapping_or_None}` filled by a
               prefetch before the event loop. When present, it avoids a Redis
               GET for keys already loaded in batch with MGET.

    Returns:
        str: Message ID if found, None otherwise
    """
    # Span for the Redis lookup
    span = None
    parent_trace_id = None
    parent_span_id = None
    primary_key = f"q:{queue_id}:{rcpt.lower()}"

    # Look in the cache first; if absent, do 1 Redis GET and reuse the same
    # mapping for both the trace_id and the message_id.
    primary_mapping = None
    if cache is not None and primary_key in cache:
        primary_mapping = cache[primary_key]
    else:
        try:
            val = redis.get(primary_key)
            if val:
                primary_mapping = json.loads(val)
        except Exception:
            primary_mapping = None
        if cache is not None:
            cache[primary_key] = primary_mapping

    if primary_mapping:
        parent_trace_id = primary_mapping.get("trace_id")
        parent_span_id = primary_mapping.get("span_id")
        if parent_trace_id:
            logger.debug(f"[OTEL] Milter trace ID found in Redis: {parent_trace_id}, Span ID: {parent_span_id}")

    logger.debug("[OTEL] ===== STARTING find_message_id_by_queue =====")
    try:
        # With the milter's trace_id, create the span as a CHILD of that trace
        if parent_trace_id:
            logger.debug(f"[OTEL] Creating span recipient.lookup as a CHILD of the milter trace (trace_id: {parent_trace_id}, span_id: {parent_span_id})")
            span = create_span_from_remote_context(
                otel_tracer,
                parent_trace_id,
                parent_span_id,
                "recipient.lookup",
            )
        else:
            # Without the milter's trace_id, create a normal (independent) span
            logger.debug(f"[OTEL] Creating span recipient.lookup - queue_id: {queue_id}, rcpt: {rcpt}")
            span = otel_tracer.start_span("recipient.lookup")
        if span:
            span.set_attribute("recipient.lookup.queue_id", str(queue_id))
            span.set_attribute("recipient.lookup.rcpt", str(rcpt))
            logger.debug(f"[OTEL] Span recipient.lookup created - queue_id: {queue_id}, rcpt: {rcpt}")
            logger.debug(f"[OTEL] Span ID: {format(span.get_span_context().span_id, '016x') if hasattr(span, 'get_span_context') else 'N/A'}, Trace ID: {format(span.get_span_context().trace_id, '032x') if hasattr(span, 'get_span_context') else 'N/A'}")
    except Exception as e:
        logger.error(f"[OTEL] Error creating span recipient.lookup: {e}", exc_info=True)
        span = None

    try:
        # Reuse the mapping already loaded (cache or the GET above)
        key = primary_key
        val = primary_mapping  # already the parsed dict (or None)

        if val:
            mapping = val  # primary_mapping is already a dict
            message_id = mapping.get("message_id")
            logger.debug(f"[REDIS_LOOKUP] Key {key} found - mapping: {mapping}")
            if message_id:
                logger.debug(
                    f"[REDIS_LOOKUP] Message ID found through the specific key: {key} -> {message_id}"
                )
                if span:
                    try:
                        span.set_attribute("recipient.lookup.found", True)
                        span.set_attribute("recipient.lookup.message_id", str(message_id))
                        # Take the trace_id from the mapping, if any, and add standard correlation tags
                        trace_id = mapping.get("trace_id")
                        if trace_id:
                            # Standard tags without prefix, for search
                            span.set_attribute("milter_trace_id", str(trace_id))
                            span.set_attribute("trace_id_milter", str(trace_id))
                            # Unified correlation tag: finds all related traces
                            span.set_attribute("correlation_id", str(trace_id))
                            # Prefixed tags, for context
                            span.set_attribute("recipient.lookup.trace_id", str(trace_id))
                            span.set_attribute("recipient.lookup.milter_trace_id", str(trace_id))
                            logger.debug(f"[OTEL] Trace ID {trace_id} found in the Redis mapping (specific key) and added for correlation")
                        span.end()
                        logger.debug("[OTEL] Span recipient.lookup finished - message_id found (specific key)")
                    except Exception as e:
                        logger.warning(f"[OTEL] Error finishing span redis.lookup: {e}")
                return message_id
            else:
                logger.debug(
                    f"[REDIS_LOOKUP] Key {key} found but the mapping has no message_id"
                )

        # MISS on the primary key q:{queue_id}:{rcpt} -> return None in O(1), WITHOUT SCAN.
        #
        # The primary key is the ONLY correct one (the milter always writes
        # q:{queue_id}:{rcpt}). On a miss (TTL expired, or the queue_id changed at the
        # outbound relay) the message cannot be resolved anyway, and scanning the
        # keyspace (`q:{queue_id}:*`, `q:*:{rcpt}`) would cost seconds per miss on a
        # large Redis and saturate the ingest workers during a burst of deferred
        # retries. The event falls back to the synthetic ID downstream.
        logger.debug(
            f"[REDIS_LOOKUP] MISS on q:{queue_id}:{rcpt.lower()} - returning None (no SCAN)"
        )

        if span:
            try:
                span.set_attribute("recipient.lookup.found", False)
                span.end()
                logger.debug("[OTEL] Span recipient.lookup finished - message_id not found")
            except Exception as e:
                logger.warning(f"[OTEL] Error finishing span redis.lookup: {e}")
        return None

    except Exception as e:
        logger.error(f"Error looking up message_id in Redis: {e}")
        if span:
            try:
                span.record_exception(e)
                span.set_status(StatusCode.ERROR, str(e))
                span.end()
                logger.debug(f"[OTEL] Span recipient.lookup marked as error: {e}")
            except Exception as span_error:
                logger.warning(f"[OTEL] Error marking the span as error: {span_error}")
        return None


# Short queue IDs are uppercase hex; long queue IDs (enable_long_queue_ids)
# are mixed-case alphanumeric. Accept both.
_QUEUED_AS_RE = re.compile(r"queued as ([A-Za-z0-9]+)")


def chain_relay_queue_mapping(
    queue_id: str,
    rcpt: str,
    status_text: str,
    cache: dict | None = None,
) -> str | None:
    """
    Propagate the q:{queue_id}:{rcpt} mapping to the smarthost's queue_id.

    The hop from the local Postfix to an internal smarthost (CONNECT_INTERNAL_RELAY_SUFFIX)
    is dropped by the anti-double-count guard, but its log line carries
    "queued as <QID>": the queue_id the smarthost gave the message. This chain
    lets the REAL delivery, logged by a smarthost without a milter, be matched
    to its message; otherwise it would arrive with a queue_id that was never
    mapped and get a synthetic smtp-delivered-* message_id (a delivered event
    with no campaign attached, and the tracking service's "no delivered" gate
    would hold back the opens).

    Written with SET NX: if the smarthost runs a milter itself, its richer
    mapping (with trace) is not overwritten when it arrives first.

    Returns:
        str: the smarthost's queue_id if the chain was written/possible, None
        otherwise (no "queued as" in the line, or no source mapping).
    """
    try:
        match = _QUEUED_AS_RE.search(status_text or "")
        if not match:
            return None
        downstream_qid = match.group(1)
        if downstream_qid == queue_id:
            return None

        source_key = f"q:{queue_id}:{rcpt.lower()}"
        mapping = None
        if cache is not None and source_key in cache:
            mapping = cache[source_key]
        else:
            val = redis.get(source_key)
            if val:
                mapping = json.loads(val)
            if cache is not None:
                cache[source_key] = mapping

        if not mapping or not mapping.get("message_id"):
            logger.debug(
                f"[RELAY_CHAIN] No source mapping for {source_key} - chain not written"
            )
            return None

        chained = dict(mapping)
        chained["queue_id"] = downstream_qid
        chained["chained_from"] = queue_id
        dest_key = f"q:{downstream_qid}:{rcpt.lower()}"
        created = redis.set_if_absent(dest_key, REDIS_TTL_SEC, json.dumps(chained))
        logger.debug(
            f"[RELAY_CHAIN] {source_key} -> {dest_key} "
            f"(message_id={mapping.get('message_id')}, created={bool(created)})"
        )
        return downstream_qid
    except Exception as e:
        logger.warning(f"[RELAY_CHAIN] Error propagating the relay mapping: {e}")
        return None


def should_ignore_bounce(
    rcpt: str,
    host: str = None,
    service: str = None,
    queue_id: str = None,
) -> bool:
    """
    Check whether a bounce must be ignored because it comes from an internal flow.

    A bounce only counts when:
    - the host is not internal (a host ending in CONNECT_INTERNAL_RELAY_SUFFIX -> ignore)
    - the service is not lmtp (lmtp -> ignore)
    - the recipient is not an internal bounce address (bounces+...@rbm<n>...) -> ignore
    - the queue_id comes from the final SMTP flow, not from internal LMTP

    Args:
        rcpt: Recipient address
        host: Host that processed the event
        service: Service that processed the event
        queue_id: Postfix queue ID

    Returns:
        bool: True if the bounce must be ignored, False otherwise
    """
    try:
        rcpt_normalized = rcpt.lower().strip()

        # 1. Internal host (ends with the internal relay suffix)
        if host:
            host_normalized = host.lower().strip()
            if _is_internal_host(host_normalized):
                logger.debug(
                    f"[BOUNCE_FILTER] Ignoring bounce: internal host ({host_normalized})"
                )
                return True

        # 2. Internal LMTP service
        if service:
            service_normalized = service.lower().strip()
            if service_normalized == "lmtp":
                logger.debug(
                    f"[BOUNCE_FILTER] Ignoring bounce: internal LMTP service ({service_normalized})"
                )
                return True

        # 3. Internal bounce address (bounces+...@rbm<n>...)
        # Pattern: bounces+...@rbm...
        if rcpt_normalized.startswith("bounces+"):
            # Is it an rbm domain (rbm followed by digits)?
            if "@rbm" in rcpt_normalized:
                # rbm followed by digits (e.g. rbm265489)
                rbm_pattern = r"@rbm\d+"
                if re.search(rbm_pattern, rcpt_normalized):
                    logger.debug(
                        f"[BOUNCE_FILTER] Ignoring bounce: internal bounce address ({rcpt_normalized})"
                    )
                    return True

        # 4. Otherwise do not ignore it: a valid bounce from a remote server
        logger.debug(
            f"[BOUNCE_FILTER] Valid bounce: rcpt={rcpt_normalized}, host={host}, service={service}"
        )
        return False

    except Exception as e:
        logger.warning(
            f"[BOUNCE_FILTER] Error checking whether to ignore the bounce: {e}")
        # On error, do not ignore (conservative)
        return False


def process_delivery_event(
    message_id: str,
    rcpt: str,
    queue_id: str,
    status: str,
    dsn: str = "",
    host: str = None,
    service: str = None,
    timestamp: str = None,
    status_msg: str = "",
    relay_host: str | None = None,
) -> dict[str, Any]:
    """
    Process one email delivery event

    Args:
        message_id: Message ID
        rcpt: Recipient address
        queue_id: Postfix queue ID
        status: Delivery status
        dsn: Delivery Status Notification code
        host: Host that processed the event
        service: Service that processed the event
        timestamp: Event timestamp
        status_msg: Postfix status message

    Returns:
        dict: Processing result
    """
    # Span for the event processing
    trace_id = None
    span = None
    parent_trace_id = None
    parent_span_id = None
    queue_wait_ms = None  # initialized at function scope
    logger.debug("[OTEL] ===== STARTING process_delivery =====")

    # FIRST: read trace_id and span_id (anchor_span_id) from Redis BEFORE creating the span (to create it as a child)
    # The span_id comes from mail.message (the anchor span), not from mail.pipeline
    try:
        key = f"q:{queue_id}:{rcpt.lower()}"
        val = redis.get(key)
        if val:
            mapping = json.loads(val)
            parent_trace_id = mapping.get("trace_id")  # mail.pipeline trace ID
            # mail.message span ID (anchor span): every asynchronous span must be its child
            parent_span_id = mapping.get("span_id") or mapping.get("anchor_span_id")
            logger.debug(f"[OTEL] Redis data - trace_id: {parent_trace_id}, span_id: {mapping.get('span_id')}, anchor_span_id: {mapping.get('anchor_span_id')}, final parent_span_id: {parent_span_id}")

            # queue_wait_ms: gap between the milter finishing and the events-updater starting
            mail_message_start_ts_ms = mapping.get("mail_message_start_ts_ms")
            if mail_message_start_ts_ms:
                try:
                    import time
                    now_ms = int(time.time() * 1000)
                    mail_message_start = int(mail_message_start_ts_ms)
                    queue_wait_ms = now_ms - mail_message_start
                    logger.debug(f"[OTEL] Queue wait computed: {queue_wait_ms}ms (mail_message_start: {mail_message_start}, now: {now_ms})")
                except Exception as e:
                    logger.warning(f"[OTEL] Error computing queue_wait_ms: {e}")

            if parent_trace_id:
                if parent_span_id:
                    logger.debug(f"[OTEL] Trace ID and anchor span ID found in Redis - trace_id: {parent_trace_id}, anchor_span_id: {parent_span_id}, Queue wait: {queue_wait_ms}ms - creating the span as a CHILD of the anchor span")
                else:
                    logger.warning(f"[OTEL] Trace ID found but anchor span ID NOT found! trace_id: {parent_trace_id}, span_id in mapping: {mapping.get('span_id')}, anchor_span_id in mapping: {mapping.get('anchor_span_id')}")
            else:
                # Correlation is best-effort: with OTEL_TRACES_ENABLED=false the
                # milter does not write a trace_id to Redis. Its absence is expected
                # and not actionable here, so it stays at debug level.
                logger.debug(f"[OTEL] No trace ID in the Redis mapping (correlation inactive): {mapping}")
    except Exception as e:
        logger.warning(f"[OTEL] Error reading trace_id/span_id from Redis before creating the span: {e}")

    try:
        # With the milter's trace_id, create the span as a CHILD of that trace
        if parent_trace_id:
            logger.debug(f"[OTEL] Creating span delivery.process_event as a CHILD of the milter trace (trace_id: {parent_trace_id}, span_id: {parent_span_id})")
            span = create_span_from_remote_context(
                otel_tracer,
                parent_trace_id,
                parent_span_id,
                "delivery.process_event",
            )
            if span:
                trace_id = parent_trace_id  # use the parent's trace_id for tags
        else:
            # Without the milter's trace_id, create a normal (independent) span
            logger.debug("[OTEL] Milter trace ID not found - creating an independent span")
            logger.debug(f"[OTEL] Creating span delivery.process_event - message_id: {message_id}, queue_id: {queue_id}")
            span = otel_tracer.start_span("delivery.process_event")
        if span:
            span.set_attribute("events_updater.message_id", str(message_id))
            span.set_attribute("events_updater.rcpt", str(rcpt))
            span.set_attribute("events_updater.queue_id", str(queue_id))
            span.set_attribute("events_updater.status", str(status))
            # Standard tags without prefix, for search
            span.set_attribute("message_id", str(message_id))
            span.set_attribute("rcpt", str(rcpt))
            # queue_wait_ms metric, when available
            if queue_wait_ms is not None:
                span.set_attribute("pipeline.queue_wait_ms", float(queue_wait_ms))
                logger.debug(f"[OTEL] Queue wait {queue_wait_ms}ms added to span delivery.process_event")
            if dsn:
                span.set_attribute("events_updater.dsn", str(dsn))
            if host:
                span.set_attribute("events_updater.host", str(host))
            if service:
                span.set_attribute("events_updater.service", str(service))
            logger.debug(f"[OTEL] Span delivery.process_event created: {span}")
            logger.debug(f"[OTEL] Span ID: {format(span.get_span_context().span_id, '016x') if hasattr(span, 'get_span_context') else 'N/A'}, Trace ID: {format(span.get_span_context().trace_id, '032x') if hasattr(span, 'get_span_context') else 'N/A'}, message_id: {message_id}, queue_id: {queue_id}")

        # Correlation tags, when there is a milter trace_id
        if span and trace_id:
            # Standard tags without prefix, for search
            span.set_attribute("milter_trace_id", str(trace_id))
            span.set_attribute("trace_id_milter", str(trace_id))
            # Unified correlation tag: finds all related traces
            span.set_attribute("correlation_id", str(trace_id))
            # Prefixed tags, for context
            span.set_attribute("events_updater.trace_id", str(trace_id))
            span.set_attribute("events_updater.milter_trace_id", str(trace_id))
            span.set_attribute("events_updater.parent_trace_id", str(trace_id))
            logger.debug(f"[OTEL] Trace ID {trace_id} added to the span for correlation")
            logger.debug(f"[OTEL] CORRELATION: delivery.process_event -> milter trace_id={trace_id} (span created as a CHILD)")
    except Exception as e:
        logger.error(f"[OTEL] Error creating span delivery.process_event: {e}", exc_info=True)
        span = None

    try:
        # Normalize the recipient
        rcpt_normalized = rcpt.lower()

        # Attempt count
        attempt = get_attempt_count(message_id, rcpt_normalized)

        # Look up domain_name in Redis (more precise than taking it from rcpt)
        domain_name = "unknown"  # fallback

        # Only accept the Redis domain_name if the entry refers to the SAME message_id
        # as the event being processed. Postfix queue IDs can collide across MTA nodes
        # (and across repeated recipients), so a lookup by queue_id+rcpt alone could
        # pick an old mapping of another sender and attribute the delivery event to
        # the wrong tenant.
        def _domain_from_mapping(raw: bytes | str, source_key: str) -> str | None:
            try:
                mapping = json.loads(raw)
            except (json.JSONDecodeError, KeyError) as exc:
                logger.warning(f"Error parsing the Redis mapping for {source_key}: {exc}")
                return None
            stored_msg_id = mapping.get("message_id")
            if stored_msg_id and message_id and stored_msg_id != message_id:
                # Expected: a queue_id reused/colliding across messages (same rcpt,
                # old keys). Rejecting is correct and not actionable, so this is
                # logged at debug level.
                logger.debug(
                    f"[DOMAIN_LOOKUP] Ignoring stale key {source_key}: "
                    f"stored message_id={stored_msg_id} != event message_id={message_id}"
                )
                return None
            return mapping.get("domain_name")

        try:
            # Use the exact key with queue_id (the same key used to find the message_id)
            key = f"q:{queue_id}:{rcpt_normalized}"
            val = redis.get(key)

            if val:
                candidate = _domain_from_mapping(val, key)
                if candidate:
                    domain_name = candidate
                    logger.debug(
                        f"[DOMAIN_LOOKUP] domain_name found through the exact key {key}: {domain_name}"
                    )

            # On a miss of the exact key, Redis is NOT scanned: scanning the keyspace
            # (`q:{queue_id}:*` or `q:*:{rcpt}`) for every event without a mapping
            # would cost seconds per event on a large Redis. domain_name comes from the
            # rcpt just below (a reliable O(1) fallback: the domain is in the address).
        except Exception as e:
            logger.warning(f"Error looking up domain_name in Redis: {e}")

        # Fallback: take it from rcpt
        if domain_name == "unknown":
            domain_name = (
                rcpt_normalized.split(
                    "@")[-1] if "@" in rcpt_normalized else "unknown"
            )

        # Strip suspicious characters
        if ">" in domain_name or "<" in domain_name:
            logger.warning(
                f"domain_name contains suspicious characters: '{domain_name}'")
            domain_name = domain_name.replace(">", "").replace("<", "").strip()

        # Current status in the database (if any)
        current_status = None
        try:
            result = fetch_one(
                "SELECT status FROM deliveries WHERE message_id = %s AND rcpt = %s",
                (message_id, rcpt_normalized),
            )
            if result:
                current_status = result["status"]
                logger.debug(
                    f"[STATUS_MAPPING] Current status found in the database: '{current_status}'"
                )
        except Exception as e:
            logger.warning(
                f"[STATUS_MAPPING] Error reading the current status: {e}")

        # Span status.mapping, child of delivery.process_event
        status_mapping_span = None
        if span:
            try:
                status_mapping_span = otel_tracer.start_span(
                    "status.mapping",
                    context=trace.set_span_in_context(span),
                )
                status_mapping_span.set_attribute("status.original", str(status))
                status_mapping_span.set_attribute("status.dsn", str(dsn) if dsn else "")
                if current_status:
                    status_mapping_span.set_attribute("status.current", str(current_status))
                logger.debug("[OTEL] Span status.mapping created")
            except Exception as e:
                logger.warning(f"[OTEL] Error creating span status.mapping: {e}")

        # Map the status from the DSN
        mapped_status = map_delivery_status(status, dsn, current_status)
        logger.debug(
            f"[STATUS_MAPPING] Final status to store: '{mapped_status}'"
        )

        # Finish span status.mapping
        if status_mapping_span:
            try:
                status_mapping_span.set_attribute("status.mapped", str(mapped_status))
                status_mapping_span.end()
                logger.debug("[OTEL] Span status.mapping finished")
            except Exception:
                pass

        # Ignore bounces from internal flows. `relay_host` is the hop that refused; `host` is
        # the node that logged the line (always ours), so it cannot decide this.
        if mapped_status == "bounced":
            if should_ignore_bounce(rcpt_normalized, relay_host, service, queue_id):
                logger.info(
                    f"[BOUNCE_FILTER] Ignoring internal bounce: {rcpt_normalized} (relay={relay_host}, service={service})"
                )
                # Do not mark as bounced: keep the previous status or use deferred
                if current_status and current_status in ["sent", "deferred"]:
                    mapped_status = current_status
                else:
                    mapped_status = "deferred"  # fallback so the event is not lost
                logger.debug(
                    f"[BOUNCE_FILTER] Status changed from 'bounced' to '{mapped_status}' (internal bounce ignored)"
                )

        # Anti-loop fast path: reprocessing WITHOUT a status change is a no-op.
        # Vector (HTTP sink with unbounded retries) re-sends the same maillog line
        # when the consumer is slow, so the same delivery can arrive N times. If the
        # stored status does NOT change, there is nothing to do: skip the
        # upsert/insert/outbox/log and answer 200 quickly, so that Vector stops
        # retrying. Transitions (sent/deferred->delivered, ->bounced, the first
        # event) follow the normal flow. Open/click events come from the tracking
        # service and do not pass through here.
        # Known trade-off: repeated 'deferred' retries do not increment
        # deliveries.attempt on this path.
        if current_status is not None and mapped_status == current_status:
            # Even without a transition in `deliveries`, send_ledger can be BEHIND
            # this event: the reaper returns rows to 'queued', and the event that
            # would close them can arrive as a reprocess without a status change.
            # Reconciling here keeps such rows from staying 'queued' forever.
            # The reconcile is idempotent and guarded by state IN, so running it on
            # the fast path never re-marks a terminal row nor reopens anything.
            if mapped_status in ("sent", "delivered"):
                reconcile_send_ledger_delivered(message_id, rcpt_normalized)
            elif mapped_status == "bounced":
                reconcile_send_ledger_failed(message_id, rcpt_normalized, dsn)
            logger.debug(
                "[FAST_PATH] skipping reprocess without a status change "
                "(message_id=%s rcpt=%s status=%s)",
                message_id, rcpt_normalized, mapped_status,
            )
            return {
                "status": "success",
                "skipped": True,
                "reason": "no_status_change",
                "message_id": message_id,
                "rcpt": rcpt_normalized,
                "queue_id": queue_id,
                "delivery_status": mapped_status,
            }

        # domain_id
        domain_id = get_domain_id(domain_name)

        # Span engagement.upsert, child of delivery.process_event
        engagement_upsert_span = None
        if span:
            try:
                engagement_upsert_span = otel_tracer.start_span(
                    "engagement.upsert",
                    context=trace.set_span_in_context(span),
                )
                engagement_upsert_span.set_attribute("engagement.message_id", str(message_id))
                engagement_upsert_span.set_attribute("engagement.rcpt", str(rcpt_normalized))
                engagement_upsert_span.set_attribute("engagement.status", str(mapped_status))
                logger.debug("[OTEL] Span engagement.upsert created")
            except Exception as e:
                logger.warning(f"[OTEL] Error creating span engagement.upsert: {e}")

        # Update the deliveries table and decide ATOMICALLY whether this event is a
        # real status transition. `status_changed` is the source of truth for the
        # outbox gate below (NOT `mapped_status != current_status` read before the
        # write, which races across replicas/ingestion threads).
        status_changed = upsert_delivery_returning_changed(
            message_id, rcpt_normalized, domain_name, domain_id, mapped_status
        )

        # send_ledger: if the relay ACCEPTED the message (sent) or the destination
        # confirmed it (delivered), it has left the local Postfix queue, so mark it
        # terminal in the ledger and the reaper will NOT re-inject it. Independent of
        # status_changed (idempotent and guarded), so a mark that failed on an
        # earlier pass is retried.
        if mapped_status in ("sent", "delivered"):
            reconcile_send_ledger_delivered(message_id, rcpt_normalized)
        # Bounce (5.x) = left the queue without delivery and without retry -> terminal
        # failed (so the reaper does not re-inject bounced messages).
        elif mapped_status == "bounced":
            reconcile_send_ledger_failed(message_id, rcpt_normalized, dsn)
        # Deferred (4.x) = Postfix is alive and retrying -> push the deadline so the
        # reaper waits; if the events stop (node down), the deadline expires and the
        # reaper takes over.
        elif mapped_status == "deferred":
            reconcile_send_ledger_deferred(message_id, rcpt_normalized)

        # Insert the delivery event with the mapped status
        normalized_timestamp = normalize_timestamp(timestamp)
        exec_sql(
            INSERT_EVENT,
            (
                message_id,
                rcpt_normalized,
                domain_name,
                domain_id,
                queue_id,
                mapped_status,  # mapped status
                dsn,
                attempt,
                host,
                service,
                normalized_timestamp,
            ),
        )

        # Finish span engagement.upsert
        if engagement_upsert_span:
            try:
                engagement_upsert_span.end()
                logger.debug("[OTEL] Span engagement.upsert finished")
            except Exception:
                pass

        # Span suppression.apply for bounces, child of delivery.process_event
        suppression_apply_span = None
        suppression_result = None
        # Only a real HARD bounce (invalid recipient) suppresses. Policy bounces
        # (5.7.x: relay quota/limit, block, reputation) and soft bounces (4.x) do
        # NOT suppress; otherwise hitting a relay quota would suppress valid
        # addresses in bulk.
        bounce_class = classify_bounce_type(dsn, status_msg)
        if mapped_status == "bounced" and bounce_class != "hard":
            logger.info(
                f"[SUPPRESSION] Bounce '{bounce_class}' (dsn={dsn}) for "
                f"{rcpt_normalized} on {domain_name} - NOT suppressing "
                f"(only hard bounces suppress)"
            )
        if mapped_status == "bounced" and bounce_class == "hard":
            if span:
                try:
                    suppression_apply_span = otel_tracer.start_span(
                        "suppression.apply",
                        context=trace.set_span_in_context(span),
                    )
                    suppression_apply_span.set_attribute("suppression.rcpt", str(rcpt_normalized))
                    suppression_apply_span.set_attribute("suppression.domain_name", str(domain_name))
                    suppression_apply_span.set_attribute("suppression.trigger", "bounce")
                    logger.debug("[OTEL] Span suppression.apply created")
                except Exception as e:
                    logger.warning(f"[OTEL] Error creating span suppression.apply: {e}")

            logger.debug(
                f"[SUPPRESSION] Bounce detected - applying suppression rules for {rcpt_normalized}"
            )
            suppression_result = apply_suppression_rules_for_bounce(
                rcpt_normalized, domain_name, bounce_type=bounce_class
            )

            # Finish span suppression.apply
            if suppression_apply_span:
                try:
                    suppression_apply_span.set_attribute("suppression.applied", suppression_result.get("suppressed", False) if suppression_result else False)
                    if suppression_result and suppression_result.get("suppressed"):
                        suppression_apply_span.set_attribute("suppression.type", str(suppression_result.get("type", "unknown")))
                    suppression_apply_span.end()
                    logger.debug("[OTEL] Span suppression.apply finished")
                except Exception:
                    pass

        # Span events.outbox.enqueue for the outbox write, child of delivery.process_event
        # (named "events.outbox.enqueue" to tell it apart from the milter's outbox.enqueue)
        outbox_enqueue_span = None
        outbox_trace_context = None
        # Deduplication gate: only enqueue in the outbox when the status REALLY
        # changes. `status_changed` comes from the atomic UPSERT above
        # (upsert_delivery_returning_changed), decided by the rowcount under the
        # UNIQUE KEY, NOT by comparing `mapped_status != current_status` read before
        # the write (that races across replicas/threads and could let two calls
        # enqueue). Without this gate, reprocessing the same line (a restart, or
        # Vector re-POSTing) would emit a duplicate 'delivered'/'deferred' event on
        # every pass. Valid transitions still pass (sent/deferred->delivered,
        # ->bounced, first deferred); reprocessing an already stored status is ignored.
        if mapped_status in ["delivered", "bounced", "deferred"] and status_changed:
            if span:
                try:
                    outbox_enqueue_span = otel_tracer.start_span(
                        "events.outbox.enqueue",
                        context=trace.set_span_in_context(span),
                    )
                    outbox_enqueue_span.set_attribute("outbox.event_type", str(mapped_status))
                    outbox_enqueue_span.set_attribute("outbox.message_id", str(message_id))
                    outbox_enqueue_span.set_attribute("outbox.rcpt", str(rcpt_normalized))
                    logger.debug(f"[OTEL] Span events.outbox.enqueue created for event {mapped_status}")

                    # Take the span's trace_context for propagation to the outbox
                    # The span_id here is the current span's (outbox.enqueue), not the parent's
                    _ctx = outbox_enqueue_span.get_span_context()
                    span_trace_id = format(_ctx.trace_id, '032x') if _ctx.trace_id else None
                    span_span_id = format(_ctx.span_id, '016x') if _ctx.span_id else None
                    if span_trace_id:
                        outbox_trace_context = {
                            "trace_id": span_trace_id,
                            "span_id": span_span_id,
                        }
                        logger.debug(f"[OTEL] Trace context extracted for the outbox: trace_id={span_trace_id}, span_id={span_span_id}")
                except Exception as e:
                    logger.warning(f"[OTEL] Error creating span events.outbox.enqueue: {e}")

            # Base data
            kafka_data = {
                "dsn": dsn,
                "attempt": attempt,
                "host": host,
                "service": service,
                "timestamp": normalize_timestamp_to_iso(timestamp),
                "queue_id": queue_id,
                "original_status": status,
            }

            # For delivered events, add delivery_time and server_response
            if mapped_status == "delivered":
                # The timestamp, when available, is the delivery_time
                if normalized_timestamp:
                    original_timestamp = timestamp
                    kafka_data["delivery_time"] = normalize_timestamp_to_iso(
                        original_timestamp
                    )

                # The DSN, when available, is the server_response
                if dsn:
                    kafka_data["server_response"] = dsn
                elif host:
                    kafka_data["server_response"] = f"Host: {host}"

                # The real Postfix status_msg, when available
                if status_msg:
                    kafka_data["status_msg"] = status_msg
                else:
                    kafka_data["status_msg"] = status

            trigger_kafka_event(
                event_type=mapped_status,
                message_id=message_id,
                recipient_email=rcpt_normalized,
                domain_name=domain_name,
                trace_context=outbox_trace_context,
                **kafka_data,
            )

            # Finish span events.outbox.enqueue
            if outbox_enqueue_span:
                try:
                    outbox_enqueue_span.end()
                    logger.debug("[OTEL] Span events.outbox.enqueue finished")
                except Exception:
                    pass

        logger.info(
            f"log_type=audit action=mail_event_processed "
            f"message_id={message_id} recipient={rcpt_normalized} "
            f"status={mapped_status} attempt={attempt} "
            f"domain_name={domain_name} domain_id={domain_id}"
        )

        # Final span tags
        if span:
            try:
                span.set_attribute("events_updater.domain_name", str(domain_name))
                span.set_attribute("events_updater.domain_id", str(domain_id))
                span.set_attribute("events_updater.mapped_status", str(mapped_status))
                span.set_attribute("events_updater.attempt", attempt)
                if suppression_result:
                    span.set_attribute("events_updater.suppression_applied", bool(suppression_result.get("suppressed", False)))
            except Exception as e:
                logger.warning(f"[OTEL] Error adding tags to the span: {e}")

        return {
            "status": "success",
            "message_id": message_id,
            "rcpt": rcpt_normalized,
            "queue_id": queue_id,
            "delivery_status": mapped_status,
            "original_status": status,
            "dsn": dsn,
            "attempt": attempt,
            "suppression_applied": suppression_result,
        }

    except Exception as e:
        logger.error(f"Error processing delivery event: {e}")
        if span:
            try:
                span.record_exception(e)
                span.set_status(StatusCode.ERROR, str(e))
                logger.debug(f"[OTEL] Span delivery.process_event marked as error: {e}")
            except Exception as span_error:
                logger.warning(f"[OTEL] Error marking the span as error: {span_error}")
        raise HTTPException(status_code=500, detail=f"Internal error: {e!s}") from e
    finally:
        # Finish the span: ALWAYS, in the finally block (even on error)
        if span:
            try:
                span.end()
                logger.debug(f"[OTEL] Span delivery.process_event finished - Span ID: {format(span.get_span_context().span_id, '016x') if hasattr(span, 'get_span_context') else 'N/A'}")
            except Exception as e:
                logger.warning(f"[OTEL] Error finishing spans: {e}", exc_info=True)


@app.post("/postfix")
async def ingest_postfix(request: Request, background_tasks: BackgroundTasks):
    """
    Async dispatcher for /postfix: reads the body and hands the synchronous work
    (Redis + MySQL + CPU) to a worker thread, keeping the event loop free for the
    health probes. Without this offload, the loop would be blocked for the whole
    batch and the liveness probe (/health/live) could time out.
    """
    # Read the body BEFORE acquiring the semaphore. If we queue on the semaphore
    # with the body still unread (all INGEST_EXECUTOR_WORKERS busy on slow
    # MySQL/Redis), Vector hits its HTTP timeout and disconnects, and the later
    # `await request.body()` raises starlette.ClientDisconnect. Consuming the body
    # first (while the client is still connected) avoids that; the semaphore still
    # throttles the heavy ingest below.
    payload = await _read_ingest_body(request)
    async with _ingest_semaphore:
        return await _run_ingest(_ingest_postfix_sync, payload, background_tasks)


def _ingest_postfix_sync(payload: bytes, background_tasks: BackgroundTasks):
    """
    Main endpoint for Postfix event ingestion

    Accepts these formats:
    1. Plain JSON: {"queue_id": "...", "message_id": "...", "rcpt": "...", "status": "...", "dsn": "..."}
    2. Vector JSON: {"queue_id": "...", "to": "...", "status": "...", "dsn": "...", "timestamp": "...", "host": "...", "service": "..."}
    3. List of events: [{"queue_id": "...", ...}, ...]
    """
    span = None
    logger.debug("[OTEL] ===== STARTING ingest_postfix =====")

    # FIRST: parse the payload to get queue_id and rcpt (needed to look up the trace_id)
    try:
        raw_payload = payload.decode("utf-8")
        logger.debug(f"=== PAYLOAD RECEIVED ({len(raw_payload)} bytes) ===")
        logger.debug(f"Full payload: {raw_payload}")

        data = json.loads(raw_payload)
        logger.debug(f"Parsed payload (type: {type(data).__name__}):")
        logger.debug(json.dumps(data, indent=2, ensure_ascii=False))
        logger.debug(f"Processing a payload of {len(raw_payload)} bytes")
    except Exception as e:
        logger.warning(f"Invalid JSON received: {e}")
        raise HTTPException(
            status_code=400,
            detail=f"invalid_json: {e!s}",
        ) from e

    # SECOND: take queue_id and rcpt from the payload to look up the trace_id in Redis
    parent_trace_id = None
    parent_span_id = None
    queue_id = None
    rcpt = None

    try:
        # extract_from_message is a module-level function (reused by the prefetch).
        if isinstance(data, list) and data and isinstance(data[0], dict):
            first_event = data[0]
            queue_id = first_event.get("queue_id")
            rcpt = first_event.get("rcpt") or first_event.get("to")

            # No queue_id/rcpt fields: try to extract them from the message field
            if (not queue_id or not rcpt) and "message" in first_event:
                msg_queue_id, msg_rcpt = extract_from_message(first_event.get("message", ""))
                if not queue_id and msg_queue_id:
                    queue_id = msg_queue_id
                    logger.debug(f"[OTEL] queue_id extracted from message: {queue_id}")
                if not rcpt and msg_rcpt:
                    rcpt = msg_rcpt
                    logger.debug(f"[OTEL] rcpt extracted from message: {rcpt}")

        elif isinstance(data, dict):
            queue_id = data.get("queue_id")
            rcpt = data.get("rcpt") or data.get("to")

            # No queue_id/rcpt fields: try to extract them from the message field
            if (not queue_id or not rcpt) and "message" in data:
                msg_queue_id, msg_rcpt = extract_from_message(data.get("message", ""))
                if not queue_id and msg_queue_id:
                    queue_id = msg_queue_id
                    logger.debug(f"[OTEL] queue_id extracted from message: {queue_id}")
                if not rcpt and msg_rcpt:
                    rcpt = msg_rcpt
                    logger.debug(f"[OTEL] rcpt extracted from message: {rcpt}")

        logger.debug(f"[OTEL] Values extracted for the lookup: queue_id={queue_id}, rcpt={rcpt}")

        # Look up the trace_id in Redis by queue_id and rcpt
        if queue_id and rcpt:
            key = f"q:{queue_id}:{rcpt.lower()}"
            val = redis.get(key)
            if val:
                mapping = json.loads(val)
                parent_trace_id = mapping.get("trace_id")
                parent_span_id = mapping.get("span_id")
                if parent_trace_id:
                    logger.debug(f"[OTEL] Milter trace ID found in Redis: {parent_trace_id}, Span ID: {parent_span_id}")
    except Exception as e:
        logger.warning(f"[OTEL] Error reading the trace_id from Redis: {e}")

    # THIRD: create span event.parse as a child of the milter trace (if available)
    try:
        if parent_trace_id:
            logger.debug(f"[OTEL] Creating span event.parse as a CHILD of the milter trace (trace_id: {parent_trace_id})")
            span = create_span_from_remote_context(
                otel_tracer,
                parent_trace_id,
                parent_span_id,
                "event.parse",
            )
        else:
            logger.debug("[OTEL] Creating span event.parse as a new trace (no trace_id in Redis)")
            span = otel_tracer.start_span("event.parse")

        if span:
            logger.debug(f"[OTEL] Span event.parse created - Span ID: {format(span.get_span_context().span_id, '016x') if hasattr(span, 'get_span_context') else 'N/A'}, Trace ID: {format(span.get_span_context().trace_id, '032x') if hasattr(span, 'get_span_context') else 'N/A'}")
            # Span tags
            span.set_attribute("events_updater.payload_size", len(raw_payload))
            if queue_id:
                span.set_attribute("queue_id", str(queue_id))
            if rcpt:
                span.set_attribute("rcpt", str(rcpt))
            if isinstance(data, list):
                span.set_attribute("events_updater.format", "batch")
                span.set_attribute("events_updater.event_count", len(data))
                if data and isinstance(data[0], dict):
                    first_event = data[0]
                    if "message_id" in first_event:
                        span.set_attribute("message_id", str(first_event["message_id"]))
            elif isinstance(data, dict):
                if "message_id" in data:
                    span.set_attribute("events_updater.format", "direct_json")
                    span.set_attribute("message_id", str(data["message_id"]))
                elif "to" in data:
                    span.set_attribute("events_updater.format", "vector_json")
    except Exception as e:
        logger.error(f"[OTEL] Error creating span event.parse: {e}", exc_info=True)
        span = None

    # Note: the span is finished at the end of the function, before each return

    # A list is processed as a batch
    if isinstance(data, list):
        logger.debug(f"Processing a batch of {len(data)} events")

        # Prefetch: load all of the batch's Redis mappings in a single MGET
        # before the loop, instead of 1+ sequential GETs per event (the bottleneck
        # for Vector batches of 30+ events). Items not covered by the prefetch
        # (e.g. keys extracted from log lines by regex) fall back to an on-demand GET.
        _prefetch_cache: dict = {}
        try:
            _prefetch_keys = []
            for _ev in data:
                if not isinstance(_ev, dict):
                    continue
                _qid = _ev.get("queue_id")
                _rcpt = _ev.get("rcpt") or _ev.get("to")
                # Most Vector events arrive as raw log lines in the `message`
                # field: without extracting from it, the prefetch would be empty.
                if (not _qid or not _rcpt) and _ev.get("message"):
                    _msg_qid, _msg_rcpt = extract_from_message(_ev["message"])
                    _qid = _qid or _msg_qid
                    _rcpt = _rcpt or _msg_rcpt
                if _qid and _rcpt and "@" in str(_rcpt):
                    _prefetch_keys.append(f"q:{_qid}:{str(_rcpt).lower()}")
            if _prefetch_keys:
                # Deduplicate, keeping the order
                _prefetch_keys = list(dict.fromkeys(_prefetch_keys))
                _vals = redis.mget(_prefetch_keys)
                for _k, _v in zip(_prefetch_keys, _vals, strict=False):
                    if _v is None:
                        _prefetch_cache[_k] = None
                    else:
                        try:
                            _prefetch_cache[_k] = json.loads(_v)
                        except Exception:
                            _prefetch_cache[_k] = None
                logger.debug(
                    f"[REDIS_PREFETCH] {len(_prefetch_keys)} keys loaded in 1 MGET "
                    f"({sum(1 for v in _prefetch_cache.values() if v is not None)} hits)"
                )
        except Exception as e:
            logger.warning(f"[REDIS_PREFETCH] Failed (continuing without cache): {e}")
            _prefetch_cache = {}

        results = []
        for i, event in enumerate(data):
            try:
                logger.debug(f"Processing event {i+1}/{len(data)}")

                if isinstance(event, dict):
                    # Is it a Postfix log line (not a structured event)?
                    # Check for a 'message' field that mentions 'postfix'
                    if "message" in event and "postfix" in event.get("message", ""):
                        logger.debug(f"Event {i+1} is a Postfix log line")
                        logger.debug(
                            f"Event {i+1} - Process: {event.get('process', '')}"
                        )
                        logger.debug(
                            f"Event {i+1} - Message: {event.get('message', '')[:200]}..."
                        )

                        # Information from the milter rejection log
                        message = event.get("message", "")
                        logger.debug(
                            f"Event {i+1} - Checking conditions: 'milter-reject' in message: {'milter-reject' in message}"
                        )
                        logger.debug(
                            f"Event {i+1} - Checking conditions: 'Recipient address rejected' in message: {'Recipient address rejected' in message}"
                        )

                        # Extra debug logging: parts of the message
                        logger.debug(
                            f"Event {i+1} - First 100 chars of the message: {message[:100]}"
                        )
                        logger.debug(
                            f"Event {i+1} - Last 100 chars of the message: {message[-100:]}"
                        )

                        # A milter rejection OR a real Postfix bounce?
                        is_milter_reject = (
                            "milter-reject" in message
                            and "Recipient address rejected" in message
                        )
                        is_postfix_bounce = (
                            "status=bounced" in message and "dsn=" in message
                        )

                        # Log the detected conditions
                        logger.debug(
                            f"Event {i+1} - is_milter_reject: {is_milter_reject}"
                        )
                        logger.debug(
                            f"Event {i+1} - is_postfix_bounce: {is_postfix_bounce}"
                        )

                        # Other possible conditions in Postfix logs
                        has_dsn = "dsn=" in message
                        has_status = "status=" in message
                        has_to = "to=" in message
                        has_relay = "relay=" in message

                        logger.debug(
                            f"Event {i+1} - has_dsn: {has_dsn}, has_status: {has_status}, has_to: {has_to}, has_relay: {has_relay}"
                        )

                        # A normal delivery log (not a bounce)?
                        is_delivery_log = (
                            has_dsn
                            and has_to
                            and has_relay
                            and "delivered" in message.lower()
                        )
                        is_smtp_log = "smtp[" in message and has_to

                        logger.debug(
                            f"Event {i+1} - is_delivery_log: {is_delivery_log}, is_smtp_log: {is_smtp_log}"
                        )

                        if (
                            is_milter_reject
                            or is_postfix_bounce
                            or is_delivery_log
                            or is_smtp_log
                        ):
                            # Extract the recipient address with tolerant regexes
                            import re

                            # Flexible patterns to extract addresses
                            rcpt_patterns = [
                                r"to=<([^>]+)>",  # to=<email>
                                r"to=([^\s,]+)",  # to=email
                                # RCPT ... email
                                r"RCPT.*?([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})",
                            ]

                            from_patterns = [
                                r"from=<([^>]+)>",  # from=<email>
                                r"from=([^\s,]+)",  # from=email
                                # from ... email
                                r"from.*?([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})",
                            ]

                            rcpt = None
                            from_email = None

                            # Recipient
                            for pattern in rcpt_patterns:
                                match = re.search(
                                    pattern, message, re.IGNORECASE)
                                if match:
                                    rcpt = match.group(1).strip()
                                    break

                            # Sender
                            for pattern in from_patterns:
                                match = re.search(
                                    pattern, message, re.IGNORECASE)
                                if match:
                                    from_email = match.group(1).strip()
                                    break

                            logger.debug(
                                f"[LOG_PARSING] Event {i+1} - Extracted: rcpt='{rcpt}', from='{from_email}'"
                            )

                            if rcpt and "@" in rcpt:
                                # Details that depend on the kind of log line
                                timestamp = event.get("timestamp", "")

                                if is_milter_reject:
                                    # Milter rejection log
                                    # Look up the real message_id in Redis first
                                    message_id = find_message_id_by_queue(
                                        queue_id, rcpt, cache=_prefetch_cache
                                    )

                                    # Not in Redis: generate a fallback
                                    if not message_id:
                                        message_id = f"smtp-reject-{rcpt.split('@')[0]}-{int(datetime.now().timestamp())}"
                                        logger.debug(
                                            f"[LOG_PARSING] Message ID not found in Redis for the rejection, using fallback: {message_id}"
                                        )
                                    else:
                                        logger.debug(
                                            f"[LOG_PARSING] Message ID found in Redis for the rejection: {message_id}"
                                        )

                                    queue_id = "NOQUEUE"
                                    dsn = "5.1.1"
                                    status_msg = "Recipient address rejected: Automatic suppression - bounce detected"
                                    status = "bounced"

                                    logger.debug(
                                        f"[LOG_PARSING] Processing milter rejection log: {rcpt} -> {message_id}"
                                    )

                                elif is_postfix_bounce:
                                    # Real Postfix bounce log
                                    # Prefer the structured queue_id (Vector
                                    # grok); a 5+ hex regex is the fallback
                                    # (short queue IDs must match too).
                                    queue_match = re.search(
                                        r"\b([A-F0-9]{5,}):\s+to=", message
                                    )
                                    queue_id = (
                                        event.get("queue_id")
                                        or (queue_match.group(1) if queue_match else None)
                                        or f"bounce-{int(datetime.now().timestamp())}"
                                    )

                                    # DSN from the log line
                                    dsn_match = re.search(
                                        r"dsn=([0-9.]+)", message)
                                    dsn = dsn_match.group(
                                        1) if dsn_match else "5.4.4"

                                    # The hop that refused (relay=), as in the delivered path
                                    event_host = bounce_relay_host(message)
                                    event_service = event.get(
                                        "process") or event.get("service")

                                    # Skip internal bounces
                                    if should_ignore_bounce(rcpt, event_host, event_service, queue_id):
                                        logger.info(
                                            f"[LOG_PARSING] Ignoring internal Postfix bounce: {rcpt} (relay={event_host}, service={event_service})"
                                        )
                                        # Skip this event
                                        results.append(
                                            {
                                                "index": i,
                                                "status": "ignored",
                                                "reason": "internal_bounce",
                                                "rcpt": rcpt,
                                                "host": event_host,
                                                "service": event_service,
                                            }
                                        )
                                        continue

                                    # Look up the real message_id in Redis first
                                    message_id = find_message_id_by_queue(
                                        queue_id, rcpt, cache=_prefetch_cache
                                    )

                                    # Not in Redis: generate a fallback
                                    if not message_id:
                                        message_id = f"smtp-bounce-{queue_id}"
                                        logger.debug(
                                            f"[LOG_PARSING] Message ID not found in Redis for the bounce, using fallback: {message_id}"
                                        )
                                    else:
                                        logger.debug(
                                            f"[LOG_PARSING] Message ID found in Redis for the bounce: {message_id}"
                                        )

                                    status_msg = f"{message}"
                                    status = "bounced"

                                    logger.debug(
                                        f"[LOG_PARSING] Processing Postfix bounce log: {rcpt} -> {message_id} (queue: {queue_id}, dsn: {dsn})"
                                    )

                                elif is_delivery_log or is_smtp_log:
                                    # Normal Postfix delivery log
                                    # Prefer the STRUCTURED queue_id (Vector
                                    # grok). Fallback: a 5+ hex regex (Postfix
                                    # uses 5-15 characters depending on
                                    # PID/inode), so short queue IDs such as
                                    # 7176A17 still find their q:{qid} mapping
                                    # instead of getting a synthetic message_id.
                                    queue_match = re.search(
                                        r"\b([A-F0-9]{5,}):\s+to=", message
                                    )
                                    queue_id = (
                                        event.get("queue_id")
                                        or (queue_match.group(1) if queue_match else None)
                                        or f"delivery-{int(datetime.now().timestamp())}"
                                    )

                                    # DSN from the log line
                                    dsn_match = re.search(
                                        r"dsn=([0-9.]+)", message)
                                    dsn = dsn_match.group(
                                        1) if dsn_match else "2.0.0"

                                    # Status from the DSN
                                    if dsn.startswith("2."):
                                        status = "delivered"
                                    elif dsn.startswith("4."):
                                        status = "deferred"
                                    elif dsn.startswith("5."):
                                        status = "bounced"
                                    else:
                                        status = "sent"

                                    # For a bounce, check whether it is internal
                                    if status == "bounced":
                                        # The hop that refused (relay=), not the logging node
                                        event_host = bounce_relay_host(message)
                                        event_service = event.get(
                                            "process") or event.get("service")

                                        if should_ignore_bounce(rcpt, event_host, event_service, queue_id):
                                            logger.info(
                                                f"[LOG_PARSING] Ignoring internal bounce in a delivery log: {rcpt} (relay={event_host}, service={event_service})"
                                            )
                                            # Use deferred so it is not processed as a bounce
                                            status = "deferred"
                                            logger.debug(
                                                "[LOG_PARSING] Status changed from 'bounced' to 'deferred' (internal bounce ignored)"
                                            )

                                    # Look up the real message_id in Redis first
                                    message_id = find_message_id_by_queue(
                                        queue_id, rcpt, cache=_prefetch_cache
                                    )

                                    # Not in Redis: generate a fallback
                                    if not message_id:
                                        message_id = f"smtp-{status}-{queue_id}"
                                        logger.debug(
                                            f"[LOG_PARSING] Message ID not found in Redis, using fallback: {message_id}"
                                        )
                                    else:
                                        logger.debug(
                                            f"[LOG_PARSING] Message ID found in Redis: {message_id}"
                                        )

                                    status_msg = f"{message}"

                                    logger.debug(
                                        f"[LOG_PARSING] Processing Postfix delivery log: {rcpt} -> {message_id} (queue: {queue_id}, dsn: {dsn}, status: {status})"
                                    )

                                    # Standard tags on the ingest_postfix span, if any (for search)
                                    if span and message_id:
                                        span.set_attribute("message_id", str(message_id))
                                    if span and rcpt:
                                        span.set_attribute("rcpt", str(rcpt))

                                # Anti-DOUBLE-COUNT guard: the local Postfix can RELAY
                                # to the installation's own smarthosts/nodes. That hop
                                # logs sent/delivered but is NOT the delivery to the
                                # recipient: the smarthost logs the REAL delivery
                                # (relay=external MX). Counting the hop would count the
                                # message twice, so the internal hop is ignored; EXTERNAL
                                # relays are kept (their downstream log is not available).
                                if status in ("sent", "delivered"):
                                    _rm = re.search(r"relay=([^\s,\[]+)", message)
                                    _relay = _rm.group(1) if _rm else ""
                                    if _is_internal_host(_relay):
                                        # Before dropping the hop, propagate the
                                        # mapping to the smarthost's queue_id
                                        # ("queued as <QID>"): that is what lets
                                        # the REAL delivery be attributed.
                                        chain_relay_queue_mapping(
                                            queue_id, rcpt, message,
                                            cache=_prefetch_cache,
                                        )
                                        results.append({
                                            "index": i,
                                            "skipped": "internal_relay_hop",
                                            "relay": _relay,
                                        })
                                        continue

                                # Process as a delivery event
                                result = process_delivery_event(
                                    message_id=message_id,
                                    rcpt=rcpt,
                                    queue_id=queue_id,
                                    status=status,
                                    dsn=dsn,
                                    host=event.get("host"),
                                    service=event.get("process"),
                                    timestamp=timestamp,
                                    status_msg=status_msg,
                                    relay_host=bounce_relay_host(message),
                                )

                                results.append({"index": i, "result": result})
                                continue
                            else:
                                logger.warning(
                                    f"[LOG_PARSING] Event {i+1} - could not extract a valid address from the log line"
                                )
                                logger.warning(
                                    f"[LOG_PARSING] Full log line: {message}")

                        # Nothing could be extracted: skip
                        results.append(
                            {
                                "index": i,
                                "error": "unprocessable Postfix log line",
                            }
                        )
                        continue

                    # Normal structured event - ONLY when it is NOT a Postfix log line
                    queue_id = event.get("queue_id", "")
                    rcpt = event.get("rcpt") or event.get("to", "")
                    status = event.get("status", "")
                    dsn = event.get("dsn", "")
                    host = event.get("host")
                    service = event.get("service")
                    timestamp = event.get("timestamp")
                    status_msg = event.get("status_msg", "")

                    # Required fields, checked ONLY for structured events
                    if not queue_id or not rcpt or not status:
                        missing_fields = []
                        if not queue_id:
                            missing_fields.append("queue_id")
                        if not rcpt:
                            missing_fields.append("rcpt")
                        if not status:
                            missing_fields.append("status")

                        logger.debug(
                            f"Event {i+1} has empty required fields - skipping. Missing fields: {missing_fields}"
                        )
                        logger.debug(f"Full event {i+1}: {event}")
                        results.append(
                            {
                                "index": i,
                                "error": f"empty required fields: {missing_fields}",
                            }
                        )
                        continue

                    # Anti-DOUBLE-COUNT guard (structured Vector event): the hop from the
                    # local Postfix to an internal smarthost logs sent/delivered but is
                    # NOT the delivery to the recipient; the smarthost logs the real one.
                    if status in ("sent", "delivered"):
                        _relay = event.get("relay_name") or event.get("relay") or ""
                        if not _relay:
                            for _f in (status_msg, event.get("message", "")):
                                _rm = re.search(r"relay=([^\s,\[]+)", _f or "")
                                if _rm:
                                    _relay = _rm.group(1)
                                    break
                        if _is_internal_host(_relay):
                            # Propagate the mapping to the smarthost's queue_id
                            # before dropping the hop (same reason as in the
                            # log-parsing path above).
                            chain_relay_queue_mapping(
                                queue_id, rcpt,
                                status_msg or event.get("message", ""),
                                cache=_prefetch_cache,
                            )
                            results.append({
                                "index": i,
                                "skipped": "internal_relay_hop",
                                "relay": _relay,
                            })
                            continue

                    # Find the message_id
                    message_id = event.get("message_id")
                    if not message_id:
                        message_id = find_message_id_by_queue(queue_id, rcpt, cache=_prefetch_cache)

                    if message_id:
                        result = process_delivery_event(
                            message_id,
                            rcpt,
                            queue_id,
                            status,
                            dsn,
                            host,
                            service,
                            timestamp,
                            status_msg,
                            relay_host=event_relay_host(event),
                        )
                        results.append({"index": i, "result": result})
                    else:
                        results.append(
                            {
                                "index": i,
                                "error": "message_id not found",
                            }
                        )
                else:
                    results.append(
                        {
                            "index": i,
                            "error": "invalid event format",
                        }
                    )

            except Exception as e:
                results.append({"index": i, "error": str(e)})

        result = {
            "status": "ok",
            "format": "batch",
            "processed": len(data),
            "results": results,
        }
        # Finish the span BEFORE returning (so it is included in the trace)
        if span:
            try:
                span.end()
                logger.debug("[OTEL] Span event.parse finished (batch) - BEFORE return")
            except Exception as e:
                logger.warning(f"[OTEL] Error finishing span ingest_postfix (batch): {e}")
        return result

    # A dict is processed as a single event
    if isinstance(data, dict):
        logger.debug(
            f"Processing single event: {data.get('queue_id', 'N/A')} -> {data.get('rcpt', data.get('to', 'N/A'))} ({data.get('status', 'N/A')})"
        )

        # Format 1: plain JSON (with an explicit message_id)
        if all(key in data for key in ["queue_id", "message_id", "rcpt", "status"]):
            logger.debug("Plain JSON format detected")

            # Normalize the timestamp
            normalized_timestamp = normalize_timestamp(data.get("timestamp"))

            result = process_delivery_event(
                message_id=data["message_id"],
                rcpt=data["rcpt"],
                queue_id=data["queue_id"],
                status=data["status"],
                dsn=data.get("dsn", ""),
                host=data.get("host"),
                service=data.get("service"),
                timestamp=normalized_timestamp,
                status_msg=data.get("status_msg", ""),
                relay_host=event_relay_host(data),
            )

            response = {"status": "ok", "format": "direct_json", "result": result}
            # Finish the span BEFORE returning (so it is included in the trace)
            if span:
                try:
                    span.end()
                    logger.debug("[OTEL] Span event.parse finished (direct_json) - BEFORE return")
                except Exception as e:
                    logger.warning(f"[OTEL] Error finishing span ingest_postfix (direct_json): {e}")
            return response

        # Format 2: Vector JSON (no message_id - looked up in Redis)
        if all(key in data for key in ["queue_id", "to", "status"]):
            queue_id = data["queue_id"]
            rcpt = data["to"]
            status = data["status"]

            logger.debug("Vector JSON format detected")

            # Look up the message_id in Redis
            message_id = find_message_id_by_queue(queue_id, rcpt, cache=_prefetch_cache)

            # Add the message_id to the span, if found (for search)
            if span and message_id:
                span.set_attribute("message_id", str(message_id))

            if not message_id:
                logger.warning(
                    f"Message ID not found for queue_id: {queue_id}, rcpt: {rcpt}"
                )
                response = {
                    "status": "deferred",
                    "reason": "no_redis_mapping",
                    "queue_id": queue_id,
                    "rcpt": rcpt,
                    "suggestion": "Check that the milter processed the email before the delivery event",
                }
                if span:
                    span.set_attribute("events_updater.message_id_found", False)
                    try:
                        span.end()
                        logger.debug("[OTEL] Span event.parse finished (no_message_id) - BEFORE return")
                    except Exception as e:
                        logger.warning(f"[OTEL] Error finishing span ingest_postfix (no_message_id): {e}")
                return response

            logger.debug(f"Message ID found: {message_id}")

            # Normalize the timestamp
            normalized_timestamp = normalize_timestamp(data.get("timestamp"))

            result = process_delivery_event(
                message_id=message_id,
                rcpt=rcpt,
                queue_id=queue_id,
                status=status,
                dsn=data.get("dsn", ""),
                host=data.get("host"),
                service=data.get("service"),
                timestamp=normalized_timestamp,
                status_msg=data.get("status_msg", ""),
                relay_host=event_relay_host(data),
            )

            response = {
                "status": "ok",
                "format": "vector_json",
                "message_id": message_id,
                "vector_labels": {
                    "host": data.get("host"),
                    "service": data.get("service"),
                    "timestamp": normalized_timestamp,
                },
                "result": result,
            }
            # Finish the span BEFORE returning (so it is included in the trace)
            if span:
                try:
                    span.end()
                    logger.debug("[OTEL] Span event.parse finished (vector_json) - BEFORE return")
                except Exception as e:
                    logger.warning(f"[OTEL] Error finishing span ingest_postfix (vector_json): {e}")
            return response

        # Unrecognized format
        logger.warning(
            f"Unrecognized format - keys: {list(data.keys())}")

        response = {
            "status": "ignored",
            "reason": "no_match",
            "data_received": data,
            "supported_formats": [
                "Plain JSON: queue_id, message_id, rcpt, status",
                "Vector JSON: queue_id, to, status",
            ],
            "debug_info": {
                "keys_present": list(data.keys()),
                "format_checks": {
                    "direct_json": {
                        "queue_id": data.get("queue_id") is not None,
                        "message_id": data.get("message_id") is not None,
                        "rcpt": data.get("rcpt") is not None,
                        "status": data.get("status") is not None,
                    },
                    "vector_json": {
                        "queue_id": data.get("queue_id") is not None,
                        "to": data.get("to") is not None,
                        "status": data.get("status") is not None,
                    },
                },
            },
        }
        if span:
            span.set_attribute("events_updater.format", "no_match")
            try:
                span.end()
                logger.debug("[OTEL] Span event.parse finished (no_match) - BEFORE return")
            except Exception as e:
                logger.warning(f"[OTEL] Error finishing span ingest_postfix (no_match): {e}")
        return response

    # Unsupported data type (outside the try: only reached when data is neither a list nor a dict)
    logger.warning(f"Unsupported data type: {type(data)}")
    if span:
        span.set_attribute("events_updater.format", "unsupported")
        try:
            span.end()
            logger.debug("[OTEL] Span event.parse finished (unsupported) - BEFORE return")
        except Exception as e:
            logger.warning(f"[OTEL] Error finishing span ingest_postfix (unsupported): {e}")
    result = {
        "status": "error",
        "reason": "unsupported_data_type",
        "data_type": str(type(data)),
        "supported_types": ["dict", "list"],
    }
    return result


@app.post("/bounces/async")
async def ingest_async_bounce(request: Request):
    """Ingestion of ASYNCHRONOUS bounces (a late DSN delivered to the VERP Return-Path).

    Source: the MTA's Postfix `pipe` delivers the raw DSN email here, plus the
    VERP envelope recipient (header ``X-Bounce-Recipient``). Complements the
    synchronous path (process_delivery_event), which only sees failures at send time.

    Decodes the VERP -> original recipient + rbm domain; reads the DSN ``Status``;
    and only suppresses on a HARD bounce (5.x.x except 5.7.x policy), reusing the
    same ``apply_suppression_rules_for_bounce``. Missing/unreadable status => does
    NOT suppress (avoids over-suppression from malformed DSNs of remote MTAs).
    """
    raw = await request.body()
    envelope_rcpt = (request.headers.get("X-Bounce-Recipient") or "").strip()

    # Complaint reports (ARF/FBL) arrive through the same pipe (registered as
    # bounces+fbl@rbm<n>.<domain>) and are recognized by their content
    arf = parse_feedback_report(raw)
    if arf is not None:
        return await asyncio.to_thread(handle_feedback_report, arf, envelope_rcpt)

    decoded = decode_verp_recipient(envelope_rcpt)
    if not decoded:
        logger.warning(
            f"[ASYNC_BOUNCE] invalid VERP envelope, ignoring: {envelope_rcpt!r}"
        )
        return {"processed": False, "reason": "invalid_verp"}
    rcpt, rbm_domain = decoded

    status = extract_dsn_status(raw)
    if not status:
        logger.info(
            f"[ASYNC_BOUNCE] no Status in the DSN for {rcpt} ({rbm_domain}) - NOT suppressing"
        )
        return {
            "processed": True,
            "suppressed": False,
            "reason": "no_dsn_status",
            "rcpt": rcpt,
            "domain_name": rbm_domain,
        }

    bounce_class = classify_bounce_type(status, extract_dsn_diagnostic(raw))
    logger.info(
        f"[ASYNC_BOUNCE] rcpt={rcpt} domain={rbm_domain} status={status} "
        f"class={bounce_class}"
    )
    if bounce_class != "hard":
        return {
            "processed": True,
            "suppressed": False,
            "bounce_type": bounce_class,
            "status": status,
            "rcpt": rcpt,
            "domain_name": rbm_domain,
        }

    if not await asyncio.to_thread(
        bounce_matches_send, rcpt, rbm_domain, extract_original_message_ids(raw)
    ):
        logger.warning(
            f"[ASYNC_BOUNCE] DSN for {rcpt} ({rbm_domain}) with no matching send in the ledger - ignored"
        )
        return {
            "processed": False,
            "suppressed": False,
            "reason": "unknown_message",
            "rcpt": rcpt,
            "domain_name": rbm_domain,
        }

    result = await asyncio.to_thread(
        apply_suppression_rules_for_bounce, rcpt, rbm_domain, "hard"
    )
    suppressed = bool(result and result.get("suppressed"))
    return {
        "processed": True,
        "suppressed": suppressed,
        "bounce_type": "hard",
        "status": status,
        "rcpt": rcpt,
        "domain_name": rbm_domain,
    }


@app.post("/batch")
async def ingest_batch(request: Request):
    """
    Async dispatcher for /batch: same pattern as /postfix, offloading the
    synchronous work to a worker thread so the event loop and probes stay free.
    """
    # Read body before the semaphore — same ClientDisconnect fix as /postfix.
    payload = await _read_ingest_body(request)
    async with _ingest_semaphore:
        return await _run_ingest(_ingest_batch_sync, payload)


def _ingest_batch_sync(payload: bytes):
    """
    Endpoint for batch event ingestion

    Accepts a list of events in this format:
    [
        {"queue_id": "...", "message_id": "...", "rcpt": "...", "status": "..."},
        ...
    ]
    """
    try:
        raw_payload = payload.decode("utf-8")
        events = json.loads(raw_payload)
    except Exception as e:
        logger.warning(f"Invalid JSON received on /batch: {e}")
        raise HTTPException(
            status_code=400,
            detail=f"invalid_json: {e!s}",
        ) from e

    logger.debug(f"Processing /batch - {len(payload)} bytes")
    logger.debug(f"Parsed payload: {type(events)}")

    if not isinstance(events, list):
        raise HTTPException(status_code=400, detail="expected_list")

    try:
        results = []
        for i, event in enumerate(events):
            try:
                # Process each event directly with process_delivery_event
                if isinstance(event, dict):
                    queue_id = event.get("queue_id", "")
                    rcpt = event.get("rcpt") or event.get("to", "")
                    status = event.get("status", "")
                    dsn = event.get("dsn", "")
                    host = event.get("host")
                    service = event.get("service")
                    timestamp = event.get("timestamp")
                    status_msg = event.get("status_msg", "")

                    # Find the message_id
                    message_id = event.get("message_id")
                    if not message_id:
                        message_id = find_message_id_by_queue(queue_id, rcpt)

                    if message_id:
                        # Normalize the timestamp before processing
                        normalized_timestamp = normalize_timestamp(timestamp)
                        result = process_delivery_event(
                            message_id,
                            rcpt,
                            queue_id,
                            status,
                            dsn,
                            host,
                            service,
                            normalized_timestamp,
                            status_msg,
                            relay_host=event_relay_host(event),
                        )
                        results.append({"index": i, "result": result})
                    else:
                        # message_id MISS: Redis is NOT scanned here (a keyspace scan per
                        # event without a mapping would stall ingestion during a burst of
                        # deferred retries). The miss is only reported.
                        results.append(
                            {
                                "index": i,
                                "error": "message_id not found",
                                "suggestion": "the milter may not have processed the email, or the q: key expired (TTL)",
                                "debug_info": {"queue_id": queue_id, "rcpt": rcpt},
                            }
                        )
                else:
                    results.append(
                        {
                            "index": i,
                            "error": "invalid event format",
                        }
                    )

            except Exception as e:
                results.append({"index": i, "error": str(e)})

        return {"status": "ok", "processed": len(events), "results": results}

    except Exception as e:
        logger.error(f"Error processing batch: {e}")
        return {"status": "error", "reason": "batch_processing_error", "error": str(e)}


@app.get("/status-mapping")
async def get_status_mapping():
    """
    Show how Postfix DSN codes map to delivery statuses
    """
    return {
        "status": "ok",
        "dsn_mapping": {
            "2.x.x": "Successful delivery -> status: 'delivered'",
            "4.x.x": "Temporary failure -> status: 'deferred'",
            "5.x.x": "Permanent failure -> status: 'bounced'",
        },
        "status_definitions": {
            "sent": "Email accepted by the destination server (SMTP 250)",
            "delivered": "Email delivered to the final recipient (DSN 2.x.x)",
            "deferred": "Temporary delivery failure (DSN 4.x.x)",
            "bounced": "Permanent delivery failure (DSN 5.x.x)",
        },
        "transition_rules": {
            "sent -> delivered": "Allowed (delivery confirmation)",
            "sent -> deferred": "Allowed (another attempt)",
            "deferred -> delivered": "Allowed (success after a retry)",
            "bounced -> sent/deferred/delivered": "NOT allowed (permanent failure)",
            "sent -> bounced": "Allowed (failure detected)",
        },
        "suppression_integration": {
            "bounce_trigger": "A hard bounce (status 'bounced', bounce type 'hard') calls apply_suppression_rules_for_bounce()",
            "suppression_types": ["bounce", "complaint", "unsubscribe", "manual"],
            "automatic_rules": [
                "hard bounce: immediate, permanent suppression of the address for its domain",
                "soft, policy and protocol bounces: no suppression",
            ],
            "endpoints": {
                "/suppression/test": "Show the suppression state of a domain",
                "/suppressions": "List active suppressions",
            },
        },
        "description": "Maps Postfix DSN codes to the real delivery status and suppresses addresses on hard bounces",
        "timestamp": datetime.now().isoformat(),
    }


@app.get("/health/live")
async def health_live():
    return {"status": "ok", "service": "events_updater"}


def _check_deps_sync() -> dict:
    health_status = {
        "status": "healthy",
        "service": "events_updater",
        "version": "1.0.0",
        "timestamp": datetime.now().isoformat(),
        "database": {"status": "unknown"},
        "redis": {"status": "unknown"},
    }

    try:
        result = fetch_one("SELECT 1 as test")
        if result and result.get("test") == 1:
            health_status["database"]["status"] = "healthy"
        else:
            health_status["database"]["status"] = "unhealthy"
    except Exception as e:
        health_status["database"]["status"] = "unhealthy"
        health_status["database"]["error"] = str(e)

    try:
        redis.ping()
        health_status["redis"]["status"] = "healthy"
    except Exception as e:
        health_status["redis"]["status"] = "unhealthy"
        health_status["redis"]["error"] = str(e)

    if (
        health_status["database"]["status"] == "healthy"
        and health_status["redis"]["status"] == "healthy"
    ):
        health_status["status"] = "healthy"
    else:
        health_status["status"] = "degraded"

    return health_status


@app.get("/health/ready")
async def health_ready():
    return await asyncio.to_thread(_check_deps_sync)


@app.get("/health")
async def health_check():
    return await health_live()


@app.get("/suppressions")
async def get_suppressions(domain_name: str = None, active_only: bool = True):
    """Return the active suppressions (the synchronous MySQL work runs in a thread)."""
    return await asyncio.to_thread(_get_suppressions_sync, domain_name, active_only)


def _get_suppressions_sync(domain_name: str = None, active_only: bool = True):
    try:
        if domain_name:
            suppressions = fetch_all(
                """
                SELECT id, email, domain_name, suppression_type, reason,
                       suppressed_at, expires_at, is_active
                FROM suppressions
                WHERE domain_name = %s AND is_active = %s
                ORDER BY suppressed_at DESC
            """,
                (domain_name, active_only),
            )
        else:
            suppressions = fetch_all(
                """
                SELECT id, email, domain_name, suppression_type, reason,
                       suppressed_at, expires_at, is_active
                FROM suppressions
                WHERE is_active = %s
                ORDER BY suppressed_at DESC
            """,
                (active_only,),
            )

        return {
            "status": "success",
            "domain_name": domain_name,
            "suppressions": suppressions,
            "count": len(suppressions),
        }
    except Exception as e:
        logger.error(f"Error fetching suppressions: {e}")
        raise HTTPException(status_code=500, detail=f"Internal error: {e!s}") from e


@app.get("/stats")
async def get_stats():
    """Return basic service statistics (the synchronous MySQL work runs in a thread)."""
    return await asyncio.to_thread(_get_stats_sync)


def _get_stats_sync():
    try:
        stats = {}

        # Deliveries per status (Postfix-based)
        for status in ["sent", "delivered", "bounced", "deferred"]:
            result = fetch_one(
                "SELECT COUNT(*) as count FROM deliveries WHERE status = %s", (status,)
            )
            stats[f"{status}_count"] = result["count"] if result else 0

        # Total events
        result = fetch_one("SELECT COUNT(*) as count FROM delivery_events")
        stats["total_events"] = result["count"] if result else 0

        # Unique deliveries
        result = fetch_one("SELECT COUNT(*) as count FROM deliveries")
        stats["unique_deliveries"] = result["count"] if result else 0

        # Active suppressions
        result = fetch_one(
            "SELECT COUNT(*) as count FROM suppressions WHERE is_active = TRUE"
        )
        stats["active_suppressions"] = result["count"] if result else 0

        return {"status": "ok", "stats": stats, "timestamp": datetime.now().isoformat()}

    except Exception as e:
        logger.error(f"Error getting statistics: {e}")
        raise HTTPException(status_code=500, detail="Internal server error") from e


@app.get("/suppression/test")
async def test_suppression_system(domain_name: str = None):
    """Show the suppression state of a domain (the synchronous MySQL work runs in a thread)."""
    return await asyncio.to_thread(_test_suppression_system_sync, domain_name)


def _test_suppression_system_sync(domain_name: str = None):
    """
    Show the suppression state of a domain (recent suppressions and bounces)
    """
    try:
        if not domain_name:
            return {
                "status": "error",
                "error": "domain_name is required",
                "usage": "Use: GET /suppression/test?domain_name=example.com",
            }

        logger.info(
            f"[SUPPRESSION_TEST] Checking the suppression state of domain: {domain_name}"
        )

        # Active suppressions
        suppressions = fetch_all(
            """
            SELECT email, suppression_type, reason, suppressed_at
            FROM suppressions
            WHERE domain_name = %s AND is_active = TRUE
            ORDER BY suppressed_at DESC
            LIMIT 10
        """,
            (domain_name,),
        )

        # Recent bounces
        bounces = fetch_all(
            """
            SELECT rcpt, COUNT(*) as bounce_count, MAX(last_update) as last_bounce
            FROM deliveries
            WHERE domain_name = %s AND status = 'bounced'
            AND last_update >= DATE_SUB(NOW(), INTERVAL 24 HOUR)
            GROUP BY rcpt
            ORDER BY bounce_count DESC
            LIMIT 10
        """,
            (domain_name,),
        )

        return {
            "status": "success",
            "domain_name": domain_name,
            "system_type": "Simplified - Bounce = Immediate Suppression",
            "active_suppressions": len(suppressions),
            "recent_suppressions": suppressions,
            "recent_bounces": bounces,
            "bounce_count": len(bounces),
            "timestamp": datetime.now().isoformat(),
        }

    except Exception as e:
        logger.error(f"Error checking the suppression state: {e}")
        raise HTTPException(status_code=500, detail=f"Internal error: {e!s}") from e


@app.get("/delivery/{message_id}")
async def get_delivery_status(message_id: str):
    """Return the delivery status of one message (the synchronous MySQL work runs in a thread)."""
    return await asyncio.to_thread(_get_delivery_status_sync, message_id)


def _get_delivery_status_sync(message_id: str):
    try:
        # Delivery status
        deliveries = fetch_all(
            "SELECT rcpt, status, attempt, last_update FROM deliveries WHERE message_id = %s",
            (message_id,),
        )

        if not deliveries:
            return {"status": "not_found", "message_id": message_id}

        # Delivery events
        events = fetch_all(
            "SELECT rcpt, status, queue_id, ts FROM delivery_events WHERE message_id = %s ORDER BY ts DESC",
            (message_id,),
        )

        return {
            "status": "ok",
            "message_id": message_id,
            "deliveries": deliveries,
            "events": events,
        }

    except Exception as e:
        logger.error(f"Error fetching the delivery status: {e}")
        raise HTTPException(status_code=500, detail="Internal server error") from e


if __name__ == "__main__":
    import uvicorn

    logger.info("Starting Events Updater v1.0.0...")
    uvicorn.run(app, host=EVENTS_UPDATER_HOST, port=EVENTS_UPDATER_PORT)
