"""
RevBridge Connect email tracking milter

A Postfix milter (mail filter) that adds email tracking. It intercepts each
message during SMTP processing to:

1. Track deliveries and opens with a tracking pixel
2. Rewrite links to carry tracking tokens
3. Store message metadata in Redis for the delivery events pipeline
4. Add tracking headers to the processed messages

The milter handles both simple HTML emails and multipart MIME messages, with
their various content transfer encodings, and never blocks delivery.

Usage:
    python -m mail_milter.tracking_milter.milter_server

Dependencies:
    - pymilter, for the Postfix integration
    - Redis, for caching and tracking data
    - the shared configuration and Redis client modules
"""

# Configure observability (OTel) - must be before logging
from connect_lib.observability import setup_observability

setup_observability(service_name="mail-milter-service")

# Run with: python -m mail_milter.tracking_milter.milter_server
import base64
import ctypes
import gzip
import hashlib
import html
import json
import logging
import os
import quopri
import re
import sys
import threading
import time
from datetime import UTC, datetime
from email.header import decode_header, make_header
from typing import Any

import Milter
from Milter.utils import parseaddr

from mail_milter.common.config import (
    INJECT_PIXEL,
    MESSAGE_ID_DOMAIN,
    REDIS_TTL_SEC,
    REWRITE_LINKS,
    SHADOW_EMAIL,
    TOKEN_ENCRYPTION_KEY,
    TRACK_BASE,
    setup_logging,
)
from mail_milter.common.db import exec_sql, fetch_all, fetch_one
from mail_milter.common.email_config_cache import (
    get_approved_sender_from_redis,
    get_domain_id,
    get_domain_id_from_redis,
    get_email_config_for_domain_from_redis,
)
from mail_milter.common.kafka_client import kafka_client
from mail_milter.common.redis_client import redis

# JSON logging, configured BEFORE any use
logger = setup_logging("mail-milter-service")

# Warmup divisor (data plane): in eom() it decides whether the send leaves through a
# pool IP in warmup. OFF by default.
# _ENABLED turns the evaluation on. _DRY_RUN (default true) only LOGS what it would
# route; when false, it ADDS the X-Warmup-Route header (the MTA routes it with a
# header_checks FILTER) and increments the per-IP cap counter. Rollout:
# enabled+dry_run (measure) -> enabled without dry_run (route).
_WARMUP_DIVISOR_ENABLED = os.getenv(
    "WARMUP_DIVISOR_ENABLED", "false"
).lower() in ("1", "true", "yes", "on")
_WARMUP_DIVISOR_DRY_RUN = os.getenv(
    "WARMUP_DIVISOR_DRY_RUN", "true"
).lower() in ("1", "true", "yes", "on")

# OTel tracing
from connect_lib.observability import get_tracer

try:
    from opentelemetry import trace
    from opentelemetry.trace import NonRecordingSpan, SpanContext, StatusCode, TraceFlags
    OTEL_AVAILABLE = True
    logger.debug("[OTEL] OpenTelemetry tracing enabled")
except ImportError:
    OTEL_AVAILABLE = False
    logger.warning("[OTEL] OpenTelemetry not available - tracing disabled")

otel_tracer = get_tracer("mail-milter-service")


def _otel_traces_active() -> bool:
    """True only when the OTel tracing SDK is really set up.

    Traces can be turned off with OTEL_TRACES_ENABLED=false (the docker compose
    default). In that case ``get_tracer`` returns a no-op tracer whose spans have
    trace_id/span_id == 0, and the correlation blocks below would log a
    WARNING/CRITICAL for EVERY message. Mirrors the gating of
    ``setup_observability``.
    """
    def _b(key: str, default: str = "true") -> bool:
        return os.getenv(key, default).lower() in ("true", "1", "yes")

    return OTEL_AVAILABLE and _b("OTEL_ENABLED") and _b("OTEL_TRACES_ENABLED")


# Evaluated once at import: avoids logging a correlation failure per message
# when tracing is off by design.
OTEL_TRACES_ACTIVE = _otel_traces_active()
if OTEL_AVAILABLE and not OTEL_TRACES_ACTIVE:
    logger.info(
        "[OTEL] Traces disabled (OTEL_TRACES_ENABLED=false) - "
        "milter -> events-updater correlation inactive by design"
    )

# Token encryption
try:
    from cryptography.fernet import Fernet
    CRYPTO_AVAILABLE = True
except ImportError:
    CRYPTO_AVAILABLE = False
    Fernet = None

# Regular expression that finds URLs inside <a href> tags
A_HREF_URL_RE = re.compile(
    r'<a[^>]*href\s*=\s*["\']([^"\']*)["\'][^>]*>', re.IGNORECASE
)
PLAIN_URL_RE = re.compile(r"(https?://[^\s<]+)", re.IGNORECASE)

_UNTRACKABLE_HREF_PREFIXES = ("mailto:", "tel:", "ftp:", "file:", "javascript:", "#")


def href_is_untrackable(dest_url: str) -> bool:
    """An href that the click wrapper does NOT rewrite: empty/whitespace only (a
    merge tag with no value and no fallback in `href="{{user.x}}"` renders as
    `href=""`, which must not become `https://` with a click token) and special
    schemes (mailto, tel, ..., #)."""
    if not dest_url.strip():
        return True
    return dest_url.startswith(_UNTRACKABLE_HREF_PREFIXES)


def splice_href(match: re.Match, new_url: str) -> str:
    """Replace ONLY the matched href value (group 1) inside the tag (group 0), by
    position. `tag.replace(href, url)` would replace EVERY occurrence of the href
    text in the tag; with an empty href, `"".replace` would insert the URL between
    every character and destroy the tag."""
    tag = match.group(0)
    start = match.start(1) - match.start(0)
    end = match.end(1) - match.start(0)
    return tag[:start] + new_url + tag[end:]

# --- send_ledger: universal ADMIT (durability) -----------------------------
# The milter is the ONLY point that sees EVERY message: injected by the app (queue
# processor) OR sent over direct SMTP (a client connects to :587 and sends). So the
# ledger admission lives here, in eom, once Postfix has accepted the message
# (queue_id assigned). State 'smtp_accepted' + delivery_deadline: if the delivery
# event (status=sent/delivered) does not confirm it by the deadline, the send_ledger
# reaper reconciles it with `deliveries` or closes the row as deadletter.
#
# ON DUPLICATE KEY: for app sends, the queue processor's claim ALREADY created the
# row; here only the queue_id (which only the milter knows) is backfilled, WITHOUT
# overwriting the state. For direct SMTP, the row does not exist and is created here.
SEND_LEDGER_DELIVERY_DEADLINE_SEC = int(
    os.getenv("SEND_LEDGER_DELIVERY_DEADLINE_SEC", "600")
)
SEND_LEDGER_ADMIT_SQL = (
    "INSERT INTO send_ledger "
    "(message_id, rcpt, domain_name, domain_id, queue_id, state, max_attempts, "
    " delivery_deadline) "
    "VALUES (%s, %s, %s, %s, %s, 'smtp_accepted', 3, "
    "        NOW() + INTERVAL %s SECOND) "
    "ON DUPLICATE KEY UPDATE queue_id = VALUES(queue_id), "
    "        delivery_deadline = VALUES(delivery_deadline)"
)


def is_html_complete(html_content: str) -> bool:
    """
    Check whether the HTML ends with </html>, ignoring trailing whitespace

    Args:
        html_content: HTML content to check

    Returns:
        bool: True if it ends with </html> (ignoring whitespace), False otherwise
    """
    if not html_content:
        return False

    # Strip trailing whitespace
    stripped = html_content.rstrip()
    return stripped.endswith("</html>")


def wrap_crlf(data: bytes, width: int = 76) -> bytes:
    """
    Wrap data into CRLF lines for email encoding (RFC 2045).

    Args:
        data: Bytes to wrap
        width: Maximum line width (default: 76)

    Returns:
        bytes: Data wrapped with CRLF
    """
    if not data:
        return b""
    # Join with CRLF and end with a CRLF
    lines = [data[i: i + width] for i in range(0, len(data), width)]
    return b"\r\n".join(lines) + b"\r\n"


def html_to_text(html_content: str) -> str:
    """
    Convert HTML into basic plain text, for the multipart/alternative fallback.

    Removes tags, keeps meaningful line breaks and normalizes whitespace.
    """
    if not html_content:
        return ""

    # Remove scripts and styles
    cleaned = re.sub(
        r"(?is)<(script|style)[^>]*>.*?</\1>",
        "",
        html_content,
    )
    # Replace break tags with newlines
    cleaned = re.sub(r"(?i)<br\s*/?>", "\n", cleaned)
    cleaned = re.sub(r"(?i)</p>", "\n\n", cleaned)
    cleaned = re.sub(r"(?i)</div>", "\n\n", cleaned)
    cleaned = re.sub(r"(?i)</li>", "\n", cleaned)
    cleaned = re.sub(r"(?i)<li[^>]*>", "- ", cleaned)

    # Remove the remaining tags
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)

    # Unescape HTML entities
    cleaned = html.unescape(cleaned)

    # Normalize whitespace and line breaks
    cleaned = cleaned.replace("\r", "\n")
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = re.sub(r"[ \t]*\n[ \t]*", "\n", cleaned)

    return cleaned.strip()


def text_to_basic_html(text_content: str) -> str:
    """
    Convert plain text into basic HTML, keeping line breaks and linkifying URLs.
    """
    if not text_content:
        return "<!DOCTYPE html>\n<html>\n  <body></body>\n</html>"

    normalized = text_content.replace(
        "\r\n", "\n").replace("\r", "\n").strip("\ufeff")
    escaped = html.escape(normalized, quote=False)

    def _linkify(match: re.Match) -> str:
        url = match.group(1)
        return f'<a href="{url}">{url}</a>'

    linkified = PLAIN_URL_RE.sub(_linkify, escaped)

    paragraphs = linkified.split("\n\n")
    html_parts = []
    for paragraph in paragraphs:
        lines = [line.strip() for line in paragraph.split("\n")]
        joined = "<br />\n".join(line for line in lines if line)
        if not joined:
            continue
        html_parts.append(f"<p>{joined}</p>")

    if not html_parts:
        html_parts.append("<p>&nbsp;</p>")

    body = "\n".join(html_parts)
    return f"<!DOCTYPE html>\n<html>\n  <body>\n{body}\n  </body>\n</html>"


def extract_mime_type(headers_text: str) -> str | None:
    """
    Extract the MIME type (e.g. text/plain, multipart/mixed) from the given headers.
    """
    if not headers_text:
        return None

    match = re.search(
        r"content-type\s*:\s*([^\s;]+)", headers_text, re.IGNORECASE)
    if match:
        return match.group(1).lower()

    return None


def decode_mime_value(value: str) -> str:
    """
    Decode MIME-encoded values (RFC 2047).
    """
    if not value:
        return value

    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def encode_email_content(
    content: str, encoding: str = "base64", disable_soft_breaks: bool = False
) -> str:
    """
    Encode with the line breaks and CRLF that DKIM needs (RFC 2045).
    """
    if not content:
        return ""

    encoding = (encoding or "").lower().strip()

    if encoding == "base64":
        raw = content.encode("utf-8")
        b64 = base64.b64encode(raw)  # bytes without line breaks
        wrapped = wrap_crlf(b64, 76)  # 76 columns + CRLF
        return wrapped.decode("ascii")

    elif encoding == "quoted-printable":
        if disable_soft_breaks:
            # Strip trailing whitespace from each line before encoding
            stripped_lines = [line.rstrip() for line in content.splitlines()]
            content = "\n".join(stripped_lines)

        # quopri uses \n; normalize to CRLF
        qp_bytes = quopri.encodestring(content.encode("utf-8"))
        qp_bytes = qp_bytes.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")

        if disable_soft_breaks:
            # Remove soft breaks (= at the end of a line) and rejoin the lines
            qp_bytes = qp_bytes.replace(b"=\r\n", b"")

        # End with a CRLF
        if not qp_bytes.endswith(b"\r\n"):
            qp_bytes += b"\r\n"
        return qp_bytes.decode("ascii")

    elif encoding in ("7bit", "8bit"):
        # Always normalize to CRLF
        txt = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        return "\r\n".join(txt) + "\r\n"

    # Safe fallback: base64 with proper line breaks
    raw = content.encode("utf-8")
    b64 = base64.b64encode(raw)
    wrapped = wrap_crlf(b64, 76)
    return wrapped.decode("ascii")


def decode_email_content(content: str, encoding: str = "base64") -> str:
    """
    Decode email content according to the given encoding

    Args:
        content: Encoded email content
        encoding: Encoding ('base64', 'quoted-printable', '7bit', '8bit')

    Returns:
        str: Decoded content
    """
    try:
        if not content:
            return ""

        # Normalize the encoding
        encoding = encoding.lower().strip()

        if encoding == "base64":
            # Decode base64, removing line breaks first
            content_clean = content.replace("\n", "").replace("\r", "")
            decoded_bytes = base64.b64decode(content_clean)
            return decoded_bytes.decode("utf-8", errors="ignore")

        elif encoding == "quoted-printable":
            # Decode quoted-printable
            decoded_bytes = quopri.decodestring(content.encode("utf-8"))
            return decoded_bytes.decode("utf-8", errors="ignore")

        elif encoding in ["7bit", "8bit"]:
            # 7bit and 8bit are returned as is (already text)
            return content

        else:
            # Unrecognized encoding: try base64
            try:
                content_clean = content.replace("\n", "").replace("\r", "")
                decoded_bytes = base64.b64decode(content_clean)
                return decoded_bytes.decode("utf-8", errors="ignore")
            except:
                return content

    except Exception as e:
        # On error, return the original content
        return content


def extract_content_encoding(headers_text: str) -> str:
    """
    Extract the encoding from the Content-Transfer-Encoding header

    Args:
        headers_text: Header text of the MIME part

    Returns:
        str: Encoding (base64, quoted-printable, 7bit, 8bit)
    """
    try:
        # Find Content-Transfer-Encoding in the headers
        encoding_match = re.search(
            r"content-transfer-encoding:\s*([^\r\n]+)", headers_text, re.IGNORECASE
        )
        if encoding_match:
            encoding = encoding_match.group(1).strip().lower()
            return encoding
        return "7bit"  # default when not specified
    except Exception:
        return "7bit"


def is_attachment(headers_text: str) -> bool:
    """
    Check whether a MIME part is an attachment, from Content-Disposition.

    Args:
        headers_text: Header text of the MIME part

    Returns:
        bool: True for an attachment (Content-Disposition: attachment), False otherwise

    Note:
        Only parts with Content-Disposition: attachment count as attachments.
        Parts with Content-Disposition: inline are not attachments.
    """
    try:
        # Find Content-Disposition in the headers
        disposition_match = re.search(
            r"content-disposition:\s*([^\r\n;]+)", headers_text, re.IGNORECASE
        )
        if disposition_match:
            disposition = disposition_match.group(1).strip().lower()
            # Exactly "attachment" or starting with "attachment"
            # Valid examples: "attachment", "attachment; filename=...", etc.
            # Not: "inline", "form-data", etc.
            return disposition.startswith("attachment")
        return False  # no Content-Disposition: not an attachment
    except Exception:
        return False


def update_content_transfer_encoding(
    headers_text: str, new_encoding: str = "7bit"
) -> str:
    """
    Update the Content-Transfer-Encoding header in the MIME part headers

    Args:
        headers_text: Header text of the MIME part
        new_encoding: New encoding (default: "7bit")

    Returns:
        str: Headers with the new Content-Transfer-Encoding
    """
    try:
        # Replace an existing Content-Transfer-Encoding
        new_headers, count = re.subn(
            r"content-transfer-encoding:\s*[^\r\n]+",
            f"Content-Transfer-Encoding: {new_encoding}",
            headers_text,
            flags=re.IGNORECASE,
        )

        # Header not found: add it at the end of the headers (before \r\n\r\n)
        if count == 0:
            # Find where the headers end
            sep = headers_text.find("\r\n\r\n")
            if sep == -1:
                sep = headers_text.find("\n\n")

            if sep != -1:
                # Add the header before the separator
                new_headers = (
                    headers_text[:sep]
                    + f"\r\nContent-Transfer-Encoding: {new_encoding}"
                    + headers_text[sep:]
                )
            else:
                # No separator: add it at the end
                new_headers = (
                    headers_text +
                    f"\r\nContent-Transfer-Encoding: {new_encoding}"
                )

        return new_headers
    except Exception:
        return headers_text


def fix_malformed_header_params(headers_text: str) -> str:
    """
    Fix malformed headers whose parameters lack the '='.

    Example: 'name"value"' -> 'name="value"'
    Example: '; name"report_20251105.csv"' -> '; name="report_20251105.csv"'

    Args:
        headers_text: Header text of the MIME part

    Returns:
        str: Fixed headers
    """
    try:
        # Fix a missing = : name"value" or ; name"value"
        # Pattern: captures the preceding space/;, the parameter name and the quoted value
        # E.g. "; name"value"" or " name"value"" -> "; name="value""
        fixed = re.sub(
            r'([;\s]+)(name|filename|charset|boundary)(\s*)"([^"]+)"',
            r'\1\2="\4"',
            headers_text,
            flags=re.IGNORECASE,
        )

        # A space before the quotes without "=" (e.g. name "value" -> name="value")
        # is already covered above

        return fixed
    except Exception:
        # On error, return the original
        return headers_text


def add_domain_to_message_id(message_id: str) -> str:
    """
    Add an FQDN domain to an existing Message-ID that has none.

    Args:
        message_id: Message ID (with or without < >)

    Returns:
        str: Message-ID with a domain, as <id@domain>
    """
    # Strip < > if present
    clean_id = message_id.strip().strip("<>").strip()

    # Already has an @: return as is
    if "@" in clean_id:
        return f"<{clean_id}>"

    # Add the domain (MESSAGE_ID_DOMAIN, which defaults to DOMAIN)
    domain = MESSAGE_ID_DOMAIN
    if not domain or "." not in domain:
        domain = "localhost.localdomain"

    return f"<{clean_id}@{domain}>"


def get_api_key_by_id(api_key_id: str):
    """
    Look up an API key in the database by api_key_id.

    Args:
        api_key_id: API key ID (e.g. ak_0123456789abcdef0123456789abcdef)

    Returns:
        dict: API key data (domain_id, is_active, expires_at), or None when not found or on error

    Notes:
        api_keys.domain_id is deprecated in favor of api_key_channels.config.domain_id
        (email channel). Newer API keys are created with api_keys.domain_id=NULL and
        link the domain only through the channel config. Without the fallback below,
        the milter would have no api_key_domain_id for them, would not generate the
        envelope_from and would not write the Redis mapping q:{queue_id}:{rcpt} that
        the events_updater needs to attribute delivery_events to the right tenant.
    """
    try:
        sql = """
            SELECT
                COALESCE(
                    ak.domain_id,
                    CAST(JSON_UNQUOTE(JSON_EXTRACT(akc.config, '$.domain_id')) AS UNSIGNED)
                ) AS domain_id,
                ak.is_active,
                ak.expires_at
            FROM revbridge_connect.api_keys ak
            LEFT JOIN revbridge_connect.api_key_channels akc
                ON akc.api_key_id = ak.id
                AND akc.channel_id = (
                    SELECT id FROM revbridge_connect.channels WHERE name = 'email' LIMIT 1
                )
                AND akc.is_active = 1
            WHERE ak.api_key_id = %s
            LIMIT 1
        """
        result = fetch_one(sql, (api_key_id,))
        # fetch_one already returns None on error; no need to raise
        return result
    except Exception as e:
        logger = logging.getLogger(__name__)
        logger.error(f"Error looking up the API key by ID: {e}", exc_info=True)
        return None


def is_direct_rbm_child(name: str, domain: str) -> bool:
    """`name` is `rbm<something>.<domain>` with exactly ONE label before the domain.

    The MySQL LIKE `%` crosses dots, so checking only the suffix would accept a
    GRANDCHILD (`rbm1.sub.domain`) as a child, and a parent domain would use a
    child's VERP envelope, sending bounces and SPF to the wrong account.
    An explicit, testable rule instead of relying on LIKE.
    """
    name = (name or "").strip().lower()
    domain = (domain or "").strip().lower()
    if not name or not domain:
        return False
    suffix = f".{domain}"
    if not name.endswith(suffix):
        return False
    label = name[: -len(suffix)]
    return bool(label) and "." not in label and label.startswith("rbm")


def get_rbm_domain_for_api_key(api_key_id: str, api_key_domain_id: int | None = None) -> str | None:
    """
    Find the domain that starts with 'rbm' and ends with the API key's domain.

    Args:
        api_key_id: API key ID (e.g. ak_0123456789abcdef0123456789abcdef)
        api_key_domain_id: Domain ID of the API key (optional, avoids looking up the API key again)

    Returns:
        str: rbm domain name (e.g. rbm265489.mail.example.com), or None if not found
    """
    try:
        # No domain_id given: look up the API key in the database
        if not api_key_domain_id:
            logger = logging.getLogger(__name__)
            logger.debug(
                f"[RBM_DOMAIN] api_key_domain_id not given, looking up API key {api_key_id[:20]}... in the database"
            )
            api_key = get_api_key_by_id(api_key_id)
            if not api_key:
                logger.warning(
                    f"[RBM_DOMAIN] API key {api_key_id[:20]}... not found in the database"
                )
                return None
            api_key_domain_id = api_key.get('domain_id')
            if not api_key_domain_id:
                logger.warning(
                    f"[RBM_DOMAIN] API key {api_key_id[:20]}... has no domain_id"
                )
                return None
            logger.debug(
                f"[RBM_DOMAIN] Domain ID taken from the API key: {api_key_domain_id}"
            )
        else:
            logger = logging.getLogger(__name__)
            logger.debug(
                f"[RBM_DOMAIN] Using the given api_key_domain_id: {api_key_domain_id} (no API key lookup)"
            )

        # Name of the API key's domain
        sql = """
            SELECT name
            FROM revbridge_connect.virtual_domains
            WHERE id = %s
        """
        result = fetch_one(sql, (api_key_domain_id,))
        if not result:
            return None
        
        api_key_domain_name = result.get('name', '').lower().strip()
        if not api_key_domain_name:
            return None
        
        # The VERP envelope must be a DIRECT child of the key's domain
        # (`rbm<id>.<domain>`, a single label). The LIKE `%` crosses dots, so without
        # the NOT LIKE a PARENT domain would inherit a child's rbm (the key of
        # `shop.example.com` would get `rbm533320.mail.shop.example.com`) and send
        # bounces + SPF to ANOTHER domain's account.
        # `%.%.<domain>` matches anything with 2+ labels before the domain.
        sql_rbm = """
            SELECT name
            FROM revbridge_connect.virtual_domains
            WHERE name LIKE 'rbm%' AND name LIKE %s AND name NOT LIKE %s
            ORDER BY name ASC
            LIMIT 2
        """
        pattern_end = f'%.{api_key_domain_name}'
        pattern_deep = f'%.%.{api_key_domain_name}'
        rbm_rows = fetch_all(sql_rbm, (pattern_end, pattern_deep)) or []

        candidates = []
        for row in rbm_rows:
            name = (row.get('name') or '').strip()
            if is_direct_rbm_child(name, api_key_domain_name):
                candidates.append(name)

        if not candidates:
            # No direct child: do NOT rewrite (the caller ignores None). Borrowing
            # another domain's rbm would be worse: bounces would go to the wrong owner.
            logger.warning(
                "log_type=ops action=verp_no_direct_rbm domain=%s "
                "(envelope not rewritten; register rbm<id>.%s)",
                api_key_domain_name, api_key_domain_name,
            )
            return None

        if len(candidates) > 1:
            logger.warning(
                "log_type=ops action=verp_rbm_ambiguous domain=%s chosen=%s others=%s",
                api_key_domain_name, candidates[0], candidates[1:],
            )
        return candidates[0]
    except Exception as e:
        logger = logging.getLogger(__name__)
        logger.error(f"Error looking up the rbm domain for API key {api_key_id}: {e}")
        return None


def generate_envelope_from(api_key_id: str, rcpt: str, api_key_domain_id: int | None = None) -> str | None:
    """
    Generate the envelope_from automatically, as:
    bounces+<rcpt with @ replaced by =>@<rbm domain>

    Args:
        api_key_id: API key ID
        rcpt: Recipient address (e.g. user@example.com)
        api_key_domain_id: Domain ID of the API key (optional, avoids looking up the API key again)

    Returns:
        str: Generated envelope from (e.g. bounces+user=example.com@rbm265489.mail.example.com), or None
    """
    try:
        # rbm domain of the API key (pass domain_id when available to avoid another API key lookup)
        rbm_domain = get_rbm_domain_for_api_key(api_key_id, api_key_domain_id)
        if not rbm_domain:
            return None

        # Replace @ with = in rcpt
        rcpt_encoded = rcpt.replace('@', '=')

        # Build the envelope_from
        envelope_from = f"bounces+{rcpt_encoded}@{rbm_domain}"
        return envelope_from
    except Exception as e:
        logger = logging.getLogger(__name__)
        logger.error(f"Error generating envelope_from: {e}")
        return None


def get_associated_domain_ids(domain_id: int) -> set[int]:
    """
    Get every domain_id associated with a principal domain, including:
    - the domain_id itself
    - associated subdomains (by domain suffix)

    Args:
        domain_id: ID of the principal domain

    Returns:
        set[int]: Valid domain_ids (the domain itself and its subdomains)
    """
    try:
        associated_ids = {domain_id}  # always include the domain_id itself

        # Name of the principal domain
        sql = """
            SELECT name
            FROM revbridge_connect.virtual_domains
            WHERE id = %s
        """
        result = fetch_one(sql, (domain_id,))
        if not result:
            return associated_ids

        main_domain_name = result.get('name', '').lower().strip()
        if not main_domain_name:
            return associated_ids

        # Subdomains by domain suffix
        # Example: for main_domain = "example.com", look for "*.example.com"
        sql_suffix = """
            SELECT id, name
            FROM revbridge_connect.virtual_domains
            WHERE name LIKE %s AND name != %s
        """
        suffix_pattern = f"%.{main_domain_name}"
        subdomains_by_suffix = fetch_all(sql_suffix, (suffix_pattern, main_domain_name))
        if subdomains_by_suffix:
            for subdomain in subdomains_by_suffix:
                subdomain_name = subdomain.get('name', '').lower().strip()
                subdomain_id = subdomain.get('id')
                # Make sure it really is a subdomain (ends with .main_domain)
                if subdomain_id and subdomain_name.endswith(f".{main_domain_name}"):
                    associated_ids.add(subdomain_id)

        return associated_ids
    except Exception as e:
        logger = logging.getLogger(__name__)
        logger.error(f"Error looking up associated domain_ids: {e}")
        # On error, return at least the domain_id itself
        return {domain_id}


def extract_api_key_from_smtp_username(milter_instance) -> str | None:
    """
    Extract the API key from the SMTP username with Postfix's getsymval.

    The API key is the username of the SMTP authentication.
    Postfix exposes it through the {auth_authen} macro.

    Args:
        milter_instance: Milter instance, to call getsymval

    Returns:
        str: API key, or None if not found
    """
    try:
        # Username of the SMTP authentication
        # {auth_authen} holds the username used to authenticate
        auth_username = milter_instance.getsymval('{auth_authen}')
        if auth_username:
            auth_username = auth_username.strip()
            # The username is the API key
            if auth_username:
                return auth_username
    except Exception as e:
        logger = logging.getLogger(__name__)
        logger.debug(f"Error reading auth_authen: {e}")

    return None


def get_approved_sender_from_db(sender_email: str) -> dict[str, Any] | None:
    """
    Read approved-sender information from the database.

    Args:
        sender_email: Full sender address (e.g. news@mail.example.com)

    Returns:
        dict: Approved-sender information with principal_domain_id, or None if not found
    """
    try:
        if not sender_email:
            return None
        
        sender_email = sender_email.lower().strip()
        
        sql = """
            SELECT id, sender_email, principal_domain_id, is_active
            FROM approved_senders
            WHERE sender_email = %s AND is_active = TRUE
        """
        result = fetch_one(sql, (sender_email,))
        if result:
            return {
                'id': result.get('id'),
                'sender_email': result.get('sender_email'),
                'principal_domain_id': result.get('principal_domain_id'),
                'is_active': result.get('is_active', True)
            }
        
        return None
    except Exception as e:
        logger = logging.getLogger(__name__)
        logger.error(f"Error reading the approved sender from the database: {e}")
        return None


def sender_belongs_to_api_key_domain(sender_email: str, api_key_domain_id: int) -> bool:
    """
    Check whether sender_email belongs to the API key's domain_id or its subdomains.

    Args:
        sender_email: Sender address (e.g. user@example.com)
        api_key_domain_id: Domain ID of the API key

    Returns:
        bool: True if the sender belongs to the API key's domain or its subdomains, False otherwise
    """
    try:
        if not sender_email or "@" not in sender_email or not api_key_domain_id:
            return False

        sender_domain_name = sender_email.split("@")[-1].lower().strip()

        # domain_id of the sender
        domain_id = get_domain_id_from_redis(sender_domain_name)
        if not domain_id:
            # Fallback: the database
            sql = """
                SELECT id
                FROM revbridge_connect.virtual_domains
                WHERE name = %s
            """
            result = fetch_one(sql, (sender_domain_name,))
            if result:
                domain_id = result.get('id')
        
        if not domain_id:
            return False
        
        # Every domain_id associated with the API key's domain (subdomains included)
        associated_domain_ids = get_associated_domain_ids(api_key_domain_id)

        # Is the sender's domain_id among them?
        return domain_id in associated_domain_ids
    except Exception as e:
        logger = logging.getLogger(__name__)
        logger.error(f"Error checking whether the sender belongs to the API key's domain: {e}")
        return False


def get_approved_sender_info(sender_email: str, principal_domain_id: int) -> dict[str, Any] | None:
    """
    Get approved-sender information (Redis first, then the database).

    Args:
        sender_email: Full sender address (e.g. news@mail.example.com)
        principal_domain_id: ID of the principal domain

    Returns:
        dict: Approved-sender information with principal_domain_id, or None if not found
    """
    # Redis first
    sender_info = get_approved_sender_from_redis(sender_email, principal_domain_id)
    if sender_info:
        return sender_info

    # Fallback: the database
    sender_info = get_approved_sender_from_db(sender_email)
    if sender_info:
        # The approved sender must belong to the expected principal domain
        if sender_info.get('principal_domain_id') == principal_domain_id:
            # Cache it in Redis for next time (optional, but useful)
            try:
                if redis:
                    key = f"approved_sender:{sender_email}:{principal_domain_id}"
                    redis.setex(key, REDIS_TTL_SEC, json.dumps(sender_info))
            except Exception:
                pass  # a cache failure must not block
            return sender_info
        else:
            # The approved sender exists but belongs to another principal domain
            return None

    return None


class TrackingMilter(Milter.Milter):
    """
    Postfix milter for email tracking and link rewriting.

    Extends the Milter base class to intercept messages during SMTP
    processing. It provides:

    - Delivery tracking through Redis
    - Link rewriting with tracking tokens
    - Open tracking through pixel injection
    - Support for HTML and multipart MIME emails

    The milter is designed never to block email delivery.
    """

    def __init__(self):
        """Initialize the tracking milter with default settings and state."""
        super().__init__()

        # Tracing state
        self._mail_pipeline_span = None  # root span mail.pipeline
        self._mail_message_span = None   # anchor span mail.message
        self._smtp_envelope_span = None   # span smtp.envelope (shared by envfrom/envrcpt)
        self._trace_root_span = None      # for compatibility (will be mail.pipeline)
        self._trace_span = None          # span currently being processed

        # API trace context (when the email comes through /emails/send)
        # Taken from the X-Trace-* headers added by the API
        self._api_trace_id = None         # API trace ID (header X-Trace-Id)
        self._api_parent_span_id = None   # API parent span ID (header X-Parent-Span-Id)
        self._api_trace_origin = None     # origin service (header X-Trace-Origin)

        # Milter flags that allow modifications
        # ADDRCPT allows adding recipients (needed for the shadow email)
        # CHGHDRS allows changing headers
        # CHGFROM allows changing the envelope_from (needed for the dynamic envelope)
        addrcpt_flag = getattr(Milter, "ADDRCPT", 0)
        chghdrs_flag = getattr(Milter, "CHGHDRS", 0)
        chgfrom_flag = getattr(Milter, "CHGFROM", 0)
        if hasattr(self, "_actions"):
            self._actions |= addrcpt_flag | chghdrs_flag | chgfrom_flag
        else:
            self._actions = addrcpt_flag | chghdrs_flag | chgfrom_flag

        # Message processing state
        self.headers = {}  # email headers
        self.rcpts = []  # recipients
        self.message_id = None  # X-Connect-ID or Message-ID
        self.domain_name = None  # sender's domain name
        self.campaign_id = None  # sender's X-Connect-Campaign (passed through in events)
        self.queue_id = None  # Postfix queue ID
        self.is_html = False  # HTML content flag
        self.is_multipart = False  # multipart MIME flag
        self.global_mime_type = None  # main MIME type (e.g. multipart/alternative)
        self.global_content_transfer_encoding = None  # main body encoding
        self.multipart_boundary = None  # normalized boundary, for reassembly
        # Original boundary found in the body
        self.multipart_boundary_original = None
        self.boundary_had_prefix = False  # whether the original boundary had an extra '--'
        self.boundary_normalized = False  # whether header/body were normalized
        self._pending_boundary_normalization = None  # info to normalize the boundary in eom()
        self.MAX_BUFFER_BYTES = (
            10 * 1024 * 1024
        )  # 10MB buffer limit, for safety
        self.body_chunks = []  # chunks of the email body
        self.start_time = time.time()  # processing start timestamp
        self.email_size = 0  # total email size in bytes
        self.smtp_session = {
            "connect": None,
            "helo": None,
            "mail_from": None,
            "rcpt_to_raw": [],
        }
        self.attachments_info = []

        # Email configuration from Redis (cache)
        self.email_config = None  # full email configuration of the domain
        self.from_email = None  # sender address (for the cache lookup)

        # Approved-sender validation
        self.envelope_from = None  # Envelope From (MAIL FROM - principal domain) - e.g. contact@mail.example.com
        self.envelope_from_domain = None  # envelope from domain (principal domain) - e.g. mail.example.com
        self.header_from = None  # header From: (authorized sender) - e.g. news@mail.shop.example
        self.header_from_domain = None  # header From domain (authorized sender) - e.g. mail.shop.example
        self.approved_sender_info = None  # approved-sender information (when applicable)
        self.principal_domain_id = None  # ID of the authorized principal domain
        self.principal_domain_name = None  # name of the authorized principal domain
        self.api_key_id = None  # API key ID, kept to validate the envelope_from
        self.api_key_domain_id = None  # domain ID of the API key (set in envfrom() when the API key is valid)

        # Logger for this connection
        self.logger = logging.getLogger(f"milter.{id(self)}")
        self.logger.debug(
            f"New connection started - {datetime.now().isoformat()}")

        # Safety timeout: do NOT use signal/alarm in threads.
        # Use a simple deadline checked in the handlers.
        self.deadline = (
            time.monotonic() + 1800
        )  # 1800 s (30 min) per connection (adjust as needed)
        self.signal = None

    @staticmethod
    def _get_fernet_key() -> Fernet | None:
        """
        Get the Fernet instance used to encrypt tokens.

        Returns:
            Fernet: Configured Fernet instance, or None if encryption is unavailable
        """
        if not CRYPTO_AVAILABLE:
            return None

        if TOKEN_ENCRYPTION_KEY:
            try:
                return Fernet(TOKEN_ENCRYPTION_KEY.encode())
            except Exception as e:
                logger = logging.getLogger(__name__)
                logger.error(
                    f"Error initializing Fernet with the given key: {e}")
                return None

        # No key is ever derived from a salt in the code (anyone with the source code
        # could forge click and unsubscribe tokens). Without TOKEN_ENCRYPTION_KEY there
        # are no tokens: the milter neither rewrites links nor adds the pixel, and the
        # email goes out unchanged.
        logging.getLogger(__name__).error(
            "TOKEN_ENCRYPTION_KEY missing: tracking disabled (links and pixel not rewritten)")
        return None

    def _encrypt_token_data(self, data: dict[str, Any]) -> str | None:
        """
        Encrypt the token data with gzip compression and return it as base64url.

        The data is compressed before encryption to make the token shorter and
        better suited for URLs.

        Args:
            data: Token data (mid, rcpt, type, url, ts)

        Returns:
            str: Encrypted, compressed token in base64url, or None on error
        """
        fernet = self._get_fernet_key()
        if not fernet:
            # Never a plain-text token (base64 JSON would be forgeable: a click to
            # any URL and the unsubscribe of any recipient).
            return None

        try:
            # Serialize to JSON
            json_data = json.dumps(data, separators=(
                ',', ':'))  # compact, no spaces

            # Compress with gzip before encrypting (much smaller)
            compressed = gzip.compress(
                json_data.encode('utf-8'), compresslevel=9)

            # Encrypt the compressed data
            encrypted = fernet.encrypt(compressed)

            # Convert to base64url without padding
            return base64.urlsafe_b64encode(encrypted).decode().rstrip("=")
        except Exception as e:
            self.logger.error(f"Error encrypting the token: {e}")
            return None

    def _new_token(
        self,
        token_type: str,
        message_id: str,
        rcpt: str,
        url: str | None = None,
        campaign_id: str | None = None,
        nbytes: int = 16
    ) -> str | None:
        """
        Generate a new encrypted tracking token that carries its data.

        The data is optimized for token size:
        - JSON without spaces (separators=(',', ':'))
        - gzip compression before encryption
        - short keys for the message ID and recipient

        Args:
            token_type: Token type ('click', 'open', 'unsubscribe')
            message_id: Message ID
            rcpt: Recipient address
            url: Destination URL (required for 'click', optional for the others)
            campaign_id: Campaign ID (the sender's X-Connect-Campaign header, optional)
            nbytes: Number of random nonce bytes (default: 16, no longer used)

        Returns:
            str: Encrypted, compressed token in base64url, or None on error
        """
        # Payload (optimized for size)
        payload = {
            "v": 3,  # version 3: added domain_name (n)
            # Short type: 'c'=click, 'o'=open, 'u'=unsubscribe
            "t": token_type[0],
            "m": message_id,  # Message ID
            "r": rcpt,  # Recipient
            "s": int(time.time()),  # Timestamp (short key)
        }

        # Sender's domain_name (so suppressions land on the right domain)
        if self.domain_name and self.domain_name != "unknown":
            payload["n"] = self.domain_name

        # URL, for click tokens
        if token_type == "click" and url:
            # The URL is kept in full (no shortening)
            payload["u"] = url

        # Sender's campaign_id, if any (short key to save space)
        if campaign_id:
            payload["c"] = campaign_id  # Campaign ID

        # Encrypt and return the token
        return self._encrypt_token_data(payload)

    def _get_track_base(self) -> str:
        """
        Return the tracking base URL to use.

        Prefers tracking_domain from the Redis configuration, otherwise the default TRACK_BASE.

        Returns:
            str: Tracking base URL (e.g. "https://trk.example.com")
        """
        # tracking_domain from the Redis configuration
        if self.email_config and self.email_config.get('tracking_domain'):
            tracking_domain = self.email_config.get('tracking_domain')
            # Make sure it has a scheme
            if not tracking_domain.startswith(('http://', 'https://')):
                tracking_domain = f"https://{tracking_domain}"
            self.logger.debug(
                f"[TRACKING] Using tracking_domain from the configuration: {tracking_domain}"
            )
            return tracking_domain

        # Fallback: the default TRACK_BASE
        return TRACK_BASE

    def _get_pixel_tracking_url(self, token: str) -> str:
        """
        Return the full tracking pixel URL.

        Prefers pixel_tracking_url from the Redis configuration, otherwise track_base/o/{token}.png.

        Args:
            token: Open tracking token

        Returns:
            str: Full tracking pixel URL
        """
        # pixel_tracking_url from the Redis configuration
        if self.email_config and self.email_config.get('pixel_tracking_url'):
            pixel_url = self.email_config.get('pixel_tracking_url')
            # Replace {token} if present; otherwise append the token
            if '{token}' in pixel_url:
                return pixel_url.replace('{token}', token)
            else:
                # Append the token at the end
                return f"{pixel_url.rstrip('/')}/{token}.png"

        # Fallback: the default track_base
        track_base = self._get_track_base()
        return f"{track_base}/o/{token}.png"

    def _get_click_tracking_url(self, token: str) -> str:
        """
        Return the full click tracking URL.

        Prefers click_tracking_url from the Redis configuration, otherwise track_base/c/{token}.

        Args:
            token: Click tracking token

        Returns:
            str: Full click tracking URL
        """
        # click_tracking_url from the Redis configuration
        if self.email_config and self.email_config.get('click_tracking_url'):
            click_url = self.email_config.get('click_tracking_url')
            # Replace {token} if present; otherwise append the token
            if '{token}' in click_url:
                return click_url.replace('{token}', token)
            else:
                # Append the token at the end
                return f"{click_url.rstrip('/')}/{token}"

        # Fallback: the default track_base
        track_base = self._get_track_base()
        return f"{track_base}/c/{token}"

    def _get_unsub_tracking_url(self, token: str) -> str:
        """
        Return the full unsubscribe URL.

        Uses track_base/u/{token}.

        Args:
            token: Unsubscribe tracking token

        Returns:
            str: Full unsubscribe URL
        """
        track_base = self._get_track_base()
        return f"{track_base}/u/{token}"

    def _is_tracking_enabled(self) -> bool:
        """
        Check whether tracking is enabled for this domain.

        Reads tracking_enabled from the Redis configuration. Without a configuration,
        tracking is enabled (default behavior).

        Returns:
            bool: True if tracking is enabled, False otherwise
        """
        # No configuration: tracking is enabled by default
        if not self.email_config:
            return True

        # tracking_enabled from the configuration
        tracking_enabled = self.email_config.get('tracking_enabled', True)

        if not tracking_enabled:
            self.logger.debug(
                f"[TRACKING] Tracking disabled for domain {self.domain_name}"
            )

        return tracking_enabled

    def _should_rewrite_links(self) -> bool:
        """
        Check whether links must be rewritten for this domain.

        Reads rewrite_links from the Redis configuration. Without a configuration,
        falls back to the global REWRITE_LINKS.

        Returns:
            bool: True if links must be rewritten, False otherwise
        """
        # No configuration: use the global value
        if not self.email_config:
            return REWRITE_LINKS

        # rewrite_links from the configuration (may be None)
        rewrite_links = self.email_config.get('rewrite_links')

        # None: use the global value
        if rewrite_links is None:
            return REWRITE_LINKS

        # Convert to bool if needed (may come as the string "1"/"0" or a bool)
        if isinstance(rewrite_links, str):
            return rewrite_links.lower() in ('1', 'true', 'yes', 'on')

        return bool(rewrite_links)

    def _should_inject_pixel(self) -> bool:
        """
        Check whether the tracking pixel must be injected for this domain.

        Reads inject_pixel from the Redis configuration. Without a configuration,
        falls back to the global INJECT_PIXEL.

        Returns:
            bool: True if the pixel must be injected, False otherwise
        """
        # No configuration: use the global value
        if not self.email_config:
            return INJECT_PIXEL

        # inject_pixel from the configuration (may be None)
        inject_pixel = self.email_config.get('inject_pixel')

        # None: use the global value
        if inject_pixel is None:
            return INJECT_PIXEL

        # Convert to bool if needed (may come as the string "1"/"0" or a bool)
        if isinstance(inject_pixel, str):
            return inject_pixel.lower() in ('1', 'true', 'yes', 'on')

        return bool(inject_pixel)

    def _generate_boundary(self, prefix: str = "revb") -> str:
        """
        Generate a MIME boundary to build a multipart/alternative when needed.
        """
        rand = base64.urlsafe_b64encode(os.urandom(12)).decode().rstrip("=")
        return f"===={prefix}_{rand}===="

    def _extract_email_headers(self) -> dict:
        """
        Extract and format the message headers to include in outbox events.

        Returns:
            dict: Formatted headers for the outbox events
        """
        try:
            if not hasattr(self, "headers") or not self.headers:
                return {}

            # Important headers, always included
            important_headers = [
                "subject",
                "from",
                "to",
                "cc",
                "bcc",
                "reply-to",
                "message-id",
                "x-connect-id",
                "content-type",
                "mime-version",
                "date",
                "return-path",
                "received",
                "x-mailer",
                "x-priority",
                "x-msmail-priority",
                "importance",
                "x-original-sender",
            ]

            formatted_headers = {}

            for header_name in important_headers:
                if header_name in self.headers:
                    header_value = self.headers[header_name]

                    # A list: take the last (most recent) value
                    if isinstance(header_value, list):
                        header_value = header_value[-1]

                    # Cap the header size to avoid very large payloads
                    if len(str(header_value)) > 1000:
                        header_value = str(header_value)[:1000] + "..."

                    formatted_headers[header_name] = str(header_value).strip()

            # Add the other headers (capped in number to avoid very large payloads)
            other_headers = {}
            header_count = 0
            max_other_headers = 20  # cap on additional headers

            for header_name, header_value in self.headers.items():
                if (
                    header_name not in important_headers
                    and header_count < max_other_headers
                ):
                    if isinstance(header_value, list):
                        header_value = header_value[-1]

                    # Cap the size
                    if len(str(header_value)) > 500:
                        header_value = str(header_value)[:500] + "..."

                    other_headers[header_name] = str(header_value).strip()
                    header_count += 1

            # Merge the important and the other headers
            all_headers = {**formatted_headers, **other_headers}

            self.logger.debug(
                f"Extracted {len(all_headers)} headers for the outbox event")
            return all_headers

        except Exception as e:
            self.logger.error(f"Error extracting headers for the outbox event: {e}")
            return {}

    def _record_attachment_metadata(
        self, headers_text: str, body: str, encoding: str
    ) -> None:
        """
        Record relevant attachment metadata for auditing.
        """
        try:
            from email import message_from_string

            payload_headers = (
                headers_text if headers_text.endswith(
                    "\r\n") else headers_text + "\r\n"
            )
            msg = message_from_string(payload_headers + "\r\n")

            filename = msg.get_filename()
            if filename:
                filename = decode_mime_value(filename)
            else:
                filename_match = re.search(
                    r'filename\*?=(?:"?)([^";\r\n]+)',
                    headers_text,
                    re.IGNORECASE,
                )
                if filename_match:
                    filename = decode_mime_value(
                        filename_match.group(1).strip('"'))

            content_type = msg.get_content_type() if msg.get("Content-Type") else None
            disposition_value = msg.get("Content-Disposition")
            content_id = msg.get("Content-ID")

            decoded_bytes = None
            try:
                if encoding == "base64":
                    content_clean = re.sub(r"\s+", "", body)
                    decoded_bytes = base64.b64decode(
                        content_clean, validate=False)
                elif encoding == "quoted-printable":
                    decoded_bytes = quopri.decodestring(body.encode("utf-8"))
                else:
                    decoded_bytes = body.encode("utf-8", errors="ignore")
            except Exception as decode_error:
                self.logger.debug(
                    f"[ATTACHMENT] Failed to decode the attachment for auditing: {decode_error}"
                )
                decoded_bytes = None

            size_bytes = (
                len(decoded_bytes)
                if decoded_bytes is not None
                else len(body.encode("utf-8", errors="ignore"))
            )
            sha256_hash = (
                hashlib.sha256(decoded_bytes).hexdigest()
                if decoded_bytes is not None
                else None
            )

            metadata = {
                "filename": filename,
                "content_type": content_type,
                "disposition": disposition_value,
                "content_id": content_id,
                "transfer_encoding": encoding,
                "size_bytes": size_bytes,
                "sha256": sha256_hash,
            }

            self.attachments_info.append(metadata)
        except Exception as e:
            self.logger.error(
                f"[ATTACHMENT] Error recording attachment metadata: {e}",
                exc_info=True,
            )

    def _is_email_suppressed(self, email: str, domain_name: str) -> dict:
        """
        Check whether an address is suppressed for a given domain

        Args:
            email: Email address
            domain_name: Domain name

        Returns:
            dict: {"suppressed": bool, "type": str, "reason": str} or {"suppressed": False}
        """
        try:
            if not email or not domain_name:
                return {"suppressed": False}

            # Clean the parameters
            email_clean = email.strip().lower()

            result = fetch_one(
                """
                SELECT suppression_type, reason, bounce_type, expires_at
                FROM suppressions
                WHERE email = %s
                  AND domain_name = %s
                  AND is_active = TRUE
                  AND (expires_at IS NULL OR expires_at > NOW())
                ORDER BY suppressed_at DESC
                LIMIT 1
            """,
                (email_clean, domain_name),
            )

            if result:
                self.logger.warning(
                    f"Address {email} is suppressed: {result['suppression_type']} - {result['reason']}"
                )
                return {
                    "suppressed": True,
                    "type": result["suppression_type"],
                    "reason": result["reason"],
                    "bounce_code": result.get("bounce_type"),
                    "expires_at": (
                        result["expires_at"].isoformat()
                        if result["expires_at"]
                        else None
                    ),
                }

            return {"suppressed": False}

        except Exception as e:
            self.logger.error(
                f"[SUPPRESSION] Error checking the suppression of {email}: {e}"
            )
            # On error, do not block the email (fail-safe)
            return {"suppressed": False, "error": str(e)}

    def _expired(self) -> bool:
        """
        Check whether the processing deadline has passed.

        Returns True when the deadline was exceeded, logs it, and
        processing continues without blocking.

        Returns:
            bool: True if the deadline passed, False otherwise
        """
        if time.monotonic() > self.deadline:
            self.logger.error(
                "[TIMEOUT] DEADLINE EXCEEDED - ending processing early"
            )
            return True
        return False

    def connect(self, hostname, family, hostaddr):
        """
        Handle the SMTP connection event.

        Called when a new SMTP connection is established.

        Args:
            hostname: Host name of the connecting client
            family: Address family (e.g. AF_INET)
            hostaddr: IP address of the connecting client

        Returns:
            Milter.CONTINUE: always continue processing
        """
        # Do NOT create spans here: wait for eoh(), when the API trace context is known
        # Keep the connection data for the span
        self._connect_hostname = str(hostname)
        self._connect_family = str(family)
        if isinstance(hostaddr, (list, tuple)) and hostaddr:
            self._connect_hostaddr = str(hostaddr[0])
            self._connect_port = str(hostaddr[1]) if len(hostaddr) > 1 else None
        else:
            self._connect_hostaddr = str(hostaddr)
            self._connect_port = None

        # Span state (the spans are created in eoh())
        self._mail_pipeline_span = None
        self._trace_root_span = None
        self._trace_span = None
        self.logger.debug("[OTEL] Connection data stored - spans will be created in eoh()")

        self.logger.debug(f"[CONNECT] New connection from {hostaddr}")
        address = hostaddr
        port = None

        if isinstance(hostaddr, (list, tuple)) and hostaddr:
            address = hostaddr[0]
            if len(hostaddr) > 1:
                port = hostaddr[1]

        self.smtp_session["connect"] = {
            "hostname": hostname,
            "family": str(family),
            "address": address,
            "port": port,
        }
        return Milter.CONTINUE

    def helo(self, heloname):
        """
        Handle the HELO/EHLO command.
        """
        self.smtp_session["helo"] = heloname
        return Milter.CONTINUE

    def envfrom(self, mailfrom, *strargs):
        """
        Handle the MAIL FROM command.

        Called when the sender address is given in SMTP.

        Args:
            mailfrom: Sender address
            *strargs: Additional SMTP arguments

        Returns:
            Milter.CONTINUE: continue processing if not expired
        """
        if self._expired():
            return Milter.CONTINUE

        # --- PER-MESSAGE state reset (start of each MAIL FROM) ---
        # pymilter reuses THIS instance for the whole SMTP connection, and one
        # connection can carry SEVERAL messages (MAIL FROM...DATA repeated), all the
        # more with the worker's connection reuse (SMTP_CONNECTION_REUSE). Without a
        # reset here, the previous message's state would leak into the next one. In
        # particular self.rcpts would ACCUMULATE, and since eom() iterates
        # `for rcpt in self.rcpts`, the N-th message of a connection would emit N
        # 'sent' events. The reset also keeps headers/body/message_id/MIME from
        # leaking between messages. The envelope fields, from_email, domain_name and
        # api_key_* are set again just below in this envfrom; the other per-message
        # fields go back to their __init__ defaults.
        self.headers = {}
        self.rcpts = []
        self.message_id = None
        self.campaign_id = None
        self.queue_id = None
        self.is_html = False
        self.is_multipart = False
        self.global_mime_type = None
        self.global_content_transfer_encoding = None
        self.multipart_boundary = None
        self.multipart_boundary_original = None
        self.boundary_had_prefix = False
        self.boundary_normalized = False
        self._pending_boundary_normalization = None
        self.body_chunks = []
        self.start_time = time.time()
        self.email_size = 0
        self.attachments_info = []
        self.email_config = None
        self.header_from = None
        self.header_from_domain = None
        self.approved_sender_info = None
        self.principal_domain_id = None
        self.principal_domain_name = None
        # smtp_session: keep connect/helo (per connection), clear the per-message fields
        self.smtp_session["mail_from"] = None
        self.smtp_session["rcpt_to_raw"] = []
        # Per-message spans/trace (created again in eoh()/header())
        self._mail_pipeline_span = None
        self._mail_message_span = None
        self._trace_root_span = None
        self._api_trace_id = None
        self._api_parent_span_id = None
        self._api_trace_origin = None

        # Do NOT create spans here: wait for eoh(), when the API trace context is known
        # Keep the envelope data for the span
        self._envelope_mailfrom = str(mailfrom)
        self._smtp_envelope_span = None
        self._trace_span = None
        self.logger.debug("[OTEL] Envelope data stored - spans will be created in eoh()")

        # Keep the received envelope_from (ignored; envrcpt() builds it dynamically)
        self.envelope_from_received = mailfrom  # envelope_from received over SMTP (ignored)
        self.envelope_from = None  # built dynamically in envrcpt()
        self.from_email = mailfrom
        self.envelope_from_domain = None  # set in envrcpt() when the envelope_from is built
        self.domain_name = "unknown"  # set in envrcpt() when the envelope_from is built

        self.logger.debug(
            f"[ENVELOPE] FROM received: {mailfrom} (ignored; envrcpt() builds it dynamically)"
        )

        # Validate the API key (required): only check that it exists and is active
        # The API key is the SMTP username
        # The value sent is the api_key_id, not the hash
        api_key_id = extract_api_key_from_smtp_username(self)
        if not api_key_id:
            self.logger.warning(
                "[API_KEY] Required API key not found in the SMTP username"
            )
            self.setreply(
                "550",
                "5.7.1",
                "API key is required for email sending",
            )
            return Milter.REJECT

        # Does the API key exist, and is it active?
        api_key = get_api_key_by_id(api_key_id)
        if not api_key:
            self.logger.warning(
                f"[API_KEY] API key not found: {api_key_id[:20]}..."
            )
            self.setreply(
                "550",
                "5.7.1",
                "API key not found",
            )
            return Milter.REJECT

        if not api_key.get('is_active', False):
            self.logger.warning(
                f"[API_KEY] API key inactive: {api_key_id[:20]}..."
            )
            self.setreply(
                "550",
                "5.7.1",
                "API key is inactive",
            )
            return Milter.REJECT

        # Not expired?
        expires_at = api_key.get('expires_at')
        if expires_at:
            from datetime import datetime
            now = datetime.now(UTC)

            if isinstance(expires_at, datetime):
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=UTC)
            elif isinstance(expires_at, str):
                try:
                    expires_at = datetime.fromisoformat(
                        expires_at.replace('Z', '+00:00'))
                except ValueError:
                    try:
                        expires_at = datetime.strptime(
                            expires_at, '%Y-%m-%d %H:%M:%S')
                        expires_at = expires_at.replace(tzinfo=UTC)
                    except ValueError:
                        expires_at = None

            if expires_at and expires_at < now:
                self.logger.warning(
                    f"[API_KEY] API key expired: {api_key_id[:20]}..."
                )
                self.setreply(
                    "550",
                    "5.7.1",
                    "API key expired",
                )
                return Milter.REJECT

        self.logger.debug(
            f"[API_KEY] API key valid: {api_key_id[:20]}..."
        )

        # Keep the API key ID and domain_id to validate the envelope_from in envrcpt()
        self.api_key_id = api_key_id
        self.api_key_domain_id = api_key.get('domain_id')

        # Was the domain_id found?
        if not self.api_key_domain_id:
            self.logger.warning(
                f"[API_KEY] API key {api_key_id[:20]}... has no domain_id - "
                f"the dynamic envelope_from may not work"
            )
        else:
            self.logger.debug(
                f"[API_KEY] API key domain ID stored: {self.api_key_domain_id}"
            )

            # Set principal_domain_id early, for the approved-sender validation in header()
            # The api_key_domain_id is the principal_domain_id
            try:
                self.principal_domain_id = self.api_key_domain_id
                # Name of the API key's domain
                sql = """
                    SELECT name
                    FROM revbridge_connect.virtual_domains
                    WHERE id = %s
                """
                api_key_domain_result = fetch_one(sql, (self.api_key_domain_id,))
                if api_key_domain_result:
                    api_key_domain_name = api_key_domain_result.get('name', '').strip()
                    self.principal_domain_name = api_key_domain_name
                    self.logger.debug(
                        f"[ENVFROM] Principal domain set: {api_key_domain_name} (ID: {self.api_key_domain_id}) for the approved-sender validation"
                    )
            except Exception as e:
                self.logger.debug(
                    f"[ENVFROM] Error reading the principal domain name: {e}"
                )

        # Configuration of the API key's domain (not of the envelope_from)
        if self.api_key_domain_id:
            try:
                # Name of the API key's domain
                sql = """
                    SELECT name
                    FROM revbridge_connect.virtual_domains
                    WHERE id = %s
                """
                result = fetch_one(sql, (self.api_key_domain_id,))
                if result:
                    api_key_domain_name = result.get('name', '').lower().strip()
                    if api_key_domain_name:
                        self.logger.debug(
                            f"[EMAIL_CONFIG] Reading the Redis configuration for the API key's domain: {api_key_domain_name}"
                        )
                        self.email_config = get_email_config_for_domain_from_redis(
                            api_key_domain_name)
                        if self.email_config:
                            is_active = self.email_config.get('is_active', True)
                            if not is_active:
                                self.logger.warning(
                                    f"[EMAIL_CONFIG] Configuration found but INACTIVE for domain {api_key_domain_name} - using defaults"
                                )
                                self.email_config = None
                            else:
                                self.logger.debug(
                                    f"[EMAIL_CONFIG] Full, active configuration found for domain {api_key_domain_name}"
                                )
                        else:
                            self.logger.debug(
                                f"[EMAIL_CONFIG] Configuration NOT found in Redis for domain {api_key_domain_name} - using defaults"
                            )
            except Exception as e:
                self.logger.debug(
                    f"[EMAIL_CONFIG] Error reading the configuration from Redis: {e}"
                )
                self.email_config = None

        self.smtp_session["mail_from"] = {
            "address": mailfrom,
            "args": list(strargs) if strargs else [],
        }
        self.smtp_session["rcpt_to_raw"] = []
        self.attachments_info = []

        return Milter.CONTINUE

    def envrcpt(self, to, *strargs):
        """
        Handle the RCPT TO command.

        Called for each recipient address given in SMTP.
        Checks the suppression list before accepting the recipient.

        Args:
            to: Recipient address
            *strargs: Additional SMTP arguments

        Returns:
            Milter.REJECT: if the address is suppressed
            Milter.CONTINUE: continue processing if not expired
        """
        try:
            if self._expired():
                return Milter.CONTINUE

            # Parse out the real email address
            rcpt = parseaddr(to)[1] or to

            # Do NOT create spans here: wait for eoh(), when the API trace context is known
            # Keep the recipient data for the span
            suppression_check_span = None
            self.logger.debug("[OTEL] Recipient data stored - spans will be created in eoh()")

            if rcpt:
                self.logger.debug(
                    f"[ENVRCPT] Processing recipient: {rcpt} (total before: {len(self.rcpts)})"
                )

                # The dynamic envelope_from is built in eom(), once all recipients are known
                if not self.rcpts and self.api_key_id:
                    # eom() builds the envelope_from from the first recipient
                    self.logger.debug(
                        f"[ENVRCPT] Will build the dynamic envelope_from in eom() for rcpt {rcpt}"
                    )

                self.smtp_session.setdefault("rcpt_to_raw", []).append(
                    {
                        "address": rcpt,
                        "args": list(strargs) if strargs else [],
                    }
                )

                # Take the message_id from the headers received so far
                if not self.message_id and self.headers:
                    # X-Connect-ID or Message-ID?
                    if "x-connect-id" in self.headers:
                        self.message_id = (
                            self.headers["x-connect-id"][-1]
                            .strip()
                            .strip("<>")
                            .strip()
                        )
                        self.logger.debug(
                            f"Message ID extracted: {self.message_id}")
                    elif "message-id" in self.headers:
                        self.message_id = (
                            self.headers["message-id"][-1].strip().strip("<>").strip()
                        )
                        self.logger.debug(
                            f"Message ID extracted: {self.message_id}")

                # Is the address suppressed? (only with a domain_name)
                suppression_info = None
                if self.domain_name and self.domain_name != "unknown":
                    suppression_check = self._is_email_suppressed(
                        rcpt, self.domain_name
                    )

                    if suppression_check.get("suppressed", False):
                        suppression_info = {
                            "type": suppression_check.get("type", "unknown"),
                            "reason": suppression_check.get(
                                "reason", "Email suppressed"
                            ),
                            "bounce_code": suppression_check.get("bounce_code"),
                            "expires_at": suppression_check.get("expires_at"),
                        }
                        
                        # Tags on the suppression.check span
                        if OTEL_AVAILABLE and self._trace_span:
                            try:
                                self._trace_span.set_attribute("suppression.suppressed", True)
                                self._trace_span.set_attribute("suppression.type", str(suppression_info.get("type", "unknown")))
                                self._trace_span.set_attribute("suppression.reason", str(suppression_info.get("reason", "")))
                            except Exception:
                                pass

                        self.logger.warning(
                            f"Address SUPPRESSED: {rcpt} - Type: {suppression_info['type']}, Reason: {suppression_info['reason']}"
                        )

                        # RECORD THE SUPPRESSION AS A BOUNCE AND WRITE THE OUTBOX EVENT
                        self._register_suppression_as_bounce(
                            rcpt, suppression_info)

                        # WRITE THE SENT OUTBOX EVENT RIGHT HERE,
                        # because eom() is not called when the recipient is rejected
                        message_id_for_sent = self.message_id
                        if not message_id_for_sent:
                            # Generate a message_id when none is available
                            raw_message_id = (
                                f"suppressed-{rcpt.split('@')[0]}-{int(time.time())}"
                            )
                            message_id_for_sent = add_domain_to_message_id(
                                raw_message_id
                            )
                            self.logger.warning(
                                f"[SUPPRESSION] Message ID not available, generated: {message_id_for_sent}"
                            )

                        self.logger.debug(
                            f"[SUPPRESSION] Writing SENT outbox event for {rcpt} with message_id: {message_id_for_sent}"
                        )
                        self._trigger_kafka_sent(
                            message_id=message_id_for_sent, recipient_email=rcpt
                        )

                        # Rejection code from the suppression type
                        suppression_type = suppression_info.get(
                            "type", "unknown")
                        suppression_reason = suppression_info.get(
                            "reason", "Email suppressed"
                        )

                        if suppression_type == "bounce":
                            # Hard bounce - invalid address
                            self.setreply(
                                "550",
                                "5.1.1",
                                f"Recipient address rejected: {suppression_reason}",
                            )
                        elif suppression_type == "complaint":
                            # Spam complaint
                            self.setreply(
                                "550",
                                "5.7.1",
                                f"Delivery not authorized: {suppression_reason}",
                            )
                        elif suppression_type == "manual":
                            # Manual block
                            self.setreply(
                                "550",
                                "5.7.1",
                                f"Recipient blocked: {suppression_reason}",
                            )
                        elif suppression_type == "unsubscribe":
                            # Unsubscribe
                            self.setreply(
                                "550",
                                "5.7.1",
                                f"Recipient unsubscribed: {suppression_reason}",
                            )
                        else:
                            # Generic suppression
                            self.setreply(
                                "550",
                                "5.7.1",
                                f"Recipient suppressed: {suppression_reason}",
                            )

                        # REJECT THE RECIPIENT (do not send)
                        return Milter.REJECT

                # Add to the recipient list (only when NOT suppressed)
                self.rcpts.append(rcpt)
                self.logger.debug(
                    f"[ENVRCPT] Recipient added to the list: {rcpt} (total now: {len(self.rcpts)})"
                )

                # Finish the suppression.check span when the address is not suppressed
                if OTEL_AVAILABLE and self._trace_span:
                    try:
                        self._trace_span.set_attribute("suppression.suppressed", False)
                        self._trace_span.end()
                        self._trace_span = None
                        self.logger.debug("[OTEL] Span suppression.check finished (not suppressed)")
                    except Exception:
                        pass
            else:
                self.logger.warning(
                    f"[ENVRCPT] Empty or invalid recipient: {to}")

        except Exception as e:
            # Never block delivery because of processing errors
            # Log the error but keep accepting the recipient
            self.logger.error(
                f"[ENVRCPT] Error processing recipient {to}: {e}",
                exc_info=True,
            )
            # Still try to add it, if possible
            try:
                rcpt = parseaddr(to)[1] or to
                if rcpt and rcpt not in self.rcpts:
                    self.rcpts.append(rcpt)
                    self.logger.debug(
                        f"[ENVRCPT] Recipient added after an error: {rcpt}"
                    )
            except Exception:
                pass  # if this fails too, carry on

        # Finish the smtp.envelope span at the end of envrcpt(): the envelope phase is over
        # This keeps smtp.envelope from enclosing smtp.headers and mail.message
        if OTEL_AVAILABLE and hasattr(self, '_smtp_envelope_span') and self._smtp_envelope_span:
            try:
                # Final tags on smtp.envelope
                if self.rcpts:
                    self._smtp_envelope_span.set_attribute("smtp.rcpt_count", len(self.rcpts))
                    if len(self.rcpts) > 0:
                        self._smtp_envelope_span.set_attribute("smtp.first_rcpt", str(self.rcpts[0]))
                self._smtp_envelope_span.end()
                self._smtp_envelope_span = None
                self.logger.debug("[OTEL] Span smtp.envelope finished at the end of envrcpt() - envelope phase over")
            except Exception as e:
                self.logger.warning(f"[OTEL] Error finishing smtp.envelope: {e}", exc_info=True)

        return Milter.CONTINUE

    def _register_suppression_as_bounce(self, rcpt: str, suppression_info: dict):
        """
        Record a suppression as a bounce in the deliveries table, immediately
        """
        try:
            self.logger.debug(
                f"[SUPPRESSION] Recording the suppression as a bounce: {rcpt}"
            )

            # Status from the suppression type
            suppression_type = suppression_info.get("type", "unknown")
            suppression_reason = suppression_info.get(
                "reason", "Email suppressed")

            if suppression_type == "bounce":
                status = "bounced"
                dsn_message = suppression_reason
            elif suppression_type == "complaint":
                status = "bounced"  # counted as a bounce in statistics
                dsn_message = f"Complaint: {suppression_reason}"
            elif suppression_type == "unsubscribe":
                status = "bounced"  # counted as a bounce in statistics
                dsn_message = f"Unsubscribe: {suppression_reason}"
            elif suppression_type == "manual":
                status = "bounced"  # counted as a bounce in statistics
                dsn_message = f"Manual block: {suppression_reason}"
            else:
                status = "bounced"
                dsn_message = suppression_reason

                # Generate a unique message_id when none is available
            if not self.message_id:
                raw_message_id = f"suppressed-{rcpt.split('@')[0]}-{int(time.time())}"
                message_id = add_domain_to_message_id(raw_message_id)
            else:
                message_id = self.message_id

            self.logger.debug(
                f"[SUPPRESSION] Using message_id: {message_id} (original: {self.message_id})"
            )

            # domain_id
            domain_id = get_domain_id(self.domain_name)

            # Insert into deliveries
            exec_sql(
                """
                INSERT INTO deliveries (message_id, rcpt, domain_name, domain_id, status, attempt)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    status = VALUES(status),
                    domain_id = VALUES(domain_id),
                    last_update = CURRENT_TIMESTAMP,
                    attempt = attempt + 1
            """,
                (message_id, rcpt, self.domain_name, domain_id, status, 1),
            )

            # Insert into delivery_events, for history
            exec_sql(
                """
                INSERT IGNORE INTO delivery_events (message_id, rcpt, domain_name, domain_id, queue_id, status, dsn, attempt, host, service, timestamp)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                ON DUPLICATE KEY UPDATE
                    dsn      = VALUES(dsn),
                    status   = VALUES(status),
                    attempt  = VALUES(attempt),
                    ts       = CURRENT_TIMESTAMP
            """,
                (
                    message_id,
                    rcpt,
                    self.domain_name,
                    domain_id,
                    "suppressed",
                    status,
                    f"Suppression: {suppression_type} - {dsn_message}",
                    1,
                    "milter",
                    "suppression",
                ),
            )

            self.logger.debug(
                f"[SUPPRESSION] Recorded as a bounce: {rcpt} - {status}"
            )

            # Write the suppression outbox event (a specific suppression event)
            self._trigger_kafka_suppressed(
                message_id=message_id,
                recipient_email=rcpt,
                suppression_type=suppression_type,
                suppression_reason=suppression_reason,
            )

        except Exception as e:
            self.logger.error(
                f"[SUPPRESSION] Error recording the suppression as a bounce for {rcpt}: {e}"
            )

    def _trigger_kafka_sent(
        self, message_id: str, recipient_email: str, **extra_data
    ):
        """
        Write the email-sent event to the outbox

        Args:
            message_id: Message ID
            recipient_email: Recipient address
            **extra_data: Extra payload data
        """
        try:
            self.logger.debug(f"Writing SENT outbox event for {message_id}")

            # API trace context for propagation (distributed tracing)
            # Uses the X-Trace-Id and X-Parent-Span-Id headers captured from the API
            api_trace_context = None
            if hasattr(self, '_api_trace_id') and self._api_trace_id:
                api_trace_context = {
                    "trace_id": self._api_trace_id,
                    "span_id": getattr(self, '_api_parent_span_id', None),
                }
                self.logger.debug(
                    f"[OTEL] Using the API trace context for the SENT event: "
                    f"trace_id={self._api_trace_id}, span_id={getattr(self, '_api_parent_span_id', None)}"
                )

            # All message headers
            email_headers = self._extract_email_headers()

            # Email-specific information, when available
            if hasattr(self, "headers") and self.headers:
                if "subject" in self.headers:
                    subject_value = (
                        self.headers["subject"][-1]
                        if isinstance(self.headers["subject"], list)
                        else self.headers["subject"]
                    )
                    extra_data["subject"] = subject_value
                if "from" in self.headers:
                    from_value = (
                        self.headers["from"][-1]
                        if isinstance(self.headers["from"], list)
                        else self.headers["from"]
                    )
                    extra_data["sender_email"] = from_value

            # Processing information
            processing_data = {
                "queue_id": self.queue_id,
                "recipient_count": len(self.rcpts) if hasattr(self, "rcpts") else 1,
                "processing_time": (
                    time.time() - self.start_time if hasattr(self, "start_time") else 0
                ),
            }
            extra_data.update(processing_data)

            # All headers into the payload
            extra_data["email_headers"] = email_headers
            if getattr(self, "smtp_session", None):
                extra_data["smtp_session"] = self.smtp_session
            if getattr(self, "attachments_info", None):
                extra_data["attachments"] = self.attachments_info

            # Write the outbox event with the API trace context (if any)
            # Retry with backoff
            outbox_ok = False
            for attempt in range(1, 4):
                try:
                    kafka_result = kafka_client.queue_email_sent(
                        message_id=message_id,
                        rcpt=recipient_email,
                        domain_name=self.domain_name,
                        trace_context=api_trace_context,
                        **extra_data,
                    )
                    if kafka_result:
                        self.logger.debug(
                            f"SENT event written to the outbox for {message_id} with {len(email_headers)} headers"
                        )
                        outbox_ok = True
                        break
                    else:
                        self.logger.warning(
                            f"Outbox returned False (attempt {attempt}/3) SENT message_id={message_id}"
                        )
                except Exception as retry_err:
                    self.logger.error(
                        f"Error writing SENT to the outbox (attempt {attempt}/3): {retry_err}"
                    )
                if attempt < 3:
                    time.sleep(attempt)  # backoff: 1s, 2s
            if not outbox_ok:
                self.logger.error(
                    f"CRITICAL FAILURE: SENT NOT written to the outbox after 3 attempts! "
                    f"message_id={message_id} rcpt={recipient_email}"
                )

        except Exception as e:
            self.logger.error(f"Error writing the SENT outbox event: {e}")
            import traceback

            self.logger.error(f"Traceback: {traceback.format_exc()}")

    def _trigger_kafka_suppressed(
        self,
        message_id: str,
        recipient_email: str,
        suppression_type: str,
        suppression_reason: str,
        **extra_data,
    ):
        """
        Write the email-suppressed event to the outbox

        Args:
            message_id: Message ID
            recipient_email: Recipient address
            suppression_type: Suppression type
            suppression_reason: Suppression reason
            **extra_data: Extra payload data
        """
        try:
            self.logger.debug(
                f"[KAFKA] Writing SUPPRESSED outbox event for {message_id}"
            )
            self.logger.debug(
                f"[KAFKA] Parameters: message_id={message_id}, recipient={recipient_email}, domain={self.domain_name}"
            )
            self.logger.debug(
                f"[KAFKA] Suppression: type={suppression_type}, reason={suppression_reason}"
            )

            # All message headers
            email_headers = self._extract_email_headers()

            # Email-specific information, when available
            if hasattr(self, "headers") and self.headers:
                if "subject" in self.headers:
                    subject_value = (
                        self.headers["subject"][-1]
                        if isinstance(self.headers["subject"], list)
                        else self.headers["subject"]
                    )
                    extra_data["subject"] = subject_value
                if "from" in self.headers:
                    from_value = (
                        self.headers["from"][-1]
                        if isinstance(self.headers["from"], list)
                        else self.headers["from"]
                    )
                    extra_data["sender_email"] = from_value

            # Processing information
            processing_data = {
                "queue_id": self.queue_id,
                "recipient_count": len(self.rcpts) if hasattr(self, "rcpts") else 1,
                "processing_time": (
                    time.time() - self.start_time if hasattr(self, "start_time") else 0
                ),
            }
            extra_data.update(processing_data)

            # All headers into the payload
            extra_data["email_headers"] = email_headers

            # Write the outbox event - retry with backoff
            outbox_ok = False
            for attempt in range(1, 4):
                try:
                    kafka_result = kafka_client.queue_email_suppressed(
                        message_id=message_id,
                        rcpt=recipient_email,
                        domain_name=self.domain_name,
                        suppression_type=suppression_type,
                        suppression_reason=suppression_reason,
                        **extra_data,
                    )
                    if kafka_result:
                        self.logger.debug(
                            f"[KAFKA] SUPPRESSED event written to the outbox for {message_id} with {len(email_headers)} headers"
                        )
                        outbox_ok = True
                        break
                    else:
                        self.logger.warning(
                            f"[KAFKA] Outbox returned False (attempt {attempt}/3) SUPPRESSED message_id={message_id}"
                        )
                except Exception as retry_err:
                    self.logger.error(
                        f"[KAFKA] Error writing SUPPRESSED to the outbox (attempt {attempt}/3): {retry_err}"
                    )
                if attempt < 3:
                    time.sleep(attempt)  # backoff: 1s, 2s
            if not outbox_ok:
                self.logger.error(
                    f"[KAFKA] CRITICAL FAILURE: SUPPRESSED NOT written to the outbox after 3 attempts! "
                    f"message_id={message_id} rcpt={recipient_email}"
                )

        except Exception as e:
            self.logger.error(
                f"[KAFKA] Error writing the SUPPRESSED outbox event: {e}")
            import traceback

            self.logger.error(f"[KAFKA] Traceback: {traceback.format_exc()}")

    def _ensure_list_unsubscribe(self):
        """List-Unsubscribe + List-Unsubscribe-Post (RFC 8058, one-click).

        Gmail and Yahoo require one-click unsubscribe from bulk senders. The API
        receives ready-made HTML, so the milter adds the headers. The link is the
        same /u/{token} as {{unsub_link}}. Off by default in the code; docker compose
        turns it on with CONNECT_LIST_UNSUBSCRIBE=true. Never overrides a
        List-Unsubscribe that is already present.
        """
        if os.getenv("CONNECT_LIST_UNSUBSCRIBE", "false").lower() not in ("1", "true", "yes"):
            return
        if "list-unsubscribe" in self.headers or not self.message_id or not self.rcpts:
            return
        try:
            token = self._new_token(
                "unsubscribe", self.message_id, self.rcpts[0],
                campaign_id=self.campaign_id)
            if not token:
                return
            url = self._get_unsub_tracking_url(token)
            self.addheader("List-Unsubscribe", f"<{url}>")
            self.addheader("List-Unsubscribe-Post", "List-Unsubscribe=One-Click")
            self.headers.setdefault("list-unsubscribe", []).append(f"<{url}>")
        except Exception as e:  # the header never takes the message down
            self.logger.warning(f"List-Unsubscribe not added: {e}")

    def _ensure_feedback_id(self):
        """Feedback-ID (Gmail's complaint-rate reporting in Postmaster Tools).

        Format "campaign:mail:connect:<sender domain>": the first field groups by campaign
        (a bounded set, never a per-message value), the last one identifies the sender and
        is the From domain, the same one the DKIM signature carries. The header is signed
        (SignHeaders in opendkim.conf). A Feedback-ID already on the message is kept.
        Must only be called inside eom().
        """
        if self.headers.get("feedback-id") or not self.headers.get("from"):
            return
        from_domain = parseaddr(" ".join(self.headers["from"][-1].split()))[1].rpartition("@")[2].strip().lower()
        if not from_domain:
            return

        def _clean(value: str) -> str:
            return re.sub(r"[^A-Za-z0-9._-]", "-", str(value))[:40] or "none"

        value = f"{_clean(self.campaign_id or 'none')}:mail:connect:{_clean(from_domain)}"
        self.addheader("Feedback-ID", value)
        self.headers["feedback-id"] = [value]

    def _ensure_sender_header(self):
        """Add Sender = From when the message has no Sender.

        Without it, a relay that rewrites the envelope may add its own Sender header
        (e.g. "Sender: <local>=<domain>@relay.example.net"), and Outlook shows
        "... on behalf of <sender>", exposing a technical domain. Covers every Postfix
        input (including direct SMTP, which does not go through the API's /send).
        Must only be called inside eom().
        """
        if self.headers.get("sender") or not self.headers.get("from"):
            return
        # Unfold continuation lines (CRLF + WSP): addheader takes a single line.
        from_value = " ".join(self.headers["from"][-1].split())
        if not from_value:
            return
        self.addheader("Sender", from_value)
        self.headers["sender"] = [from_value]

    def header(self, name, value):
        """
        Process one email header.

        Called for each header of the email. Extracts important information
        such as the message ID and content type, and sets processing flags.

        Args:
            name: Header name
            value: Header value

        Returns:
            Milter.CONTINUE: continue processing if not expired
        """
        if self._expired():
            return Milter.CONTINUE

        # Do NOT create spans here: they are created in eoh(), once the API trace context is known

        lname = name.lower()
        self.headers.setdefault(lname, []).append(value)

        # Log important headers
        if lname in ["x-connect-id", "content-type", "subject", "message-id"]:
            self.logger.debug(
                f"Header {name}: {value[:100]}{'...' if len(value) > 100 else ''}"
            )

        # Sender's campaign_id (header X-Connect-Campaign): goes into the tracking
        # tokens and comes back in the open/click/unsubscribe events
        if lname == "x-connect-campaign":
            self.campaign_id = value.strip()
            self.logger.debug(f"[CAMPAIGN] Campaign ID extracted: {self.campaign_id}")

        # Capture and validate the From: header
        if lname == "from":
            # Address from the From header (may be "Name <email@domain.com>")
            from_addr = parseaddr(value)[1]  # parseaddr returns (name, email)
            if from_addr:
                self.header_from = from_addr
                # Domain of the From header
                if "@" in from_addr:
                    self.header_from_domain = from_addr.split("@")[-1].lower().strip()
                    self.logger.debug(
                        f"[HEADER_FROM] Header From: {from_addr} -> Domain: {self.header_from_domain}"
                    )

                    # Validate the From header when the API key has a domain_id
                    if self.api_key_domain_id:
                        # principal_domain_id not set in envfrom(): set it now
                        if not self.principal_domain_id:
                            try:
                                self.principal_domain_id = self.api_key_domain_id
                                # Name of the API key's domain
                                sql = """
                                    SELECT name
                                    FROM revbridge_connect.virtual_domains
                                    WHERE id = %s
                                """
                                api_key_domain_result = fetch_one(sql, (self.api_key_domain_id,))
                                if api_key_domain_result:
                                    api_key_domain_name = api_key_domain_result.get('name', '').strip()
                                    self.principal_domain_name = api_key_domain_name
                                    self.logger.debug(
                                        f"[HEADER_FROM] Principal domain set: {api_key_domain_name} (ID: {self.api_key_domain_id})"
                                    )
                            except Exception as e:
                                self.logger.debug(
                                    f"[HEADER_FROM] Error reading the principal domain name: {e}"
                                )

                        # Validate the From header with the principal domain and the API key domain_id
                        if self.principal_domain_id and self.principal_domain_name:
                            # First: does the sender belong to the API key's domain (or its subdomains)?
                            if sender_belongs_to_api_key_domain(self.header_from, self.api_key_domain_id):
                                # It does: normal case, no approved-sender check needed
                                self.logger.debug(
                                    f"[APPROVED_SENDER_VALIDATION] Header From ({self.header_from}) belongs to the API key's domain (ID: {self.api_key_domain_id}) - normal case, approved"
                                )
                            else:
                                # It does NOT: check whether it is an approved sender
                                self.logger.debug(
                                    f"[APPROVED_SENDER_VALIDATION] Header From ({self.header_from}) does NOT belong to the API key's domain (ID: {self.api_key_domain_id}) - checking approved senders"
                                )

                                # Is the From header an approved sender of this principal domain?
                                self.approved_sender_info = get_approved_sender_info(self.header_from, self.principal_domain_id)
                                if self.approved_sender_info:
                                    # The approved sender must belong to the envelope from's principal domain
                                    approved_principal_id = self.approved_sender_info.get('principal_domain_id')
                                    if approved_principal_id == self.principal_domain_id:
                                        self.logger.info(
                                            f"[APPROVED_SENDER_VALIDATION] Valid header From: {self.header_from} is an approved sender of principal domain {self.principal_domain_name} (envelope from: {self.envelope_from_domain})"
                                        )
                                    else:
                                        self.logger.warning(
                                            f"[APPROVED_SENDER_VALIDATION] Header From ({self.header_from}) is an approved sender but does not belong to the envelope from's principal domain ({self.principal_domain_name}, ID: {self.principal_domain_id})"
                                        )
                                        self.setreply(
                                            "550",
                                            "5.7.1",
                                            f"Header From ({self.header_from}) is approved sender but does not belong to envelope from principal domain ({self.principal_domain_name})",
                                        )
                                        return Milter.REJECT
                                else:
                                    # Neither an approved sender nor part of the API key's domain
                                    self.logger.warning(
                                        f"[APPROVED_SENDER_VALIDATION] Header From ({self.header_from}) does not belong to the API key's domain and is not an approved sender of principal domain ({self.principal_domain_name})"
                                    )
                                    self.setreply(
                                        "550",
                                        "5.7.1",
                                        f"Header From ({self.header_from}) must belong to API key domain or be an approved sender for principal domain ({self.principal_domain_name})",
                                    )
                                    return Milter.REJECT

        # Capture the API trace headers (distributed tracing)
        # The API adds these headers when it sends
        if lname == "x-trace-id":
            self._api_trace_id = value.strip()
            self.logger.debug(f"[OTEL] X-Trace-Id header captured from the API: {self._api_trace_id}")
        elif lname == "x-parent-span-id":
            self._api_parent_span_id = value.strip()
            self.logger.debug(f"[OTEL] X-Parent-Span-Id header captured from the API: {self._api_parent_span_id}")
        elif lname == "x-trace-origin":
            self._api_trace_origin = value.strip()
            self.logger.debug(f"[OTEL] X-Trace-Origin header captured from the API: {self._api_trace_origin}")

        # 1) Use X-Connect-ID when present (primary tracking ID)
        if lname == "x-connect-id" and not self.message_id:
            self.message_id = value.strip().strip("<>").strip()
            self.logger.debug(f"Using X-Connect-ID: {self.message_id}")

        # 2) Fallback: the standard Message-Id
        if lname == "message-id" and not self.message_id:
            self.message_id = value.strip().strip("<>").strip()
            # An empty Message-ID: generate a new one in eom
            if not self.message_id:
                self.logger.debug(
                    "Message-ID header found but empty")
                self.message_id = None  # reset, so eom generates a new one
            else:
                self.logger.debug(f"Using Message-ID: {self.message_id}")

        if lname == "content-transfer-encoding":
            self.global_content_transfer_encoding = value.strip()
            self.logger.debug(
                f"[CONTENT_TRANSFER_ENCODING] Global Content-Transfer-Encoding: "
                f"{self.global_content_transfer_encoding}"
            )

        # Content-Type header
        if lname == "content-type":
            vlow = value.lower()

            # Detect multipart MIME messages
            if "multipart/" in vlow:
                self.is_multipart = True
                # Main MIME type (e.g. multipart/alternative)
                mime_match = re.search(r'^([^;]+)', value, flags=re.I)
                if mime_match:
                    self.global_mime_type = mime_match.group(1).strip().lower()
                    self.logger.debug(
                        f"[CONTENT_TYPE] Main MIME type detected: {self.global_mime_type}"
                    )

                # boundary parameter (quoted or not)
                import re as _re

                m = _re.search(
                    r'boundary\s*=\s*("?)([^";]+)\1', value, flags=_re.I)

                if m:
                    boundary_raw = m.group(2).strip()
                    has_prefix = boundary_raw.startswith("--")
                    clean_boundary = boundary_raw[2:
                                                  ] if has_prefix else boundary_raw

                    self.boundary_had_prefix = has_prefix
                    self.boundary_normalized = not has_prefix
                    self.multipart_boundary_original = (
                        f"--{boundary_raw}" if boundary_raw else None
                    )
                    self.multipart_boundary = (
                        f"--{clean_boundary}" if clean_boundary else None
                    )

                    if has_prefix and clean_boundary:
                        quote = m.group(1) or ""
                        normalized_param = f"boundary={quote}{clean_boundary}{quote}"
                        new_value = (
                            value[: m.start()] + normalized_param +
                            value[m.end():]
                        )

                        # Keep what is needed to normalize it later, in eom()
                        # (chgheader() can only be called during the eom() callback)
                        self._pending_boundary_normalization = {
                            "header_name": name,
                            "new_value": new_value,
                            "original_value": value,
                        }
                        self.boundary_normalized = False  # True after the normalization in eom()
                        self.logger.debug(
                            f"Boundary with a '--' prefix detected, normalization pending for eom(): {boundary_raw}"
                        )
                    elif not clean_boundary:
                        # Invalid boundary: keep the original
                        self.multipart_boundary = self.multipart_boundary_original
                        self.boundary_normalized = False
                else:
                    self.multipart_boundary = None
                    self.multipart_boundary_original = None
                    self.boundary_had_prefix = False
                    self.boundary_normalized = False

                if self.multipart_boundary:
                    self.logger.debug(
                        f"MULTIPART detected; boundary={self.multipart_boundary}"
                    )

            # Detect HTML content
            if "text/html" in vlow:
                self.is_html = True
                self.logger.debug(
                    "[CONTENT_TYPE] HTML detected in the global Content-Type"
                )

        return Milter.CONTINUE

    def eoh(self):
        """
        Handle the end-of-headers event.

        Called once all headers have been processed.

        Returns:
            Milter.CONTINUE: continue processing
        """
        # Get the queue_id if available (it may not be available yet in eoh)
        if not self.queue_id:
            try:
                queue_id = self.getsymval("i")
                if queue_id:
                    self.queue_id = queue_id
            except Exception:
                pass  # the queue_id may not be available yet

        # ========================================================================
        # CREATE THE SPANS HERE - the API trace context (if any) is now known
        # ========================================================================
        if OTEL_AVAILABLE:
            try:
                # Is there an API trace context?
                has_api_trace = hasattr(self, '_api_trace_id') and self._api_trace_id

                if has_api_trace:
                    # The email came from the API: create the spans as children of the API trace
                    self.logger.debug(f"[OTEL] Creating spans as children of the API trace: trace_id={self._api_trace_id}")

                    # Convert trace_id and span_id to int
                    try:
                        api_trace_id = int(self._api_trace_id)
                        api_span_id = int(self._api_parent_span_id) if self._api_parent_span_id else 0
                    except (ValueError, TypeError) as e:
                        self.logger.warning(f"[OTEL] Error converting trace_id/span_id: {e}")
                        api_trace_id = None
                        api_span_id = 0

                    # Parent context from the API trace_id
                    if api_trace_id:
                        parent_ctx = SpanContext(
                            trace_id=api_trace_id,
                            span_id=api_span_id,
                            is_remote=True,
                            trace_flags=TraceFlags(TraceFlags.SAMPLED),
                        )
                        parent_span = NonRecordingSpan(parent_ctx)
                        ctx = trace.set_span_in_context(parent_span)

                        # mail.pipeline as a child of the API trace
                        mail_pipeline_span = otel_tracer.start_span("mail.pipeline", context=ctx)
                        self.logger.debug(f"[OTEL] mail.pipeline created as a child of the API trace (trace_id={api_trace_id})")
                    else:
                        # Fallback: mail.pipeline without a parent (new trace)
                        mail_pipeline_span = otel_tracer.start_span("mail.pipeline")
                        self.logger.debug("[OTEL] mail.pipeline created as a new trace (invalid API context)")
                else:
                    # Direct SMTP email: new trace
                    mail_pipeline_span = otel_tracer.start_span("mail.pipeline")
                    self.logger.debug("[OTEL] mail.pipeline created as a new trace (direct SMTP email)")

                # Tags on mail.pipeline
                if hasattr(self, '_connect_hostname'):
                    mail_pipeline_span.set_attribute("mail.hostname", self._connect_hostname)
                if hasattr(self, '_connect_family'):
                    mail_pipeline_span.set_attribute("mail.family", self._connect_family)
                if hasattr(self, '_connect_hostaddr'):
                    mail_pipeline_span.set_attribute("mail.hostaddr", self._connect_hostaddr)
                if hasattr(self, '_connect_port') and self._connect_port:
                    mail_pipeline_span.set_attribute("mail.port", self._connect_port)

                # Keep the root span
                self._mail_pipeline_span = mail_pipeline_span
                self._trace_root_span = mail_pipeline_span
                self._trace_span = mail_pipeline_span

                # smtp.envelope as a child of mail.pipeline
                pipeline_ctx = trace.set_span_in_context(mail_pipeline_span)

                smtp_envelope_span = otel_tracer.start_span("smtp.envelope", context=pipeline_ctx)
                if hasattr(self, '_envelope_mailfrom'):
                    smtp_envelope_span.set_attribute("smtp.mailfrom", self._envelope_mailfrom)
                if self.rcpts:
                    smtp_envelope_span.set_attribute("smtp.rcpt_count", len(self.rcpts))
                    if len(self.rcpts) > 0:
                        smtp_envelope_span.set_attribute("smtp.first_rcpt", str(self.rcpts[0]))
                self._smtp_envelope_span = smtp_envelope_span
                self.logger.debug("[OTEL] smtp.envelope created as a child of mail.pipeline")

                # policy.checks as a child of smtp.envelope
                envelope_ctx = trace.set_span_in_context(smtp_envelope_span)
                policy_checks_span = otel_tracer.start_span("policy.checks", context=envelope_ctx)
                policy_checks_span.set_attribute("policy.check_type", "api_key_validation")
                if hasattr(self, 'api_key_id') and self.api_key_id:
                    policy_checks_span.set_attribute("policy.api_key_validated", True)
                policy_checks_span.end()  # finish right away
                self.logger.debug("[OTEL] policy.checks created and finished")

                # Finish smtp.envelope
                smtp_envelope_span.end()
                self._smtp_envelope_span = None
                self.logger.debug("[OTEL] smtp.envelope finished")

                # smtp.headers as a child of mail.pipeline
                smtp_headers_span = otel_tracer.start_span("smtp.headers", context=pipeline_ctx)
                if self.message_id:
                    smtp_headers_span.set_attribute("smtp.message_id", str(self.message_id))
                if self.is_multipart:
                    smtp_headers_span.set_attribute("smtp.is_multipart", True)
                if self.is_html:
                    smtp_headers_span.set_attribute("smtp.is_html", True)
                if hasattr(self, 'headers') and self.headers:
                    header_count = sum(len(v) for v in self.headers.values())
                    smtp_headers_span.set_attribute("smtp.header_count", header_count)
                self._smtp_headers_span = smtp_headers_span
                # Finish right away: the headers phase is over
                smtp_headers_span.end()
                self._smtp_headers_span = None
                self.logger.debug("[OTEL] smtp.headers created and finished")

                self.logger.debug("[OTEL] Spans created in eoh() - mail.pipeline, smtp.envelope, policy.checks, smtp.headers")

            except Exception as e:
                self.logger.warning(f"[OTEL] Error creating spans in eoh(): {e}", exc_info=True)
                self._mail_pipeline_span = None
                self._trace_root_span = None
                self._trace_span = None

        # Safety net: make sure smtp.headers is finished (the block above already
        # finishes it). Finishing it at the end of eoh() keeps smtp.headers from
        # enclosing the body and mail.message.
        if OTEL_AVAILABLE and hasattr(self, '_smtp_headers_span') and self._smtp_headers_span:
            try:
                # Final tags on smtp.headers
                if self.message_id:
                    self._smtp_headers_span.set_attribute("smtp.message_id", str(self.message_id))
                if self.is_multipart:
                    self._smtp_headers_span.set_attribute("smtp.is_multipart", True)
                if self.is_html:
                    self._smtp_headers_span.set_attribute("smtp.is_html", True)
                if hasattr(self, 'headers') and self.headers:
                    header_count = sum(len(v) for v in self.headers.values())
                    self._smtp_headers_span.set_attribute("smtp.header_count", header_count)
                self._smtp_headers_span.end()
                self._smtp_headers_span = None
                self.logger.debug("[OTEL] Span smtp.headers finished at the end of eoh() - headers phase over")
            except Exception as e:
                self.logger.warning(f"[OTEL] Error finishing smtp.headers: {e}", exc_info=True)

        return Milter.CONTINUE

    def body(self, chunk):
        """
        Process chunks of the email body.

        Buffers the body when it is needed for link rewriting or pixel
        injection, with size limits to avoid memory problems.

        Args:
            chunk: A chunk of the email body

        Returns:
            Milter.CONTINUE: continue processing
        """
        # Span for the body, only on the first call, as a child of mail.pipeline
        if not hasattr(self, '_body_span_created'):
            if OTEL_AVAILABLE and self._mail_pipeline_span:
                try:
                    pipeline_ctx = trace.set_span_in_context(self._mail_pipeline_span)
                    span = otel_tracer.start_span("milter.body", context=pipeline_ctx)
                    span.set_attribute("milter.is_multipart", self.is_multipart)
                    span.set_attribute("milter.is_html", self.is_html)
                    # Close the span right away: body is called many times
                    span.end()
                    self._body_span_created = True
                    self.logger.debug("[OTEL] Span milter.body created as a child of mail.pipeline")
                except Exception as e:
                    self.logger.warning(f"[OTEL] Error creating span milter.body: {e}")
                    self._body_span_created = True  # mark as created so it is not retried

        # Buffer when multipart (to find the HTML part), when the features are
        # enabled, or when the global Content-Type is HTML.
        # Global values are the fallback while there is no domain configuration yet
        should_rewrite = self._should_rewrite_links() if hasattr(
            self, 'email_config') else REWRITE_LINKS
        should_inject = self._should_inject_pixel() if hasattr(
            self, 'email_config') else INJECT_PIXEL
        if self.is_multipart or self.is_html or should_rewrite or should_inject:
            if self.email_size + len(chunk) <= self.MAX_BUFFER_BYTES:
                self.body_chunks.append(chunk)
                self.email_size += len(chunk)
            else:
                self.logger.warning(
                    "BODY too large; no longer buffering to avoid excess memory use"
                )

        return Milter.CONTINUE

    def _admit_send_ledger(self):
        """Admit EVERY recipient of this message into send_ledger (durability).

        Universal: covers app sends AND direct SMTP (the milter sees them all). Runs
        in eom, once Postfix has accepted the message (queue_id assigned) -> state
        'smtp_accepted' + delivery_deadline.

        BEST-EFFORT / FAIL-OPEN: the milter is on the critical SMTP path; a ledger
        failure must never block or delay delivery. Every exception is only
        logged. domain_id is best-effort (the column accepts NULL).
        """
        try:
            domain_id = (
                get_domain_id(self.domain_name)
                if self.domain_name and self.domain_name != "unknown"
                else None
            )
        except Exception:
            domain_id = None
        for rcpt in self.rcpts:
            try:
                exec_sql(
                    SEND_LEDGER_ADMIT_SQL,
                    (
                        self.message_id,
                        rcpt.lower(),
                        self.domain_name,
                        domain_id,
                        self.queue_id,
                        SEND_LEDGER_DELIVERY_DEADLINE_SEC,
                    ),
                )
            except Exception as e:
                self.logger.warning(
                    f"[LEDGER] admit failed {self.message_id}->{rcpt}: {e}"
                )

    def eom(self):
        """
        Handle the end-of-message event.

        The main processing method, called once the complete email has
        been received. It does:

        - storage of the tracking data in Redis
        - link rewriting with tracking tokens
        - pixel injection for open tracking
        - message changes and added headers

        Returns:
            Milter.CONTINUE: continue processing if not expired
        """
        if self._expired():
            return Milter.CONTINUE

        # Main span for eom
        if OTEL_AVAILABLE:
            try:
                # Finish the previous span, if any (eom replaces envrcpt as the current span)
                if hasattr(self, '_trace_span') and self._trace_span and self._trace_span != self._trace_root_span:
                    try:
                        self._trace_span.end()
                        self._trace_span = None
                        self.logger.debug("[OTEL] Previous span finished before creating eom")
                    except Exception:
                        pass

                # trace_id of mail.pipeline (root span), to store in Redis
                # The span_id is taken from mail.message (anchor span) once it is created
                trace_id = None
                try:
                    root_span = self._mail_pipeline_span if hasattr(self, '_mail_pipeline_span') and self._mail_pipeline_span else (self._trace_root_span if hasattr(self, '_trace_root_span') and self._trace_root_span else None)

                    if root_span:
                        span_ctx = root_span.get_span_context()
                        if span_ctx and span_ctx.trace_id:
                            trace_id = str(span_ctx.trace_id)
                            self.logger.debug(f"[OTEL] Trace ID taken from mail.pipeline: {trace_id}")
                        else:
                            trace_id = None
                            if OTEL_TRACES_ACTIVE:
                                self.logger.warning("[OTEL] Could not get the trace_id from the span context")
                            else:
                                self.logger.debug("[OTEL] No trace_id (tracing disabled by design)")
                    else:
                        trace_id = None
                        self.logger.warning("[OTEL] mail.pipeline span not found to get the trace_id")

                except Exception as e:
                    trace_id = None
                    self.logger.warning(f"[OTEL] Error getting the trace_id: {e}")
            except Exception as e:
                self.logger.error(f"[OTEL] Error creating span milter.eom: {e}", exc_info=True)
                self._trace_span = None
                trace_id = None
        else:
            self._trace_span = None
            trace_id = None
            self.logger.debug("[OTEL] Tracing not available - eom span not created")

        try:
            # 'i' = queue_id (Postfix macro available in eom)
            self.queue_id = self.getsymval("i")

            # Span tags, if any
            if OTEL_AVAILABLE and self._trace_span:
                if self.queue_id:
                    self._trace_span.set_attribute("milter.queue_id", str(self.queue_id))
                if self.message_id:
                    self._trace_span.set_attribute("milter.message_id", str(self.message_id))
                    # Standard tag without prefix, for search
                    self._trace_span.set_attribute("message_id", str(self.message_id))
                if self.domain_name:
                    self._trace_span.set_attribute("milter.domain_name", str(self.domain_name))
                if trace_id:
                    self._trace_span.set_attribute("milter.trace_id", trace_id)
                    # Standard tag without prefix, for correlation
                    self._trace_span.set_attribute("trace_id", str(trace_id))
                    # Unified correlation tag: finds all related traces
                    self._trace_span.set_attribute("correlation_id", str(trace_id))

                # Recipient tags, for search (all recipients)
                if self.rcpts:
                    # The first recipient is the main search tag
                    first_rcpt = self.rcpts[0]
                    self._trace_span.set_attribute("milter.rcpt", str(first_rcpt))
                    # Standard tag without prefix, for search
                    self._trace_span.set_attribute("rcpt", str(first_rcpt))
                    # All recipients in a separate tag (several recipients)
                    if len(self.rcpts) > 1:
                        self._trace_span.set_attribute("milter.rcpt_count", len(self.rcpts))
                        self._trace_span.set_attribute("milter.rcpts", ",".join(self.rcpts))

            # The same tags on the root span mail.pipeline, so the main trace is searchable
            if OTEL_AVAILABLE and hasattr(self, '_mail_pipeline_span') and self._mail_pipeline_span:
                if self.queue_id:
                    self._mail_pipeline_span.set_attribute("mail.queue_id", str(self.queue_id))
                if self.message_id:
                    self._mail_pipeline_span.set_attribute("mail.message_id", str(self.message_id))
                    # Standard tag without prefix, for search
                    self._mail_pipeline_span.set_attribute("message_id", str(self.message_id))
                if self.domain_name:
                    self._mail_pipeline_span.set_attribute("mail.domain_name", str(self.domain_name))
                if self.rcpts:
                    # The first recipient is the main search tag
                    first_rcpt = self.rcpts[0]
                    self._mail_pipeline_span.set_attribute("mail.rcpt", str(first_rcpt))
                    # Standard tag without prefix, for search
                    self._mail_pipeline_span.set_attribute("rcpt", str(first_rcpt))
                    if len(self.rcpts) > 1:
                        self._mail_pipeline_span.set_attribute("mail.rcpt_count", len(self.rcpts))
                        self._mail_pipeline_span.set_attribute("mail.rcpts", ",".join(self.rcpts))
                if trace_id:
                    self._mail_pipeline_span.set_attribute("correlation_id", str(trace_id))

            # Normalize the boundary in the Content-Type header, if pending
            # (chgheader() can only be called during the eom() callback)
            if self._pending_boundary_normalization:
                pending = self._pending_boundary_normalization
                can_change_header = bool(
                    hasattr(self, "_actions")
                    and (self._actions & getattr(Milter, "CHGHDRS", 0))
                )

                if can_change_header:
                    header_name = pending["header_name"]
                    new_value = pending["new_value"]
                    lname = header_name.lower()

                    # Indexes of the Content-Type header
                    header_len = len(self.headers.get(lname, []))
                    header_indices = []
                    if header_len > 0:
                        header_indices.append(header_len - 1)  # 0-based index
                        header_indices.append(header_len)      # 1-based index
                    else:
                        header_indices.append(0)

                    header_updated = False
                    last_error = None
                    for header_index in header_indices:
                        try:
                            self.logger.debug(
                                f"[EOM] Trying to normalize the boundary with index {header_index}"
                            )
                            self.chgheader(header_name, header_index, new_value)
                            self.headers[lname][-1] = new_value
                            self.boundary_normalized = True
                            header_updated = True
                            self.logger.debug(
                                f"[EOM] Boundary normalized in the Content-Type header: {new_value}"
                            )
                            break
                        except Exception as chg_error:
                            last_error = chg_error
                            self.logger.debug(
                                f"[EOM] Failed to normalize the boundary with index {header_index}: {chg_error}"
                            )

                    if not header_updated:
                        if last_error is not None:
                            self.logger.warning(
                                f"[EOM] Failed to normalize the boundary in the Content-Type header: {last_error}",
                                exc_info=True,
                            )
                        self.multipart_boundary = self.multipart_boundary_original
                        self.boundary_normalized = False
                else:
                    self.logger.debug(
                        "[EOM] Boundary normalization not applied - CHGHDRS not available"
                    )
                    self.multipart_boundary = self.multipart_boundary_original
                    self.boundary_normalized = False

                # Clear the pending normalization
                self._pending_boundary_normalization = None

            # Change the envelope_from dynamically when needed.
            # This happens in eom(), once all recipients are known: the dynamic
            # envelope_from is always generated when there is an api_key_id and recipients
            if self.api_key_id and self.rcpts:
                # Build the envelope_from from the first recipient
                # (for several recipients a copy per recipient would be possible; for
                # simplicity the first one is used). api_key_domain_id is passed to avoid
                # another API key lookup (already done in envfrom())
                first_rcpt = self.rcpts[0]

                # api_key_domain_id should be available (set in envfrom())
                if not self.api_key_domain_id:
                    self.logger.warning(
                        f"[EOM] api_key_domain_id not available for API key {self.api_key_id[:20]}... - "
                        f"the API key will be looked up again (may fail with a permission error)"
                    )
                
                self.envelope_from = generate_envelope_from(
                    self.api_key_id,
                    first_rcpt,
                    api_key_domain_id=self.api_key_domain_id
                )
                
                if self.envelope_from:
                    self.logger.debug(
                        f"[EOM] Envelope from generated dynamically: {self.envelope_from} for rcpt {first_rcpt}"
                    )

                    # Apply the dynamic envelope_from with chgfrom() in eom()
                    try:
                        # Is chgfrom available?
                        if hasattr(self, 'chgfrom'):
                            result = self.chgfrom(self.envelope_from)
                            self.logger.debug(
                                f"[EOM] chgfrom() called with {self.envelope_from}, result: {result}"
                            )
                        elif hasattr(self, 'changeenvfrom'):
                            # Some implementations use changeenvfrom
                            result = self.changeenvfrom(self.envelope_from)
                            self.logger.debug(
                                f"[EOM] changeenvfrom() called with {self.envelope_from}, result: {result}"
                            )
                        else:
                            self.logger.warning(
                                f"[EOM] chgfrom() not available in the milter. "
                                f"The dynamic envelope_from ({self.envelope_from}) will not be applied to SMTP."
                            )
                    except Exception as e:
                        self.logger.error(
                            f"[EOM] Error changing the envelope_from with chgfrom(): {e}",
                            exc_info=True
                        )

                    # Update domain_name from the generated envelope_from
                    if "@" in self.envelope_from:
                        rbm_domain = self.envelope_from.split("@")[-1].lower().strip()
                        self.envelope_from_domain = rbm_domain
                        if not self.domain_name or self.domain_name == "unknown":
                            self.domain_name = rbm_domain

                        # [WARMUP_DIVISOR] Data plane: decides whether the send leaves through
                        # a pool IP in warmup (Gmail/Yahoo, not Microsoft, SPF ok, IP under its
                        # cap). Sends are PER RECIPIENT (rcpts=1), so the decision uses the
                        # first rcpt's domain. dry_run => only logs; active => adds the
                        # X-Warmup-Route header (the MTA routes it with a header_checks FILTER
                        # and strips it on the way out) and increments the cap counter. The
                        # try/except never breaks the send.
                        if _WARMUP_DIVISOR_ENABLED and self.rcpts:
                            try:
                                from mail_milter.warmup.loader import evaluate, mark_routed
                                _rd = next(
                                    (r.split("@")[-1].lower() for r in self.rcpts
                                     if isinstance(r, str) and "@" in r),
                                    None,
                                )
                                if _rd:
                                    _dec = evaluate(
                                        self.envelope_from_domain, _rd,
                                        fetch_all=fetch_all, redis=redis,
                                    )
                                    _mode = "dry_run" if _WARMUP_DIVISOR_DRY_RUN else "route"
                                    # MODULE logger, not self.logger: the parent "milter"
                                    # logger is set to WARNING in main() and would drop INFO.
                                    logger.info(
                                        f"[WARMUP_DIVISOR] {_mode} env={self.envelope_from_domain} "
                                        f"rcpt_domain={_rd} route={_dec.route} ip={_dec.ip} "
                                        f"relay={_dec.relay_host} reason={_dec.reason}"
                                    )
                                    if _dec.route and not _WARMUP_DIVISOR_DRY_RUN:
                                        self.addheader("X-Warmup-Route", _dec.relay_host)
                                        mark_routed(_dec.ip, redis)
                            except Exception as _e:
                                logger.warning(f"[WARMUP_DIVISOR] error ignored: {_e}")

                        # principal_domain_id from the API key's domain_id
                        if self.api_key_domain_id:
                            # The api_key_domain_id is the principal_domain_id
                            if not self.principal_domain_id:
                                self.principal_domain_id = self.api_key_domain_id
                                # Name of the API key's domain, if not known yet
                                if not self.principal_domain_name:
                                    sql = """
                                        SELECT name
                                        FROM revbridge_connect.virtual_domains
                                        WHERE id = %s
                                    """
                                    api_key_domain_result = fetch_one(sql, (self.api_key_domain_id,))
                                    if api_key_domain_result:
                                        api_key_domain_name = api_key_domain_result.get('name', '').strip()
                                        self.principal_domain_name = api_key_domain_name
                                        self.logger.debug(
                                            f"[EOM] Principal domain set: {api_key_domain_name} (ID: {self.api_key_domain_id})"
                                        )
                            # Use the envelope_from domain as principal_domain_name, if available
                            if self.envelope_from_domain and not self.principal_domain_name:
                                self.principal_domain_name = self.envelope_from_domain
                else:
                    self.logger.warning(
                        f"[EOM] Could not generate the envelope_from for API key {self.api_key_id[:20]}... and rcpt {first_rcpt}"
                    )

            # Log the start of processing
            processing_time = time.time() - self.start_time
            self.logger.debug(
                f"Processing email - Queue ID: {self.queue_id}, Size: {self.email_size} bytes, Time: {processing_time:.3f}s"
            )

            # Final span tags, if any
            if OTEL_AVAILABLE and self._trace_span:
                self._trace_span.set_attribute("milter.rcpt_count", len(self.rcpts))
                self._trace_span.set_attribute("milter.email_size", self.email_size)
                self._trace_span.set_attribute("milter.processing_time_ms", int(processing_time * 1000))
                tracking_enabled = self._is_tracking_enabled()
                rewrite_links = self._should_rewrite_links()
                inject_pixel = self._should_inject_pixel()
                self._trace_span.set_attribute("milter.tracking_enabled", tracking_enabled)
                self._trace_span.set_attribute("milter.rewrite_links", rewrite_links)
                self._trace_span.set_attribute("milter.inject_pixel", inject_pixel)

            # Fallback: generate a message_id when none was found
            if not self.message_id and self.queue_id:
                # Base the unique message_id on the queue_id
                raw_message_id = f"rbm-{self.queue_id}-{int(time.time())}"
                # Add a domain to make it valid
                self.message_id = add_domain_to_message_id(raw_message_id)
                self.logger.debug(f"Message ID generated: {self.message_id}")

            # Does the email need a Message-ID header?
            has_message_id_header = (
                "message-id" in self.headers and self.headers["message-id"]
            )
            has_empty_message_id = False

            # Does the Message-ID exist but is empty?
            if has_message_id_header:
                existing_message_id = (
                    self.headers["message-id"][-1].strip().strip("<>").strip()
                )
                has_empty_message_id = (
                    not existing_message_id or existing_message_id == ""
                )

            if self.message_id and (not has_message_id_header or has_empty_message_id):
                # Add a Message-ID header when missing or empty,
                # with a domain and the right format
                message_id_header = add_domain_to_message_id(self.message_id)
                self.addheader("Message-ID", message_id_header)
                # Keep self.headers in sync with the added header
                self.headers.setdefault("message-id", []).append(message_id_header)
                self.logger.debug(
                    f"Message-ID header added: {message_id_header}")

            self._ensure_sender_header()
            self._ensure_list_unsubscribe()
            self._ensure_feedback_id()

            # Span mail.message (anchor), when there is a message_id, as a child of mail.pipeline
            # This is the anchor span: every asynchronous span (events-updater) must be its child
            anchor_span_id = None
            self.logger.debug(f"[OTEL] Checking mail.message creation - OTEL_AVAILABLE: {OTEL_AVAILABLE}, message_id: {self.message_id}, _mail_message_span exists: {hasattr(self, '_mail_message_span')}")
            if OTEL_AVAILABLE and self.message_id:
                try:
                    # Create or reuse the mail.message span
                    if not self._mail_message_span:
                        # Use the mail.pipeline context, to create it as a child
                        if self._mail_pipeline_span:
                            pipeline_ctx = trace.set_span_in_context(self._mail_pipeline_span)
                            mail_message_span = otel_tracer.start_span("mail.message", context=pipeline_ctx)
                        else:
                            # Fallback: create it without a context
                            mail_message_span = otel_tracer.start_span("mail.message")
                            self.logger.debug(f"[OTEL] Span mail.message created (without mail.pipeline) - message_id: {self.message_id}")

                        mail_message_span.set_attribute("mail.message_id", str(self.message_id))
                        mail_message_span.set_attribute("message_id", str(self.message_id))
                        if self.domain_name:
                            mail_message_span.set_attribute("mail.domain_name", str(self.domain_name))
                        if self.rcpts:
                            first_rcpt = self.rcpts[0]
                            mail_message_span.set_attribute("mail.rcpt", str(first_rcpt))
                            mail_message_span.set_attribute("rcpt", str(first_rcpt))
                        # anchor_span_id (the mail.message span_id), for correlation
                        span_ctx = mail_message_span.get_span_context()
                        if span_ctx and span_ctx.span_id:
                            anchor_span_id = str(span_ctx.span_id)
                            mail_message_span.set_attribute("mail.anchor_span_id", anchor_span_id)
                            self.logger.debug(f"[OTEL] Span mail.message (anchor) created - span_id: {anchor_span_id}, message_id: {self.message_id}")
                        self._mail_message_span = mail_message_span
                        self.logger.debug(f"[OTEL] Span mail.message created as a child of mail.pipeline - message_id: {self.message_id}")
                    else:
                        # It already exists: take its span_id
                        self.logger.debug("[OTEL] mail.message already exists, reading its span_id...")
                        span_ctx = self._mail_message_span.get_span_context()
                        if span_ctx and span_ctx.span_id:
                            anchor_span_id = str(span_ctx.span_id)
                            self.logger.debug(f"[OTEL] Using the existing mail.message span - span_id: {anchor_span_id}")
                        else:
                            self.logger.warning("[OTEL] mail.message exists but has no span_id!")
                except Exception as e:
                    self.logger.warning(f"[OTEL] Error creating span mail.message: {e}", exc_info=True)

            # Persist the (qid, rcpt) -> message_id mapping in Redis
            if self.queue_id and self.message_id and self.rcpts:
                # Universal send_ledger admission (durability): records every
                # recipient of this message, for app sends AND direct SMTP.
                # Best-effort/fail-open, independent of Redis.
                self._admit_send_ledger()
                # Is Redis available?
                if redis is None:
                    self.logger.error(
                        "[REDIS_SAVE] Redis not available - the mapping will not be saved"
                    )
                else:
                    self.logger.debug(
                        f"[REDIS_SAVE] Saving mapping - queue_id: {self.queue_id}, message_id: {self.message_id}, recipients: {self.rcpts}"
                    )
                    for rcpt in self.rcpts:
                        # queue_id -> message_id mapping key
                        mapping_key = f"q:{self.queue_id}:{rcpt.lower()}"

                        try:
                            # Main mapping (queue_id, rcpt) -> message_id
                            mapping_data = {
                                "message_id": self.message_id,
                                "rcpt": rcpt.lower(),
                                "domain_name": self.domain_name,
                                "queue_id": self.queue_id,
                            }
                            # trace_id and span_id, for distributed tracing
                            # PRIORITY: the API trace (X-Trace-* headers), when present,
                            # so the trace starts in the API and continues in the milter
                            effective_trace_id = None
                            effective_span_id = None
                            trace_origin = "smtp_direct"  # default: direct SMTP email

                            if hasattr(self, '_api_trace_id') and self._api_trace_id:
                                # The email came from the API: use the API trace context
                                effective_trace_id = self._api_trace_id
                                effective_span_id = getattr(self, '_api_parent_span_id', None)
                                trace_origin = getattr(self, '_api_trace_origin', None) or "api"
                                self.logger.info(
                                    f"[OTEL] Using the API trace context: trace_id={effective_trace_id}, "
                                    f"span_id={effective_span_id}, origin={trace_origin}"
                                )
                                # Correlation tags on the milter span
                                if self._mail_pipeline_span:
                                    self._mail_pipeline_span.set_attribute("trace.api_trace_id", effective_trace_id)
                                    self._mail_pipeline_span.set_attribute("trace.origin", trace_origin)
                                    self._mail_pipeline_span.set_attribute("trace.continued_from_api", True)
                            elif trace_id:
                                # Direct SMTP email: use the milter trace
                                effective_trace_id = trace_id
                                self.logger.debug(f"[OTEL] Using the milter trace: trace_id={effective_trace_id}")
                                if self._mail_pipeline_span:
                                    self._mail_pipeline_span.set_attribute("trace.origin", "smtp_direct")
                                    self._mail_pipeline_span.set_attribute("trace.continued_from_api", False)

                            if effective_trace_id:
                                mapping_data["trace_id"] = effective_trace_id
                                mapping_data["trace_origin"] = trace_origin
                                self.logger.debug(f"[OTEL] Trace ID {effective_trace_id} (origin: {trace_origin}) added to the Redis mapping")

                                # sampling_priority in the mapping, kept for compatibility with the events-updater
                                # (with OTel, sampling is controlled by TraceFlags)
                                mapping_data["sampling_priority"] = 1  # Always sampled
                                self.logger.debug("[OTEL] sampling_priority=1 (always sampled) added to the Redis mapping")
                            else:
                                self.logger.debug("[OTEL] Trace ID not available - not added to the mapping")

                            # span_id for propagation
                            # PRIORITY: the API span_id, when present, is the parent;
                            # otherwise the mail.message anchor_span_id
                            effective_span_id_for_redis = None

                            if effective_span_id:
                                # Span ID from the API: the events-updater creates spans as children of the API
                                effective_span_id_for_redis = effective_span_id
                                self.logger.debug(f"[OTEL] Using the API span_id: {effective_span_id_for_redis}")
                            else:
                                # Use the anchor_span_id (mail.message) instead of the mail.pipeline span_id:
                                # mail.message is the anchor span, every asynchronous span must be its child
                                if not anchor_span_id and hasattr(self, '_mail_message_span') and self._mail_message_span:
                                    try:
                                        span_ctx = self._mail_message_span.get_span_context()
                                        if span_ctx and span_ctx.span_id:
                                            anchor_span_id = str(span_ctx.span_id)
                                            self.logger.debug(f"[OTEL] Anchor span ID taken from the existing mail.message: {anchor_span_id}")
                                    except Exception as e:
                                        self.logger.warning(f"[OTEL] Error getting the mail.message span_id: {e}")
                                effective_span_id_for_redis = anchor_span_id

                            if effective_span_id_for_redis:
                                mapping_data["span_id"] = effective_span_id_for_redis
                                mapping_data["anchor_span_id"] = effective_span_id_for_redis  # extra key, for clarity
                                self.logger.debug(f"[OTEL] Span ID {effective_span_id_for_redis} added to the Redis mapping (distributed tracing)")
                                self.logger.debug(f"[OTEL] Trace ID {effective_trace_id} and span ID {effective_span_id_for_redis} saved in Redis for correlation with the events-updater")
                            else:
                                # Fallback: the mail.pipeline span_id when mail.message is not available
                                if hasattr(self, '_mail_pipeline_span') and self._mail_pipeline_span:
                                    try:
                                        span_ctx = self._mail_pipeline_span.get_span_context()
                                        if span_ctx and span_ctx.span_id:
                                            fallback_span_id = str(span_ctx.span_id)
                                            mapping_data["span_id"] = fallback_span_id
                                            self.logger.warning(f"[OTEL] Using the mail.pipeline span_id as a fallback: {fallback_span_id} (mail.message not available)")
                                    except Exception as e:
                                        self.logger.warning(f"[OTEL] Error getting the mail.pipeline span_id as a fallback: {e}")
                                if "span_id" not in mapping_data:
                                    if OTEL_TRACES_ACTIVE:
                                        self.logger.error("[OTEL] CRITICAL: no span_id available for correlation! The events-updater cannot create child spans.")
                                    else:
                                        self.logger.debug("[OTEL] No span_id (tracing disabled by design) - no correlation")

                            # mail.message start timestamp, so the events-updater can compute queue_wait
                            if self._mail_message_span:
                                try:
                                    # OTel SDK spans expose start_time (nanoseconds)
                                    start_time_ns = getattr(self._mail_message_span, 'start_time', None)
                                    if start_time_ns is not None:
                                        mail_message_start_ts_ms = int(start_time_ns / 1_000_000)
                                        mapping_data["mail_message_start_ts_ms"] = str(mail_message_start_ts_ms)
                                        self.logger.debug(f"[OTEL] mail_message_start_ts_ms {mail_message_start_ts_ms} added to the Redis mapping")
                                except Exception as e:
                                    self.logger.debug(f"[OTEL] Error getting mail_message_start_ts_ms: {e}")

                            mapping_result = redis.setex(
                                mapping_key,
                                REDIS_TTL_SEC,
                                json.dumps(mapping_data, default=str),
                            )
                            if mapping_result:
                                self.logger.debug(
                                    f"[REDIS_SAVE] Mapping saved - Key: {mapping_key}, Message ID: {self.message_id}"
                                )
                            else:
                                self.logger.error(
                                    f"[REDIS_SAVE] Failed to save the mapping in Redis - Key: {mapping_key}"
                                )

                        except Exception as e:
                            self.logger.error(
                                f"[REDIS_SAVE] Failed to save to the Redis cache - Key: {mapping_key}, Error: {e}, Type: {type(e).__name__}",
                                exc_info=True,
                            )
            else:
                missing_data = []
                if not self.queue_id:
                    missing_data.append("Queue ID")
                if not self.message_id:
                    missing_data.append("Message ID")
                if not self.rcpts:
                    missing_data.append("Recipients")

                self.logger.warning(
                    f"Incomplete data - Missing: {', '.join(missing_data)}"
                )

            # Link rewriting + pixel injection (only when enabled and there is data)
            # Is tracking enabled for this domain?
            tracking_enabled = self._is_tracking_enabled()
            rewrite_links = self._should_rewrite_links()
            inject_pixel = self._should_inject_pixel()

            if (
                tracking_enabled
                and (rewrite_links or inject_pixel)
                and self.message_id
                and self.rcpts
                and self.body_chunks
            ):
                track_base = self._get_track_base()
                self.logger.debug(
                    f"MODIFYING EMAIL - Links: {rewrite_links}, Pixel: {inject_pixel}, "
                    f"Track Base: {track_base}"
                )
            elif not tracking_enabled:
                self.logger.debug(
                    f"Tracking disabled for domain {self.domain_name} - skipping link rewriting and pixel injection"
                )
            elif not (rewrite_links or inject_pixel):
                self.logger.debug(
                    f"Link rewriting and pixel injection disabled for domain {self.domain_name}"
                )
            else:
                # Process the email normally, even without tracking
                pass

            # Continue when the required data is present
            if (
                self.message_id
                and self.rcpts
                and self.body_chunks
            ):
                rcpt0 = self.rcpts[0]
                original_bytes = b"".join(self.body_chunks)
                original = original_bytes.decode("utf-8", errors="ignore")
                self.logger.debug(
                    f"Original content extracted - Size: {len(original)} chars, Chunks: {len(self.body_chunks)}"
                )
                self.logger.debug(
                    f"Original content (first 200 chars): {original[:200]}"
                )
                self.logger.debug(
                    f"Original content (last 200 chars): {original[-200:]}"
                )
                self.logger.debug(
                    f"Original ends with </html>: {is_html_complete(original)}"
                )
                modified = original

                def _replace_unsub_placeholder(text: str) -> str:
                    """
                    Replace the {{unsub_link}} placeholder with the unsubscribe URL.

                    The replacement happens whether or not tracking is enabled,
                    because the placeholder comes from the email template.

                    Args:
                        text: Text content to process

                    Returns:
                        str: Text with the placeholder replaced (or unchanged if not found)
                    """
                    if "{{unsub_link}}" not in text:
                        return text

                    token = self._new_token(
                        "unsubscribe", self.message_id, rcpt0,
                        campaign_id=self.campaign_id)
                    if not token:
                        self.logger.error("Error generating the unsubscribe token")
                        return text

                    unsub_url = self._get_unsub_tracking_url(token)
                    self.logger.debug(
                        f"Replacing {{{{unsub_link}}}} with {unsub_url}"
                    )
                    return text.replace("{{unsub_link}}", unsub_url)

                def _rewrite_links_in_text(text: str) -> tuple[str, int]:
                    """
                    Rewrite clickable URLs (inside <a href> tags) with tracking tokens.

                    Args:
                        text: Text content to process

                    Returns:
                        tuple: (modified_text, number_of_links_found)
                    """
                    # Replace the {{unsub_link}} placeholder before any rewriting
                    text = _replace_unsub_placeholder(text)

                    if not rewrite_links:
                        return text, 0

                    def _rewrite_href_link(match) -> str:
                        """Rewrite an <a href> tag with a tracking token."""
                        full_tag = match.group(
                            0)  # full <a href="..."> tag
                        dest_url = match.group(1)  # URL inside the href

                        # Skip empty hrefs and special links (mailto, tel, etc.)
                        if href_is_untrackable(dest_url):
                            return full_tag

                        # Skip unsubscribe links (already /u/ tracking URLs):
                        # wrapping them again would create false click metrics
                        track_base = self._get_track_base()
                        if dest_url.startswith(f"{track_base}/u/"):
                            return full_tag

                        # Add a scheme when missing
                        if not dest_url.startswith(("http://", "https://")):
                            dest_url = f"https://{dest_url}"

                        token = self._new_token(
                            "click", self.message_id, rcpt0, dest_url,
                            campaign_id=self.campaign_id)
                        if not token:
                            self.logger.error("Error generating the click token")
                            return full_tag

                        # The token already carries all the data, encrypted

                        # Replace the original URL with the tracking URL
                        tracking_url = self._get_click_tracking_url(token)
                        return splice_href(match, tracking_url)

                    # Find and rewrite clickable links only
                    matches = list(A_HREF_URL_RE.finditer(text))
                    modified_text = A_HREF_URL_RE.sub(_rewrite_href_link, text)

                    return modified_text, len(matches)

                def _process_multipart_part(
                    part_content: str,
                    part_index: int = None,
                    depth: int = 0,
                    parent_mime: str | None = None,
                ) -> tuple[str, bool]:
                    """
                    Recursively process a multipart part, modifying the HTML it contains.

                    Args:
                        part_content: Full part content (headers + body)
                        part_index: Part index (for logging)
                        depth: Recursion depth (for logging)

                    Returns:
                        tuple: (modified_part, was_modified)
                    """
                    indent = "  " * depth
                    part_label = (
                        f"{indent}Part {part_index}"
                        if part_index is not None
                        else f"{indent}Part"
                    )

                    # Split the headers from the body
                    sep = part_content.find("\r\n\r\n")
                    if sep == -1:
                        sep = part_content.find("\n\n")

                    if sep == -1:
                        self.logger.debug(
                            f"{part_label} has no separator, returning the original"
                        )
                        return part_content, False

                    p_headers = part_content[:sep]
                    p_body = (
                        part_content[sep + 4:]
                        if "\r\n\r\n" in part_content[: sep + 6]
                        else part_content[sep + 2:]
                    )
                    plow = p_headers.lower()
                    mime_type = extract_mime_type(p_headers)

                    # FIRST CHECK: attachments are not processed
                    # Attachments of any type must not be modified
                    if is_attachment(p_headers):
                        self.logger.debug(
                            f"{part_label} - Attachment detected (Content-Disposition: attachment), kept unchanged"
                        )
                        part_encoding = extract_content_encoding(p_headers)
                        self._record_attachment_metadata(
                            p_headers, p_body, part_encoding
                        )
                        # Keep the attachment exactly as is (only fix malformed headers if needed)
                        p_headers_fixed = fix_malformed_header_params(
                            p_headers)
                        if p_headers_fixed != p_headers:
                            fixed_part_content = p_headers_fixed + \
                                part_content[sep:]
                            return fixed_part_content, True
                        return part_content, False

                    # Nested multipart?
                    if "content-type: multipart/" in plow:
                        # Boundary of the nested multipart
                        import re as _re

                        m = _re.search(
                            r'boundary\s*=\s*("?)([^";]+)\1', p_headers, flags=_re.I
                        )
                        if m:
                            nested_boundary = m.group(2).strip()
                            nested_boundary = (
                                nested_boundary
                                if nested_boundary.startswith("--")
                                else f"--{nested_boundary}"
                            )

                            self.logger.debug(
                                f"{part_label} - Nested multipart detected (depth={depth}), boundary={nested_boundary}"
                            )

                            # Process the nested multipart recursively
                            nested_parts = p_body.split(nested_boundary)
                            nested_changed = False

                            self.logger.debug(
                                f"{part_label} - Nested parts found: {len(nested_parts)}"
                            )

                            for j in range(1, len(nested_parts) - 1):
                                nested_part = nested_parts[j]
                                processed_part, part_changed = _process_multipart_part(
                                    nested_part,
                                    j,
                                    depth + 1,
                                    mime_type,
                                )
                                if part_changed:
                                    nested_parts[j] = processed_part
                                    nested_changed = True

                            if nested_changed:
                                new_body = nested_boundary.join(nested_parts)
                                # The nested multipart body needs no decoding/re-encoding:
                                # it holds the processed parts (already encoded).
                                # Just return the modified part
                                return p_headers + "\r\n\r\n" + new_body, True

                    # HTML?
                    if "content-type: text/html" in plow:
                        # Note: the attachment check above already ran, so this HTML is not an attachment
                        self.logger.debug(
                            f"{part_label} - HTML detected (depth={depth})"
                        )
                        # Encoding of the part
                        part_encoding = extract_content_encoding(p_headers)
                        self.logger.debug(
                            f"{part_label} - HTML part detected with encoding: {part_encoding}"
                        )
                        self.logger.debug(
                            f"{part_label} - Original body size: {len(p_body)} chars"
                        )

                        # Decode the content when needed
                        if part_encoding in ["base64", "quoted-printable"]:
                            self.logger.debug(
                                f"{part_label} - Decoding {part_encoding} content for processing"
                            )
                            decoded_body = decode_email_content(
                                p_body, part_encoding)
                            self.logger.debug(
                                f"{part_label} - Decoded content: {len(decoded_body)} chars"
                            )

                            # Span tracking.rewrite, child of mail.message
                            tracking_rewrite_span = None
                            if OTEL_AVAILABLE and self._mail_message_span:
                                try:
                                    tracking_rewrite_span = otel_tracer.start_span("tracking.rewrite", context=trace.set_span_in_context(self._mail_message_span))
                                    tracking_rewrite_span.set_attribute("tracking.type", "links")
                                    self.logger.debug("[OTEL] Span tracking.rewrite created")
                                except Exception as e:
                                    self.logger.warning(f"[OTEL] Error creating span tracking.rewrite: {e}")

                            # Process the decoded content
                            new_body, links_found = _rewrite_links_in_text(
                                decoded_body)
                            self.logger.debug(
                                f"{part_label} - After link rewriting: {len(new_body)} chars, links found: {links_found}"
                            )

                            # Finish span tracking.rewrite
                            if tracking_rewrite_span:
                                try:
                                    tracking_rewrite_span.set_attribute("tracking.links_found", links_found)
                                    tracking_rewrite_span.end()
                                    self.logger.debug("[OTEL] Span tracking.rewrite finished")
                                except Exception:
                                    pass

                            # Span tracking.inject, child of mail.message
                            tracking_inject_span = None
                            # Add the tracking pixel when enabled (before encoding)
                            if inject_pixel:
                                if OTEL_AVAILABLE and self._mail_message_span:
                                    try:
                                        tracking_inject_span = otel_tracer.start_span("tracking.inject", context=trace.set_span_in_context(self._mail_message_span))
                                        tracking_inject_span.set_attribute("tracking.type", "pixel")
                                        self.logger.debug("[OTEL] Span tracking.inject created")
                                    except Exception as e:
                                        self.logger.warning(f"[OTEL] Error creating span tracking.inject: {e}")
                                pixel = None
                                try:
                                    otok = self._new_token(
                                        "open", self.message_id, rcpt0,
                                        campaign_id=self.campaign_id)
                                    if otok:
                                        pixel_url = self._get_pixel_tracking_url(otok)
                                        pixel = f'<img src="{pixel_url}" width="1" height="1" style="display:none" alt="image.png" />'
                                        self.logger.debug(
                                            f"{part_label} - Pixel token created: {otok}"
                                        )
                                    else:
                                        self.logger.error(
                                            f"{part_label} - Error generating the open token")
                                except Exception as e:
                                    self.logger.error(
                                        f"{part_label} - Error creating the pixel: {e}",
                                        exc_info=True,
                                    )

                                # Add the pixel to the content before encoding (if it was created)
                                if pixel:
                                    low = new_body.lower()
                                    idx = low.rfind("</body>")
                                    if idx != -1:
                                        new_body = (
                                            new_body[:idx] +
                                            pixel + new_body[idx:]
                                        )
                                        self.logger.debug(
                                            f"{part_label} - Pixel added before </body> at position {idx}"
                                        )
                                    else:
                                        new_body = new_body + pixel
                                        self.logger.debug(
                                            f"{part_label} - Pixel added at the end (</body> not found)"
                                        )

                                # Finish span tracking.inject
                                if tracking_inject_span:
                                    try:
                                        tracking_inject_span.set_attribute("tracking.pixel_injected", pixel is not None)
                                        tracking_inject_span.end()
                                        self.logger.debug("[OTEL] Span tracking.inject finished")
                                    except Exception:
                                        pass

                            # Encode as quoted-printable (standardizes; CRLF without extra soft breaks)
                            p_headers = update_content_transfer_encoding(
                                p_headers, "quoted-printable"
                            )
                            new_body = encode_email_content(
                                new_body, "quoted-printable"
                            )
                            self.logger.debug(
                                f"{part_label} - Content encoded as quoted-printable (soft breaks disabled) - Size: {len(new_body)}"
                            )
                        else:
                            # 7bit/8bit: process directly
                            # Span tracking.rewrite, child of mail.message
                            tracking_rewrite_span_2 = None
                            if OTEL_AVAILABLE and self._mail_message_span:
                                try:
                                    tracking_rewrite_span_2 = otel_tracer.start_span("tracking.rewrite", context=trace.set_span_in_context(self._mail_message_span))
                                    tracking_rewrite_span_2.set_attribute("tracking.type", "links")
                                    self.logger.debug("[OTEL] Span tracking.rewrite created (7bit/8bit)")
                                except Exception as e:
                                    self.logger.warning(f"[OTEL] Error creating span tracking.rewrite: {e}")

                            new_body, links_found = _rewrite_links_in_text(
                                p_body)
                            self.logger.debug(
                                f"{part_label} - After link rewriting: {len(new_body)} chars, links found: {links_found}"
                            )

                            # Finish span tracking.rewrite
                            if tracking_rewrite_span_2:
                                try:
                                    tracking_rewrite_span_2.set_attribute("tracking.links_found", links_found)
                                    tracking_rewrite_span_2.end()
                                    self.logger.debug("[OTEL] Span tracking.rewrite finished (7bit/8bit)")
                                except Exception:
                                    pass

                            # Span tracking.inject, child of mail.message
                            tracking_inject_span_2 = None
                            # Add the pixel when enabled
                            if inject_pixel:
                                if OTEL_AVAILABLE and self._mail_message_span:
                                    try:
                                        tracking_inject_span_2 = otel_tracer.start_span("tracking.inject", context=trace.set_span_in_context(self._mail_message_span))
                                        tracking_inject_span_2.set_attribute("tracking.type", "pixel")
                                        self.logger.debug("[OTEL] Span tracking.inject created (7bit/8bit)")
                                    except Exception as e:
                                        self.logger.warning(f"[OTEL] Error creating span tracking.inject: {e}")
                                pixel = None
                                try:
                                    otok = self._new_token(
                                        "open", self.message_id, rcpt0,
                                        campaign_id=self.campaign_id)
                                    if otok:
                                        pixel_url = self._get_pixel_tracking_url(otok)
                                        pixel = f'<img src="{pixel_url}" width="1" height="1" style="display:none" />'
                                        self.logger.debug(
                                            f"{part_label} - Pixel token created: {otok}"
                                        )
                                    else:
                                        self.logger.error(
                                            f"{part_label} - Error generating the open token")
                                except Exception as e:
                                    self.logger.error(
                                        f"{part_label} - Error creating the pixel: {e}",
                                        exc_info=True,
                                    )

                                # Add the pixel to the content (if it was created)
                                if pixel:
                                    low = new_body.lower()
                                    idx = low.rfind("</body>")
                                    if idx != -1:
                                        new_body = (
                                            new_body[:idx] +
                                            pixel + new_body[idx:]
                                        )
                                        self.logger.debug(
                                            f"{part_label} - Pixel injected before </body> at position {idx}"
                                        )
                                    else:
                                        new_body = new_body + pixel
                                        self.logger.debug(
                                            f"{part_label} - Pixel injected at the end (</body> not found)"
                                        )

                                # Finish span tracking.inject
                                if tracking_inject_span_2:
                                    try:
                                        tracking_inject_span_2.set_attribute("tracking.pixel_injected", pixel is not None)
                                        tracking_inject_span_2.end()
                                        self.logger.debug("[OTEL] Span tracking.inject finished (7bit/8bit)")
                                    except Exception:
                                        pass

                            # Convert to quoted-printable to standardize the encoding
                            p_headers = update_content_transfer_encoding(
                                p_headers, "quoted-printable"
                            )
                            new_body = encode_email_content(
                                new_body, "quoted-printable"
                            )
                            self.logger.debug(
                                f"{part_label} - Content encoded as quoted-printable - Size: {len(new_body)}"
                            )

                        # Return the modified part, if it changed
                        if new_body != p_body:
                            self.logger.debug(
                                f"{part_label} - HTML modified: {len(new_body)} chars (original: {len(p_body)} chars)"
                            )
                            return p_headers + "\r\n\r\n" + new_body, True

                    # Convert plain text into multipart/alternative with generated HTML
                    if (
                        mime_type == "text/plain"
                        and parent_mime != "multipart/alternative"
                    ):
                        self.logger.debug(
                            f"{part_label} - Plain text detected (depth={depth}), converting to multipart/alternative"
                        )

                        part_encoding = (
                            extract_content_encoding(p_headers) or ""
                        ).lower()
                        recognized_encodings = {
                            "base64",
                            "quoted-printable",
                            "7bit",
                            "8bit",
                        }
                        text_body = p_body

                        if part_encoding in {"base64", "quoted-printable"}:
                            try:
                                text_body = decode_email_content(
                                    p_body, part_encoding)
                                self.logger.debug(
                                    f"{part_label} - Plain text decoded ({part_encoding}) - size: {len(text_body)} chars"
                                )
                            except Exception as decode_err:
                                self.logger.error(
                                    f"{part_label} - Failed to decode the plain text ({part_encoding}): {decode_err}",
                                    exc_info=True,
                                )
                                text_body = p_body
                        elif part_encoding in {"7bit", "8bit"}:
                            text_body = p_body.replace(
                                "\r\n", "\n").replace("\r", "\n")
                            self.logger.debug(
                                f"{part_label} - Plain text normalized ({part_encoding}) - size: {len(text_body)} chars"
                            )
                        elif (
                            part_encoding and part_encoding not in recognized_encodings
                        ):
                            self.logger.warning(
                                f"{part_label} - Unrecognized plain-text encoding ({part_encoding}); keeping the original content"
                            )

                        html_body = text_to_basic_html(text_body)

                        # Span tracking.rewrite, child of mail.message (converted plain text)
                        tracking_rewrite_span_3 = None
                        if OTEL_AVAILABLE and self._mail_message_span:
                            try:
                                tracking_rewrite_span_3 = otel_tracer.start_span("tracking.rewrite", context=trace.set_span_in_context(self._mail_message_span))
                                tracking_rewrite_span_3.set_attribute("tracking.type", "links")
                                tracking_rewrite_span_3.set_attribute("tracking.source", "text_plain_converted")
                                self.logger.debug("[OTEL] Span tracking.rewrite created (plain text)")
                            except Exception as e:
                                self.logger.warning(f"[OTEL] Error creating span tracking.rewrite: {e}")

                        html_body, links_found = _rewrite_links_in_text(
                            html_body)
                        self.logger.debug(
                            f"{part_label} - HTML generated from plain text - size: {len(html_body)} chars, links found: {links_found}"
                        )

                        # Finish span tracking.rewrite
                        if tracking_rewrite_span_3:
                            try:
                                tracking_rewrite_span_3.set_attribute("tracking.links_found", links_found)
                                tracking_rewrite_span_3.end()
                                self.logger.debug("[OTEL] Span tracking.rewrite finished (plain text)")
                            except Exception:
                                pass

                        # Span tracking.inject, child of mail.message (converted plain text)
                        tracking_inject_span_3 = None
                        if inject_pixel:
                            if OTEL_AVAILABLE and self._mail_message_span:
                                try:
                                    tracking_inject_span_3 = otel_tracer.start_span("tracking.inject", context=trace.set_span_in_context(self._mail_message_span))
                                    tracking_inject_span_3.set_attribute("tracking.type", "pixel")
                                    tracking_inject_span_3.set_attribute("tracking.source", "text_plain_converted")
                                    self.logger.debug("[OTEL] Span tracking.inject created (plain text)")
                                except Exception as e:
                                    self.logger.warning(f"[OTEL] Error creating span tracking.inject: {e}")
                            pixel = None
                            try:
                                otok = self._new_token(
                                    "open", self.message_id, rcpt0,
                                    campaign_id=self.campaign_id)
                                if otok:
                                    pixel_url = self._get_pixel_tracking_url(otok)
                                    pixel = f'<img src="{pixel_url}" width="1" height="1" style="display:none" />'
                                    self.logger.debug(
                                        f"{part_label} - Pixel injected for plain text - token created: {otok}"
                                    )
                                else:
                                    self.logger.error(
                                        f"{part_label} - Error generating the open token")
                            except Exception as e:
                                self.logger.error(
                                    f"{part_label} - Error creating the pixel: {e}",
                                    exc_info=True,
                                )

                            # Add the pixel to the generated HTML (if it was created)
                            if pixel:
                                low = html_body.lower()
                                idx = low.rfind("</body>")
                                if idx != -1:
                                    html_body = (
                                        html_body[:idx] +
                                        pixel + html_body[idx:]
                                    )
                                    self.logger.debug(
                                        f"{part_label} - Pixel added before </body> at position {idx}"
                                    )
                                else:
                                    html_body = html_body + pixel
                                    self.logger.debug(
                                        f"{part_label} - Pixel added at the end of the generated HTML"
                                    )

                            # Finish span tracking.inject
                            if tracking_inject_span_3:
                                try:
                                    tracking_inject_span_3.set_attribute("tracking.pixel_injected", pixel is not None)
                                    tracking_inject_span_3.end()
                                    self.logger.debug("[OTEL] Span tracking.inject finished (plain text)")
                                except Exception:
                                    pass

                        html_encoded = encode_email_content(
                            html_body,
                            "quoted-printable",
                            disable_soft_breaks=False,
                        )
                        text_encoded = encode_email_content(
                            text_body,
                            "quoted-printable",
                            disable_soft_breaks=False,
                        )

                        boundary = self._generate_boundary("alt")
                        multipart_body = (
                            f"--{boundary}\r\n"
                            "Content-Type: text/plain; charset=utf-8\r\n"
                            "Content-Transfer-Encoding: quoted-printable\r\n"
                            "\r\n"
                            f"{text_encoded}"
                            f"--{boundary}\r\n"
                            "Content-Type: text/html; charset=utf-8\r\n"
                            "Content-Transfer-Encoding: quoted-printable\r\n"
                            "\r\n"
                            f"{html_encoded}"
                            f"--{boundary}--\r\n"
                        )

                        updated_header_lines = []
                        content_type_updated = False
                        mime_version_present = False

                        for line in p_headers.splitlines():
                            lower = line.lower()
                            if lower.startswith("content-type:"):
                                updated_header_lines.append(
                                    f'Content-Type: multipart/alternative; boundary="{boundary}"'
                                )
                                content_type_updated = True
                            elif lower.startswith("content-transfer-encoding:"):
                                continue
                            else:
                                if lower.startswith("mime-version:"):
                                    mime_version_present = True
                                updated_header_lines.append(line)

                        if not content_type_updated:
                            updated_header_lines.insert(
                                0,
                                f'Content-Type: multipart/alternative; boundary="{boundary}"',
                            )
                        if not mime_version_present:
                            updated_header_lines.append("MIME-Version: 1.0")

                        updated_headers = "\r\n".join(updated_header_lines)

                        self.logger.debug(
                            f"{part_label} - Plain text converted to multipart/alternative with boundary {boundary}"
                        )

                        return updated_headers + "\r\n\r\n" + multipart_body, True

                    # Not HTML, plain text or multipart: fix malformed headers and return
                    # (parameters without '=', e.g. name"value" -> name="value")
                    if sep != -1:
                        p_headers_fixed = fix_malformed_header_params(
                            p_headers)
                        if p_headers_fixed != p_headers:
                            # Headers were fixed: return the fixed part
                            fixed_part_content = p_headers_fixed + \
                                part_content[sep:]
                            return fixed_part_content, True
                    return part_content, False

                # Multipart MIME messages
                boundary_for_split = (
                    self.multipart_boundary_original or self.multipart_boundary
                )
                boundary_for_join = (
                    self.multipart_boundary
                    if self.boundary_normalized and self.multipart_boundary
                    else boundary_for_split
                )

                if self.is_multipart and boundary_for_split:
                    # Rewrite only the HTML part of the multipart (recursively)
                    parts = modified.split(boundary_for_split)

                    if self.boundary_normalized and self.boundary_had_prefix:
                        if parts and parts[0].startswith("--"):
                            parts[0] = parts[0][2:]
                        for idx in range(1, len(parts) - 1):
                            if parts[idx].endswith("--"):
                                parts[idx] = parts[idx][:-2]

                    changed = False
                    self.logger.debug(
                        f"MULTIPART - Parts found: {len(parts)}")
                    self.logger.debug(
                        f"MULTIPART - Loop range: 1 to {len(parts) - 1}"
                    )

                    for i in range(1, len(parts) - 1):  # skip the preamble and epilogue
                        self.logger.debug(
                            f"MULTIPART - Processing part {i} of {len(parts)}"
                        )
                        part = parts[i]
                        self.logger.debug(
                            f"MULTIPART - Size of part {i}: {len(part)} chars"
                        )

                        # Process recursively (may be a nested multipart or HTML)
                        # The main MIME type goes in as parent_mime to avoid needless conversions
                        parent_mime = self.global_mime_type if self.global_mime_type else None
                        processed_part, part_changed = _process_multipart_part(
                            part, i, 0, parent_mime
                        )

                        if part_changed:
                            parts[i] = processed_part
                            changed = True
                            self.logger.debug(
                                f"MULTIPART - Part {i} modified"
                            )

                    if changed:
                        modified = boundary_for_join.join(parts)
                        self.logger.debug(
                            f"MULTIPART modified - Final size: {len(modified)} chars"
                        )
                        self.logger.debug(
                            f"MULTIPART modified (last 200 chars): {modified[-200:]}"
                        )
                        self.logger.debug(
                            f"MULTIPART ends with </html>: {is_html_complete(modified)}"
                        )
                elif self.is_html:
                    global_encoding = (
                        (self.global_content_transfer_encoding or "").lower().strip()
                    )
                    self.logger.debug(
                        "Simple HTML detected - Content-Transfer-Encoding: "
                        f"{global_encoding or 'not specified'}"
                    )

                    recognized_encodings = {
                        "base64",
                        "quoted-printable",
                        "7bit",
                        "8bit",
                    }
                    reencode_encoding = (
                        global_encoding
                        if global_encoding in recognized_encodings
                        else None
                    )

                    if reencode_encoding is None:
                        if global_encoding:
                            self.logger.debug(
                                f"Simple HTML with an unrecognized encoding ({global_encoding}); using quoted-printable"
                            )
                        reencode_encoding = "quoted-printable"

                    html_body = modified

                    if global_encoding in ["base64", "quoted-printable"]:
                        html_body = decode_email_content(
                            modified, global_encoding)
                        self.logger.debug(
                            f"Simple HTML decoded ({global_encoding}) - size: {len(html_body)} chars"
                        )
                    elif global_encoding in ["7bit", "8bit"]:
                        html_body = modified.replace(
                            "\r\n", "\n").replace("\r", "\n")
                        self.logger.debug(
                            f"Simple HTML normalized ({global_encoding}) - size: {len(html_body)} chars"
                        )

                    html_body, links_found = _rewrite_links_in_text(html_body)
                    self.logger.debug(
                        f"Simple HTML after link rewriting: {len(html_body)} chars, links found: {links_found}"
                    )

                    if inject_pixel:
                        # Encrypted token that carries its data
                        try:
                            otok = self._new_token(
                                "open", self.message_id, rcpt0,
                                campaign_id=self.campaign_id)
                            if otok:
                                pixel_url = self._get_pixel_tracking_url(otok)
                                pixel = (
                                    f'<img src="{pixel_url}" width="1" height="1" '
                                    'style="display:none" />'
                                )
                            self.logger.debug(f"Pixel token created: {otok}")
                        except Exception as pixel_error:
                            pixel = None
                            self.logger.error(
                                f"Error creating the pixel token: {pixel_error}",
                                exc_info=True,
                            )

                        if pixel:
                            low = html_body.lower()
                            idx = low.rfind("</body>")
                            if idx != -1:
                                # Add the pixel before the </body> tag
                                html_body = html_body[:idx] + \
                                    pixel + html_body[idx:]
                                self.logger.debug(
                                    f"Pixel injected before </body> at position {idx}"
                                )
                            else:
                                # No </body>: add it at the end
                                html_body = html_body + pixel
                                self.logger.debug(
                                    "Pixel injected at the end (</body> not found)"
                                )
                        else:
                            self.logger.warning(
                                "Pixel not injected - the token could not be created"
                            )

                    if rewrite_links:
                        self.logger.debug(f"Links rewritten: {links_found}")

                    html_part_encoding = reencode_encoding or "quoted-printable"
                    html_encoded = encode_email_content(
                        html_body,
                        html_part_encoding,
                        disable_soft_breaks=False,
                    )

                    convert_to_multipart = not self.is_multipart
                    can_change_header = bool(
                        hasattr(self, "_actions")
                        and (self._actions & getattr(Milter, "CHGHDRS", 0))
                    )

                    if convert_to_multipart:
                        text_fallback = html_to_text(html_body)
                        if not text_fallback:
                            text_fallback = "This email contains HTML content."

                        text_encoded = encode_email_content(
                            text_fallback,
                            "quoted-printable",
                            disable_soft_breaks=False,
                        )

                        boundary = self._generate_boundary()
                        multipart_body = (
                            f"--{boundary}\r\n"
                            "Content-Type: text/plain; charset=utf-8\r\n"
                            "Content-Transfer-Encoding: quoted-printable\r\n"
                            "\r\n"
                            f"{text_encoded}"
                            f"--{boundary}\r\n"
                            "Content-Type: text/html; charset=utf-8\r\n"
                            f"Content-Transfer-Encoding: {html_part_encoding}\r\n"
                            "\r\n"
                            f"{html_encoded}"
                            f"--{boundary}--\r\n"
                        )

                        modified = multipart_body
                        self.is_multipart = True
                        self.boundary_had_prefix = False
                        self.boundary_normalized = True
                        self.multipart_boundary = f"--{boundary}"
                        self.multipart_boundary_original = self.multipart_boundary
                        self.global_content_transfer_encoding = "7bit"

                        self.logger.debug(
                            f"Simple HTML converted to multipart/alternative with boundary {boundary}"
                        )

                        new_content_type = (
                            f'multipart/alternative; boundary="{boundary}"'
                        )

                        content_type_headers = self.headers.get(
                            "content-type", [])
                        updated_ct = False
                        last_error = None
                        if content_type_headers and can_change_header:
                            for idx in range(len(content_type_headers)):
                                try:
                                    self.chgheader(
                                        "Content-Type", idx, new_content_type
                                    )
                                    self.headers["content-type"][idx] = new_content_type
                                    updated_ct = True
                                    break
                                except Exception as err:
                                    last_error = err
                        if not updated_ct:
                            if last_error:
                                self.logger.warning(
                                    f"Failed to update Content-Type to multipart: {last_error}"
                                )
                            else:
                                self.logger.debug(
                                    "No original Content-Type; adding a new multipart header"
                                )
                            try:
                                self.addheader(
                                    "Content-Type", new_content_type)
                                self.headers.setdefault("content-type", []).append(
                                    new_content_type
                                )
                            except Exception as add_err:
                                self.logger.error(
                                    f"Could not add the multipart Content-Type: {add_err}"
                                )

                        new_cte = "7bit"
                        cte_headers = self.headers.get(
                            "content-transfer-encoding", [])
                        updated_cte = False
                        last_cte_error = None
                        if cte_headers and can_change_header:
                            for idx in range(len(cte_headers)):
                                try:
                                    self.chgheader(
                                        "Content-Transfer-Encoding", idx, new_cte
                                    )
                                    self.headers["content-transfer-encoding"][
                                        idx
                                    ] = new_cte
                                    updated_cte = True
                                    break
                                except Exception as err:
                                    last_cte_error = err
                        if not updated_cte:
                            try:
                                if last_cte_error:
                                    self.logger.warning(
                                        f"Failed to update Content-Transfer-Encoding: {last_cte_error}"
                                    )
                                self.addheader(
                                    "Content-Transfer-Encoding", new_cte)
                                self.headers.setdefault(
                                    "content-transfer-encoding", []
                                ).append(new_cte)
                            except Exception as add_cte_error:
                                self.logger.error(
                                    f"Could not adjust Content-Transfer-Encoding: {add_cte_error}"
                                )
                    else:
                        modified = html_encoded
                        self.global_content_transfer_encoding = html_part_encoding

                        if global_encoding != html_part_encoding:
                            cte_headers = self.headers.get(
                                "content-transfer-encoding", []
                            )
                            updated_cte = False
                            last_cte_error = None
                            if cte_headers and can_change_header:
                                for idx in range(len(cte_headers)):
                                    try:
                                        self.chgheader(
                                            "Content-Transfer-Encoding",
                                            idx,
                                            html_part_encoding,
                                        )
                                        self.headers["content-transfer-encoding"][
                                            idx
                                        ] = html_part_encoding
                                        updated_cte = True
                                        break
                                    except Exception as err:
                                        last_cte_error = err
                            if not updated_cte:
                                if not cte_headers:
                                    try:
                                        self.addheader(
                                            "Content-Transfer-Encoding",
                                            html_part_encoding,
                                        )
                                        self.headers.setdefault(
                                            "content-transfer-encoding", []
                                        ).append(html_part_encoding)
                                    except Exception as add_err:
                                        self.logger.error(
                                            f"Could not add Content-Transfer-Encoding: {add_err}"
                                        )
                                elif last_cte_error:
                                    self.logger.warning(
                                        f"Failed to update the global Content-Transfer-Encoding: {last_cte_error}"
                                    )

                else:
                    global_encoding = (
                        (self.global_content_transfer_encoding or "").lower().strip()
                    )
                    self.logger.debug(
                        "Plain text detected - Content-Transfer-Encoding: "
                        f"{global_encoding or 'not specified'}"
                    )

                    recognized_encodings = {
                        "base64",
                        "quoted-printable",
                        "7bit",
                        "8bit",
                    }
                    text_body = modified

                    if global_encoding in {"base64", "quoted-printable"}:
                        try:
                            text_body = decode_email_content(
                                modified, global_encoding)
                            self.logger.debug(
                                f"Plain text decoded ({global_encoding}) - size: {len(text_body)} chars"
                            )
                        except Exception as decode_err:
                            self.logger.error(
                                f"Failed to decode the plain text ({global_encoding}): {decode_err}",
                                exc_info=True,
                            )
                            text_body = modified
                    elif global_encoding in {"7bit", "8bit"}:
                        text_body = modified.replace(
                            "\r\n", "\n").replace("\r", "\n")
                        self.logger.debug(
                            f"Plain text normalized ({global_encoding}) - size: {len(text_body)} chars"
                        )
                    elif (
                        global_encoding and global_encoding not in recognized_encodings
                    ):
                        self.logger.warning(
                            f"Unrecognized plain-text encoding ({global_encoding}); keeping the original content"
                        )

                    html_body = text_to_basic_html(text_body)

                    # Span tracking.rewrite, child of mail.message (global plain text)
                    tracking_rewrite_span_global = None
                    if OTEL_AVAILABLE and self._mail_message_span:
                        try:
                            tracking_rewrite_span_global = otel_tracer.start_span("tracking.rewrite", context=trace.set_span_in_context(self._mail_message_span))
                            tracking_rewrite_span_global.set_attribute("tracking.type", "links")
                            tracking_rewrite_span_global.set_attribute("tracking.source", "text_plain_global")
                            self.logger.debug("[OTEL] Span tracking.rewrite created (global plain text)")
                        except Exception as e:
                            self.logger.warning(f"[OTEL] Error creating span tracking.rewrite: {e}")

                    html_body, links_found = _rewrite_links_in_text(html_body)
                    self.logger.debug(
                        f"HTML generated from plain text - size: {len(html_body)} chars, links found: {links_found}"
                    )

                    # Finish span tracking.rewrite
                    if tracking_rewrite_span_global:
                        try:
                            tracking_rewrite_span_global.set_attribute("tracking.links_found", links_found)
                            tracking_rewrite_span_global.end()
                            self.logger.debug("[OTEL] Span tracking.rewrite finished (global plain text)")
                        except Exception:
                            pass

                    # Span tracking.inject, child of mail.message (global plain text)
                    tracking_inject_span_global = None
                    if inject_pixel:
                        if OTEL_AVAILABLE and self._mail_message_span:
                            try:
                                tracking_inject_span_global = otel_tracer.start_span("tracking.inject", context=trace.set_span_in_context(self._mail_message_span))
                                tracking_inject_span_global.set_attribute("tracking.type", "pixel")
                                tracking_inject_span_global.set_attribute("tracking.source", "text_plain_global")
                                self.logger.debug("[OTEL] Span tracking.inject created (global plain text)")
                            except Exception as e:
                                self.logger.warning(f"[OTEL] Error creating span tracking.inject: {e}")
                        pixel = None
                        try:
                            otok = self._new_token(
                                "open", self.message_id, rcpt0,
                                campaign_id=self.campaign_id)
                            if otok:
                                pixel_url = self._get_pixel_tracking_url(otok)
                                pixel = f'<img src="{pixel_url}" width="1" height="1" style="display:none" />'
                                self.logger.debug(
                                    f"Pixel injected for plain text - token created: {otok}"
                                )
                            else:
                                self.logger.error(
                                    "Error generating the open token")
                        except Exception as e:
                            self.logger.error(
                                f"Error creating the pixel: {e}",
                                exc_info=True,
                            )

                        # Add the pixel to the generated HTML (if it was created)
                        if pixel:
                            low = html_body.lower()
                            idx = low.rfind("</body>")
                            if idx != -1:
                                html_body = html_body[:idx] + \
                                    pixel + html_body[idx:]
                                self.logger.debug(
                                    f"Pixel added before </body> at position {idx}"
                                )
                            else:
                                html_body = html_body + pixel
                                self.logger.debug(
                                    "Pixel added at the end of the generated HTML"
                                )

                        # Finish span tracking.inject
                        if tracking_inject_span_global:
                            try:
                                tracking_inject_span_global.set_attribute("tracking.pixel_injected", pixel is not None)
                                tracking_inject_span_global.end()
                                self.logger.debug("[OTEL] Span tracking.inject finished (global plain text)")
                            except Exception:
                                pass

                    html_part_encoding = "quoted-printable"
                    html_encoded = encode_email_content(
                        html_body,
                        html_part_encoding,
                        disable_soft_breaks=False,
                    )

                    convert_to_multipart = not self.is_multipart
                    can_change_header = bool(
                        hasattr(self, "_actions")
                        and (self._actions & getattr(Milter, "CHGHDRS", 0))
                    )

                    if convert_to_multipart:
                        text_encoded = encode_email_content(
                            text_body,
                            "quoted-printable",
                            disable_soft_breaks=False,
                        )

                        boundary = self._generate_boundary()
                        multipart_body = (
                            f"--{boundary}\r\n"
                            "Content-Type: text/plain; charset=utf-8\r\n"
                            "Content-Transfer-Encoding: quoted-printable\r\n"
                            "\r\n"
                            f"{text_encoded}"
                            f"--{boundary}\r\n"
                            "Content-Type: text/html; charset=utf-8\r\n"
                            f"Content-Transfer-Encoding: {html_part_encoding}\r\n"
                            "\r\n"
                            f"{html_encoded}"
                            f"--{boundary}--\r\n"
                        )

                        modified = multipart_body
                        self.is_multipart = True
                        self.boundary_had_prefix = False
                        self.boundary_normalized = True
                        self.multipart_boundary = f"--{boundary}"
                        self.multipart_boundary_original = self.multipart_boundary
                        self.global_content_transfer_encoding = "7bit"

                        self.logger.debug(
                            f"Plain text converted to multipart/alternative with boundary {boundary}"
                        )

                        new_content_type = (
                            f'multipart/alternative; boundary="{boundary}"'
                        )

                        content_type_headers = self.headers.get(
                            "content-type", [])
                        updated_ct = False
                        last_error = None
                        if content_type_headers and can_change_header:
                            for idx in range(len(content_type_headers)):
                                try:
                                    self.chgheader(
                                        "Content-Type", idx, new_content_type
                                    )
                                    self.headers["content-type"][idx] = new_content_type
                                    updated_ct = True
                                    break
                                except Exception as err:
                                    last_error = err
                        if not updated_ct:
                            if last_error:
                                self.logger.warning(
                                    f"Failed to update Content-Type to multipart (plain text): {last_error}"
                                )
                            else:
                                self.logger.debug(
                                    "No original Content-Type; adding a new multipart header (plain text)"
                                )
                            try:
                                self.addheader(
                                    "Content-Type", new_content_type)
                                self.headers.setdefault("content-type", []).append(
                                    new_content_type
                                )
                            except Exception as add_err:
                                self.logger.error(
                                    f"Could not add the multipart Content-Type (plain text): {add_err}"
                                )

                        new_cte = "7bit"
                        cte_headers = self.headers.get(
                            "content-transfer-encoding", [])
                        updated_cte = False
                        last_cte_error = None
                        if cte_headers and can_change_header:
                            for idx in range(len(cte_headers)):
                                try:
                                    self.chgheader(
                                        "Content-Transfer-Encoding", idx, new_cte
                                    )
                                    self.headers["content-transfer-encoding"][
                                        idx
                                    ] = new_cte
                                    updated_cte = True
                                    break
                                except Exception as err:
                                    last_cte_error = err
                        if not updated_cte:
                            try:
                                if last_cte_error:
                                    self.logger.warning(
                                        f"Failed to update Content-Transfer-Encoding (plain text): {last_cte_error}"
                                    )
                                self.addheader(
                                    "Content-Transfer-Encoding", new_cte)
                                self.headers.setdefault(
                                    "content-transfer-encoding", []
                                ).append(new_cte)
                            except Exception as add_cte_error:
                                self.logger.error(
                                    f"Could not adjust Content-Transfer-Encoding (plain text): {add_cte_error}"
                                )
                    else:
                        modified = html_encoded
                        self.global_content_transfer_encoding = html_part_encoding

                # Apply the changes if the content changed
                if modified != original:
                    try:
                        # Detailed log of the content before replacebody
                        modified_bytes = modified.encode("utf-8")
                        self.logger.debug(
                            f"Modified content - Original size: {len(original)} chars, Modified: {len(modified)} chars, Bytes: {len(modified_bytes)}"
                        )
                        self.logger.debug(
                            f"Modified content (first 200 chars): {modified[:200]}"
                        )
                        self.logger.debug(
                            f"Modified content (last 200 chars): {modified[-200:]}"
                        )
                        self.logger.debug(
                            f"Ends with </html>: {is_html_complete(modified)}"
                        )
                        self.logger.debug(
                            f"Ends with </body>: {modified.rstrip().endswith('</body>')}"
                        )

                        # Look for special characters or encoding problems
                        self.logger.debug(
                            f"Modified content (last 500 chars): {modified[-500:]}"
                        )

                        # Check the encoding
                        try:
                            # Decode again to check
                            test_decode = modified_bytes.decode("utf-8")
                            self.logger.debug(
                                f"Decoding test - Size: {len(test_decode)} chars"
                            )
                            self.logger.debug(
                                f"Decoding test (last 200 chars): {test_decode[-200:]}"
                            )
                            self.logger.debug(
                                f"Test ends with </html>: {is_html_complete(test_decode)}"
                            )
                        except Exception as decode_error:
                            self.logger.error(
                                f"Error in the decoding test: {decode_error}"
                            )

                        # Check the HTML part of the multipart specifically (recursively)
                        if self.is_multipart and boundary_for_join:
                            self.logger.debug(
                                "=== CHECKING THE HTML IN THE MULTIPART ==="
                            )

                            def _find_html_in_part(
                                part_content: str, depth: int = 0
                            ) -> None:
                                """Recursive function that finds the HTML at any level"""
                                indent = "  " * depth
                                sep = part_content.find("\r\n\r\n")
                                if sep == -1:
                                    sep = part_content.find("\n\n")
                                if sep == -1:
                                    return

                                p_headers = part_content[:sep]
                                p_body = (
                                    part_content[sep + 4:]
                                    if "\r\n\r\n" in part_content[: sep + 6]
                                    else part_content[sep + 2:]
                                )
                                plow = p_headers.lower()

                                # Nested multipart: process recursively
                                if "content-type: multipart/" in plow:
                                    import re as _re

                                    m = _re.search(
                                        r'boundary\s*=\s*("?)([^";]+)\1',
                                        p_headers,
                                        flags=_re.I,
                                    )
                                    if m:
                                        nested_boundary = m.group(2).strip()
                                        nested_boundary = (
                                            nested_boundary
                                            if nested_boundary.startswith("--")
                                            else f"--{nested_boundary}"
                                        )
                                        nested_parts = p_body.split(
                                            nested_boundary)
                                        self.logger.debug(
                                            f"{indent}Nested multipart (depth={depth}), {len(nested_parts)} parts"
                                        )
                                        for j in range(1, len(nested_parts) - 1):
                                            _find_html_in_part(
                                                nested_parts[j], depth + 1
                                            )

                                # HTML: log its details
                                elif "content-type: text/html" in plow:
                                    self.logger.debug(
                                        f"{indent}HTML part found (depth={depth}): {len(part_content)} chars"
                                    )
                                    # Decode when needed, for the check
                                    encoding = extract_content_encoding(
                                        p_headers)
                                    if encoding in ["base64", "quoted-printable"]:
                                        html_body = decode_email_content(
                                            p_body, encoding
                                        )
                                    else:
                                        html_body = p_body

                                    self.logger.debug(
                                        f"{indent}Decoded HTML body: {len(html_body)} chars"
                                    )
                                    self.logger.debug(
                                        f"{indent}HTML body ends with </html>: {is_html_complete(html_body)}"
                                    )
                                    if len(html_body) > 0:
                                        self.logger.debug(
                                            f"{indent}HTML body (last 200 chars): {html_body[-200:]}"
                                        )

                            parts_check = modified.split(boundary_for_join)
                            for idx, part in enumerate(
                                parts_check[1:-1]
                            ):  # skip the preamble and epilogue
                                _find_html_in_part(part, 0)

                        self.replacebody(modified_bytes)
                        self.addheader("X-Connect-Tracked", "yes")
                        # Keep self.headers in sync with the added header
                        self.headers.setdefault("x-connect-tracked", []).append("yes")
                        self.logger.debug(
                            "Email modified - body replaced, header added"
                        )
                    except (AttributeError, Exception) as e:
                        # Fallback when there is no milter context (local tests)
                        self.logger.warning(
                            f"Could not modify the email (local test): {e}"
                        )
                else:
                    # Either nothing to rewrite, or a multipart with an unsupported encoding
                    try:
                        reason = (
                            "skip-encoding"
                            if (self.is_multipart and self.multipart_boundary)
                            else "no-change"
                        )
                        self.addheader("X-Connect-Tracked", reason)
                        # Keep self.headers in sync with the added header
                        self.headers.setdefault("x-connect-tracked", []).append(reason)
                    except Exception:
                        pass

                    # Detailed log of why it was not modified
                    self.logger.warning(
                        f"Email not modified: {reason} - "
                        f"is_html={self.is_html}, is_multipart={self.is_multipart}, "
                        f"REWRITE_LINKS={REWRITE_LINKS}, INJECT_PIXEL={INJECT_PIXEL}, "
                        f"message_id={bool(self.message_id)}, rcpts={len(self.rcpts) if self.rcpts else 0}, "
                        f"body_chunks={len(self.body_chunks) if self.body_chunks else 0}"
                    )
            else:
                # Even without changes, mark that the milter saw the message (debug/observability)
                try:
                    self.addheader("X-Connect-Tracked", "seen")
                    # Keep self.headers in sync with the added header
                    self.headers.setdefault("x-connect-tracked", []).append("seen")
                except Exception:
                    pass

                try:
                    original_bytes = b"".join(self.body_chunks)
                except Exception:
                    pass

            # Log the completion
            total_time = time.time() - self.start_time
            self.logger.debug(
                f"Email processed - Total time: {total_time:.3f}s, Queue ID: {self.queue_id}"
            )

            # Recipient addresses are not logged here: the audit line is per event,
            # emitted by kafka_client when the outbox write succeeds. Only the count
            # is logged, at debug level.
            self.logger.debug(
                f"[EOM] Processing email with {len(self.rcpts)} recipients"
            )

            # Span outbox.enqueue, child of mail.message
            outbox_enqueue_span = None
            # Write the email-sent event for each recipient
            if self.message_id and self.rcpts:
                if OTEL_AVAILABLE and self._mail_message_span:
                    try:
                        outbox_enqueue_span = otel_tracer.start_span("outbox.enqueue", context=trace.set_span_in_context(self._mail_message_span))
                        outbox_enqueue_span.set_attribute("outbox.event_type", "sent")
                        outbox_enqueue_span.set_attribute("outbox.message_id", str(self.message_id))
                        outbox_enqueue_span.set_attribute("outbox.recipient_count", len(self.rcpts))
                        self.logger.debug("[OTEL] Span outbox.enqueue created")
                    except Exception as e:
                        self.logger.warning(f"[OTEL] Error creating span outbox.enqueue: {e}")

                self.logger.debug(
                    f"[KAFKA_SENT] Writing SENT events for {len(self.rcpts)} recipients with message_id: {self.message_id}"
                )
                for rcpt in self.rcpts:
                    self.logger.debug(
                        f"[KAFKA_SENT] Writing SENT outbox event for {rcpt} with message_id: {self.message_id}"
                    )
                    self._trigger_kafka_sent(
                        message_id=self.message_id, recipient_email=rcpt
                    )

                # Finish span outbox.enqueue
                if outbox_enqueue_span:
                    try:
                        outbox_enqueue_span.end()
                        self.logger.debug("[OTEL] Span outbox.enqueue finished")
                    except Exception:
                        pass
            else:
                self.logger.warning(
                    f"Conditions not met to write the SENT event - message_id: {bool(self.message_id)}, rcpts: {len(self.rcpts) if self.rcpts else 0}"
                )

            # Shadow email for monitoring (if configured)
            # IMPORTANT: addrcpt must be called BEFORE returning from eom,
            # and needs the ADDRCPT flag set in __init__

            # Shadow email: the Redis configuration if available, otherwise the global one
            shadow_email_to_use = None
            if self.email_config and self.email_config.get('shadow_email'):
                shadow_email_to_use = self.email_config.get('shadow_email')
                self.logger.debug(
                    f"[SHADOW_EMAIL] Using the Redis shadow email for domain {self.domain_name}: {shadow_email_to_use}"
                )
            elif SHADOW_EMAIL:
                shadow_email_to_use = SHADOW_EMAIL
                self.logger.debug(
                    f"[SHADOW_EMAIL] Using the global shadow email: {shadow_email_to_use}"
                )

            if shadow_email_to_use:
                try:
                    # Is the ADDRCPT flag set?
                    if not hasattr(self, "_actions") or not (
                        self._actions & Milter.ADDRCPT
                    ):
                        self.logger.warning(
                            f"[SHADOW_EMAIL] ADDRCPT flag not set - shadow email not added. "
                            f"Original recipients: {len(self.rcpts)}"
                        )
                    else:
                        # Make sure the shadow email is not already an original recipient
                        shadow_email_normalized = shadow_email_to_use.strip().lower()
                        is_already_recipient = any(
                            rcpt.lower() == shadow_email_normalized
                            for rcpt in self.rcpts
                        )

                        if not is_already_recipient:
                            # Span routing.shadow, child of mail.message
                            routing_shadow_span = None
                            if OTEL_AVAILABLE and self._mail_message_span:
                                try:
                                    routing_shadow_span = otel_tracer.start_span("routing.shadow", context=trace.set_span_in_context(self._mail_message_span))
                                    routing_shadow_span.set_attribute("routing.shadow_email", str(shadow_email_to_use))
                                    self.logger.debug("[OTEL] Span routing.shadow created")
                                except Exception as e:
                                    self.logger.warning(f"[OTEL] Error creating span routing.shadow: {e}")

                            # Number of recipients before adding the shadow
                            recipients_before = len(self.rcpts)

                            # Add the shadow recipient to the SMTP envelope
                            # addrcpt() adds an envelope RCPT TO, not a header:
                            # the recipient gets the email but does not appear in To/Cc/Bcc
                            # (equivalent to a BCC from the end user's point of view)
                            try:
                                result = self.addrcpt(shadow_email_to_use)

                                # Finish span routing.shadow
                                if routing_shadow_span:
                                    try:
                                        routing_shadow_span.set_attribute("routing.shadow_added", result == Milter.CONTINUE if hasattr(Milter, 'CONTINUE') else True)
                                        routing_shadow_span.end()
                                        self.logger.debug("[OTEL] Span routing.shadow finished")
                                    except Exception:
                                        pass

                                if result == Milter.CONTINUE:
                                    self.logger.info(
                                        f"[SHADOW_EMAIL] Copy sent to the shadow email: {shadow_email_to_use} "
                                        f"(original recipients: {recipients_before})"
                                    )
                                else:
                                    self.logger.warning(
                                        f"[SHADOW_EMAIL] addrcpt returned {result} for {shadow_email_to_use} "
                                        f"(original recipients: {recipients_before}, unaffected)"
                                    )
                            except AttributeError as attr_error:
                                # Common error: addrcpt not available or flag not set
                                self.logger.error(
                                    f"[SHADOW_EMAIL] addrcpt not available or ADDRCPT flag not set: {attr_error}. "
                                    f"The original recipients ({recipients_before}) were not affected.",
                                    exc_info=True,
                                )
                            except Exception as addrcpt_error:
                                # An error adding the shadow email never blocks delivery
                                self.logger.error(
                                    f"[SHADOW_EMAIL] Error calling addrcpt for {SHADOW_EMAIL}: {addrcpt_error}. "
                                    f"The original recipients ({recipients_before}) were not affected.",
                                    exc_info=True,
                                )
                        else:
                            self.logger.debug(
                                f"[SHADOW_EMAIL] Shadow email {SHADOW_EMAIL} is already a recipient, not adding a duplicate"
                            )
                except Exception as shadow_error:
                    # An error adding the shadow email never blocks delivery
                    self.logger.error(
                        f"[SHADOW_EMAIL] Error processing shadow email {SHADOW_EMAIL}: {shadow_error}. "
                        f"The original recipients ({len(self.rcpts)}) were not affected.",
                        exc_info=True,
                    )

            return Milter.CONTINUE

        except Exception as e:
            # Never block delivery
            error_msg = f"error: {e.__class__.__name__}"
            self.logger.error(f"CRITICAL ERROR: {e} - {error_msg}")

            # Mark the span as an error, if any
            if OTEL_AVAILABLE and self._trace_span:
                try:
                    self._trace_span.record_exception(e)
                    self._trace_span.set_status(StatusCode.ERROR, str(e))
                    self._trace_span.end()
                    self._trace_span = None
                    self.logger.debug(f"[OTEL] Span milter.eom marked as error and finished: {e}")
                except Exception as span_error:
                    self.logger.warning(f"[OTEL] Error marking the span as error: {span_error}")

            # Try to add an error header (may fail)
            try:
                self.addheader("X-Connect-Tracked", error_msg)
                # Keep self.headers in sync with the added header
                if hasattr(self, "headers"):
                    self.headers.setdefault("x-connect-tracked", []).append(error_msg)
            except Exception as header_error:
                self.logger.debug(
                    f"Could not add the error header: {header_error}"
                )

            return Milter.CONTINUE
        finally:
            # Close the spans: ALWAYS, in the finally block (even on error)
            # OTel spans are exported automatically via OTLP BatchSpanProcessor
            if OTEL_AVAILABLE:
                try:
                    # Finish mail.message (skip if already ended)
                    if hasattr(self, '_mail_message_span') and self._mail_message_span:
                        try:
                            self._mail_message_span.end()
                            self._mail_message_span = None
                            self.logger.debug("[OTEL] Span mail.message finished")
                        except Exception:
                            pass

                    # Make sure smtp.envelope is finished (skip if already ended)
                    if hasattr(self, '_smtp_envelope_span') and self._smtp_envelope_span:
                        try:
                            self._smtp_envelope_span.end()
                            self._smtp_envelope_span = None
                        except Exception:
                            pass

                    # Make sure smtp.headers is finished (skip if already ended)
                    if hasattr(self, '_smtp_headers_span') and self._smtp_headers_span:
                        try:
                            self._smtp_headers_span.end()
                            self._smtp_headers_span = None
                        except Exception:
                            pass

                    # Finish mail.pipeline last (root span)
                    if hasattr(self, '_mail_pipeline_span') and self._mail_pipeline_span:
                        try:
                            milter_done_ts_ms = int(time.time() * 1000)
                            self._mail_pipeline_span.set_attribute("milter.done_ts_ms", str(milter_done_ts_ms))
                            self._mail_pipeline_span.end()
                            self._mail_pipeline_span = None
                            self.logger.debug(f"[OTEL] Span mail.pipeline finished (milter_done_ts_ms={milter_done_ts_ms})")
                        except Exception as e:
                            self.logger.warning(f"[OTEL] Error finishing mail.pipeline: {e}", exc_info=True)

                except Exception as e:
                    self.logger.warning(f"[OTEL] Error finishing spans: {e}", exc_info=True)

    def __del__(self):
        """
        Destructor, for cleanup.

        Runs the final cleanup when the milter instance is destroyed.
        """
        try:
            # Cancel the safety timeout
            if hasattr(self, "signal") and self.signal:
                self.signal.alarm(0)

            # Log the completion
            if hasattr(self, "start_time"):
                elapsed = time.time() - self.start_time
                if hasattr(self, "logger"):
                    self.logger.debug(
                        f"Connection finished - Total time: {elapsed:.3f}s"
                    )
        except Exception as cleanup_error:
            # Cleanup errors are ignored silently
            pass


def _start_malloc_trim_thread():
    """Periodically return freed heap memory to the OS via glibc malloc_trim(0).

    Why: this is a threaded milter (one worker thread per SMTP connection) whose
    per-message working set (body buffers, payload (de)serialization, client I/O)
    leaves freed memory in glibc's per-arena free lists, which glibc does NOT
    return to the OS. Without trimming, RSS ratchets up to the all-time
    high-water mark and, under sustained load, can reach the container memory
    limit. MALLOC_ARENA_MAX caps the arena COUNT but does not force release;
    malloc_trim(0) does.

    Interval via MALLOC_TRIM_INTERVAL_SEC (default 45s; set 0 to disable).
    """
    interval = int(os.getenv("MALLOC_TRIM_INTERVAL_SEC", "45"))
    if interval <= 0:
        logger.info("log_type=ops event=malloc_trim_disabled")
        return
    try:
        libc = ctypes.CDLL("libc.so.6")
        libc.malloc_trim.argtypes = [ctypes.c_size_t]
        libc.malloc_trim.restype = ctypes.c_int
    except Exception as e:
        logger.warning(f"malloc_trim thread disabled (libc unavailable): {e}")
        return

    def _loop():
        while True:
            time.sleep(interval)
            try:
                libc.malloc_trim(0)
            except Exception as e:
                logger.debug(f"malloc_trim failed: {e}")

    threading.Thread(target=_loop, name="malloc-trim", daemon=True).start()
    logger.info(
        f"log_type=ops event=malloc_trim_thread_started interval_sec={interval}"
    )


def main():
    """
    Main entry point of the tracking milter.

    Sets up logging, checks the configuration and starts the milter server
    listening on port 8901. Postfix connects to it to process emails in real
    time during SMTP delivery.

    Configuration:
        - Port: 8901 (inet:8901@0.0.0.0)
        - Timeout: 60 seconds
        - Log file: /var/log/revbridge-milter.log (if permissions allow)

    Handles a graceful shutdown on keyboard interrupt and reports
    configuration problems in detail.
    """

    # The container runs as `nobody` on a read-only filesystem, so the file
    # handler below normally fails; this is logged at debug level only.
    try:
        file_handler = logging.FileHandler("/var/log/revbridge-milter.log")
        logging.getLogger().addHandler(file_handler)
        log_destination = "/var/log/revbridge-milter.log + stdout"
    except (PermissionError, OSError) as e:
        logger.debug(f"File logging unavailable (running stdout-only): {e}")
        log_destination = "stdout"

    redis_ok = False
    try:
        redis.ping()
        redis_ok = True
    except Exception as e:
        logger.error(f"Redis not connected: {e}")
        logger.warning(
            "The cache will not work - emails will be processed without tracking"
        )

    # Silence the pymilter library's per-connection chatter ("Milter:", "close",
    # "hello from X"). It logs every SMTP handshake at info; traces and our own
    # audit lines already cover the flow.
    logging.getLogger("Milter").setLevel(logging.WARNING)
    logging.getLogger("milter").setLevel(logging.WARNING)

    # A single ops event at startup (log_type=ops event=service_started).
    logger.info(
        f"log_type=ops event=service_started component=mail-milter-service "
        f"version=v1.0.0 port=8901 log_destination={log_destination} "
        f"redis={'ok' if redis_ok else 'down'}"
    )

    # Return freed memory to the OS periodically (keeps RSS from ratcheting up)
    _start_malloc_trim_thread()

    # Milter factory, then start the server
    Milter.factory = TrackingMilter
    # Milter name, address (host, port), timeout (s)
    try:
        # Explicit endpoint (canonical port@host form), so the bind address is unambiguous.
        Milter.runmilter("revbridge-tracking", "inet:8901@0.0.0.0", 60)
    except KeyboardInterrupt:
        logger.info("Milter interrupted by the user")
    except Exception as e:
        logger.error(f"Error starting the milter: {e}")
        logger.error(
            "Check that port 8901 is available and that you have permission to bind it"
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
