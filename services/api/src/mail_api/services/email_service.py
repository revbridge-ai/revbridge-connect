"""
Email sending service
"""

import json
import logging
import os
import smtplib
import ssl
import time
import uuid
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate
from typing import Any

from mail_api.common.config import (
    EMAIL_QUEUE_KEY,
    EMAIL_QUEUE_PREFIX,
    REDIS_TTL_SEC,
    SMTP_BASE_PORT,
    SMTP_CONNECTION_REUSE,
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_REUSE_IDLE_TIMEOUT_SEC,
    SMTP_REUSE_MAX_CONNS,
    SMTP_REUSE_MAX_MESSAGES,
    SMTP_SECURE,
    SMTP_SKIP_DB_CONFIG,
    SMTP_USE_STARTTLS,
    SMTP_USER,
    get_smtp_credentials_for_email,
)
from mail_api.common.redis_client import get_prefixed_key, redis_client
from mail_api.models.email_models import (
    EmailResponse,
    EmailStatus,
    SendEmailRequest,
)
from mail_api.services.merge_tags import header_safe, resolve_tokens_v2

logger = logging.getLogger(__name__)


def _spf_aligned_envelope(
    from_address: str | None,
    smtp_host: str | None,
    envelope_prefix: str | None = None,
) -> str | None:
    """Envelope-from (Return-Path) on a subdomain with SPF, so Microsoft's SPF check
    (550 5.7.515) passes without touching the client's DNS.

    smtplib uses the From header as MAIL FROM when from_addr is omitted. The From
    domain (e.g. mail.example.com) often does NOT publish SPF, but the sending
    subdomain rbm<id>.<from-domain> does (it is the same domain that carries the
    DKIM key and the VERP bounces). Keeping the SAME mailbox and only switching the
    domain to the subdomain makes SPF pass and align (relaxed) with the From.
    The visible From domain does NOT change.

    The rbm<id> prefix comes from `envelope_prefix` (email_configs.envelope_prefix)
    when present, which decouples the identity from smtp_host: every domain can use
    one shared smtp_host with ONE certificate, without a per-domain SAN. Fallback:
    derived from the 2nd label of smtp_host (smtp.rbm1001.example.com -> rbm1001) for
    rows without the column. Returns None when it cannot be derived safely (the
    caller then uses the From).
    """
    if not from_address or "@" not in from_address:
        return None
    local, _, from_domain = from_address.partition("@")
    if not local or not from_domain:
        return None
    prefix = (envelope_prefix or "").strip()
    if not prefix:
        parts = (smtp_host or "").split(".")
        if len(parts) >= 3:
            prefix = parts[1]  # smtp.rbm1001.example.com -> rbm1001
    if not prefix.startswith("rbm"):
        return None
    # From is already on the SPF subdomain -> nothing to do (avoids rbm.rbm.<domain>).
    if from_domain.startswith(prefix + "."):
        return None
    return f"{local}@{prefix}.{from_domain}"


class MailService:
    """Email sending service with SMTP and Redis queue support"""

    def __init__(self):
        """Initializes the email service"""
        self.smtp_host = SMTP_HOST
        self.smtp_port = SMTP_PORT
        self.smtp_secure = SMTP_SECURE
        self.smtp_use_starttls = SMTP_USE_STARTTLS
        self.smtp_user = SMTP_USER
        self.smtp_password = SMTP_PASSWORD

        # Dynamic server-to-port mapping
        # server-X → X587 (e.g. server-1 → 1587, server-2 → 2587)
        self.base_port = SMTP_BASE_PORT

        # SSL context
        self.ssl_context = ssl.create_default_context()
        if not self.smtp_secure:
            self.ssl_context.check_hostname = False
            self.ssl_context.verify_mode = ssl.CERT_NONE

        # Pool of reusable SMTP connections (feature flag SMTP_CONNECTION_REUSE).
        # MULTI-SLOT: one live connection PER KEY (host,port,secure,starttls,user),
        # up to SMTP_REUSE_MAX_CONNS, with LRU eviction + idle timeout. Needed to
        # coexist with queue fairness (round-robin interleaves clients: with a single
        # slot, every client switch would close and reopen the connection, defeating
        # reuse and bringing back the connection churn). The worker processes the
        # queue serially, so there is no concurrency and no lock.
        # value: {key: (conn, message_count, monotonic_time_of_last_use)}
        self._pool: dict[tuple, tuple[smtplib.SMTP, int, float]] = {}

    @staticmethod
    def _conn_key(host, port, secure, starttls, user) -> tuple:
        """Identity of the SMTP connection. Reuse is only allowed between messages with
        the SAME key, i.e. the same relay authenticated as the same customer."""
        return (host or "", int(port or 0), bool(secure), bool(starttls), user or "")

    def _safe_quit(self, conn: smtplib.SMTP | None) -> None:
        """Closes an SMTP connection without propagating errors."""
        if conn is None:
            return
        try:
            conn.quit()
        except Exception:
            try:
                conn.close()
            except Exception:
                pass

    def _pool_checkout(self, key: tuple):
        """Takes the live connection of `key` out of the pool, or returns None.

        Ownership always moves out (the entry is removed): the returned connection
        is either reused (checked in again on success) or discarded (on error).
        Discards it if it reached the message cap, sat idle too long, or failed the
        liveness NOOP. Returns (conn, count) with count = messages already sent.
        """
        if not SMTP_CONNECTION_REUSE:
            return None
        entry = self._pool.pop(key, None)
        if entry is None:
            return None
        conn, count, last = entry
        if (
            count >= SMTP_REUSE_MAX_MESSAGES
            or (time.monotonic() - last) > SMTP_REUSE_IDLE_TIMEOUT_SEC
        ):
            self._safe_quit(conn)
            return None
        # Liveness: a cheap NOOP detects a socket closed by the server before we
        # try to send (avoids losing/duplicating the message on a dead socket).
        try:
            code, _ = conn.noop()
            if code != 250:
                self._safe_quit(conn)
                return None
        except Exception:
            self._safe_quit(conn)
            return None
        return conn, count

    def _pool_checkin(self, key: tuple, conn: smtplib.SMTP, count: int) -> None:
        """Returns the connection to the pool. Hygiene on the way in: evicts idle
        connections (idle > timeout) and, if the pool is full, evicts the least
        recently used one (LRU), keeping at most SMTP_REUSE_MAX_CONNS open sockets."""
        now = time.monotonic()
        stale = [
            k for k, (_, _, last) in self._pool.items()
            if (now - last) > SMTP_REUSE_IDLE_TIMEOUT_SEC
        ]
        for k in stale:
            old_conn, _, _ = self._pool.pop(k)
            self._safe_quit(old_conn)
        while len(self._pool) >= SMTP_REUSE_MAX_CONNS:
            lru_key = min(self._pool, key=lambda k: self._pool[k][2])
            old_conn, _, _ = self._pool.pop(lru_key)
            self._safe_quit(old_conn)
        self._pool[key] = (conn, count, now)

    def _get_server_port(self, server: str | None) -> int | None:
        """Returns the SMTP port for the given server"""
        if server is None or server == "":
            return None

        # Validate the server-X format
        import re

        server_pattern = r"^server-(\d+)$"
        match = re.match(server_pattern, server)

        if not match:
            raise ValueError(
                f"Server '{server}' must use the 'server-X' format (e.g. server-1, server-2)"
            )

        # Extract the server number
        server_number = int(match.group(1))

        # Compute the port dynamically: X587
        port = int(f"{server_number}{self.base_port}")

        return port

    async def _create_transporter(
        self, custom_port: int | None = None, from_email: str | None = None, api_key: str | None = None
    ) -> smtplib.SMTP:
        """Creates an SMTP connection with the credentials of the sender email"""
        try:
            # Use the custom port when given, otherwise the default one
            port = custom_port if custom_port is not None else self.smtp_port

            # Errno 99 guard: self.smtp_host defaults to 'localhost', where nothing
            # listens. Getting here means NO per-domain email_config was resolved (e.g.
            # a missing from_address). Connecting to localhost would only produce a
            # silent [Errno 99] that burns the message and hides the real failure.
            # Fail LOUDLY: the message is marked as failed/deferred and the caller
            # (the enqueuer) must include a from_address with a valid email_config.
            # SMTP_SKIP_DB_CONFIG=true (local development simulator) is the only
            # legitimate localhost case.
            if not SMTP_SKIP_DB_CONFIG and self.smtp_host in ("localhost", "127.0.0.1", "", None):
                raise RuntimeError(
                    f"[MAIL_SEND] Refusing to connect through '{self.smtp_host or '(empty)'}':{port} "
                    f"(SMTP_SKIP_DB_CONFIG=false) for from={from_email!r}: no per-domain SMTP "
                    f"config was resolved. Nothing listens on localhost, so this would fail "
                    f"with a silent [Errno 99]. The enqueuer must include a from_address with "
                    f"a valid email_config."
                )

            if self.smtp_secure:
                server = smtplib.SMTP_SSL(
                    self.smtp_host, port, context=self.ssl_context
                )
            else:
                server = smtplib.SMTP(self.smtp_host, port)
                if self.smtp_use_starttls:
                    server.starttls(context=self.ssl_context)

            # With SMTP_SKIP_DB_CONFIG=true, only use the env credentials (no database lookup)
            if SMTP_SKIP_DB_CONFIG:
                smtp_user, smtp_password = self.smtp_user, self.smtp_password
                logger.debug(
                    f"SMTP_SKIP_DB_CONFIG=true, using the env credentials: "
                    f"user={smtp_user or '(none)'}, password={'***' if smtp_password else '(none)'}"
                )
            elif from_email:
                # Credentials specific to the sender email
                smtp_user, smtp_password = await get_smtp_credentials_for_email(
                    from_email, api_key=api_key
                )
                logger.info(
                    f"Credentials for {from_email}: user={smtp_user}, password={'***' if smtp_password else None}"
                )
            else:
                smtp_user, smtp_password = self.smtp_user, self.smtp_password

            if smtp_user and smtp_password:
                server.login(smtp_user, smtp_password)
            return server
        except Exception as e:
            logger.error(f"Error creating the SMTP transporter: {e!s}")
            raise

    def _replace_tokens(
        self,
        text: str,
        substitutions: dict[str, str | None],
        token_policy: str = "legacy",
        html: bool = False,
    ) -> str:
        """
        Replaces tokens in the text with the given substitutions.

        token_policy="v2" (merge tags): the {{NAME|fallback}} grammar of
        mail_api.services.merge_tags: inline fallback, user.*/event.*, a missing
        value becomes empty, {{unsub_link}} is never touched.
        html=True (text/html parts only, v2 only): profile/event/recipient values
        are HTML-escaped; fallback and structural values go in raw.
        The legacy policy ignores html.

        token_policy="legacy" (default, byte-identical to the original behavior):
        recursively processes the substitution values to allow nested placeholders.
        For example, if substitutions['subject'] = "Hello {{first_name}}", it first
        replaces {{first_name}} inside the value of 'subject', then uses that
        processed value to replace {{subject}} in the text. A None value (JSON
        null) becomes "".
        """
        if token_policy == "v2":
            return resolve_tokens_v2(text, substitutions, html=html)

        # First, process the substitution values iteratively until they converge
        # (resolving every dependency between substitutions)
        processed_substitutions = {
            k: "" if v is None else str(v) for k, v in substitutions.items()
        }
        max_iterations = 20  # Prevent infinite loops
        iteration = 0

        while iteration < max_iterations:
            changed = False
            new_substitutions = {}

            for key, value in processed_substitutions.items():
                previous_value = value
                # Apply every substitution (except the key itself, to avoid loops)
                new_value = value
                for sub_key, sub_value in processed_substitutions.items():
                    if sub_key != key:  # Do not substitute the key itself
                        pattern = r"\{\{\s*" + re.escape(sub_key) + r"\s*\}\}"
                        # Callable replacement: values with backslashes (e.g. the "\N"
                        # of a MySQL export) go in literally, without escape parsing
                        new_value = re.sub(
                            pattern, lambda _m, _v=str(sub_value): _v, new_value
                        )

                new_substitutions[key] = new_value
                if new_value != previous_value:
                    changed = True

            processed_substitutions = new_substitutions

            # No changes: converged
            if not changed:
                break
            iteration += 1

        # Now replace the tokens in the text with the processed substitutions
        result = text
        for key, value in processed_substitutions.items():
            pattern = r"\{\{\s*" + re.escape(key) + r"\s*\}\}"
            result = re.sub(pattern, lambda _m, _v=str(value): _v, result)
        return result

    def _create_message(self, mail_data: dict[str, Any]) -> MIMEMultipart:
        """Builds a MIME message from the email data"""
        try:
            msg = MIMEMultipart("alternative")
        except Exception as e:
            logger.error(f"Error creating MIMEMultipart: {e!s}")
            raise

        try:
            # Headers
            for key, value in mail_data.get("headers", {}).items():
                msg[key] = str(value)
        except Exception as e:
            logger.error(f"Error processing headers: {e!s}")
            raise

        try:
            # From
            from_addr = mail_data["from"]
            msg["From"] = f"{from_addr['name']} <{from_addr['address']}>"
            # Sender = From: without it, some relays inject their own Sender header
            # (e.g. "Sender: <local>=<domain>@relay.example.net") and Outlook shows
            # "... on behalf of <client>", exposing the technical domain.
            if "Sender" not in msg:
                msg["Sender"] = msg["From"]
        except Exception as e:
            logger.error(f"Error processing from: {e!s}")
            raise

        try:
            # To - handle both single object and list formats
            to_data = mail_data["to"]
            if isinstance(to_data, list) and len(to_data) > 0:
                to_addr = to_data[0]  # Use first recipient
            else:
                to_addr = to_data
            msg["To"] = f"{to_addr['name']} <{to_addr['address']}>"
        except Exception as e:
            logger.error(f"Error processing to: {e!s}")
            raise

        try:
            # Date (RFC 5322 requires it; Postfix does not add it for submissions from other hosts,
            # and a message without it looks worse to spam filters)
            if "Date" not in msg:
                msg["Date"] = formatdate(localtime=False, usegmt=True)
            # Subject
            msg["Subject"] = mail_data["subject"]
        except Exception as e:
            logger.error(f"Error processing subject: {e!s}")
            raise

        try:
            # Reply-To (optional)
            reply_to = mail_data.get("reply_to")
            if reply_to:
                if isinstance(reply_to, dict):
                    # Format: {"name": "...", "address": "..."}
                    if reply_to.get("name"):
                        msg["Reply-To"] = f"{reply_to['name']} <{reply_to['address']}>"
                    else:
                        msg["Reply-To"] = reply_to["address"]
                else:
                    # Plain string format
                    msg["Reply-To"] = str(reply_to)
        except Exception as e:
            logger.warning(f"Error processing reply_to (continuing without it): {e!s}")

        try:
            # Content - handle both array format and direct html/text format
            content = mail_data.get("content", [])

            # Check if using direct html/text format (for backward compatibility)
            if "html" in mail_data or "text" in mail_data:
                # Direct format: add html and text directly
                if "text" in mail_data:
                    text_part = MIMEText(mail_data["text"], "plain", "utf-8")
                    msg.attach(text_part)
                if "html" in mail_data:
                    html_part = MIMEText(mail_data["html"], "html", "utf-8")
                    msg.attach(html_part)
            else:
                # Array format: process content array
                # Add plain text if available
                text_content = None
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "text/plain":
                        text_content = c
                        break

                if text_content:
                    text_part = MIMEText(text_content["value"], "plain", "utf-8")
                    msg.attach(text_part)

                # Add HTML if available
                html_content = None
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "text/html":
                        html_content = c
                        break

                if html_content:
                    html_part = MIMEText(html_content["value"], "html", "utf-8")
                    msg.attach(html_part)
        except Exception as e:
            logger.error(f"Error processing content: {e!s}")
            raise

        return msg

    async def send(
        self, mail_data: dict[str, Any], server: str | None = None, api_key: str | None = None
    ) -> dict[str, Any]:
        """
        Sends an email over SMTP

        Args:
            mail_data: Email data in the format:
                {
                    'headers': dict,
                    'to': {'name': str, 'address': str},
                    'from': {'name': str, 'address': str},
                    'subject': str,
                    'content': [{'type': str, 'value': str}],
                    'priority': int
                }
            server: Specific SMTP server (e.g. server-1, server-2) - optional

        Returns:
            dict: Information about the send
        """
        # Fields for the logs
        to_data = mail_data.get("to", {})
        from_data = mail_data.get("from", {})

        if isinstance(to_data, list) and len(to_data) > 0:
            to_address = to_data[0].get("address", "unknown")
        else:
            to_address = (
                to_data.get("address", "unknown")
                if isinstance(to_data, dict)
                else "unknown"
            )

        from_address = (
            from_data.get("address", "unknown")
            if isinstance(from_data, dict)
            else "unknown"
        )
        subject = mail_data.get("subject", "unknown")

        logger.info(
            f"[MAIL_SEND] Starting send: from={from_address}, to={to_address}, subject='{subject}', server={server}"
        )

        try:
            # Port for the given server
            smtp_port = self._get_server_port(server)
            port = smtp_port if smtp_port is not None else self.smtp_port
            logger.info(f"[MAIL_SEND] SMTP port selected: {port} (server={server})")

            # Build the MIME message
            try:
                logger.info("[MAIL_SEND] Building the MIME message...")
                msg = self._create_message(mail_data)
                logger.info(
                    f"[MAIL_SEND] MIME message built. Message-ID: {msg.get('Message-ID', 'N/A')}"
                )
            except Exception as create_error:
                logger.error(
                    f"[MAIL_SEND] Error building the MIME message for {to_address}: {create_error!s}",
                    exc_info=True,
                )
                raise

            # Resolve the SMTP config and build `open_fresh()` (opens a new connection).
            # The connection comes from one of two sources (the per-domain config in the
            # database, or the default config from env), but both go through the SAME
            # optional pool (SMTP_CONNECTION_REUSE) that reuses the session between
            # consecutive emails of the same customer.
            db_config = None
            conn_key: tuple

            if from_address and not SMTP_SKIP_DB_CONFIG:
                from mail_api.common.config import get_smtp_config_for_email
                # Config lookup: an error here (Redis/DB) has no useful fallback.
                # self.smtp_host defaults to 'localhost' (nothing listens there), so
                # falling back to it would only produce an Errno 99 that hides the real failure.
                try:
                    db_config = await get_smtp_config_for_email(from_address, use_cache=True)
                except Exception as e:
                    logger.error(f"[MAIL_SEND] Failed to fetch the SMTP configuration for {from_address}: {e}", exc_info=True)
                    raise

                if not db_config:
                    raise RuntimeError(
                        f"[MAIL_SEND] No email_config found for {from_address}. "
                        f"Create a config (POST /email/email-configs) before sending."
                    )

                domain = from_address.split("@")[1] if "@" in from_address else "unknown"
                logger.info(f"[MAIL_SEND] Using the SMTP configuration (Redis/database) for domain {domain}: {db_config.get('smtp_host')}:{db_config.get('smtp_port')}")
                temp_smtp_host = db_config.get('smtp_host') or self.smtp_host
                # Same guard: an email_config with an empty smtp_host would fall back to
                # self.smtp_host (localhost) → [Errno 99]. An invalid config must fail loudly.
                if temp_smtp_host in ("localhost", "127.0.0.1", "", None):
                    raise RuntimeError(
                        f"[MAIL_SEND] The email_config of {from_address} has an empty/localhost smtp_host "
                        f"(smtp_host={db_config.get('smtp_host')!r}): invalid config; "
                        f"refusing to send through localhost."
                    )
                temp_port = db_config.get('smtp_port') or (port if smtp_port is not None else self.smtp_port)
                temp_smtp_secure = db_config.get('smtp_secure', self.smtp_secure)
                temp_smtp_use_starttls = db_config.get('smtp_use_starttls', self.smtp_use_starttls)
                api_key_to_use = api_key or mail_data.get("api_key")
                smtp_user, smtp_password = await get_smtp_credentials_for_email(
                    from_address, api_key=api_key_to_use
                )
                # Connection identity key: includes smtp_user (the customer's credential).
                # Reuse only happens between messages with the identical key.
                conn_key = self._conn_key(
                    temp_smtp_host, temp_port, temp_smtp_secure, temp_smtp_use_starttls, smtp_user
                )

                async def open_fresh():
                    # Connect to the configured relay: an error here MUST surface with the
                    # right host/port in the log, never fall back to self.smtp_host (default
                    # localhost), which would hide an Errno 99 / port exhaustion on the real relay.
                    try:
                        if temp_smtp_secure:
                            conn = smtplib.SMTP_SSL(
                                temp_smtp_host, temp_port, context=self.ssl_context
                            )
                        else:
                            conn = smtplib.SMTP(temp_smtp_host, temp_port)
                            if temp_smtp_use_starttls:
                                conn.starttls(context=self.ssl_context)
                        if smtp_user and smtp_password:
                            conn.login(smtp_user, smtp_password)
                        logger.info(f"[MAIL_SEND] Connected to SMTP {temp_smtp_host}:{temp_port}")
                        return conn
                    except Exception as conn_error:
                        logger.error(
                            f"[MAIL_SEND] Error connecting to SMTP {temp_smtp_host}:{temp_port} for {to_address}: {conn_error!s}",
                            exc_info=True,
                        )
                        raise
            else:
                # SMTP_SKIP_DB_CONFIG=true (local development with a simulator) or a
                # missing from_address: uses self.smtp_host/self.smtp_port (env vars).
                conn_key = self._conn_key(
                    self.smtp_host, port, self.smtp_secure, self.smtp_use_starttls, self.smtp_user
                )
                api_key_to_use = api_key or mail_data.get("api_key")

                async def open_fresh():
                    try:
                        logger.info(
                            f"[MAIL_SEND] Connecting to SMTP {self.smtp_host}:{port}..."
                        )
                        conn = await self._create_transporter(
                            smtp_port, from_address, api_key=api_key_to_use
                        )
                        logger.info("[MAIL_SEND] Connected to SMTP")
                        return conn
                    except Exception as smtp_error:
                        logger.error(
                            f"[MAIL_SEND] Error connecting to SMTP {self.smtp_host}:{port} for {to_address}: {smtp_error!s}",
                            exc_info=True,
                        )
                        raise

            # Reuse a pooled connection (same key/customer) or open a new one.
            checked_out = self._pool_checkout(conn_key)
            if checked_out is not None:
                smtp_server, sent_count = checked_out
                reused = True
                logger.debug(
                    f"[MAIL_SEND] Reusing the SMTP connection (messages on this connection={sent_count})"
                )
            else:
                smtp_server = await open_fresh()
                sent_count = 0
                reused = False

            # Envelope-from (Return-Path) on a subdomain with SPF, so Microsoft's SPF
            # check (5.7.515) does not fail. Returns None when it cannot be derived
            # safely => smtplib uses the From.
            envelope_from = _spf_aligned_envelope(
                from_address,
                db_config.get("smtp_host") if db_config else None,
                db_config.get("envelope_prefix") if db_config else None,
            )
            if envelope_from:
                logger.info(
                    f"[MAIL_SEND] Envelope-from (Return-Path)={envelope_from} "
                    f"(subdomain with SPF; From unchanged)"
                )

            try:
                # Send the email - handle both single object and list formats
                logger.info(f"[MAIL_SEND] Sending the email over SMTP to {to_address}...")
                try:
                    result = smtp_server.send_message(
                        msg, from_addr=envelope_from, to_addrs=[to_address]
                    )
                except (
                    smtplib.SMTPServerDisconnected,
                    smtplib.SMTPConnectError,
                    ConnectionError,
                    BrokenPipeError,
                    OSError,
                ) as conn_err:
                    # The reused connection dropped (socket closed by the server between
                    # bursts): reopen ONCE and resend transparently. On a freshly opened
                    # connection, let the error surface to the existing
                    # deferral/fallback/retry flow.
                    if not reused:
                        raise
                    logger.warning(
                        f"[MAIL_SEND] Reused SMTP connection unavailable ({conn_err!s}); "
                        f"reopening and resending to {to_address}"
                    )
                    self._safe_quit(smtp_server)
                    smtp_server = await open_fresh()
                    sent_count = 0
                    reused = False
                    result = smtp_server.send_message(
                        msg, from_addr=envelope_from, to_addrs=[to_address]
                    )

                logger.info(
                    f"[MAIL_SEND] Email sent to {to_address}. SMTP result: {result}"
                )

                # Success: return the connection to the pool (reuse on) or close it.
                if SMTP_CONNECTION_REUSE:
                    self._pool_checkin(conn_key, smtp_server, sent_count + 1)
                else:
                    self._safe_quit(smtp_server)

                message_id = msg.get("Message-ID") or str(uuid.uuid4())
                return {
                    "message_id": message_id,
                    "status": "sent",
                    "timestamp": datetime.now().isoformat(),
                    "smtp_result": result,
                    "smtp_port_used": port,
                    "server_used": server,
                }

            except Exception:
                # Send failure: the connection may be in a dirty state (incomplete SMTP
                # transaction). NEVER return it to the pool: close it and let the error
                # surface to the existing deferral/fallback/retry flow.
                self._safe_quit(smtp_server)
                raise

        except Exception as e:
            logger.error(
                f"[MAIL_SEND] Error sending the email to {to_address}: {e!s}",
                exc_info=True,
            )
            raise

    async def send_with_config(
        self, mail_data: dict[str, Any], smtp_config: dict[str, Any]
    ) -> dict[str, Any]:
        """
        Sends an email with an explicit SMTP configuration (for fallback).

        Args:
            mail_data: Email data (same format as send())
            smtp_config: Dict with smtp_host, smtp_port, smtp_secure, smtp_user, smtp_password
        """
        to_data = mail_data.get("to", {})
        if isinstance(to_data, list) and len(to_data) > 0:
            to_address = to_data[0].get("address", "unknown")
        else:
            to_address = to_data.get("address", "unknown") if isinstance(to_data, dict) else "unknown"

        from_address = mail_data.get("from", {}).get("address", "unknown")
        fallback_host = smtp_config.get("smtp_host")
        fallback_port = smtp_config.get("smtp_port", 587)
        fallback_secure = smtp_config.get("smtp_secure", False)
        fallback_use_starttls = smtp_config.get("smtp_use_starttls", True)
        fallback_user = smtp_config.get("smtp_user", "")
        fallback_password = smtp_config.get("smtp_password", "")

        logger.info(
            f"[MAIL_SEND_FALLBACK] Sending through fallback {fallback_host}:{fallback_port} "
            f"from={from_address}, to={to_address}"
        )

        try:
            msg = self._create_message(mail_data)

            if fallback_secure:
                smtp_server = smtplib.SMTP_SSL(fallback_host, fallback_port, context=self.ssl_context)
            else:
                smtp_server = smtplib.SMTP(fallback_host, fallback_port)
                # Try STARTTLS if the server supports it, otherwise continue without it
                try:
                    smtp_server.starttls(context=self.ssl_context)
                except smtplib.SMTPNotSupportedError:
                    logger.debug(f"[MAIL_SEND_FALLBACK] STARTTLS not supported by {fallback_host}:{fallback_port}, continuing without TLS")

            if fallback_user and fallback_password:
                smtp_server.login(fallback_user, fallback_password)

            envelope_from = _spf_aligned_envelope(
                from_address,
                smtp_config.get("smtp_host"),
                smtp_config.get("envelope_prefix"),
            )

            try:
                result = smtp_server.send_message(
                    msg, from_addr=envelope_from, to_addrs=[to_address]
                )
                logger.info(
                    f"[MAIL_SEND_FALLBACK] Email sent through {fallback_host} to {to_address}"
                )
                message_id = msg.get("Message-ID") or str(uuid.uuid4())
                return {
                    "message_id": message_id,
                    "status": "sent",
                    "timestamp": datetime.now().isoformat(),
                    "smtp_result": result,
                    "smtp_port_used": fallback_port,
                    "fallback_host": fallback_host,
                }
            finally:
                smtp_server.quit()

        except Exception as e:
            logger.error(
                f"[MAIL_SEND_FALLBACK] Error sending through {fallback_host} to {to_address}: {e}",
                exc_info=True,
            )
            raise

    async def queue_email(
        self,
        domain_name: str,
        email_request: SendEmailRequest,
        api_key: str | None = None,
        customer_id: str | None = None,
        client_id: str | None = None,
        message_id: str | None = None,
    ) -> EmailResponse:
        """
        Adds an email to the processing queue

        Args:
            domain_name: Domain name
            email_request: Email request data
            api_key: API key for SMTP authentication
            message_id: Pre-generated ID (idempotency). If None, a new one is generated.

        Returns:
            EmailResponse: Response with the queued email's information
        """
        message_id = message_id or str(uuid.uuid4())
        total_recipients = sum(len(p.to) for p in email_request.personalizations)

        logger.info(
            f"[MAIL_QUEUE] Starting enqueue: message_id={message_id}, domain_name={domain_name}, recipients={total_recipients}"
        )
        logger.info(
            f"[MAIL_QUEUE] Sender: {email_request.from_.name} <{email_request.from_.address}>"
        )
        logger.info(f"[MAIL_QUEUE] Subject: {email_request.subject}")
        logger.info(
            f"[MAIL_QUEUE] Priority: {email_request.priority}, Server: {email_request.server}"
        )

        try:
            emails_enqueued = 0

            # Process the personalizations
            for idx, personalization in enumerate(email_request.personalizations):
                logger.info(
                    f"[MAIL_QUEUE] Processing personalization {idx+1}/{len(email_request.personalizations)}"
                )

                # Merge headers (personalization headers override the global ones)
                merged_headers = {**email_request.headers, **personalization.headers}
                logger.info(
                    f"[MAIL_QUEUE] Merged headers: {len(merged_headers)} headers"
                )
                if personalization.token_policy == "v2":
                    logger.info(
                        f"[MERGE-TAGS] token_policy=v2 message_id={message_id} "
                        f"recipients={len(personalization.to)} "
                        f"keys={len(personalization.substitutions)}"
                    )

                for recipient in personalization.to:
                    logger.info(
                        f"[MAIL_QUEUE] Processing recipient: {recipient.name} <{recipient.address}>"
                    )

                    # Replace tokens in the subject and content
                    subject = self._replace_tokens(
                        email_request.subject,
                        personalization.substitutions,
                        personalization.token_policy,
                    )
                    if personalization.token_policy == "v2":
                        subject = header_safe(subject)
                        # v2 carries profile/event data in the subject: no text in the log.
                        logger.info(
                            f"[MAIL_QUEUE] Subject processed: len={len(subject)}"
                        )
                    else:
                        logger.info(
                            f"[MAIL_QUEUE] Subject processed: '{subject[:50]}...'"
                        )

                    # Process the content
                    processed_content = []
                    for content in email_request.content:
                        processed_value = self._replace_tokens(
                            content.value,
                            personalization.substitutions,
                            personalization.token_policy,
                            html=content.type == "text/html",
                        )
                        # RevBridge Connect: with CONNECT_IMAGE_PROXY_WRAP=true the API wraps the
                        # <img> tags in the image proxy (solid background behind transparent PNGs).
                        # URLs that already point at an image proxy are skipped, so a URL is never
                        # wrapped twice. Off by default (templates may come wrapped already).
                        if content.type == "text/html" and os.getenv(
                            "CONNECT_IMAGE_PROXY_WRAP", "false"
                        ).lower() in ("1", "true", "yes"):
                            from mail_api.common.image_proxy import maybe_process_html_images

                            processed_value = await maybe_process_html_images(processed_value)
                        processed_content.append(
                            {"type": content.type, "value": processed_value}
                        )
                    logger.info(
                        f"[MAIL_QUEUE] Content processed: {len(processed_content)} parts ({', '.join([c['type'] for c in processed_content])})"
                    )

                    # Replace tokens in the headers
                    final_headers = {}
                    for key, value in merged_headers.items():
                        final_headers[key] = self._replace_tokens(
                            str(value),
                            personalization.substitutions,
                            personalization.token_policy,
                        )
                        if personalization.token_policy == "v2":
                            final_headers[key] = header_safe(final_headers[key])

                    logger.info(
                        f"[MAIL_QUEUE] Headers with tokens replaced: {len(final_headers)} headers"
                    )

                    # Build the email data
                    mail_data = {
                        "headers": final_headers,
                        "to": {"name": recipient.name, "address": recipient.address},
                        "from": {
                            "name": email_request.from_.name,
                            "address": email_request.from_.address,
                        },
                        "subject": subject,
                        "content": processed_content,
                        "priority": email_request.priority,
                        "server": email_request.server,
                    }

                    # Add reply_to if given in the request
                    if email_request.reply_to:
                        mail_data["reply_to"] = {
                            "name": email_request.reply_to.name or "",
                            "address": email_request.reply_to.address,
                        }

                    # Add straight to the Redis queue
                    queue_data = {
                        "domain_name": domain_name,
                        "message_id": message_id,
                        "mail_data": mail_data,
                        "timestamp": datetime.now().isoformat(),
                        "scheduled_at": getattr(email_request, "scheduled_at", None),
                        "api_key": api_key,  # The JWT's api_key, used for SMTP
                        # always the authenticated client (the payload's customer_id is ignored)
                        "customer_id": customer_id,
                        "client_id": client_id,
                    }

                    # Durable admission in the ledger BEFORE enqueuing: the source of truth
                    # on the sending side (enables at-least-once + dedupe + send×delivery
                    # reconciliation). Best-effort: it never blocks the enqueue.
                    from mail_api.services import send_ledger
                    await send_ledger.admit(
                        message_id=message_id,
                        rcpt=recipient.address,
                        domain_name=domain_name,
                        customer_id=customer_id,
                    )

                    # Add to the send queue
                    logger.info(
                        f"[MAIL_QUEUE] Adding the email to the Redis queue for {recipient.address}..."
                    )
                    await self._add_to_send_queue(queue_data)
                    emails_enqueued += 1
                    logger.info(
                        f"[MAIL_QUEUE] Email enqueued {emails_enqueued}/{total_recipients}: {recipient.address} (message_id={message_id})"
                    )

            logger.info(
                f"[MAIL_QUEUE] Enqueue complete: {emails_enqueued} emails enqueued out of {total_recipients} recipients (message_id={message_id})"
            )

            return EmailResponse(
                message_id=message_id,
                status=EmailStatus.QUEUED,
                domain_name=domain_name,
                queue="email-queue",
                timestamp=datetime.now().isoformat(),
                recipients_count=total_recipients,
            )

        except Exception as e:
            logger.error(
                f"[MAIL_QUEUE] Error enqueuing email {message_id} (domain_name={domain_name}): {e}",
                exc_info=True,
            )
            raise

    async def _add_to_send_queue(self, email_data: dict[str, Any]):
        """Adds the email to the send queue (supports scheduling via scheduled_at)"""
        from datetime import datetime

        import pytz

        message_id = email_data.get("message_id", "unknown")
        domain_name = os.getenv("DOMAIN_NAME", "default")

        # Is there a scheduled_at (scheduling)?
        scheduled_at_str = email_data.get("scheduled_at")
        scheduled_at_ts = None

        if scheduled_at_str:
            try:
                scheduled_at_dt = datetime.fromisoformat(scheduled_at_str.replace("Z", "+00:00"))
                scheduled_at_ts = scheduled_at_dt.timestamp()

                # In the future: add to the scheduled sorted set
                now_ts = datetime.now(pytz.UTC).timestamp()
                if scheduled_at_ts > now_ts:
                    logger.info(
                        f"[MAIL_QUEUE] Email {message_id} scheduled for {scheduled_at_str} "
                        f"(in {(scheduled_at_ts - now_ts)/60:.1f} minutes)"
                    )
                    await self._add_to_scheduled_queue(email_data, scheduled_at_ts, domain_name)
                    return
            except Exception as e:
                logger.warning(
                    f"[MAIL_QUEUE] Error processing scheduled_at for {message_id}: {e}. "
                    f"Sending immediately."
                )

        # No scheduled_at, or in the past: add to the LIST queue (immediate)
        # Recipient address for tracking
        mail_data = email_data.get("mail_data", {})
        to_data = mail_data.get("to", {})
        recipient_address = (
            to_data.get("address", "unknown")
            if isinstance(to_data, dict)
            else "unknown"
        )

        context_info = f"message_id={message_id}, recipient={recipient_address}, domain_name={domain_name}"

        try:
            from mail_api.common.config import (
                EMAIL_CUSTOMER_QUEUE_PREFIX,
                EMAIL_CUSTOMER_REGISTRY_KEY,
                EMAIL_QUEUE_TOKEN,
                QUEUE_FAIRNESS_ENABLED,
            )

            async def _enqueue(item_json: str):
                """Fairness ON: item in the client's queue + a TOKEN in e:queue (which
                keeps the workers' blocking BRPOP unchanged). OFF: classic behavior
                (item straight into e:queue). Order item→registry→token: a crash in
                the middle leaves a dormant item (a window of milliseconds, visible
                as 'queued' in the ledger), never a token without an item."""
                if QUEUE_FAIRNESS_ENABLED:
                    fair_cid = str(
                        email_data.get("customer_id")
                        or email_data.get("client_id")
                        or email_data.get("domain_name")
                        or "default"
                    )
                    await redis_client.rpush(
                        f"{EMAIL_CUSTOMER_QUEUE_PREFIX}{fair_cid}", item_json
                    )
                    await redis_client.sadd(EMAIL_CUSTOMER_REGISTRY_KEY, fair_cid)
                    await redis_client.rpush(EMAIL_QUEUE_KEY, EMAIL_QUEUE_TOKEN)
                else:
                    await redis_client.rpush(EMAIL_QUEUE_KEY, item_json)

            # Inline payload in the queue (FIFO via RPUSH); Redis has AOF enabled.
            email_json = json.dumps(email_data, default=str)
            await _enqueue(email_json)

            # SADD: tracks pending recipients per message_id
            pending_key = f"e:pending:{message_id}"
            await redis_client.sadd(pending_key, recipient_address)
            await redis_client.expire(pending_key, REDIS_TTL_SEC)

            logger.info(
                f"[MAIL_QUEUE] Email added to the queue "
                f"(RPUSH {EMAIL_QUEUE_KEY}, mode=inline): {context_info}"
            )
        except Exception as e:
            logger.error(
                f"[MAIL_QUEUE] CRITICAL FAILURE adding the email to the Redis queue: {context_info}. Error: {e}",
                exc_info=True,
            )
            raise

    async def _add_to_scheduled_queue(
        self,
        email_data: dict[str, Any],
        scheduled_at_ts: float,
        domain_name: str
    ):
        """
        Adds the email to the sorted set of scheduled emails.

        Uses a Redis ZSET with score = scheduled timestamp.
        The queue processor checks this sorted set periodically.
        """
        from datetime import datetime

        import pytz

        message_id = email_data.get("message_id", "unknown")

        # Recipient, for the unique key
        mail_data = email_data.get("mail_data", {})
        to_data = mail_data.get("to", {})
        recipient_address = (
            to_data.get("address", "unknown")
            if isinstance(to_data, dict)
            else "unknown"
        )

        unique_key = f"{message_id}:{recipient_address}"

        # Sorted set key: e:scheduled:{domain_name}
        scheduled_set_key = get_prefixed_key(
            f"scheduled:{domain_name}",
            EMAIL_QUEUE_PREFIX,
            domain_name=domain_name
        )

        # Value = JSON of the full email
        email_json = json.dumps(email_data)

        try:
            # ZADD: adds to the sorted set with score = timestamp
            await redis_client.zadd(
                scheduled_set_key,
                {unique_key: scheduled_at_ts}
            )
            # Registry of the domains with scheduled sends: the queue processors walk it
            # to release them (one sorted set per domain)
            await redis_client.sadd(f"{EMAIL_QUEUE_PREFIX}:scheduled_domains", domain_name)

            # Also store the full payload under a separate key
            # (the ZSET only holds the key, the payload is needed too)
            email_key = get_prefixed_key(
                unique_key,
                EMAIL_QUEUE_PREFIX,
                domain_name=domain_name
            )
            # TTL = time until scheduled_at + 1 hour of margin
            ttl = int(scheduled_at_ts - datetime.now(pytz.UTC).timestamp()) + 3600
            await redis_client.setex(email_key, ttl, email_json)

            logger.info(
                f"[MAIL_QUEUE] Email {message_id} added to the scheduled sorted set "
                f"(scheduled_at_ts={scheduled_at_ts}, ttl={ttl}s)"
            )
        except Exception as e:
            logger.error(
                f"[MAIL_QUEUE] Error adding the scheduled email to the queue: {e}",
                exc_info=True
            )
            raise


# Regex import (used by the token replacement)
import re
