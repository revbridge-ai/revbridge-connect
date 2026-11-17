"""
Tracking router
Endpoints that track email opens, clicks and unsubscribes
"""

import asyncio
import os
from html import escape as _html_escape

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response, StreamingResponse

from mail_tracking.common.config import (
    OPEN_MIN_DELTA_SECONDS,
)
from mail_tracking.common.db import exec_sql, fetch_one
from mail_tracking.common.kafka_client import kafka_client
from mail_tracking.services.bot_detection import detect_bot

import io
import json
import logging
import re
from datetime import datetime

logger = logging.getLogger(__name__)

# Router of the tracking endpoints
tracking_router = APIRouter(prefix="", tags=["tracking"])

# SQL that inserts engagement events
# Note: the token is not stored; it was already validated and its data is available
INSERT_ENGAGEMENT = """
INSERT INTO engagement_events (message_id, rcpt, type, url, token, ip_address, user_agent, referer, open_source, open_status, open_confidence_score, domain_id, is_bot, bot_reason, ts)
VALUES (%s, %s, %s, %s, '', %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
"""


def get_domain_name_from_message_id(
    message_id: str, fallback_domain_name: str | None = None
) -> str:
    """
    Look up domain_name in the deliveries or delivery_events table by message_id.
    Uses the token's domain_name (v3+) when the tables do not have the row yet
    (a click that arrives before the events_updater); last, the DOMAIN_NAME env var.

    Args:
        message_id: Message ID
        fallback_domain_name: domain_name from the tracking token (v3+)

    Returns:
        str: The domain_name found, or the fallback
    """
    try:
        # deliveries first
        delivery_info = fetch_one(
            "SELECT domain_name FROM deliveries WHERE message_id = %s LIMIT 1",
            (message_id,),
        )
        if delivery_info and delivery_info.get("domain_name"):
            domain_name = delivery_info["domain_name"]
            logger.debug(
                f"[TRACKING] domain_name taken from the deliveries table: {domain_name} for message_id={message_id}"
            )
            return domain_name

        # Not in deliveries: try delivery_events
        delivery_event_info = fetch_one(
            "SELECT domain_name FROM delivery_events WHERE message_id = %s LIMIT 1",
            (message_id,),
        )
        if delivery_event_info and delivery_event_info.get("domain_name"):
            domain_name = delivery_event_info["domain_name"]
            logger.debug(
                f"[TRACKING] domain_name taken from the delivery_events table: {domain_name} for message_id={message_id}"
            )
            return domain_name

        # The token's own domain_name (v3+), before the env fallback
        if fallback_domain_name:
            logger.info(
                f"log_type=control event=domain_name_from_token_fallback "
                f"message_id={message_id} domain_name={fallback_domain_name}"
            )
            return fallback_domain_name

        # Fallback: the environment variable, when the database has nothing
        domain_name = os.getenv("DOMAIN_NAME", "default")
        logger.warning(
            f"[TRACKING] domain_name not found in the deliveries or delivery_events tables for message_id={message_id}, using fallback: {domain_name}"
        )
        return domain_name
    except Exception as e:
        logger.error(
            f"[TRACKING] Error reading domain_name from the deliveries/delivery_events tables: {e}, using fallback"
        )
        return os.getenv("DOMAIN_NAME", "default")


def get_domain_id_from_message_id(
    message_id: str, fallback_domain_name: str | None = None
) -> int:
    """
    Look up domain_id in the deliveries or delivery_events table by message_id.
    Raises when it is not found (domain_id is required).

    Args:
        message_id: Message ID
        fallback_domain_name: domain_name from the tracking token (v3+).
            Used when the deliveries row does not exist yet: scanner clicks
            arrive seconds after delivery, before the events_updater has
            processed the Postfix log (a window of ~15-30s).

    Returns:
        int: The domain_id found

    Raises:
        ValueError: If no domain_id is found
    """
    try:
        # deliveries first (domain_id directly)
        delivery_info = fetch_one(
            "SELECT domain_id FROM deliveries WHERE message_id = %s LIMIT 1",
            (message_id,),
        )
        if delivery_info and delivery_info.get("domain_id"):
            domain_id = delivery_info["domain_id"]
            logger.debug(
                f"[TRACKING] domain_id taken from the deliveries table: {domain_id} for message_id={message_id}"
            )
            return domain_id

        # Not in deliveries: try delivery_events (domain_id directly)
        delivery_event_info = fetch_one(
            "SELECT domain_id FROM delivery_events WHERE message_id = %s LIMIT 1",
            (message_id,),
        )
        if delivery_event_info and delivery_event_info.get("domain_id"):
            domain_id = delivery_event_info["domain_id"]
            logger.debug(
                f"[TRACKING] domain_id taken from the delivery_events table: {domain_id} for message_id={message_id}"
            )
            return domain_id

        from mail_tracking.shared.domain_validation import get_domain_id_from_name

        # Not in the tables: use the token's own domain_name (v3+), which covers
        # a click that arrives before the events_updater, without relying on timing.
        if fallback_domain_name:
            domain_id = get_domain_id_from_name(fallback_domain_name)
            if domain_id:
                logger.info(
                    f"log_type=control event=domain_id_from_token_fallback "
                    f"message_id={message_id} domain_name={fallback_domain_name} domain_id={domain_id}"
                )
                return domain_id

        # Try the domain of the message_id (e.g. <rbm-xxx@mail.example.com>)
        if "@" in message_id:
            extracted_domain = message_id.split("@")[-1].rstrip(">").strip()
            if extracted_domain:
                domain_id = get_domain_id_from_name(extracted_domain)
                if domain_id:
                    logger.warning(
                        f"[TRACKING] domain_id not found in deliveries/delivery_events for message_id={message_id}, "
                        f"taken from the message_id: domain={extracted_domain}, domain_id={domain_id}"
                    )
                    return domain_id

        # Fallback: the DOMAIN_NAME environment variable
        fallback_domain_name = os.getenv("DOMAIN_NAME")
        if fallback_domain_name:
            domain_id = get_domain_id_from_name(fallback_domain_name)
            if domain_id:
                logger.warning(
                    f"[TRACKING] domain_id not found for message_id={message_id}, using the env fallback: {domain_id}"
                )
                return domain_id

        raise ValueError(
            f"domain_id not found for message_id={message_id} and no fallback is configured"
        )
    except ValueError:
        raise
    except Exception as e:
        logger.error(f"[TRACKING] Error looking up domain_id: {e}")
        raise ValueError(
            f"Error looking up domain_id for message_id={message_id}: {e!s}"
        ) from e


# Open classification helpers
def classify_open_source(user_agent: str | None, ip_address: str | None) -> str:
    """Classify the source of an open (simplified).

    Returns values compatible with the table's ENUM:
    'gmail_proxy', 'apple_mpp', 'other_proxy', 'direct', 'unknown'
    """
    try:
        ua = (user_agent or "").lower()

        # Known proxies
        if "googleimageproxy" in ua:
            return "gmail_proxy"
        # Apple Mail's bare WebKit agent (what the Mail Privacy Protection proxy sends). A
        # browser on a Mac or an iPhone also says AppleWebKit, but adds its own token
        # (Safari/, Chrome/, CriOS/, Firefox/, FxiOS/, Edg/): that is a person opening the
        # pixel, not the proxy. Apple's proxy is also caught by IP (bot_detection).
        if "applewebkit" in ua and (
            "iphone" in ua or "ipad" in ua or "macintosh" in ua
        ) and not any(
            token in ua for token in ("safari/", "chrome/", "crios/", "firefox/", "fxios/", "edg/")
        ):
            return "apple_mpp"
        if any(
            proxy in ua
            for proxy in [
                "bot",
                "crawler",
                "spider",
                "scraper",
                "proxy",
                "preview",
                "fetch",
            ]
        ):
            return "other_proxy"

        # Email client or mobile app: direct
        if any(
            client in ua
            for client in ["thunderbird", "mail", "mobile", "android", "iphone", "ipad"]
        ):
            return "direct"

        # Unidentified pattern: assume direct (human)
        return "direct"
    except Exception:
        return "unknown"


def classify_open_status(open_source: str) -> str:
    """Map open_source to a status compatible with the table's ENUM.

    Compatible statuses: 'proxy_only', 'likely_human', 'confirmed_human'
    """
    if open_source in ["gmail_proxy", "apple_mpp", "other_proxy"]:
        return "proxy_only"
    return "likely_human"


def get_client_info(request: Request) -> dict:
    """Extract the client information used for tracking"""
    ip_address = get_real_client_ip(request)
    return {
        "ip_address": ip_address,
        "user_agent": request.headers.get("user-agent"),
        "referer": get_referer(request),
        "location": get_location_info(ip_address, request),
    }


def get_location_info(ip_address: str, request: Request) -> dict:
    """
    Get location information from the IP and headers.

    Args:
        ip_address: Client IP
        request: FastAPI Request object

    Returns:
        dict: Location information
    """
    location_info = {
        "country": None,
        "region": None,
        "city": None,
        "timezone": None,
        "source": "unknown",
    }

    # Location from proxy/CDN headers
    location_headers = {
        "cf-ipcountry": "country",  # Cloudflare
        "x-country-code": "country",  # Custom
        "x-region": "region",  # Custom
        "x-city": "city",  # Custom
        "x-timezone": "timezone",  # Custom
    }

    for header, field in location_headers.items():
        value = request.headers.get(header)
        if value and value.strip():
            location_info[field] = value.strip()
            location_info["source"] = "header"
            logger.info(
                f"[LOCATION_DETECTION] {field} found through header {header}: {value}"
            )

    return location_info


def get_referer(request: Request) -> str:
    """
    Get the request's referer, checking several headers.

    Headers checked (in priority order):
    1. Referer (standard)
    2. Referrer (alternative)
    3. X-Forwarded-Referer (proxy)
    4. X-Original-Referer (proxy)
    5. X-Referer (custom)

    Args:
        request: FastAPI Request object

    Returns:
        str: Referer URL, or None if not found
    """
    # Referer headers (in priority order)
    referer_headers = [
        "referer",  # HTTP standard
        "referrer",  # alternative spelling
        "x-forwarded-referer",  # Proxy
        "x-original-referer",  # Proxy
        "x-referer",  # Custom
    ]

    for header in referer_headers:
        referer = request.headers.get(header)
        if referer and referer.strip():
            # Must be a valid URL
            if is_valid_url(referer.strip()):
                logger.info(
                    f"[REFERER_DETECTION] Referer found through header {header}: {referer}"
                )
                return referer.strip()

    # The User-Agent may tell where the request comes from
    user_agent = request.headers.get("user-agent", "")
    if user_agent and any(
        keyword in user_agent.lower()
        for keyword in ["gmail", "outlook", "yahoo", "apple mail"]
    ):
        # An email client user agent: the referer may be empty
        logger.debug("[REFERER_DETECTION] Empty referer - email client detected")
        return None

    logger.debug("[REFERER_DETECTION] No valid referer found")
    return None


def is_valid_url(url: str) -> bool:
    """
    Check whether a string is a valid URL.

    Args:
        url: String with the URL

    Returns:
        bool: True for a valid URL, False otherwise
    """
    if not url or not isinstance(url, str):
        return False

    url = url.strip()

    # Basic checks
    if len(url) < 4:  # minimum for "http"
        return False

    # Must start with a valid scheme
    valid_protocols = ["http://", "https://", "ftp://", "mailto:"]
    if not any(url.lower().startswith(protocol) for protocol in valid_protocols):
        return False

    # Must parse as a URL
    try:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        return bool(parsed.netloc)  # must have a domain
    except:
        return False


def get_real_client_ip(request: Request) -> str:
    """
    Get the client's real IP, honoring the common proxy headers.

    Order:
    1. X-Forwarded-For (most common)
    2. X-Real-IP
    3. CF-Connecting-IP (Cloudflare)
    4. X-Client-IP
    5. X-Forwarded
    6. Forwarded-For
    7. Forwarded
    8. request.client.host (fallback)

    Args:
        request: FastAPI Request object

    Returns:
        str: Client IP, or 'unknown' if not found
    """
    # Common proxy headers (in priority order)
    proxy_headers = [
        "X-Forwarded-For",
        "X-Real-IP",
        "CF-Connecting-IP",  # Cloudflare
        "X-Client-IP",
        "X-Forwarded",
        "Forwarded-For",
        "Forwarded",
    ]

    for header in proxy_headers:
        ip = request.headers.get(header)
        if ip:
            # X-Forwarded-For may hold several comma-separated IPs;
            # the first one is usually the client's original IP
            if "," in ip:
                ip = ip.split(",")[0].strip()

            # Basic IP validation
            if is_valid_ip(ip):
                logger.debug(f"[IP_DETECTION] IP found through header {header}: {ip}")
                return ip

    # Fallback: the connection's own IP
    if request.client and request.client.host:
        logger.info(
            f"[IP_DETECTION] Using the connection IP: {request.client.host}"
        )
        return request.client.host

    logger.warning("[IP_DETECTION] No valid IP found")
    return "unknown"


def is_valid_ip(ip: str) -> bool:
    """
    Check whether a string is a valid IP (IPv4 or IPv6).

    Args:
        ip: String with the IP

    Returns:
        bool: True for a valid IP, False otherwise
    """
    if not ip or not isinstance(ip, str):
        return False

    # Strip whitespace
    ip = ip.strip()

    # Not localhost nor the usual Docker private IPs
    if ip in ["127.0.0.1", "::1", "localhost", "172.18.0.4", "172.17.0.1"]:
        return False

    # Only valid IP characters
    # IPv4 regex
    ipv4_pattern = r"^(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)$"
    # IPv6 regex (simplified)
    ipv6_pattern = r"^(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}$"

    return bool(re.match(ipv4_pattern, ip) or re.match(ipv6_pattern, ip))


async def trigger_kafka_event(
    event_type: str,
    message_id: str,
    rcpt: str,
    domain_name: str,
    additional_data: dict = None,
    campaign_id: str = None,
):
    """
    Write the event to the outbox, asynchronously

    Args:
        event_type: Event type ('open', 'click', 'unsubscribe')
        message_id: Message ID
        rcpt: Recipient address
        additional_data: Additional event data
        domain_name: Domain name
        campaign_id: Campaign ID (the sender's X-Connect-Campaign header, optional)
    """
    try:
        extra_data = additional_data.copy() if additional_data else {}

        # Outbox call for the event type
        kafka_fn = None
        kafka_kwargs = {}

        if event_type == "open":
            kafka_fn = kafka_client.queue_email_opened
            kafka_kwargs = dict(
                message_id=message_id,
                recipient_email=rcpt,
                domain_name=domain_name,
                user_agent=extra_data.get("user_agent"),
                ip_address=extra_data.get("ip_address"),
                referer=extra_data.get("referer"),
                location=extra_data.get("location"),
                open_source=extra_data.get("open_source"),
                open_status=extra_data.get("open_status"),
                open_confidence_score=extra_data.get("open_confidence_score"),
                campaign_id=campaign_id,
                **{k: v for k, v in extra_data.items() if k not in [
                    "user_agent", "ip_address", "referer", "location",
                    "open_source", "open_status", "open_confidence_score"
                ]},
            )
        elif event_type == "click":
            kafka_fn = kafka_client.queue_email_clicked
            kafka_kwargs = dict(
                message_id=message_id,
                recipient_email=rcpt,
                domain_name=domain_name,
                clicked_url=extra_data.get("clicked_url"),
                user_agent=extra_data.get("user_agent"),
                ip_address=extra_data.get("ip_address"),
                referer=extra_data.get("referer"),
                location=extra_data.get("location"),
                campaign_id=campaign_id,
                **{k: v for k, v in extra_data.items() if k not in [
                    "clicked_url", "user_agent", "ip_address", "referer", "location"
                ]},
            )
        elif event_type == "unsubscribe":
            kafka_fn = kafka_client.queue_email_unsubscribed
            kafka_kwargs = dict(
                message_id=message_id,
                recipient_email=rcpt,
                domain_name=domain_name,
                user_agent=extra_data.get("user_agent"),
                ip_address=extra_data.get("ip_address"),
                referer=extra_data.get("referer"),
                location=extra_data.get("location"),
                suppression_reason=extra_data.get("suppression_reason"),
                campaign_id=campaign_id,
                **{k: v for k, v in extra_data.items() if k not in [
                    "user_agent", "ip_address", "referer", "location", "suppression_reason"
                ]},
            )

        if kafka_fn is None:
            logger.warning(f"[KAFKA] Event type '{event_type}' not mapped")
            return

        # Retry with backoff
        import time
        outbox_ok = False
        for attempt in range(1, 4):
            try:
                kafka_success = kafka_fn(**kafka_kwargs)
                if kafka_success:
                    # Outbox enqueue success: the kafka_client itself emits
                    # the audit line. This is the plumbing confirmation.
                    logger.debug(
                        f"[KAFKA] Event {event_type} written to the outbox "
                        f"for message_id={message_id}"
                    )
                    outbox_ok = True
                    break
                else:
                    logger.warning(
                        f"[KAFKA] Outbox returned False (attempt {attempt}/3) "
                        f"event={event_type} message_id={message_id}"
                    )
            except Exception as retry_err:
                logger.error(
                    f"[KAFKA] Error writing to the outbox (attempt {attempt}/3): {retry_err}"
                )
            if attempt < 3:
                time.sleep(attempt)  # backoff: 1s, 2s
        if not outbox_ok:
            logger.error(
                f"[KAFKA] CRITICAL FAILURE: {event_type} NOT written to the outbox after 3 attempts! "
                f"message_id={message_id} rcpt={rcpt}"
            )

    except Exception as e:
        logger.error(f"[KAFKA] Error writing the outbox event {event_type}: {e}")
        # The outbox never fails the main tracking flow


async def validate_token(token: str, token_type: str) -> dict | None:
    """
    Validate a tracking token by decrypting it.

    The data is encrypted inside the token itself, so no Redis lookup is
    needed. The token carries: mid, rcpt, type, url (for clicks), ts.

    Args:
        token: Encrypted token to validate
        token_type: Token type ('open', 'click' or 'unsubscribe')

    Returns:
        dict: Token data if valid, None if invalid
    """
    try:
        from mail_tracking.common.token_decoder import decode_tracking_token

        # Decrypt the token
        token_data = decode_tracking_token(token)
        if not token_data:
            logger.warning("[TOKEN_VALIDATION] The token could not be decrypted")
            return None

        # Check the type
        if token_data.get("type") != token_type:
            logger.warning(
                f"[TOKEN_VALIDATION] Wrong type: expected {token_type}, "
                f"got {token_data.get('type')}"
            )
            return None

        # Not expired? (age check)
        if "ts" in token_data:
            token_age = datetime.now().timestamp() - token_data["ts"]
            if token_age > 30 * 24 * 3600:  # 30 days
                # info, not warning: expired tokens are expected traffic from
                # email scanners (SafeLinks, Mimecast, Proofpoint, Google Image
                # Proxy) and synthetic monitors hitting tracking pixels long
                # after delivery. The pixel still returns 200 but no event is
                # registered, which is the correct behaviour.
                logger.info(f"[TOKEN_VALIDATION] Token expired: age={token_age}s")
                return None

        return token_data

    except Exception as e:
        logger.error(f"[TOKEN_VALIDATION] Error validating the token: {e}", exc_info=True)
        return None


# Set of pending tasks: holds a strong reference while they run.
# Without it, asyncio could collect them before they finish (the known CPython
# warning "Task was destroyed but it is pending").
_pending_open_tasks: set[asyncio.Task] = set()


def _schedule_open_processing(token: str, client_info: dict) -> None:
    """Schedule _process_open_event as a fire-and-forget task (outside the request scope)."""
    task = asyncio.create_task(_process_open_event(token, client_info))
    _pending_open_tasks.add(task)
    task.add_done_callback(_pending_open_tasks.discard)


async def _process_open_event(token: str, client_info: dict) -> None:
    """
    Process an open event in the background.

    Runs after the pixel has already been returned to the client (best-effort).
    Never raises: errors are logged and do not affect the response.
    """
    try:
        token_data = await validate_token(token, "open")
        if not token_data:
            logger.info("[TRACKING_OPEN] Invalid token - ignored in the background")
            return

        delta_result = fetch_one(
            """
            SELECT TIMESTAMPDIFF(SECOND, MIN(ts), NOW()) AS delta_seconds,
                   MIN(ts) AS first_delivered
            FROM delivery_events
            WHERE message_id = %s AND rcpt = %s AND status = 'delivered'
            """,
            (token_data["mid"], token_data["rcpt"]),
        )

        delta_seconds = delta_result.get("delta_seconds") if delta_result else None
        first_delivered = delta_result.get("first_delivered") if delta_result else None
        logger.debug(
            f"[TRACKING_OPEN] Time check: first_delivered={first_delivered}, delta_seconds={delta_seconds}"
        )

        if delta_seconds is None or delta_seconds < OPEN_MIN_DELTA_SECONDS:
            reason = (
                "no delivered"
                if delta_seconds is None
                else f"delta={delta_seconds}s"
                + (
                    " (negative skew)"
                    if delta_seconds < 0
                    else f" < {OPEN_MIN_DELTA_SECONDS}s"
                )
            )
            # Control event: the dedupe / minimum-delay rule fired and this
            # open won't be counted. One log per suppressed open with
            # everything needed to investigate (mid, recipient, ip, ua, reason).
            logger.info(
                f"log_type=control event=open_dedupe_suppressed reason={reason!r} "
                f"message_id={token_data['mid']} recipient={token_data['rcpt']} "
                f"ip={client_info['ip_address']} ua={client_info['user_agent']!r}"
            )
            return

        open_source = classify_open_source(
            client_info.get("user_agent"), client_info.get("ip_address")
        )
        open_status = classify_open_status(open_source)
        logger.debug(
            f"[TRACKING_OPEN] Classification: mid={token_data['mid']}, rcpt={token_data['rcpt']}, "
            f"open_source={open_source}, open_status={open_status}"
        )

        is_bot, bot_reason = detect_bot(
            client_info.get("user_agent"),
            client_info.get("ip_address"),
            event_type="open",
        )

        domain_id = get_domain_id_from_message_id(
            token_data["mid"], token_data.get("domain_name")
        )
        exec_sql(
            INSERT_ENGAGEMENT,
            (
                token_data["mid"],
                token_data["rcpt"],
                "open",
                None,
                client_info["ip_address"],
                client_info["user_agent"],
                client_info["referer"],
                open_source,
                open_status,
                1,
                domain_id,
                is_bot,
                bot_reason,
            ),
        )

        if is_bot:
            # Bot open: the row above is kept for auditing, but it does not count
            # as engagement: no outbox event (it never becomes 'opened' downstream).
            logger.info(
                f"log_type=control event=open_bot_suppressed bot_reason={bot_reason} "
                f"message_id={token_data['mid']} recipient={token_data['rcpt']} "
                f"ip={client_info['ip_address']} ua={client_info['user_agent']!r}"
            )
            return

        domain_name = get_domain_name_from_message_id(
                token_data["mid"], token_data.get("domain_name")
            )
        await trigger_kafka_event(
            event_type="open",
            message_id=token_data["mid"],
            rcpt=token_data["rcpt"],
            domain_name=domain_name,
            additional_data={
                "user_agent": client_info["user_agent"],
                "ip_address": client_info["ip_address"],
                "referer": client_info["referer"],
                "location": client_info["location"],
                "opened_at": datetime.utcnow().isoformat(),
                "open_source": open_source,
                "open_status": open_status,
                "open_confidence_score": 1,
            },
            campaign_id=token_data.get("campaign_id"),
        )
        # A single audit line per open.
        location = client_info.get("location") or {}
        logger.info(
            f"log_type=audit action=email_open_tracked "
            f"message_id={token_data['mid']} recipient={token_data['rcpt']} "
            f"domain_name={domain_name} open_source={open_source} open_status={open_status} "
            f"ip={client_info['ip_address']} country={location.get('country_code', 'unknown')} "
            f"delta_seconds={delta_seconds}"
        )

    except Exception as e:
        logger.error(f"[TRACKING_OPEN] Error in the background: {e}", exc_info=True)


@tracking_router.get(
    "/o/{token}.png",
    summary="Track an email open",
    description="""
## Email open tracking

Public endpoint that tracks email opens with a transparent pixel.

### How it works
1. Returns a transparent 1x1 PNG immediately
2. In the background: validates the token, records the open in the database
   and writes an `opened` event to the event outbox
3. Location: country/region/city from CDN headers (e.g. CF-IPCountry), when present
4. Client information: User-Agent, IP, referer

### Collected data
- Timestamp of the open
- IP address
- User agent
- Referer (if available)
- Location (when possible)

### Security
- Public route: no authentication
- The token is validated (integrity and age)
- Rate limited

### Example: the pixel in the email HTML

Use an image tag with an empty `alt` attribute (for accessibility), visually hidden:

```html
<img src="https://t.example.com/o/{token}.png" alt="" width="1" height="1" style="display:none !important; width:1px; height:1px;" aria-hidden="true" />
```
    """,
    responses={
        200: {
            "description": "Transparent pixel returned. The event processing (token validation, INSERT, outbox) happens in the background; this route always returns 200, so it neither reveals whether a token is valid nor blocks the client.",
            "content": {"image/png": {"example": "Transparent 1x1 PNG pixel"}},
        },
    },
    tags=["tracking"],
    dependencies=[],  # no authentication dependencies
    operation_id="tracking_open",
)
async def track_open_by_token(
    token: str,
    request: Request,
):
    """
    Track an email open from an opaque token.

    The pixel is returned immediately; validation, classification, the MySQL
    INSERT and the outbox write all happen in the background so the response
    is never blocked. Always returns 200 with the pixel: invalid tokens are
    handled silently in the background.

    **Public route** - no authentication
    """
    # Capture the request data before handing off (the request may not be
    # available in the task once the response has been sent)
    client_info = get_client_info(request)
    # asyncio.create_task instead of FastAPI BackgroundTasks: BackgroundTasks run
    # inside the request's ASGI scope, so the OTel request span would only close
    # when they finish and the measured latency would include the background work.
    # A fire-and-forget task decouples it from the request: ASGI returns at once
    # and the latency metrics reflect what the client sees.
    _schedule_open_processing(token, client_info)

    pixel = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\xdac\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    response = StreamingResponse(io.BytesIO(pixel), media_type="image/png")
    response.headers["Cache-Control"] = "no-store, no-cache, max-age=0, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    response.headers["Content-Description"] = ""
    response.headers["Content-Disposition"] = 'inline; filename="pixel.png"'
    response.headers["Content-Length"] = str(len(pixel))
    return response


@tracking_router.get(
    "/c/{token}",
    summary="Track a link click",
    description="""
## Link click tracking

Public endpoint that tracks clicks on email links and redirects to the original URL.

### How it works
1. Validates the token (integrity and age)
2. Records the click in the database
3. Writes a `clicked` event to the event outbox
4. Redirects to the original URL
5. Collects the client information

### Collected data
- Clicked URL (the original link)
- Timestamp of the click
- IP address
- User agent
- Referer (if available)
- Location (when possible)

### Security
- Public route: no authentication
- The token is validated (integrity and age)
- The URL is validated before the redirect
- Rate limited

### Behavior
- Status 302: redirect to the original URL
- Status 400: URL not found in the token
- Status 404: invalid or expired token
    """,
    responses={
        302: {
            "description": "Redirect to the original URL",
            "headers": {
                "Location": {
                    "description": "Original URL of the clicked link",
                    "schema": {
                        "type": "string",
                        "example": "https://example.com/landing-page",
                    },
                }
            },
        },
        400: {
            "description": "URL not found in the token",
            "content": {
                "application/json": {
                    "example": {"detail": "URL not found in the token"}
                }
            },
        },
        404: {
            "description": "Invalid or expired token",
            "content": {
                "application/json": {
                    "example": {"detail": "Invalid or expired token"}
                }
            },
        },
    },
    tags=["tracking"],
    dependencies=[],  # no authentication dependencies
    operation_id="tracking_click",
)
async def track_click_by_token(token: str, request: Request):
    """
    Track a link click from an opaque token

    **Public route** - no authentication

    Args:
        token: Click tracking token
        request: FastAPI request object
    """
    # Validate the token
    token_data = await validate_token(token, "click")
    if not token_data:
        raise HTTPException(status_code=404, detail="Invalid or expired token")

    # Client information
    client_info = get_client_info(request)

    # Bot click (SafeLinks/Mimecast/Proofpoint scanners, prefetch, headless,
    # cloud VPS): kept for auditing, but no outbox event and no open is
    # promoted or created. It is still redirected, so the scanner does not
    # break and the tracking is not exposed.
    is_bot, bot_reason = detect_bot(
        client_info.get("user_agent"), client_info.get("ip_address")
    )
    if is_bot:
        try:
            domain_id = get_domain_id_from_message_id(
                token_data["mid"], token_data.get("domain_name")
            )
            exec_sql(
                INSERT_ENGAGEMENT,
                (
                    token_data["mid"],
                    token_data["rcpt"],
                    "click",
                    token_data.get("url"),
                    client_info["ip_address"],
                    client_info["user_agent"],
                    client_info["referer"],
                    None,
                    None,
                    None,
                    domain_id,
                    True,
                    bot_reason,
                ),
            )
            logger.info(
                f"log_type=control event=click_bot_suppressed bot_reason={bot_reason} "
                f"message_id={token_data['mid']} recipient={token_data['rcpt']} "
                f"ip={client_info['ip_address']} ua={client_info['user_agent']!r}"
            )
        except Exception as e:
            logger.error(f"[TRACKING_CLICK] Error recording the bot click: {e}")
        if token_data.get("url"):
            return RedirectResponse(url=token_data["url"], status_code=302)
        raise HTTPException(status_code=400, detail="URL not found in the token")

    # Record the click event
    try:
        # domain_id
        domain_id = get_domain_id_from_message_id(
            token_data["mid"], token_data.get("domain_name")
        )

        exec_sql(
            INSERT_ENGAGEMENT,
            (
                token_data["mid"],
                token_data["rcpt"],
                "click",
                token_data.get("url"),
                client_info["ip_address"],
                client_info["user_agent"],
                client_info["referer"],
                None,
                None,
                None,
                domain_id,
                False,
                None,
            ),
        )

        logger.info(
            f"[TRACKING_CLICK] Event recorded: message_id={token_data['mid']}, rcpt={token_data['rcpt']}, url={token_data.get('url')}"
        )

        # domain_name from the deliveries table, by message_id
        domain_name = get_domain_name_from_message_id(
            token_data["mid"], token_data.get("domain_name")
        )

        # Write the click outbox event
        await trigger_kafka_event(
            event_type="click",
            message_id=token_data["mid"],
            rcpt=token_data["rcpt"],
            domain_name=domain_name,
            additional_data={
                "user_agent": client_info["user_agent"],
                "ip_address": client_info["ip_address"],
                "referer": client_info["referer"],
                "location": client_info["location"],
                "clicked_url": token_data.get("url"),
                "clicked_at": datetime.utcnow().isoformat(),
            },
            campaign_id=token_data.get("campaign_id"),
        )

        # Promote open_status to confirmed_human, only on the latest related 'open'
        try:
            last_open = fetch_one(
                """
                SELECT id FROM engagement_events
                WHERE message_id = %s AND rcpt = %s AND type = 'open'
                ORDER BY id DESC
                LIMIT 1
                """,
                (token_data["mid"], token_data["rcpt"]),
            )
            if last_open and last_open.get("id"):
                # Current score of the open, before promoting it
                current_open = fetch_one(
                    """
                    SELECT open_confidence_score, open_status FROM engagement_events
                    WHERE id = %s
                    """,
                    (last_open["id"],),
                )
                current_score = (
                    current_open.get("open_confidence_score", 0) if current_open else 0
                )

                # Only promote when the score is >= 30 (a click is strong evidence of human interaction)
                if current_score >= 30:
                    exec_sql(
                        """
                        UPDATE engagement_events
                        SET open_status = 'confirmed_human',
                            open_confidence_score = CASE
                                WHEN open_confidence_score IS NULL OR open_confidence_score < 90 THEN 90
                                ELSE open_confidence_score
                            END
                        WHERE id = %s
                        AND (open_status IS NULL OR open_status IN ('proxy_only', 'likely_human', 'unknown'))
                        """,
                        (last_open["id"],),
                    )
                    logger.info(
                        f"[TRACKING_CLICK] open_status promoted to confirmed_human with score=90: message_id={token_data['mid']}, rcpt={token_data['rcpt']}, open_id={last_open['id']}, previous_score={current_score}"
                    )
                else:
                    logger.info(
                        f"[TRACKING_CLICK] Click detected but score={current_score} < 30 -> open_status not promoted: message_id={token_data['mid']}, rcpt={token_data['rcpt']}, open_id={last_open['id']}"
                    )
            else:
                # No open event found: create an implicit one from the click
                logger.info(
                    f"[TRACKING_CLICK] No open event to promote: message_id={token_data['mid']}, rcpt={token_data['rcpt']}. Creating an implicit open event."
                )

                # Implicit open event, based on the click
                # (a click is strong evidence that the email was opened)
                implicit_open_source = classify_open_source(
                    client_info.get("user_agent"), client_info.get("ip_address")
                )
                implicit_open_status = (
                    "confirmed_human"  # a click is strong evidence of human interaction
                )
                implicit_score = 90  # high score: a click is strong evidence

                # domain_id
                domain_id = get_domain_id_from_message_id(
                    token_data["mid"], token_data.get("domain_name")
                )

                exec_sql(
                    INSERT_ENGAGEMENT,
                    (
                        token_data["mid"],
                        token_data["rcpt"],
                        "open",
                        None,
                        client_info["ip_address"],
                        client_info["user_agent"],
                        client_info["referer"],
                        implicit_open_source,
                        implicit_open_status,
                        implicit_score,
                        domain_id,
                        False,
                        None,
                    ),
                )

                logger.info(
                    f"[TRACKING_CLICK] Implicit open event created: message_id={token_data['mid']}, rcpt={token_data['rcpt']}, "
                    f"open_source={implicit_open_source}, open_status={implicit_open_status}, score={implicit_score}"
                )

                # Write the implicit open outbox event
                domain_name = get_domain_name_from_message_id(
                    token_data["mid"], token_data.get("domain_name")
                )
                await trigger_kafka_event(
                    event_type="open",
                    message_id=token_data["mid"],
                    rcpt=token_data["rcpt"],
                    domain_name=domain_name,
                    additional_data={
                        "user_agent": client_info["user_agent"],
                        "ip_address": client_info["ip_address"],
                        "referer": client_info["referer"],
                        "location": client_info["location"],
                        "opened_at": datetime.utcnow().isoformat(),
                        "open_source": implicit_open_source,
                        "open_status": implicit_open_status,
                        "open_confidence_score": implicit_score,
                        "implicit_open": True,  # marks an implicit open
                        "triggered_by_click": True,  # created by a click
                    },
                    campaign_id=token_data.get("campaign_id"),
                )
                logger.info(
                    f"[KAFKA] Implicit open outbox event written for message_id={token_data['mid']}"
                )
        except Exception as e:
            logger.warning(f"[TRACKING_CLICK] Failed to promote open_status: {e}")

    except Exception as e:
        logger.error(f"[TRACKING_CLICK] Error recording the event: {e}")
        # Do not fail the tracking: only log the error

    # Redirect to the original URL
    if token_data.get("url"):
        return RedirectResponse(url=token_data["url"], status_code=302)
    else:
        raise HTTPException(status_code=400, detail="URL not found in the token")


# One-click POST (RFC 8058): the mailbox provider (Gmail, Yahoo) POSTs
# "List-Unsubscribe=One-Click" to the URL of the List-Unsubscribe header; same processing as the GET.
@tracking_router.post(
    "/u/{token}",
    summary="One-click unsubscribe (RFC 8058)",
    include_in_schema=True,
)
@tracking_router.get(
    "/u/{token}",
    summary="Track an unsubscribe",
    description="""
## Unsubscribe tracking

Public endpoint that records an email unsubscribe and adds the address to the
suppression list automatically.

### How it works
1. Validates the token (integrity and age)
2. Records the unsubscribe in the database
3. Adds the address to the suppression list
4. Writes an `unsubscribed` event to the event outbox
5. Shows a confirmation page

### Collected data
- Unsubscribed address
- Timestamp of the unsubscribe
- IP address
- User agent
- Referer (if available)
- Location (when possible)

### Suppression list
- Automatic: the address is added automatically
- Permanent: does not expire (until removed manually)
- Type: unsubscribe
- Reason: includes the reason and the message_id

### Security
- Public route: no authentication
- The token is validated (integrity and age)
- Rate limited

### Page
- A responsive HTML page confirms the unsubscribe to the recipient
    """,
    responses={
        200: {
            "description": "Unsubscribe confirmation page",
            "content": {
                "text/html": {
                    "example": """
                    <!DOCTYPE html>
                    <html lang="en">
                    <head>
                        <meta charset="UTF-8">
                        <title>Unsubscribe confirmed</title>
                    </head>
                    <body>
                        <div class="container">
                            <h1>Unsubscribe confirmed</h1>
                            <p>You have been removed from our mailing list.</p>
                        </div>
                    </body>
                    </html>
                    """
                }
            },
        },
        404: {
            "description": "Invalid or expired token",
            "content": {
                "application/json": {
                    "example": {"detail": "Invalid or expired token"}
                }
            },
        },
    },
    tags=["tracking"],
    dependencies=[],  # no authentication dependencies
    operation_id="tracking_unsubscribe",
)
async def track_unsubscribe_by_token(token: str, request: Request):
    """
    Track an unsubscribe from an opaque token and add the address to the suppression list

    **Public route** - no authentication

    Args:
        token: Unsubscribe tracking token
        request: FastAPI request object
    """
    logger.info(f"[TRACKING_UNSUBSCRIBE] Request received (token_len={len(token)})")

    # Validate the token
    token_data = await validate_token(token, "unsubscribe")
    if not token_data:
        raise HTTPException(status_code=404, detail="Invalid or expired token")

    # Client information
    client_info = get_client_info(request)
    email = token_data["rcpt"]
    message_id = token_data["mid"]

    # domain_name from the token (the sender's domain)
    # Fallback: from the message_id, for tokens that do not carry it
    domain_name = token_data.get("domain_name")
    if not domain_name and message_id:
        # From the message_id (format: <rbm-XXX-YYY@domain>)
        if "@" in message_id:
            domain_name = message_id.split("@")[-1].rstrip(">")
    if not domain_name:
        domain_name = "unknown"

    # Normalize the address
    email_normalized = email.strip().lower()
    domain_clean = domain_name.strip().lower()

    # Suppression data
    reason = f"User unsubscribed via link - message_id: {message_id}"

    logger.info(
        f"[UNSUBSCRIBE] Processing unsubscribe: {email_normalized} on {domain_clean}"
    )

    # Record the unsubscribe event in engagement_events
    try:
        # domain_id
        domain_id = get_domain_id_from_message_id(
            message_id, token_data.get("domain_name")
        )

        # Unsubscribe: is_bot is for auditing only. The unsubscribe is processed
        # as usual even when it comes from a scanner (losing a real unsubscribe
        # is worse than accepting a false one).
        unsub_is_bot, unsub_bot_reason = detect_bot(
            client_info.get("user_agent"), client_info.get("ip_address")
        )
        exec_sql(
            INSERT_ENGAGEMENT,
            (
                message_id,
                email,
                "unsubscribe",
                None,
                client_info["ip_address"],
                client_info["user_agent"],
                client_info["referer"],
                None,
                None,
                None,
                domain_id,
                unsub_is_bot,
                unsub_bot_reason,
            ),
        )

        logger.info(
            f"[TRACKING_UNSUBSCRIBE] Event recorded: message_id={message_id}, rcpt={email}"
        )

        # Write the unsubscribe outbox event
        await trigger_kafka_event(
            event_type="unsubscribe",
            message_id=message_id,
            rcpt=email,
            domain_name=domain_clean,
            additional_data={
                "user_agent": client_info["user_agent"],
                "ip_address": client_info["ip_address"],
                "referer": client_info["referer"],
                "location": client_info["location"],
                "unsubscribed_at": datetime.utcnow().isoformat(),
                "suppression_reason": reason,
            },
            campaign_id=token_data.get("campaign_id"),
        )

    except Exception as e:
        logger.error(f"[TRACKING_UNSUBSCRIBE] Error recording the event: {e}")
        # Do not fail the tracking: only log the error

    # Add the address to the suppression list
    try:
        # Is there a suppression already?
        existing = fetch_one(
            """
            SELECT id, suppression_type, is_active, expires_at
            FROM suppressions
            WHERE email = %s AND domain_name = %s
        """,
            (email_normalized, domain_clean),
        )

        # Branch on whether the row exists, not on is_active: the SELECT above does
        # not filter on is_active and the unique key is (email, domain_name), so an
        # inactive suppression is updated (the UPDATE sets is_active = TRUE and
        # reactivates it); an INSERT would violate `unique_email_domain`.
        if existing:
            # Update the existing suppression (reactivates it if it was inactive)
            exec_sql(
                """
                UPDATE suppressions
                SET suppression_type = 'unsubscribe',
                    reason = %s,
                    message_id = %s,
                    suppressed_at = CURRENT_TIMESTAMP,
                    is_active = TRUE
                WHERE id = %s
            """,
                (reason, message_id, existing["id"]),
            )

            suppression_id = existing["id"]
            logger.info(
                f"[UNSUBSCRIBE] Suppression updated: {email_normalized} (ID: {suppression_id})"
            )
        else:
            # Create a new suppression
            exec_sql(
                """
                INSERT INTO suppressions (email, domain_name, suppression_type, reason, message_id, expires_at, is_active)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
                (
                    email_normalized,
                    domain_clean,
                    "unsubscribe",
                    reason,
                    message_id,
                    None,  # no expiry: a permanent suppression
                    True,
                ),
            )

            # ID of the new suppression
            suppression_result = fetch_one(
                """
                SELECT id FROM suppressions
                WHERE email = %s AND domain_name = %s AND is_active = TRUE
            """,
                (email_normalized, domain_clean),
            )

            suppression_id = suppression_result["id"] if suppression_result else None
            logger.info(
                f"[UNSUBSCRIBE] New suppression created: {email_normalized} (ID: {suppression_id})"
            )

        # Record the suppression event
        if suppression_id:
            exec_sql(
                """
                INSERT INTO suppression_events (suppression_id, email, domain_name, event_type, new_status, reason, user_id, ip_address)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
                (
                    suppression_id,
                    email_normalized,
                    domain_clean,
                    "created" if not existing else "updated",
                    True,
                    f"Unsubscribe: {reason}",
                    "api",
                    (
                        client_info["ip_address"]
                        if client_info["ip_address"]
                        else "unknown"
                    ),
                ),
            )

    except Exception as e:
        logger.error(f"[UNSUBSCRIBE] Error processing the suppression: {e}")
        # Do not fail the tracking: only log the error

    # Return the unsubscribe confirmation page
    html_content = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Unsubscribe confirmed</title>
        <style>
            body {{
                font-family: Arial, sans-serif;
                max-width: 600px;
                margin: 50px auto;
                padding: 20px;
                background-color: #f5f5f5;
            }}
            .container {{
                background-color: white;
                padding: 30px;
                border-radius: 8px;
                box-shadow: 0 2px 10px rgba(0,0,0,0.1);
                text-align: center;
            }}
            .success-icon {{
                color: #28a745;
                font-size: 48px;
                margin-bottom: 20px;
            }}
            h1 {{
                color: #333;
                margin-bottom: 20px;
            }}
            p {{
                color: #666;
                line-height: 1.6;
                margin-bottom: 15px;
            }}
            .email {{
                font-weight: bold;
                color: #007bff;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="success-icon">✓</div>
            <h1>Unsubscribe confirmed</h1>
            <p>You have been removed from our mailing list.</p>
            <p>Email: <span class="email">{_html_escape(email)}</span></p>
            <p>You will no longer receive emails from us. Thank you!</p>
        </div>
    </body>
    </html>
    """

    return Response(content=html_content, media_type="text/html")
