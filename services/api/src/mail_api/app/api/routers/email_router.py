"""
Email Router
Endpoints for sending, tracking, and managing transactional emails.
"""

import logging
import os
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from mail_api.common.config import SUPPRESSION_CHECK_MODE
from mail_api.common.db import fetch_all, fetch_one
from mail_shared.db.mysql_sync import exec_sql_async
from mail_api.common.idempotency import (
    claim_idempotency_key,
    get_idempotent_response,
    release_idempotency_key,
    store_idempotent_response,
)
from mail_api.models.email_models import EmailResponse, EmailStatus, SendEmailRequest
from mail_api.services import volume_governor
from mail_api.services.email_service import MailService
from mail_api.services.suppression_filter import filter_personalizations, sender_domains
from mail_api.shared.domain_validation import get_user_domain_id, get_user_domain_name_async, validate_authorized_sender
from connect_lib.auth.hybrid import require_read_emails_hybrid, require_write_emails_hybrid

logger = logging.getLogger(__name__)

# Router for the email endpoints
# No prefix: the routes are /send, /status, etc.
email_router = APIRouter(tags=["emails"])

# Service instance
mail_service = MailService()


def _tenant_delivery_filter(domain_id, domain_name: str):
    """SQL and parameters that match a delivery_events row to the tenant.

    The events updater records each delivery with the sending host
    (rbm<id>.<domain>) and with the domain_id OF THAT host, which is its own row
    in domains, not the id or the name of the key's virtual domain. That is why
    the filter accepts the tenant's id, the exact name and any host under the name.
    """
    if domain_id is not None:
        return (
            "(domain_id = %s OR domain_name = %s OR domain_name LIKE %s)",
            (domain_id, domain_name, f"%.{domain_name}"),
        )
    return (
        "(domain_name = %s OR domain_name LIKE %s)",
        (domain_name, f"%.{domain_name}"),
    )


@email_router.post(
    "/send",
    response_model=EmailResponse,
    summary="Send Email",
    description="""
## Send Transactional or Marketing Email

Submit emails for delivery through our high-performance queue system.

### Features

- **HTML & Plain Text**: Full support for rich HTML templates and plain text fallback
- **Dynamic Personalization**: Variable substitution per recipient for personalized content
- **Async Processing**: High-throughput queue-based delivery for optimal performance
- **Automatic Tracking**: Built-in open and click tracking (configurable)
- **Custom Headers**: Support for custom email headers

### Authentication

Requires a valid JWT token or API key in the `Authorization` header.

```
Authorization: Bearer <token>
```

### Rate Limits

- Maximum 1,000 recipients per request
- Priority levels: 1-10 (10 = highest)
- Request timeout: 30 seconds
    """,
    responses={
        200: {
            "description": "Email queued successfully",
            "content": {
                "application/json": {
                    "example": {
                        "message_id": "msg_1234567890abcdef",
                        "status": "queued",
                        "domain_name": "example.com",
                        "queue": "email_queue",
                        "timestamp": "2025-01-21T16:30:00Z",
                        "recipients_count": 1,
                    }
                }
            },
        },
        400: {
            "description": "Invalid request data",
            "content": {
                "application/json": {
                    "example": {"detail": "At least one personalization is required"}
                }
            },
        },
        401: {
            "description": "Invalid or expired token",
            "content": {
                "application/json": {
                    "example": {"detail": "Invalid or expired token"}
                }
            },
        },
        403: {
            "description": "Sender not authorized",
            "content": {
                "application/json": {
                    "example": {"detail": "Sender email not authorized for this domain"}
                }
            },
        },
        422: {
            "description": "Validation error",
            "content": {
                "application/json": {
                    "example": {
                        "detail": [
                            {
                                "loc": ["body", "subject"],
                                "msg": "field required",
                                "type": "value_error.missing",
                            }
                        ]
                    }
                }
            },
        },
        500: {
            "description": "Internal server error",
            "content": {
                "application/json": {"example": {"detail": "Internal server error"}}
            },
        },
    },
    tags=["emails"],
    operation_id="emails_send",
)
async def send_email(
    email_request: SendEmailRequest,
    request: Request,
    current_user: dict = Depends(require_write_emails_hybrid),
):
    """
    Sends email through the queue system

    **Protected Route** - Requires an authentication token

    Args:
        email_request: Email request data
        request: Request object, used to extract the token
        current_user: Authenticated user

    Returns:
        EmailResponse: Response with the queued email's information
    """
    try:
        # Extract the original token from the Authorization header
        auth_header = request.headers.get("Authorization", "")
        api_key = None
        if auth_header.startswith("Bearer "):
            api_key = auth_header.split(" ")[1]
            logger.debug(
                f"[EMAIL_ROUTER] Token extracted from the header (length={len(api_key)})"
            )

        # Idempotency: the caller sends an Idempotency-Key (e.g.
        # campaign:user:decision). On a retry (HTTP client, job runner, gateway) the
        # cached response is returned WITHOUT enqueuing again, so no duplicate email.
        # Checked early: a retry skips the sender validation.
        raw_idempotency_key = (
            request.headers.get("Idempotency-Key")
            or request.headers.get("idempotency-key")
            or ""
        ).strip()[:255]
        # The key is scoped to the caller's customer: two customers using the same key never
        # see each other's cached response, and one cannot suppress the other's send.
        idempotency_scope = (
            current_user.get("client_id") or current_user.get("api_key_id") or current_user.get("user_id") or ""
        )
        idempotency_key = f"{idempotency_scope}:{raw_idempotency_key}" if raw_idempotency_key and idempotency_scope else None
        if idempotency_key:
            cached = await get_idempotent_response(idempotency_key)
            if cached:
                logger.warning(
                    "[EMAIL_ROUTER] Idempotent retry: key=%s, returning the cached "
                    "message_id=%s", idempotency_key, cached.get("message_id"),
                )
                return EmailResponse(**cached)

        # domain_name from the user's JWT FIRST (async, cached)
        domain_name = await get_user_domain_name_async(current_user)

        # Sender email, for the validation
        sender_email = email_request.from_.address

        # CRITICAL VALIDATION: is the sender authorized?
        # A sender is authorized if it:
        # 1. Belongs to the API key's domain (or its subdomains), OR
        # 2. Is an approved sender for the user's main domain
        is_authorized = await validate_authorized_sender(sender_email, current_user)
        if not is_authorized:
            sender_domain = sender_email.split("@")[1] if "@" in sender_email else "unknown"
            logger.warning(
                f"[EMAIL_ROUTER] Send attempt blocked: sender={sender_email}, sender_domain={sender_domain}, jwt_domain={domain_name}, user={current_user.get('username', 'unknown')}"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Sending from the address '{sender_email}' is not allowed. The sender must belong to your authorized domain ('{domain_name}') or be an approved sender."
            )

        # Log the received request (after validation)
        logger.info("[EMAIL_ROUTER] Received POST /emails/send request")
        logger.info(
            f"[EMAIL_ROUTER] Request data: subject='{email_request.subject}', sender='{email_request.from_.address}', domain='{domain_name}', personalizations={len(email_request.personalizations)}"
        )

        logger.info(f"[EMAIL_ROUTER] Processing the request for domain {domain_name}")
        logger.info(
            f"[EMAIL_ROUTER] Authenticated user: {current_user.get('username', 'unknown')}"
        )

        # === SUPPRESSION FILTER ===
        # A suppressed recipient never enters the queue.
        domains_for_suppression = sender_domains(domain_name, sender_email)
        kept_personalizations, suppressed_map = await filter_personalizations(
            email_request.personalizations, domains_for_suppression
        )
        if suppressed_map:
            por_tipo: dict[str, int] = {}
            for tipo in suppressed_map.values():
                por_tipo[tipo] = por_tipo.get(tipo, 0) + 1
            logger.info(
                "log_type=audit action=suppression_filter mode=%s domain=%s "
                "suppressed=%d by_type=%s",
                SUPPRESSION_CHECK_MODE, domain_name, len(suppressed_map), por_tipo,
            )

        if not kept_personalizations:
            # Every recipient is suppressed: nothing to enqueue. 200 with
            # recipients_count=0: the request was valid and there is nothing for
            # the caller to retry.
            logger.warning(
                "log_type=audit action=send_all_suppressed domain=%s "
                "recipients=%d", domain_name, len(suppressed_map),
            )
            return EmailResponse(
                message_id=str(uuid.uuid4()),
                status=EmailStatus.SUPPRESSED,
                domain_name=domain_name,
                queue="email-queue",
                timestamp=datetime.utcnow().isoformat(),
                recipients_count=0,
            )

        if kept_personalizations is not email_request.personalizations:
            email_request = email_request.model_copy(
                update={"personalizations": kept_personalizations}
            )

        # Tenant of the send = the authenticated client. The payload's customer_id is
        # ignored: accepting it would let a client write sends to another client's
        # ledger (and statistics).
        client_id = current_user.get("client_id") or current_user.get("user_id") or current_user.get("api_key_id")
        total_recipients = sum(len(p.to) for p in email_request.personalizations)

        # === VOLUME GOVERNOR (per-domain ramp-up) ===
        # After the suppressions (only real recipients count).
        # Excess above the domain's daily quota is DEFERRED, never dropped.
        gov = await volume_governor.evaluate(domain_name, total_recipients)
        if gov.reject:
            logger.warning(
                "log_type=audit action=volume_governor_reject domain=%s key=%s "
                "total=%d quota=%d", domain_name, gov.governed_key,
                total_recipients, gov.quota,
            )
            raise HTTPException(
                status_code=429,
                detail={
                    "error": "volume_quota_exceeded",
                    "message": (
                        f"Volume ({total_recipients}) is many times the domain's "
                        f"healthy daily quota ({gov.quota}). "
                        "Split the send over several days or contact the operator."
                    ),
                    "daily_quota": gov.quota,
                },
            )
        if gov.defer > 0 and gov.tomorrow_room is not None:
            # The deferred part goes out tomorrow in one go; it must fit tomorrow's quota,
            # otherwise a large first campaign from a new domain would just land a day later.
            _, defer_check = volume_governor.split_personalizations(
                email_request.personalizations, gov.admit
            )
            deferred_n = sum(len(p.to) for p in defer_check)
            if deferred_n > gov.tomorrow_room:
                logger.warning(
                    "log_type=audit action=volume_governor_full domain=%s key=%s "
                    "total=%d quota=%d deferred=%d tomorrow_room=%d", domain_name,
                    gov.governed_key, total_recipients, gov.quota, deferred_n,
                    gov.tomorrow_room,
                )
                raise HTTPException(
                    status_code=429,
                    detail={
                        "error": "volume_quota_full",
                        "message": (
                            f"The daily limit of this domain ({gov.quota} emails) is used up "
                            "for today and tomorrow. New domains start low and the limit "
                            "grows by about 30% for each day of clean delivery. Send the "
                            "rest later (see Retry-After)."
                        ),
                        "daily_quota": gov.quota,
                    },
                    headers={"Retry-After": str(volume_governor.seconds_until_next_day())},
                )
        if gov.defer or gov.would_defer:
            logger.info(
                "log_type=audit action=volume_governor mode=%s domain=%s key=%s "
                "exempt=%s quota=%d consumed=%d total=%d admit=%d defer=%d "
                "would_defer=%d", gov.mode, domain_name, gov.governed_key,
                gov.exempt, gov.quota, gov.consumed, total_recipients,
                gov.admit, gov.defer, gov.would_defer,
            )

        # Idempotency (race-safe claim BEFORE enqueuing): the message_id is generated
        # here and the key is reserved via SETNX. If a concurrent request already
        # reserved it (same key), its response is returned and nothing is enqueued:
        # no duplicate email. Without a key, the normal flow goes on.
        message_id = str(uuid.uuid4())
        idem_claimed = False
        if idempotency_key:
            claim_response = {
                "message_id": message_id,
                "status": EmailStatus.QUEUED.value,
                "domain_name": domain_name,
                "queue": "email-queue",
                "timestamp": datetime.utcnow().isoformat(),
                "recipients_count": total_recipients,
            }
            if not await claim_idempotency_key(idempotency_key, claim_response):
                cached = await get_idempotent_response(idempotency_key)
                if cached:
                    logger.warning(
                        "[EMAIL_ROUTER] Idempotent race lost: key=%s, returning the "
                        "cached message_id=%s", idempotency_key, cached.get("message_id"),
                    )
                    return EmailResponse(**cached)
                # Cache miss after losing the race (TTL expired?): the normal flow goes on.
            else:
                idem_claimed = True

        # === GOVERNOR SPLIT (enforce with excess only) ===
        # The admitted part goes in now; the excess becomes a second enqueue with
        # scheduled_at in tomorrow's window (the existing scheduled:{domain} ZSET).
        # Conservative boundary: a personalization that would cross the limit goes
        # WHOLE to the deferred part. If NOTHING fits today, the whole request is scheduled.
        deferred_request = None
        deferred_all = False
        if gov.defer > 0:
            admit_ps, defer_ps = volume_governor.split_personalizations(
                email_request.personalizations, gov.admit
            )
            if defer_ps:
                deferred_request = email_request.model_copy(
                    update={
                        "personalizations": defer_ps,
                        "scheduled_at": gov.defer_until_iso,
                    }
                )
                if admit_ps:
                    email_request = email_request.model_copy(
                        update={"personalizations": admit_ps}
                    )
                else:
                    email_request = deferred_request
                    deferred_request = None
                    deferred_all = True

        # Enqueue the email
        logger.info(
            f"[EMAIL_ROUTER] Calling mail_service.queue_email for domain {domain_name}..."
        )
        scheduled_extra = 0
        try:
            response = await mail_service.queue_email(
                domain_name,
                email_request,
                api_key=api_key,
                customer_id=client_id,
                client_id=client_id,
                message_id=message_id,
            )
            if deferred_request is not None:
                # A failure here propagates (releases the idempotency key): losing
                # the deferred part silently would be worse than duplicating the
                # admitted part on a retry (same trade-off as the partial enqueue below).
                defer_resp = await mail_service.queue_email(
                    domain_name,
                    deferred_request,
                    api_key=api_key,
                    customer_id=client_id,
                    client_id=client_id,
                    message_id=str(uuid.uuid4()),
                )
                scheduled_extra = defer_resp.recipients_count
        except Exception:
            # The enqueue failed AFTER the claim: release the key, otherwise the
            # caller's retry gets the cached "queued" answer for an email that never
            # reached the queue (a silent loss). Recipients enqueued before a partial
            # failure may be duplicated on the retry: an accepted trade-off,
            # duplicating a few is better than losing all of them.
            if idem_claimed:
                await release_idempotency_key(idempotency_key)
            raise

        # Governor consumption (best-effort): today = admitted now; tomorrow =
        # deferred (reserves the target day's quota, deferred traffic has priority).
        # Exempt domains count too (observability), they are just never held.
        if gov.governed_key:
            if deferred_all:
                await volume_governor.register_admission(
                    gov.governed_key, response.recipients_count, offset_days=1
                )
            else:
                await volume_governor.register_admission(
                    gov.governed_key, response.recipients_count
                )
                if scheduled_extra:
                    await volume_governor.register_admission(
                        gov.governed_key, scheduled_extra, offset_days=1
                    )

        # The response reflects the split: recipients_count = total accepted (today +
        # scheduled), scheduled_count = how many go tomorrow.
        if deferred_all:
            response = response.model_copy(
                update={"scheduled_count": response.recipients_count}
            )
        elif scheduled_extra:
            response = response.model_copy(
                update={
                    "recipients_count": response.recipients_count + scheduled_extra,
                    "scheduled_count": scheduled_extra,
                }
            )

        # Overwrite the cache with the authoritative response (the real
        # recipients_count/queue of queue_email), keeping the TTL for the retries.
        if idempotency_key:
            await store_idempotent_response(idempotency_key, response.model_dump(mode="json"))

        logger.info(
            f"[EMAIL_ROUTER] Email enqueued: message_id={response.message_id}, domain_name={domain_name}, recipients={response.recipients_count}"
        )

        # RevBridge Connect: subject and sender per message, for the panel's email list.
        # Best-effort: it never blocks the send.
        if os.getenv("CONNECT_MESSAGE_META", "true").lower() in ("1", "true", "yes"):
            try:
                await exec_sql_async(
                    "INSERT IGNORE INTO message_meta (message_id, customer_id, sender_email, subject, recipients_count) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (
                        response.message_id,
                        current_user.get("client_id"),
                        email_request.from_.address,
                        email_request.subject[:998],
                        int(response.recipients_count or 0),
                    ),
                )
            except Exception as meta_err:  # noqa: BLE001
                logger.warning(f"[EMAIL_ROUTER] message_meta not written: {meta_err}")

        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"[EMAIL_ROUTER] Error processing the POST /emails/send request: {e}",
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail="Internal server error") from e


@email_router.get(
    "/status/{message_id}",
    summary="Get Email Status",
    description="""
## Get Email Delivery Status

Retrieve the current delivery status for a specific email message.

### Status Values

| Status | Description |
|--------|-------------|
| `pending` | Awaiting processing |
| `queued` | In the delivery queue |
| `sending` | Currently being sent |
| `sent` | Successfully sent to mail server |
| `delivered` | Confirmed delivery to recipient |
| `bounced` | Message bounced back |
| `failed` | Delivery failed |

### Response Fields

- `message_id`: Unique message identifier
- `recipient`: Recipient email address
- `status`: Current delivery status
- `dsn`: DSN (Delivery Status Notification) code, if available
- `attempt`: Number of delivery attempts
- `ts`: Timestamp of the last status update

### Authentication

Requires a valid JWT token or API key in the `Authorization` header.
    """,
    responses={
        200: {
            "description": "Status retrieved successfully",
            "content": {
                "application/json": {
                    "example": {
                        "message_id": "msg_1234567890abcdef",
                        "recipient": "user@example.com",
                        "status": "delivered",
                        "dsn": "2.0.0",
                        "attempt": 1,
                        "ts": "2025-01-21T16:30:00Z",
                    }
                }
            },
        },
        401: {
            "description": "Invalid or expired token",
            "content": {
                "application/json": {
                    "example": {"detail": "Invalid or expired token"}
                }
            },
        },
        404: {
            "description": "Email not found",
            "content": {
                "application/json": {"example": {"detail": "Email not found"}}
            },
        },
        500: {
            "description": "Internal server error",
            "content": {
                "application/json": {"example": {"detail": "Internal server error"}}
            },
        },
    },
    tags=["emails"],
    operation_id="emails_status",
)
async def get_email_status(
    message_id: str = Path(
        ..., description="Unique message ID", examples=["msg_1234567890abcdef"]
    ),
    current_user: dict = Depends(require_read_emails_hybrid),
):
    """
    Fetches the status of one email

    Args:
        message_id: Message ID
        current_user: Authenticated user

    Returns:
        dict: Email status
    """
    try:
        # domain_name from the user's JWT
        domain_name = await get_user_domain_name_async(current_user)

        logger.info(
            f"[EMAIL_STATUS] Fetching the status of message_id: {message_id}, domain_name: {domain_name}"
        )

        # Is there data in the delivery_events table?
        table_check = fetch_one("SELECT COUNT(*) as total FROM delivery_events LIMIT 1")
        logger.info(
            f"[EMAIL_STATUS] Total rows in the delivery_events table: {table_check['total'] if table_check else 'N/A'}"
        )

        # Fetch the status from the delivery_events table (the real data)
        tenant_sql, tenant_params = _tenant_delivery_filter(
            get_user_domain_id(current_user), domain_name
        )
        delivery_info = fetch_one(
            f"""
            SELECT
                message_id,
                rcpt,
                status,
                dsn,
                attempt,
                ts
            FROM delivery_events
            WHERE message_id = %s
            AND {tenant_sql}
            ORDER BY ts DESC
            LIMIT 1
            """,
            (message_id, *tenant_params),
        )

        logger.info(f"[EMAIL_STATUS] Query result: {delivery_info}")

        if not delivery_info:
            logger.warning(
                f"[EMAIL_STATUS] No row found for message_id: {message_id}, domain_name: {domain_name}"
            )
            raise HTTPException(status_code=404, detail="Email not found")

        result = {
            "message_id": delivery_info["message_id"],
            "recipient": delivery_info["rcpt"],
            "status": delivery_info["status"],
            "dsn": delivery_info.get("dsn"),
            "attempt": delivery_info.get("attempt"),
            "ts": delivery_info["ts"].isoformat() if delivery_info["ts"] else None,
        }

        logger.info(f"[EMAIL_STATUS] Returning result: {result}")
        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching the email status: {e}")
        raise HTTPException(status_code=500, detail="Internal server error") from e


@email_router.get(
    "/history",
    summary="Get Email History",
    description="""
## Get Email History

Retrieve email delivery history with advanced filtering and pagination.

### Available Filters

| Parameter | Type | Description |
|-----------|------|-------------|
| `status` | string | Filter by delivery status (sent, delivered, bounced, etc.) |
| `start_date` | string | Start date in YYYY-MM-DD format |
| `end_date` | string | End date in YYYY-MM-DD format |
| `page` | integer | Page number (default: 1) |
| `limit` | integer | Items per page (default: 50, max: 1000) |

### Response Structure

- `emails`: Array of email records with delivery details
- `pagination`: Pagination metadata including total count and page info

### Sorting

Results are sorted by timestamp in descending order (most recent first).

### Authentication

Requires a valid JWT token or API key in the `Authorization` header.
    """,
    responses={
        200: {
            "description": "History retrieved successfully",
            "content": {
                "application/json": {
                    "example": {
                        "emails": [
                            {
                                "message_id": "msg_1234567890abcdef",
                                "recipient": "user@example.com",
                                "status": "delivered",
                                "dsn": "2.0.0",
                                "attempt": 1,
                                "ts": "2025-01-21T16:30:00Z",
                            }
                        ],
                        "pagination": {
                            "page": 1,
                            "limit": 50,
                            "total": 150,
                            "total_pages": 3,
                            "has_next": True,
                            "has_prev": False,
                        },
                    }
                }
            },
        },
        400: {
            "description": "Invalid parameters",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Start date must be before end date"
                    }
                }
            },
        },
        401: {
            "description": "Invalid or expired token",
            "content": {
                "application/json": {
                    "example": {"detail": "Invalid or expired token"}
                }
            },
        },
        500: {
            "description": "Internal server error",
            "content": {
                "application/json": {"example": {"detail": "Internal server error"}}
            },
        },
    },
    tags=["emails"],
    operation_id="emails_history",
)
async def get_email_history(
    page: int = Query(1, ge=1, description="Page number", examples=[1]),
    limit: int = Query(
        50, ge=1, le=1000, description="Items per page", examples=[50]
    ),
    status: str | None = Query(
        None, description="Filter by status", examples=["delivered"]
    ),
    start_date: str | None = Query(
        None, description="Start date (YYYY-MM-DD)", examples=["2025-01-01"]
    ),
    end_date: str | None = Query(
        None, description="End date (YYYY-MM-DD)", examples=["2025-01-31"]
    ),
    current_user: dict = Depends(require_read_emails_hybrid),
):
    """
    Fetches the email history with pagination

    Args:
        page: Page number
        limit: Items per page
        status: Filter by status
        start_date: Start date (YYYY-MM-DD)
        end_date: End date (YYYY-MM-DD)
        current_user: Authenticated user

    Returns:
        dict: Email history
    """
    try:
        # domain_name from the user's JWT
        domain_name = await get_user_domain_name_async(current_user)

        offset = (page - 1) * limit

        logger.info(
            f"[EMAIL_HISTORY] Fetching the history for domain_name: {domain_name}, page: {page}, limit: {limit}"
        )
        logger.info(
            f"[EMAIL_HISTORY] Filters - status: {status}, start_date: {start_date}, end_date: {end_date}"
        )

        # Build the WHERE filters
        where_conditions = ["domain_name = %s"]
        params = [domain_name]

        if status:
            where_conditions.append("status = %s")
            params.append(status)

        if start_date:
            where_conditions.append("DATE(ts) >= %s")
            params.append(start_date)

        if end_date:
            where_conditions.append("DATE(ts) <= %s")
            params.append(end_date)

        where_clause = " AND ".join(where_conditions)
        logger.info(f"[EMAIL_HISTORY] WHERE clause: {where_clause}")
        logger.info(f"[EMAIL_HISTORY] Params: {params}")

        # Fetch the emails with pagination - delivery_events is the main source
        emails_query = f"""
            SELECT
                message_id,
                rcpt,
                status,
                dsn,
                attempt,
                ts
            FROM delivery_events
            WHERE {where_clause}
            ORDER BY ts DESC
            LIMIT %s OFFSET %s
        """

        params.extend([limit, offset])
        logger.info(f"[EMAIL_HISTORY] Running query: {emails_query}")
        logger.info(f"[EMAIL_HISTORY] Query params: {params}")

        emails = fetch_all(emails_query, params)
        logger.info(
            f"[EMAIL_HISTORY] Emails found: {len(emails) if emails else 0}"
        )

        # Count the total rows
        count_query = f"""
            SELECT COUNT(*) as total
            FROM delivery_events
            WHERE {where_clause}
        """
        count_params = params[:-2]  # Remove limit and offset
        logger.info(f"[EMAIL_HISTORY] Running count query: {count_query}")
        logger.info(f"[EMAIL_HISTORY] Count params: {count_params}")

        total_result = fetch_one(count_query, count_params)
        total = total_result["total"] if total_result else 0
        logger.info(f"[EMAIL_HISTORY] Total rows: {total}")

        # Pagination
        total_pages = (total + limit - 1) // limit
        has_next = page < total_pages
        has_prev = page > 1

        # Format the emails for the response
        formatted_emails = []
        for email in emails:
            formatted_emails.append(
                {
                    "message_id": email["message_id"],
                    "recipient": email["rcpt"],
                    "status": email["status"],
                    "dsn": email.get("dsn"),
                    "attempt": email.get("attempt"),
                    "ts": email["ts"].isoformat() if email["ts"] else None,
                }
            )

        result = {
            "emails": formatted_emails,
            "pagination": {
                "page": page,
                "limit": limit,
                "total": total,
                "total_pages": total_pages,
                "has_next": has_next,
                "has_prev": has_prev,
            },
        }

        logger.info(
            f"[EMAIL_HISTORY] Returning a result with {len(formatted_emails)} emails"
        )
        return result

    except Exception as e:
        logger.error(f"Error fetching the email history: {e}")
        raise HTTPException(status_code=500, detail="Internal server error") from e


@email_router.get(
    "/execution-status/{message_id}",
    summary="Get Execution Status",
    description="""
## Get Queue Processor Execution Status

Check the processing status of a message in the email queue processor pipeline.

### Execution Status Values

| Status | Description |
|--------|-------------|
| `queued` | Message is in the Redis queue awaiting processing |
| `running` | Message was dequeued and is being processed, or temporarily deferred |
| `succeeded` | Message was processed successfully (sent or delivered) |
| `failed` | Message processing failed (bounced, rejected, or failed) |
| `not_found` | Message not found in queue or delivery events |

### Verification Logic

1. Checks if the message is still in the Redis queue
2. Checks for records in the delivery events table
3. Returns the most recent status found

### Response Fields

- `message_id`: Unique message identifier
- `execution_status`: Current execution status
- `in_queue`: Whether the message is still in the queue
- `in_delivery_events`: Whether delivery events exist
- `delivery_status`: The delivery status from events table
- `last_event_timestamp`: Timestamp of the last event

### Authentication

Requires a valid JWT token or API key in the `Authorization` header.
    """,
    responses={
        200: {
            "description": "Execution status retrieved successfully",
            "content": {
                "application/json": {
                    "example": {
                        "message_id": "msg_1234567890abcdef",
                        "execution_status": "succeeded",
                        "in_queue": False,
                        "in_delivery_events": True,
                        "delivery_status": "sent",
                        "last_event_timestamp": "2025-01-21T16:30:00Z",
                    }
                }
            },
        },
        401: {
            "description": "Invalid or expired token",
            "content": {
                "application/json": {
                    "example": {"detail": "Invalid or expired token"}
                }
            },
        },
        500: {
            "description": "Internal server error",
            "content": {
                "application/json": {"example": {"detail": "Internal server error"}}
            },
        },
    },
    tags=["emails"],
    operation_id="emails_execution_status",
)
async def get_email_execution_status(
    message_id: str = Path(
        ..., description="Unique message ID", examples=["msg_1234567890abcdef"]
    ),
    current_user: dict = Depends(require_read_emails_hybrid),
):
    """
    Checks whether a message was processed correctly by the email queue processor

    Args:
        message_id: Message ID
        current_user: Authenticated user

    Returns:
        dict: Execution status of the message
    """
    try:
        from mail_api.common.redis_client import redis_client

        # domain_name and domain_id from the user's JWT/API key; the tenant filter on
        # delivery_events is in _tenant_delivery_filter.
        domain_name = await get_user_domain_name_async(current_user)
        domain_id = get_user_domain_id(current_user)

        logger.info(
            f"[EMAIL_EXECUTION_STATUS] Checking the execution status of message_id: {message_id}, domain_name: {domain_name}, domain_id: {domain_id}"
        )

        # 1. Is the message still pending (tracking SET)?
        in_queue = False

        try:
            # EXISTS O(1) on the pending SET instead of SCAN O(N)
            pending_key = f"e:pending:{message_id}"
            in_queue = await redis_client.exists(pending_key)

            if in_queue:
                logger.info(
                    f"[EMAIL_EXECUTION_STATUS] Message found as pending ({pending_key})"
                )
            else:
                logger.info(
                    "[EMAIL_EXECUTION_STATUS] Message not found as pending"
                )
        except Exception as redis_error:
            logger.warning(
                f"[EMAIL_EXECUTION_STATUS] Error checking the Redis queue: {redis_error}"
            )

        # 2. Is there a row in the delivery_events table?
        delivery_info = None
        in_delivery_events = False

        try:
            tenant_sql, tenant_params = _tenant_delivery_filter(domain_id, domain_name)
            delivery_info = fetch_one(
                f"""
                SELECT
                    message_id,
                    rcpt,
                    status,
                    attempt,
                    ts
                FROM delivery_events
                WHERE message_id = %s
                AND {tenant_sql}
                ORDER BY ts DESC
                LIMIT 1
                """,
                (message_id, *tenant_params),
            )

            if delivery_info:
                in_delivery_events = True
                logger.info(
                    f"[EMAIL_EXECUTION_STATUS] Message found in the delivery_events table with status: {delivery_info['status']}"
                )
            else:
                logger.info(
                    "[EMAIL_EXECUTION_STATUS] Message not found in the delivery_events table"
                )
        except Exception as db_error:
            logger.warning(
                f"[EMAIL_EXECUTION_STATUS] Error checking the delivery_events table: {db_error}"
            )

        # 3. Determine the execution status
        execution_status = "not_found"
        delivery_status = None
        last_event_timestamp = None

        if in_queue:
            # The message is still in the queue
            execution_status = "queued"
        elif in_delivery_events and delivery_info:
            # The message was processed: check its status
            delivery_status = delivery_info["status"]
            last_event_timestamp = (
                delivery_info["ts"].isoformat() if delivery_info["ts"] else None
            )

            # Map the delivery_events status to an execution status
            # (the statuses written by the milter and the events updater):
            # - sent, delivered = execution succeeded
            # - deferred = still processing (running)
            # - failed, rejected, bounced, soft_bounced, hard_bounced = failure
            if delivery_status in ["sent", "delivered"]:
                execution_status = "succeeded"
            elif delivery_status == "deferred":
                # Temporary failure, may still be processed
                execution_status = "running"
            elif delivery_status in ["failed", "rejected", "bounced", "soft_bounced", "hard_bounced"]:
                execution_status = "failed"
            else:
                # Unknown status: treat it as a success unless it is clearly a failure
                logger.warning(
                    f"[EMAIL_EXECUTION_STATUS] Unknown status: {delivery_status}, treating it as succeeded"
                )
                execution_status = "succeeded"
        elif not in_queue and not in_delivery_events:
            # The message left the queue but there is no completion record:
            # it may be in progress or it was lost
            execution_status = "running"

        result = {
            "message_id": message_id,
            "execution_status": execution_status,
            "in_queue": in_queue,
            "in_delivery_events": in_delivery_events,
            "delivery_status": delivery_status,
            "last_event_timestamp": last_event_timestamp,
        }

        logger.info(
            f"[EMAIL_EXECUTION_STATUS] Returning result: execution_status={execution_status}, in_queue={in_queue}, in_delivery_events={in_delivery_events}"
        )
        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"[EMAIL_EXECUTION_STATUS] Error checking the execution status: {e}",
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail="Internal server error") from e
