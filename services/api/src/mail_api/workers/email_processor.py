"""
Redis queue processor for emails
Processes the queued messages directly
"""

import asyncio
import json
import logging
import os
import errno
import smtplib
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from mail_api.common.redis_client import redis_client
from mail_api.models.email_models import EmailStatus
from mail_api.services.email_service import MailService

logger = logging.getLogger(__name__)


def _redact_url(url: str) -> str:
    """URL without credentials (user:password@); scheme, host, port and db stay in the log.

    The startup log prints the Redis URL, and the password must never be in it."""
    if not url:
        return str(url)
    parts = urlsplit(url)
    return urlunsplit(parts._replace(netloc=parts.netloc.rpartition("@")[2]))


class EmailProcessor:
    """Redis queue processor for emails"""

    def __init__(self):
        self.mail_service = MailService()
        self.running = False
        # Worker identity for the send_ledger claim (host + pid).
        self.worker_id = f"{os.getenv('HOSTNAME', 'worker')}:{os.getpid()}"
        # Fairness round-robin with a QUANTUM: serves up to N consecutive items of
        # the same client before rotating. Pure alternation (quantum=1) would defeat
        # SMTP connection reuse (every client switch closes/reopens the session).
        self._rr_index = 0
        self._rr_current: str | None = None
        self._rr_served = 0

    async def _rr_pop_customer_item(self):
        """Round-robin with a quantum across the client queues (fairness).

        Keeps serving the client of the current turn up to QUEUE_FAIRNESS_QUANTUM
        items (its SMTP connection pays off within the turn); once the quantum (or
        its queue) runs out, rotates through the e:q:customers SET and RPOPs the
        first non-empty queue, so each active client gets one turn per round,
        regardless of the campaign size. An empty queue leaves the registry (lazily;
        the producer SADDs it again on every push). Returns the item JSON, or None
        if no registered queue had an item."""
        from mail_api.common import config

        # Turn in progress: stay on the same client until the quantum or the queue runs out.
        if self._rr_current is not None and self._rr_served < config.QUEUE_FAIRNESS_QUANTUM:
            item = await redis_client.rpop(
                f"{config.EMAIL_CUSTOMER_QUEUE_PREFIX}{self._rr_current}"
            )
            if item is not None:
                self._rr_served += 1
                return item.decode() if isinstance(item, bytes) else item
        self._rr_current = None

        members = await redis_client.smembers(config.EMAIL_CUSTOMER_REGISTRY_KEY)
        if not members:
            return None
        customers = sorted(
            m.decode() if isinstance(m, bytes) else str(m) for m in members
        )
        n = len(customers)
        self._rr_index = (self._rr_index + 1) % n
        for offset in range(n):
            cid = customers[(self._rr_index + offset) % n]
            item = await redis_client.rpop(f"{config.EMAIL_CUSTOMER_QUEUE_PREFIX}{cid}")
            if item is not None:
                self._rr_current = cid
                self._rr_served = 1
                return item.decode() if isinstance(item, bytes) else item
            # Empty: lazy registry cleanup. If the producer did RPUSH+SADD between
            # our empty RPOP and the SREM, the SREM removed a valid registration;
            # the LLEN after the SREM catches that and registers it again (otherwise
            # the item would stay out of the RR until this client's next send).
            queue_key = f"{config.EMAIL_CUSTOMER_QUEUE_PREFIX}{cid}"
            await redis_client.srem(config.EMAIL_CUSTOMER_REGISTRY_KEY, cid)
            if await redis_client.llen(queue_key):
                await redis_client.sadd(config.EMAIL_CUSTOMER_REGISTRY_KEY, cid)
        return None

    async def start(self):
        """Starts the queue processor"""
        self.running = True
        logger.info("Starting the email queue processor")

        # Check the Redis connection and configuration
        try:
            await redis_client.ping()
            logger.info("[QUEUE_PROCESSOR] Redis connection checked")

            from mail_api.common.config import EMAIL_QUEUE_KEY, REDIS_URL

            # Size of the LIST queue
            try:
                queue_len = await redis_client.llen(EMAIL_QUEUE_KEY)
                logger.info(
                    f"[QUEUE_PROCESSOR] Emails in the LIST queue ({EMAIL_QUEUE_KEY}): {queue_len}"
                )
            except Exception as e:
                logger.warning(f"[QUEUE_PROCESSOR] Error checking the LIST queue: {e}")

            logger.info(f"[QUEUE_PROCESSOR] Configured Redis URL: {_redact_url(REDIS_URL)}")

            # One-time: migrate legacy STRING keys (e:*:*) to the LIST
            await self._migrate_legacy_keys()

        except Exception as redis_check_error:
            logger.error(
                f"[QUEUE_PROCESSOR] Error checking Redis: {redis_check_error}"
            )

        try:
            await self._process_email_queue()
        except Exception as e:
            logger.error(f"Error in the queue processor: {e}")
        finally:
            self.running = False

    async def _migrate_legacy_keys(self):
        """Migrates legacy STRING keys (e:*:*) to the LIST queue (one-time, at startup)."""
        from mail_api.common.config import EMAIL_QUEUE_KEY, EMAIL_QUEUE_PREFIX

        try:
            pattern = f"{EMAIL_QUEUE_PREFIX}:*:*"
            old_keys = await redis_client.scan_keys(pattern)
            # Filter out keys that must NOT be migrated:
            # - scheduled (ZSET) and pending (SET): different format
            # - resend/resent: keys of the old deferred resender (removed)
            # - queue: the target LIST itself
            old_keys = [
                k for k in old_keys
                if ":scheduled:" not in k
                and ":pending:" not in k
                and ":resend:" not in k
                and ":resent:" not in k
                and k != EMAIL_QUEUE_KEY
            ]

            if not old_keys:
                logger.info("[MIGRATION] No legacy key found to migrate")
                return

            migrated = 0
            skipped = 0
            for key in old_keys:
                try:
                    # Check the key type - only migrate STRING
                    key_type = await redis_client.key_type(key)
                    if key_type != "string":
                        logger.debug(f"[MIGRATION] Skipping key {key} (type={key_type}, expected=string)")
                        skipped += 1
                        continue

                    email_json = await redis_client.get(key)
                    if email_json:
                        await redis_client.rpush(EMAIL_QUEUE_KEY, email_json)
                        await redis_client.delete(key)
                        migrated += 1
                except Exception as e:
                    logger.warning(f"[MIGRATION] Error migrating key {key}: {e}")

            logger.info(f"[MIGRATION] Migrated {migrated} of {len(old_keys)} legacy keys to {EMAIL_QUEUE_KEY} (skipped={skipped})")
        except Exception as e:
            logger.warning(f"[MIGRATION] Error migrating legacy keys: {e}")

    async def stop(self):
        """Stops the queue processor"""
        self.running = False
        logger.info("Stopping the email queue processor")

    async def _process_email_queue(self):
        """Processes the send queue via BRPOP (blocking, O(1))."""
        from mail_api.common.config import (
            EMAIL_BRPOP_TIMEOUT,
            EMAIL_QUEUE_KEY,
            EMAIL_QUEUE_TOKEN,
        )

        MAX_RETRIES = 3

        while self.running:
            try:
                # FIRST: move the scheduled emails whose time has come
                await self._process_scheduled_emails()

                # BRPOP: blocks until there is an item or the timeout
                result = await redis_client.brpop(EMAIL_QUEUE_KEY, timeout=EMAIL_BRPOP_TIMEOUT)

                if not result and not self.running:
                    continue  # stop() during the BRPOP: no new work
                synthetic_permit = not result
                if synthetic_permit:
                    # e:queue was empty for the whole timeout: a synthetic permit goes
                    # through the RR looking for an item WITHOUT a token (a worker died
                    # between the BRPOP of the token and the RPOP of the item, or a
                    # network error in between). Without an item, the permit dies in the
                    # RR itself; cost: 1 SMEMBERS per timeout. This sweep is what makes
                    # it safe to drop a token without an item (below) without leaving a
                    # dormant item behind.
                    result = (EMAIL_QUEUE_KEY, EMAIL_QUEUE_TOKEN)

                # result = (key, value)
                _, email_json = result

                # Decode bytes if needed
                if isinstance(email_json, bytes):
                    email_json = email_json.decode("utf-8")

                # Parse JSON
                try:
                    email = json.loads(email_json)
                except (json.JSONDecodeError, Exception) as json_error:
                    logger.error(
                        f"[QUEUE_ERROR] Error parsing the queued email JSON: {json_error}. "
                        f"Content (first 500 chars): {email_json[:500]}",
                        exc_info=True,
                    )
                    continue

                # Validate the basic structure
                if not isinstance(email, dict):
                    logger.error(f"[QUEUE_ERROR] Email is not a dictionary: {type(email)}")
                    continue

                # Fairness TOKEN: e:queue only carries a permit (1 per item); the real
                # item lives in the client's queue, and we pick WHICH client to pull
                # from via round-robin, so a huge campaign gets about 1/N of the
                # throughput instead of holding every worker. The consumer ALWAYS
                # understands tokens (whatever the producer flag) for a safe rollback.
                from_token = bool(email.get("_t"))

                from mail_api.common.config import SEND_LEDGER_LEASE_SEC
                from mail_api.services import send_ledger

                # A permit covers at most ONE real send: a dropped item (a zombie
                # already terminal in the ledger, invalid JSON, missing fields) does
                # NOT consume the token; the next RR item is pulled under the SAME
                # permit. Otherwise every zombie would steal a token and the real item
                # would wait for a new token from another send.
                claimed_email = None
                popped_under_permit = 0
                while True:
                    if from_token:
                        item_json = await self._rr_pop_customer_item()
                        if item_json is None:
                            # No client queue had an item: the permit dies here and
                            # NEVER goes back to e:queue. The producer writes
                            # item→registry→token, so a visible token without a visible
                            # item is not waiting for any item: it is surplus (the
                            # zombie skip in this loop consumes N items with 1 permit).
                            # Putting it back would create an eternal token
                            # (BRPOP→None→RPUSH) that buries the retries (LPUSH) and
                            # keeps the queue from draining.
                            if popped_under_permit == 0 and not synthetic_permit:
                                logger.info(
                                    "[QUEUE_FAIRNESS] Token without an item dropped (surplus)"
                                )
                            break
                        popped_under_permit += 1
                        if synthetic_permit and popped_under_permit == 1:
                            logger.info(
                                "[QUEUE_FAIRNESS] Idle sweep found an item without a token "
                                "in a client queue"
                            )
                        try:
                            email = json.loads(item_json)
                        except (json.JSONDecodeError, Exception) as json_error:
                            logger.error(
                                f"[QUEUE_ERROR] Invalid client queue item: {json_error}. "
                                f"Content (500 chars): {item_json[:500]}"
                            )
                            continue
                        if not isinstance(email, dict):
                            logger.error(f"[QUEUE_ERROR] Client item is not a dict: {type(email)}")
                            continue

                    message_id = email.get("message_id")
                    domain_name = email.get("domain_name")

                    if not message_id or not domain_name:
                        logger.error(
                            f"[QUEUE_ERROR] Email missing required fields: "
                            f"message_id={message_id}, domain_name={domain_name}"
                        )
                        if from_token:
                            continue
                        break

                    # Recipient, for tracking
                    mail_data = email.get("mail_data", {})
                    to_data = mail_data.get("to", {})
                    recipient_address = (
                        to_data.get("address", "unknown")
                        if isinstance(to_data, dict)
                        else "unknown"
                    )

                    # Remove the recipient from the pending SET
                    pending_key = f"e:pending:{message_id}"
                    try:
                        await redis_client.srem(pending_key, recipient_address)
                    except Exception as srem_error:
                        logger.warning(f"[QUEUE_PROCESSOR] Error in SREM of the pending key: {srem_error}")

                    logger.info(
                        f"[QUEUE_PROCESSOR] Email received via BRPOP: message_id={message_id}, "
                        f"recipient={recipient_address}, domain={domain_name}"
                    )

                    # Atomic claim in the ledger BEFORE SMTP: makes sure only one worker
                    # sends this (message_id, rcpt). rowcount=0 => already claimed by
                    # another worker (valid lease) or already terminal => SKIP (dedupe,
                    # avoids sending twice). Fail-open: a database error makes claim()
                    # return True and the send goes on (delivery first).
                    claimed = await send_ledger.claim(
                        message_id, recipient_address, domain_name,
                        self.worker_id, SEND_LEDGER_LEASE_SEC,
                    )
                    if not claimed:
                        logger.info(
                            f"[LEDGER] {message_id} -> {recipient_address} already claimed "
                            f"or terminal; skipping (dedupe)."
                        )
                        if from_token:
                            continue
                        break

                    claimed_email = email
                    break

                if claimed_email is None:
                    continue
                email = claimed_email

                # Process the email
                try:
                    await self._process_single_email(email)
                    logger.info(
                        f"Email processed: {message_id} -> {recipient_address} (domain {domain_name})"
                    )
                except Exception as process_error:
                    logger.error(
                        f"Error processing email {message_id}: {process_error}",
                        exc_info=True,
                    )
                    # Retry: put it back in the queue with a retry counter
                    retry_count = email.get("_retry_count", 0)
                    if retry_count < MAX_RETRIES:
                        try:
                            email["_retry_count"] = retry_count + 1
                            retry_json = json.dumps(email, default=str)
                            # LPUSH (not RPUSH): consumption is via BRPOP (the tail of
                            # the list), so LPUSH puts the retry at the HEAD of the list =
                            # consumed last (end of the queue), instead of being popped
                            # again right away. Avoids a LIFO hot loop of retries against
                            # an already overloaded relay (a retry storm).
                            await redis_client.lpush(EMAIL_QUEUE_KEY, retry_json)
                            # Add it back to the pending SET for tracking
                            await redis_client.sadd(pending_key, recipient_address)
                            # Ledger: back to 'queued' (releases the lease) so the
                            # re-enqueued item can be claimed again. WITHOUT this, the
                            # claim would find state='sending' with a valid lease and
                            # SKIP the retry -> lost email.
                            from mail_api.services import send_ledger
                            await send_ledger.mark_requeued(
                                message_id, recipient_address, str(process_error)
                            )
                            logger.info(
                                f"Email {message_id} put back in the queue for retry "
                                f"({retry_count + 1}/{MAX_RETRIES})"
                            )
                        except Exception as restore_error:
                            logger.warning(
                                f"Error putting email {message_id} back in the queue: {restore_error}"
                            )
                    else:
                        logger.error(
                            f"Email {message_id} reached max retries ({MAX_RETRIES}), dropping it"
                        )
                        # Ledger: terminal in deadletter (out of attempts).
                        from mail_api.services import send_ledger
                        await send_ledger.mark_deadletter(
                            message_id, recipient_address, str(process_error)
                        )

            except Exception as e:
                logger.error(f"Error processing the email queue: {e}", exc_info=True)
                await asyncio.sleep(1)

    async def _process_single_email(self, email_data: dict[str, Any]):
        """Processes a single email"""
        domain_name = email_data.get("domain_name")
        message_id = email_data.get("message_id")
        # Set early so it is available in the except (e.g. an empty mail_data
        # raises before the recipient is extracted); used by the ledger.
        to_address = "unknown"

        try:
            mail_data = email_data.get("mail_data", {})

            if not mail_data:
                raise ValueError("mail_data is empty or missing")

            # Recipient, for the log
            to_data = mail_data.get("to")
            to_address = "unknown"
            if to_data and isinstance(to_data, list) and len(to_data) > 0:
                first_to = to_data[0]
                if isinstance(first_to, dict):
                    to_address = first_to.get("address", "unknown")
                else:
                    to_address = str(first_to)
            elif to_data and isinstance(to_data, dict):
                to_address = to_data.get("address", "unknown")

            logger.info(
                f"Processing email {message_id} to {to_address} (domain {domain_name})"
            )

            # Record the start of the processing
            try:
                await self._log_email_event(
                    domain_name=domain_name,
                    message_id=message_id,
                    status=EmailStatus.SENDING,
                    metadata={"raw_email": mail_data},
                )
            except Exception as log_error:
                logger.warning(
                    f"Error recording the start log for {message_id}: {log_error}"
                )

            # Custom SMTP server, if any
            server = mail_data.get("server")

            # Fetch the domain configuration from Redis before sending (optimization)
            # This preloads the configuration and makes sure it is in the cache
            try:
                from mail_api.common.email_config_cache import get_email_config_for_domain_name_from_redis

                if domain_name:
                    domain_config = await get_email_config_for_domain_name_from_redis(domain_name)
                    if domain_config:
                        logger.info(
                            f"[QUEUE_PROCESSOR] Configuration found in Redis for domain {domain_name}: "
                            f"SMTP={domain_config.get('smtp_host')}:{domain_config.get('smtp_port')}, "
                            f"Tracking={domain_config.get('tracking_enabled', True)}"
                        )
                        # Apply the config's default_reply_to when the request has none
                        if not mail_data.get("reply_to") and domain_config.get("default_reply_to"):
                            mail_data["reply_to"] = domain_config["default_reply_to"]
                            logger.info(
                                f"[QUEUE_PROCESSOR] Using the config's default_reply_to: "
                                f"{domain_config['default_reply_to']}"
                            )
                        # The settings are used automatically by mail_service.send(),
                        # which reads them from Redis when it has the from_address
                    else:
                        logger.debug(
                            f"[QUEUE_PROCESSOR] Configuration not found in Redis for domain {domain_name}, "
                            f"mail_service will fetch it if needed"
                        )
            except Exception as config_error:
                logger.debug(
                    f"[QUEUE_PROCESSOR] Error reading the configuration from Redis: {config_error} "
                    f"(mail_service will fetch it if needed)"
                )

            # End-to-end identity: stamps the ledger's message_id in the X-Connect-ID
            # header (which the milter PREFERS over Message-ID). Without it, the email
            # leaves without a Message-ID, Postfix generates one (<rbm-...>), the milter
            # admits/delivers under THAT identity and the worker's uuid row never gets
            # the delivered event: the reaper would see a "loss" for delivered emails
            # and resend them (mass duplicates). setdefault keeps an X-Connect-ID
            # sent by the caller.
            mail_data.setdefault("headers", {}).setdefault("X-Connect-ID", message_id)

            # Send the email (with fallback to alternative configs on a 4xx)
            api_key = email_data.get("api_key")
            result = await self._send_with_fallback(
                mail_data=mail_data,
                server=server,
                api_key=api_key,
                domain_name=domain_name,
                message_id=message_id,
                to_address=to_address,
            )

            # The relay accepted it (2xx): record the submission success in the ledger
            # and arm the reaper's delivery_deadline. Done right here (before the log)
            # so that, after a crash, the recovery knows it was already handed to
            # Postfix and does NOT resend it immediately.
            from mail_api.common.config import LEDGER_DELIVERY_DEADLINE_SEC
            from mail_api.services import send_ledger
            await send_ledger.mark_accepted(
                message_id, to_address, LEDGER_DELIVERY_DEADLINE_SEC
            )

            # Record the success
            try:
                await self._log_email_event(
                    domain_name=domain_name,
                    message_id=message_id,
                    status=EmailStatus.SENT,
                    metadata={"raw_email": mail_data, "result": result},
                )
            except Exception as log_error:
                logger.warning(
                    f"Error recording the success log for {message_id}: {log_error}"
                )

        except Exception as e:
            from mail_api.common.config import WORKER_REQUEUE_DEFERRED

            # A TEMPORARY failure (4xx, disconnect, rate limiting) is recoverable and
            # NOT terminal: re-raise it so _process_email_queue re-enqueues it instead
            # of treating it as a success and dropping the payload.
            if WORKER_REQUEUE_DEFERRED and self._is_smtp_deferred(e):
                logger.warning(
                    f"Recoverable deferral on email {message_id} "
                    f"(domain {domain_name}): {e!s}. Re-enqueuing for retry."
                )
                raise

            # PERMANENT failure (5xx, auth, invalid payload): terminal.
            logger.error(
                f"Permanent failure on email {message_id} (domain {domain_name}): {e!s}",
                exc_info=True,
            )

            # Ledger: terminal in 'failed'.
            from mail_api.services import send_ledger
            await send_ledger.mark_failed(
                message_id, to_address, str(e), getattr(e, "smtp_code", None)
            )

            # 'failed' event in the outbox: a failure before Postfix never goes through
            # the milter (the normal audit), so webhooks only learn about it from here.
            # Best-effort: it must not hide the real error.
            try:
                from mail_api.common.kafka_client import kafka_client

                hdrs = {str(k).lower(): v for k, v in (mail_data.get("headers") or {}).items()}
                smtp_code = getattr(e, "smtp_code", None)
                kafka_client.queue_email_failed(
                    message_id=message_id,
                    recipient_email=to_address,
                    domain_name=domain_name,
                    error_message=str(e)[:500],
                    error_code=str(smtp_code) if smtp_code else None,
                    # campaign_id: passed through from the client's own X-Connect-Campaign header
                    campaign_id=hdrs.get("x-connect-campaign"),
                )
            except Exception as evt_err:
                logger.error(
                    f"Error enqueuing the failed event for {message_id}: {evt_err}",
                    exc_info=True,
                )

            # Record the terminal failure (best-effort; the real audit is the milter)
            try:
                await self._log_email_event(
                    domain_name=domain_name,
                    message_id=message_id,
                    status=EmailStatus.FAILED,
                    metadata={
                        "raw_email": email_data.get("mail_data", {}),
                        "error": str(e),
                    },
                )
            except Exception as log_error:
                logger.error(
                    f"Error recording the failure log for {message_id}: {log_error}",
                    exc_info=True,
                )

    def _is_smtp_deferred(self, error: Exception) -> bool:
        """Whether the SMTP error means a deferral (4xx)"""
        if isinstance(error, smtplib.SMTPResponseException):
            return 400 <= error.smtp_code < 500
        # Some connection errors also mean the relay is overloaded
        if isinstance(error, (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError)):
            return True
        # RevBridge Connect: an MTA briefly down (starting, restarting, network) arrives
        # here as a raw socket OSError (e.g. [Errno 111] Connection refused), not as an
        # SMTPConnectError; it is treated as temporary.
        if isinstance(error, (ConnectionError, TimeoutError)):
            return True
        if isinstance(error, OSError) and getattr(error, "errno", None) in (
            errno.ECONNREFUSED, errno.ECONNRESET, errno.ECONNABORTED, errno.EHOSTUNREACH,
            errno.ENETUNREACH, errno.ETIMEDOUT, errno.EPIPE,
        ):
            return True
        # Look for common deferral patterns in the error message
        error_msg = str(error).lower()
        return any(kw in error_msg for kw in [
            "deferred", "try again later", "rate limit", "too many",
            "temporarily", "421", "450", "451", "452",
        ])

    async def _send_with_fallback(
        self,
        mail_data: dict[str, Any],
        server: str | None,
        api_key: str | None,
        domain_name: str,
        message_id: str,
        to_address: str,
    ) -> dict[str, Any]:
        """
        Tries to send with the default config. On a 4xx (deferred), fetches the
        fallback configs of the same domain and tries each one.
        """
        # 1. Try the default config (normal flow)
        primary_error: Exception | None = None
        try:
            result = await self.mail_service.send(mail_data, server, api_key=api_key)
            logger.info(f"Email {message_id} sent to {to_address}")
            return result
        except Exception as exc:
            # Bind to outer scope: the `except as X:` name is deleted when the
            # block exits (PEP 3134), so storing it here lets the fallback path
            # below reference it without UnboundLocalError.
            primary_error = exc
            if not self._is_smtp_deferred(primary_error):
                # Permanent error (5xx, auth, etc): no fallback
                logger.error(
                    f"Permanent error sending {message_id} to {to_address}: {primary_error}",
                    exc_info=True,
                )
                raise

            logger.warning(
                f"[FALLBACK] SMTP deferred for {message_id} ({to_address}): {primary_error}. "
                f"Looking up fallback configs for domain {domain_name}..."
            )

        # 2. Fetch the fallback configs (non-default, active) of the same domain
        try:
            from mail_api.common.email_config_cache import get_fallback_configs_for_domain
            fallback_configs = await get_fallback_configs_for_domain(domain_name)
        except Exception as e:
            logger.error(f"[FALLBACK] Error fetching the fallback configs: {e}")
            raise primary_error from e

        if not fallback_configs:
            logger.error(
                f"[FALLBACK] No fallback config found for {domain_name}. "
                f"Create an email_config with is_default=false and is_active=true."
            )
            raise primary_error

        # 3. Try each fallback config in priority order
        last_error = primary_error
        for fb_config in fallback_configs:
            fb_host = fb_config.get("smtp_host")
            fb_name = fb_config.get("name", fb_config.get("config_id"))
            logger.info(
                f"[FALLBACK] Trying config '{fb_name}' ({fb_host}) "
                f"for {message_id} -> {to_address}"
            )
            try:
                result = await self.mail_service.send_with_config(mail_data, fb_config)
                logger.info(
                    f"[FALLBACK] Email {message_id} sent through '{fb_name}' "
                    f"({fb_host}) to {to_address}"
                )
                return result
            except Exception as fb_error:
                logger.warning(
                    f"[FALLBACK] Config '{fb_name}' ({fb_host}) failed: {fb_error}"
                )
                last_error = fb_error

        # Every config failed
        logger.error(
            f"[FALLBACK] Every config failed for {message_id} -> {to_address} "
            f"(tried {len(fallback_configs)} fallback(s))"
        )
        raise last_error

    async def _log_email_event(
        self,
        domain_name: str,
        message_id: str,
        status: EmailStatus,
        metadata: dict[str, Any] | None = None,
    ):
        """Email event log (the audit is done by the SMTP milter)"""
        # The audit is done by the SMTP milter, so this only logs
        logger.info(
            f"Email {message_id} status: {status.value} for domain {domain_name}"
        )

    async def _process_scheduled_emails(self):
        """
        Moves the scheduled emails whose time has come to the main queue.

        One Redis Sorted Set (ZSET) per sending domain, where:
        - Score = scheduled timestamp
        - Value = unique key of the email
        queue_email lists the domains with scheduled sends in the `scheduled_domains` SET,
        and every domain in it is checked (DOMAIN_NAME, when set, is checked too).
        """
        from mail_api.common.config import EMAIL_QUEUE_PREFIX

        try:
            members = await redis_client.smembers(f"{EMAIL_QUEUE_PREFIX}:scheduled_domains") or set()
        except Exception as e:
            logger.error(f"[SCHEDULED_QUEUE] Error reading the scheduled domains: {e}")
            members = set()
        domains = {m.decode() if isinstance(m, bytes) else m for m in members}
        if os.getenv("DOMAIN_NAME"):
            domains.add(os.getenv("DOMAIN_NAME"))
        for domain_name in sorted(domains):
            await self._release_scheduled(domain_name)

    async def _release_scheduled(self, domain_name: str):
        """Moves the due emails of ONE domain's scheduled ZSET to the main queue."""
        from datetime import datetime

        import pytz

        from mail_api.common.config import EMAIL_QUEUE_PREFIX
        from mail_api.common.redis_client import get_prefixed_key

        try:
            scheduled_set_key = get_prefixed_key(
                f"scheduled:{domain_name}",
                EMAIL_QUEUE_PREFIX,
                domain_name=domain_name
            )

            now_ts = datetime.now(pytz.UTC).timestamp()

            # Scheduled emails whose time has come (score <= now_ts)
            # ZRANGEBYSCORE returns items with a score between -inf and now_ts
            scheduled_items = await redis_client.zrangebyscore(
                scheduled_set_key,
                min="-inf",
                max=now_ts,
                withscores=True
            )

            if not scheduled_items:
                return  # No email to process

            logger.info(
                f"[SCHEDULED_QUEUE] Found {len(scheduled_items)} scheduled email(s) "
                f"ready to send"
            )

            for unique_key, scheduled_ts in scheduled_items:
                try:
                    # Fetch the full email payload
                    email_key = get_prefixed_key(
                        unique_key,
                        EMAIL_QUEUE_PREFIX,
                        domain_name=domain_name
                    )

                    email_json = await redis_client.get(email_key)
                    if not email_json:
                        logger.warning(
                            f"[SCHEDULED_QUEUE] Payload not found for {unique_key}, "
                            f"removing it from the sorted set"
                        )
                        # Remove it from the sorted set even without a payload
                        await redis_client.zrem(scheduled_set_key, unique_key)
                        continue

                    # Parse the JSON
                    email_data = json.loads(email_json)

                    # Drop scheduled_at from the payload (already used)
                    email_data.pop("scheduled_at", None)

                    # Remove it from the sorted set. ZREM is the claim: with several queue
                    # processors, only the one that removed the member moves the email.
                    if not await redis_client.zrem(scheduled_set_key, unique_key):
                        continue

                    # Add it to the main queue (without scheduled_at, processed immediately)
                    await self._add_email_to_immediate_queue(email_data, domain_name)

                    logger.info(
                        f"[SCHEDULED_QUEUE] Email {email_data.get('message_id')} moved "
                        f"from the scheduled queue to the main queue (scheduled for {scheduled_ts})"
                    )

                except Exception as e:
                    logger.error(
                        f"[SCHEDULED_QUEUE] Error processing scheduled email {unique_key}: {e}",
                        exc_info=True
                    )
                    # Keep processing the other emails even if one fails
                    continue

        except Exception as e:
            logger.error(
                f"[SCHEDULED_QUEUE] Error checking scheduled emails: {e}",
                exc_info=True
            )

    async def _add_email_to_immediate_queue(
        self,
        email_data: dict[str, Any],
        domain_name: str
    ):
        """
        Adds the email to the immediate LIST queue (no scheduling).
        Uses RPUSH + SADD for tracking.
        """
        from mail_api.common.config import EMAIL_QUEUE_KEY, REDIS_TTL_SEC

        message_id = email_data.get("message_id", "unknown")

        # Recipient
        mail_data = email_data.get("mail_data", {})
        to_data = mail_data.get("to", {})
        recipient_address = (
            to_data.get("address", "unknown")
            if isinstance(to_data, dict)
            else "unknown"
        )

        # RPUSH to the LIST queue
        email_json = json.dumps(email_data, default=str)
        await redis_client.rpush(EMAIL_QUEUE_KEY, email_json)

        # SADD to track pending recipients
        pending_key = f"e:pending:{message_id}"
        await redis_client.sadd(pending_key, recipient_address)
        await redis_client.expire(pending_key, REDIS_TTL_SEC)


# Backward-compatible alias
QueueProcessor = EmailProcessor
