"""Webhook dispatcher (RevBridge Connect).

Where the events come from:
  1. `kafka_outbox` (MySQL): tracking (opened, click, unsubscribed), the events updater (sent,
     delivered, bounced, deferred) and the milter write the UnifiedEngagementEvent there when
     CONNECT_EVENT_OUTBOX=true. The dispatcher reads the table directly (no Kafka).
  2. Redis `w:<domain>:...`: events already addressed to a webhook (API test) or generic ones.
  3. Redis `wq:retry` (sorted set, score = when to try): retries with a real wait.

What the receiver gets: a POST with the JSON payload, User-Agent "RevBridge-Webhook/1.0",
X-Webhook-Event, X-Webhook-Message-Id, X-Webhook-Timestamp, X-Webhook-Worker, the webhook's
extra headers and X-Webhook-Signature = "sha256=" + HMAC-SHA256(secret, body). Success =
HTTP < 400. A 5xx, a network error or a timeout is retried; a 4xx is not.

Rules:
  - A webhook only receives events of its own domain (or a subdomain); a webhook without a
    domain_name receives nothing.
  - Every request (redirects included) goes through the SSRF check.
  - Retries wait with exponential backoff in `wq:retry`, for addressed and generic events.
  - Clicks: the tracking service's "click" becomes "clicked" (what webhooks subscribe to), and
    the specific fields (clicked_url, delivery_time, bounce_code...) are filled from the event
    metadata.
  - message_headers: Subject and From are stored at send time (message_meta).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from collections import Counter
from datetime import datetime, timedelta

import httpx
import redis.asyncio as aioredis

from mail_api.common.db import exec_sql_async, fetch_all_async, fetch_one_async, get_conn
from mail_api.shared.utils.webhook_utils import generate_webhook_signature, remove_surrogate_chars

from .event_fields import EVENT_FIELD_MAP, split_event_fields
from .payloads import WebhookEventType, WebhookQueueMessage, create_event_payload, event_type_variants
from .ssrf import BlockedDestination, guard_request

logger = logging.getLogger("mail_api.webhook_dispatch")


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
POLL_INTERVAL = float(os.getenv("WEBHOOK_POLL_INTERVAL", "1.0"))
BATCH = max(1, int(os.getenv("WEBHOOK_BATCH_SIZE", "50")))
RETRY_DELAY = int(os.getenv("WEBHOOK_RETRY_DELAY", "5"))
MAX_RETRY_DELAY = int(os.getenv("WEBHOOK_MAX_RETRY_DELAY", "300"))
DEFAULT_RETRY = int(os.getenv("WEBHOOK_DEFAULT_RETRY", "3"))
DEFAULT_TIMEOUT = int(os.getenv("WEBHOOK_DEFAULT_TIMEOUT", "30"))
OUTBOX_ENABLED = _truthy(os.getenv("CONNECT_EVENT_OUTBOX", "false"))
OUTBOX_RETRY_DELAY = int(os.getenv("WEBHOOK_OUTBOX_RETRY_DELAY", "30"))
OUTBOX_MAX_RETRIES = int(os.getenv("WEBHOOK_OUTBOX_MAX_RETRIES", "5"))
OUTBOX_RETENTION_DAYS = int(os.getenv("WEBHOOK_OUTBOX_RETENTION_DAYS", "7"))
WORKER = os.getenv("WEBHOOK_WORKER_NAME", "worker-1")
RETRY_KEY = "wq:retry"
RESPONSE_MAX = 10000
BOUNCE_EVENTS = ("bounced", "hard_bounced", "soft_bounced")


# ───────────────────────── outbox (MySQL, synchronous in a thread) ─────────────────────────

def _claim_outbox(batch: int) -> list[dict]:
    """Claims up to `batch` email events (FOR UPDATE SKIP LOCKED)."""
    with get_conn() as conn:
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute(
                "SELECT id, payload, retry_count FROM kafka_outbox FORCE INDEX (idx_status_created) "
                "WHERE status = 'pending' AND service_type = 'email' "
                "AND (retry_count = 0 OR TIMESTAMPDIFF(SECOND, updated_at, NOW()) > retry_count * %s) "
                "ORDER BY created_at ASC LIMIT %s FOR UPDATE SKIP LOCKED",
                (OUTBOX_RETRY_DELAY, batch),
            )
            rows = cur.fetchall()
            if len(rows) < batch:
                # a dispatcher that died midway leaves rows in 'processing'
                cur.execute(
                    "SELECT id, payload, retry_count FROM kafka_outbox FORCE INDEX (idx_status_created) "
                    "WHERE status = 'processing' AND service_type = 'email' "
                    "AND TIMESTAMPDIFF(SECOND, updated_at, NOW()) > 120 "
                    "ORDER BY created_at ASC LIMIT %s FOR UPDATE SKIP LOCKED",
                    (batch - len(rows),),
                )
                rows += cur.fetchall()
            if rows:
                ids = [r["id"] for r in rows]
                cur.execute(
                    "UPDATE kafka_outbox SET status = 'processing', updated_at = CURRENT_TIMESTAMP "
                    f"WHERE id IN ({','.join(['%s'] * len(ids))})",
                    tuple(ids),
                )
            conn.commit()
            return rows
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()


async def _finish_outbox(row_id: int, ok: bool, error: str | None = None) -> None:
    if ok:
        await exec_sql_async(
            "UPDATE kafka_outbox SET status = 'sent', sent_at = CURRENT_TIMESTAMP, error_message = NULL "
            "WHERE id = %s",
            (row_id,),
        )
    else:
        await exec_sql_async(
            "UPDATE kafka_outbox SET status = IF(retry_count + 1 >= %s, 'failed', 'pending'), "
            "retry_count = retry_count + 1, error_message = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
            (OUTBOX_MAX_RETRIES, (error or "")[:2000], row_id),
        )


# ───────────────────────── pure rules (testable) ─────────────────────────

def in_family(domain: str | None, owner: str | None) -> bool:
    """The event's domain is the webhook's domain or one of its subdomains (the rbm<n>.<domain> of the Return-Path)."""
    d = (domain or "").strip().lower().rstrip(".")
    o = (owner or "").strip().lower().rstrip(".")
    return bool(d) and bool(o) and (d == o or d.endswith("." + o))


def unwrap_event(event_dict: dict) -> dict:
    """Extracts the fields of a generic event.

    Supports the UnifiedEngagementEvent ("event_type": "engagement") written to the outbox.
    """
    event_type = event_dict.get("event_type")
    message_id = event_dict.get("message_id")
    payload_data = event_dict.get("payload_data") or {}
    recipient = event_dict.get("rcpt") or event_dict.get("recipient_email")
    domain_name = event_dict.get("domain_name")
    service_type = event_dict.get("service_type") or "email"
    if event_type == "engagement":
        if event_dict.get("action"):
            event_type = event_dict["action"]
        channel = event_dict.get("channel") or {}
        if not message_id and channel:
            message_id = channel.get("provider_event_id")
        contact = event_dict.get("contact") or {}
        if not recipient and contact and contact.get("identifier_type") == "email":
            recipient = contact.get("identifier")
        metadata = event_dict.get("metadata") or {}
        if not domain_name:
            domain_name = metadata.get("domain_name")
        if not domain_name:
            domain_name = (event_dict.get("tenant") or {}).get("id")
        if channel.get("type"):
            service_type = channel["type"]
        if not payload_data and metadata:
            payload_data = metadata
    # RevBridge Connect: the tracking service emits "click"; email webhooks subscribe to "clicked"
    if event_type == "click" and service_type == "email":
        event_type = "clicked"
    return {
        "event_type": event_type,
        "message_id": message_id,
        "payload_data": dict(payload_data) if isinstance(payload_data, dict) else {},
        "recipient": recipient,
        "domain_name": domain_name,
        "service_type": service_type,
    }


def lift_specific_fields(event_type: str, data: dict) -> dict:
    """Fills the payload's specific fields from the event metadata.

    In the UnifiedEngagementEvent they live in metadata.provider_data, under metadata names
    (click_url, error_code, error_message), or are missing.
    """
    data = dict(data)
    pd = data.get("provider_data") if isinstance(data.get("provider_data"), dict) else {}
    for f in EVENT_FIELD_MAP.get(event_type, []):
        if data.get(f) is None and pd.get(f) is not None:
            data[f] = pd[f]
    if event_type == "clicked" and not data.get("clicked_url") and data.get("click_url"):
        data["clicked_url"] = data["click_url"]
    if event_type in BOUNCE_EVENTS:
        if data.get("bounce_code") is None and data.get("error_code") is not None:
            data["bounce_code"] = str(data["error_code"])
        if data.get("bounce_reason") is None and data.get("error_message"):
            data["bounce_reason"] = data["error_message"]
    return data


def retry_delay(attempt: int) -> int:
    """Wait before attempt+1 (exponential, capped)."""
    return min(RETRY_DELAY * (2 ** max(0, attempt - 1)), MAX_RETRY_DELAY)


def request_headers(message: WebhookQueueMessage, webhook: dict, payload_json: str) -> dict:
    """The standard headers, in a fixed order; the webhook may add its own."""
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "RevBridge-Webhook/1.0",
        "X-Webhook-Event": message.event_type.value,
        "X-Webhook-Message-Id": message.message_id,
        "X-Webhook-Timestamp": str(int(time.time())),
        "X-Webhook-Worker": WORKER,
    }
    extra = webhook.get("headers")
    if isinstance(extra, str) and extra:
        try:
            extra = json.loads(extra)
        except ValueError:
            extra = None
    if isinstance(extra, dict):
        headers.update({str(k): str(v) for k, v in extra.items()})
    if webhook.get("secret_key"):
        headers["X-Webhook-Signature"] = generate_webhook_signature(payload_json, webhook["secret_key"])
    return headers


# ───────────────────────── dispatcher ─────────────────────────

class Dispatcher:
    def __init__(self) -> None:
        self.redis = aioredis.from_url(REDIS_URL, decode_responses=True)
        self.stats: Counter = Counter()
        self.running = True
        self._last_housekeeping = 0.0
        self._last_metrics = 0.0

    # ── main loop ──
    async def run(self) -> None:
        logger.info("webhook dispatcher running: outbox=%s batch=%s worker=%s", OUTBOX_ENABLED, BATCH, WORKER)
        while self.running:
            done = 0
            try:
                if OUTBOX_ENABLED:
                    done += await self.drain_outbox()
                done += await self.drain_redis()
                done += await self.drain_retries()
                await self.housekeeping()
            except Exception:  # noqa: BLE001
                logger.exception("error in the dispatcher loop")
                await asyncio.sleep(POLL_INTERVAL * 5)
            if done == 0:
                await asyncio.sleep(POLL_INTERVAL)
        await self.redis.aclose()

    def stop(self, *_args) -> None:
        self.running = False

    # ── sources ──
    async def drain_outbox(self) -> int:
        rows = await asyncio.to_thread(_claim_outbox, BATCH)
        for row in rows:
            try:
                payload = row["payload"]
                if isinstance(payload, (bytes, bytearray)):
                    payload = payload.decode("utf-8")
                if isinstance(payload, str):
                    payload = json.loads(payload)
                await self.process_generic_event(payload)
                await _finish_outbox(row["id"], True)
                self.stats["outbox_processed"] += 1
            except Exception as e:  # noqa: BLE001
                logger.exception("outbox event %s failed", row.get("id"))
                await _finish_outbox(row["id"], False, f"{type(e).__name__}: {e}")
                self.stats["outbox_failed"] += 1
        return len(rows)

    async def drain_redis(self) -> int:
        keys: list[str] = []
        async for key in self.redis.scan_iter(match="w:*:*", count=500):
            keys.append(key)
            if len(keys) >= BATCH:
                break
        done = 0
        for key in keys:
            raw = await self.redis.getdel(key)  # atomic: one worker per key
            if not raw:
                continue
            done += 1
            try:
                msg = json.loads(raw)
            except ValueError:
                logger.warning("key %s with invalid JSON dropped", key)
                continue
            try:
                if msg.get("webhook_id"):
                    message = WebhookQueueMessage(**msg)
                    ok, retry, _skip = await self.process_single_event(message)
                    if not ok and retry:
                        await self.schedule_retry(message)
                else:
                    await self.process_generic_event(msg)
            except Exception:  # noqa: BLE001
                logger.exception("Redis event %s failed", key)
        return done

    async def drain_retries(self) -> int:
        due = await self.redis.zrangebyscore(RETRY_KEY, "-inf", time.time(), start=0, num=BATCH)
        done = 0
        for raw in due:
            if not await self.redis.zrem(RETRY_KEY, raw):
                continue  # another worker took it
            done += 1
            try:
                message = WebhookQueueMessage(**json.loads(raw))
                ok, retry, _skip = await self.process_single_event(message)
                if not ok and retry:
                    await self.schedule_retry(message)
            except Exception:  # noqa: BLE001
                logger.exception("retry failed")
        return done

    async def schedule_retry(self, message: WebhookQueueMessage) -> None:
        if message.attempt >= message.max_attempts:
            logger.warning("webhook %s ran out of %s attempts: message_id=%s event=%s", message.webhook_id,
                           message.max_attempts, message.message_id, message.event_type.value)
            self.stats["exhausted"] += 1
            return
        delay = retry_delay(message.attempt)
        nxt = message.model_copy(update={"attempt": message.attempt + 1})
        # no sort_keys: the order of the payload keys is part of the signed body
        member = json.dumps(nxt.model_dump(mode="json"), default=str)
        await self.redis.zadd(RETRY_KEY, {member: time.time() + delay})
        await self.update_event_status(message, "retrying", next_retry_at=datetime.utcnow() + timedelta(seconds=delay))
        self.stats["retries_scheduled"] += 1

    # ── events ──
    async def process_generic_event(self, event_dict: dict) -> bool:
        ev = unwrap_event(event_dict)
        if ev["service_type"] != "email":
            return True  # Connect only has email
        if ev["event_type"] == "skipped":
            return True
        try:
            wtype = WebhookEventType(ev["event_type"])
        except ValueError:
            logger.info("event type without webhooks: %s", ev["event_type"])
            return True
        domain_name, message_id = ev["domain_name"], ev["message_id"]
        if not domain_name or not message_id:
            logger.warning("event without a domain or message_id: %s", ev["event_type"])
            return False
        # synthetic id from the events updater (smtp-delivered-<queue>, smtp-bounce-…, smtp-reject-…):
        # a log line with no client message behind it (e.g. the DSN/ARF itself delivered to the
        # bounce pipe). Not a send event of the client: it does not go to webhooks.
        if str(message_id).startswith("smtp-"):
            self.stats["skipped_synthetic"] += 1
            return True
        webhooks = await self.active_webhooks(wtype, domain_name)
        if not webhooks:
            return True
        data = lift_specific_fields(wtype.value, ev["payload_data"])
        headers_cache: dict | None = None
        ok_any = False
        for wh in webhooks:
            specific, general = split_event_fields(wtype.value, dict(data), worker_name=WORKER)
            message_headers = general.pop("message_headers", None)
            if not message_headers:
                if headers_cache is None:
                    headers_cache = await self.message_headers(message_id) or {}
                message_headers = headers_cache or None
            domain_id = wh.get("domain_id")
            if not domain_id:
                from mail_api.shared.domain_validation import get_domain_id_from_name

                domain_id = await asyncio.to_thread(get_domain_id_from_name, domain_name)
            if not domain_id:
                logger.error("webhook %s has no domain_id and domain %s is unknown", wh["id"], domain_name)
                continue
            try:
                payload = create_event_payload(
                    event_type=wtype,
                    message_id=message_id,
                    recipient_email=ev["recipient"],
                    domain_name=domain_name,
                    domain_id=domain_id,
                    data=general,
                    message_headers=message_headers,
                    **specific,
                )
            except Exception:  # noqa: BLE001
                logger.exception("invalid payload: webhook=%s event=%s message_id=%s", wh["id"], wtype.value, message_id)
                continue
            payload_dict = remove_surrogate_chars(payload.model_dump())
            if ev["recipient"] and "rcpt" not in payload_dict:
                payload_dict["rcpt"] = ev["recipient"]
            # serialize (json.dumps with default=str) right here, so the body of a retry is
            # byte-for-byte identical to the first attempt's
            payload_dict = json.loads(json.dumps(payload_dict, default=str))
            message = WebhookQueueMessage(
                webhook_id=wh["id"],
                event_type=wtype,
                message_id=message_id,
                payload=payload_dict,
                max_attempts=wh.get("retry_count") or DEFAULT_RETRY,
            )
            ok, retry, _skip = await self.process_single_event(message, webhook=wh)
            if not ok and retry:
                await self.schedule_retry(message)  # generic events are retried too
            ok_any = ok_any or ok
        return ok_any

    async def process_single_event(self, message: WebhookQueueMessage, webhook: dict | None = None):
        """Sends an event to a webhook. Returns (success, retry, skip_reason)."""
        wh = webhook or await fetch_one_async("SELECT * FROM webhooks WHERE id = %s", (message.webhook_id,))
        if not wh or not wh.get("is_active"):
            self.stats["skipped_inactive"] += 1
            return True, False, "skipped_inactive"
        domain_name = message.payload.get("domain_name")
        service_type = message.payload.get("service_type", "email")
        if hasattr(service_type, "value"):
            service_type = service_type.value
        # isolation between clients: email only goes to webhooks of the same domain
        if service_type == "email" and not in_family(domain_name, wh.get("domain_name")):
            logger.error("message domain (%s) outside webhook %s (%s): dropped", domain_name, wh["id"],
                         wh.get("domain_name"))
            self.stats["skipped_tenant_mismatch"] += 1
            return True, False, "skipped_tenant_mismatch"
        recipient = (
            message.payload.get("rcpt") or message.payload.get("recipient_email")
            or message.payload.get("recipient") or message.payload.get("to") or "unknown@unknown"
        )
        existing = await fetch_one_async(
            "SELECT id, status, response_code FROM webhook_events WHERE webhook_id = %s AND message_id = %s AND event_type = %s",
            (message.webhook_id, message.message_id, message.event_type.value),
        )
        if not existing:
            import uuid

            await exec_sql_async(
                "INSERT INTO webhook_events (service_type, webhook_id, event_id, event_type, message_id, recipient_email, "
                "domain_name, domain_id, payload, status, retry_count) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', 0)",
                ("email", message.webhook_id, str(uuid.uuid4()), message.event_type.value, message.message_id, recipient,
                 domain_name, wh.get("domain_id"), json.dumps(message.payload, default=str)),
            )
        elif existing.get("status") == "failed" and existing.get("response_code") and 400 <= int(existing["response_code"]) < 500:
            self.stats["skipped_permanent_failure"] += 1
            return True, False, "skipped_permanent_failure"

        payload_json = json.dumps(message.payload, default=str)
        headers = request_headers(message, wh, payload_json)
        timeout = int(wh.get("timeout_seconds") or DEFAULT_TIMEOUT)
        started = time.time()
        code, body, resp_headers, error, retryable = 0, "", {}, None, False
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(timeout),
                limits=httpx.Limits(max_keepalive_connections=2, max_connections=5),
                follow_redirects=True,
                max_redirects=5,
                http2=False,
                event_hooks={"request": [guard_request]},
            ) as client:
                resp = await client.post(str(wh["url"]), content=payload_json, headers=headers)
                try:
                    content = await resp.aread()
                    body = content[:RESPONSE_MAX].decode("utf-8", errors="ignore")
                    resp_headers = dict(resp.headers)
                    code = resp.status_code
                finally:
                    await resp.aclose()
        except BlockedDestination as e:
            error, retryable = f"Blocked destination: {e}", False
            self.stats["blocked_ssrf"] += 1
        except httpx.TooManyRedirects as e:
            error, retryable = f"Too many redirects: {e}", False
        except (httpx.ReadError, httpx.ConnectError, httpx.TimeoutException) as e:
            error, retryable = f"{type(e).__name__}: {e}", True
        except Exception as e:  # noqa: BLE001
            error, retryable = f"Unexpected error: {e}", True
        duration_ms = int((time.time() - started) * 1000)

        if error is None and code < 400:
            await self.log_attempt(message, wh, headers, code, resp_headers, body, duration_ms, None)
            await self.update_event_status(message, "success", response_code=code, response_body=body[:1000])
            self.stats["delivered"] += 1
            logger.info("webhook delivered: webhook=%s event=%s message_id=%s attempt=%s status=%s %sms",
                        wh["id"], message.event_type.value, message.message_id, message.attempt, code, duration_ms)
            return True, False, None
        if error is None:
            error, retryable = f"HTTP {code}: {body[:500]}", code >= 500
        await self.log_attempt(message, wh, headers, code, resp_headers, body or error, duration_ms, error)
        await self.update_event_status(message, "failed", response_code=code or None, response_body=(body or "")[:1000],
                                       error_message=error[:1000])
        self.stats["failed"] += 1
        logger.warning("webhook failed: webhook=%s event=%s message_id=%s attempt=%s retry=%s error=%s",
                       wh["id"], message.event_type.value, message.message_id, message.attempt, retryable, error[:200])
        return False, retryable, None

    # ── database ──
    async def active_webhooks(self, wtype: WebhookEventType, domain_name: str) -> list[dict]:
        variants = event_type_variants(wtype)
        or_clause = " OR ".join(["JSON_CONTAINS(events, %s)"] * len(variants))
        dom = domain_name.strip().lower().rstrip(".")
        rows = await fetch_all_async(
            f"SELECT * FROM webhooks WHERE is_active = 1 AND ({or_clause}) "
            "AND (service_type = 'email' OR service_type IS NULL) "
            "AND domain_name IS NOT NULL AND domain_name <> '' "
            "AND (LOWER(domain_name) = %s OR %s LIKE CONCAT('%%.', LOWER(domain_name)))",
            tuple([json.dumps(v) for v in variants] + [dom, dom]),
        ) or []
        # strict check in Python (LIKE would treat '_' and '%' in the name as wildcards)
        return [r for r in rows if in_family(dom, r.get("domain_name"))]

    async def message_headers(self, message_id: str) -> dict | None:
        row = await fetch_one_async(
            "SELECT subject, sender_email FROM message_meta WHERE message_id = %s LIMIT 1", (message_id,)
        )
        if not row:
            return None
        out = {}
        if row.get("subject"):
            out["Subject"] = str(row["subject"])
        if row.get("sender_email"):
            out["From"] = str(row["sender_email"])
        return out or None

    async def log_attempt(self, message, wh, headers, code, resp_headers, body, duration_ms, error) -> None:
        try:
            row = await fetch_one_async(
                "SELECT id FROM webhook_events WHERE webhook_id = %s AND message_id = %s AND event_type = %s",
                (message.webhook_id, message.message_id, message.event_type.value),
            )
            if not row:
                return
            await exec_sql_async(
                "INSERT INTO webhook_logs (webhook_event_id, attempt_number, request_url, request_headers, request_payload, "
                "response_code, response_headers, response_body, duration_ms, error_message) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (row["id"], message.attempt, str(wh["url"]), json.dumps(headers, default=str),
                 json.dumps(message.payload, default=str), code or None, json.dumps(resp_headers, default=str) if resp_headers else None,
                 (body or "")[:RESPONSE_MAX], duration_ms, error),
            )
        except Exception:  # noqa: BLE001
            logger.exception("failed to record the attempt of webhook %s", message.webhook_id)

    async def update_event_status(self, message, status, response_code=None, response_body=None,
                                  error_message=None, next_retry_at=None) -> None:
        sets, params = ["status = %s", "retry_count = %s"], [status, max(0, message.attempt - 1)]
        if response_code is not None:
            sets.append("response_code = %s")
            params.append(response_code)
        if response_body is not None:
            sets.append("response_body = %s")
            params.append(response_body)
        if error_message is not None:
            sets.append("error_message = %s")
            params.append(error_message)
        if next_retry_at is not None:
            sets.append("next_retry_at = %s")
            params.append(next_retry_at)
        if status == "success":
            sets += ["sent_at = CURRENT_TIMESTAMP", "delivered_at = CURRENT_TIMESTAMP", "next_retry_at = NULL"]
        params += [message.webhook_id, message.message_id, message.event_type.value]
        try:
            await exec_sql_async(
                f"UPDATE webhook_events SET {', '.join(sets)} WHERE webhook_id = %s AND message_id = %s AND event_type = %s",
                tuple(params),
            )
        except Exception:  # noqa: BLE001
            logger.exception("failed to update the event of webhook %s", message.webhook_id)

    # ── housekeeping and metrics ──
    async def housekeeping(self) -> None:
        now = time.time()
        if now - self._last_metrics >= 60:
            self._last_metrics = now
            await self.publish_metrics()
        if now - self._last_housekeeping >= 3600:
            self._last_housekeeping = now
            try:
                await exec_sql_async(
                    "DELETE FROM kafka_outbox WHERE status = 'sent' AND created_at < NOW() - INTERVAL %s DAY LIMIT 5000",
                    (OUTBOX_RETENTION_DAYS,),
                )
            except Exception:  # noqa: BLE001
                logger.exception("outbox cleanup failed")

    async def publish_metrics(self) -> None:
        """Same format as mail_infra.metrics_sink: the API serves it on GET /metrics."""
        try:
            ts = int(time.time())
            pending = 0
            if OUTBOX_ENABLED:
                row = await fetch_one_async(
                    "SELECT COUNT(*) AS n FROM kafka_outbox WHERE status IN ('pending', 'processing') AND service_type = 'email'"
                )
                pending = int((row or {}).get("n") or 0)
            series = [
                {"metric": f"webhook.{k}_total", "points": [[ts, v]], "type": "gauge", "tags": []}
                for k, v in sorted(self.stats.items())
            ]
            series.append({"metric": "webhook.outbox_pending", "points": [[ts, pending]], "type": "gauge", "tags": []})
            series.append({"metric": "webhook.retry_queue", "points": [[ts, await self.redis.zcard(RETRY_KEY)]],
                           "type": "gauge", "tags": []})
            await self.redis.set("connect:metrics:webhooks", json.dumps({"job": "webhooks", "ts": ts, "series": series}),
                                 ex=900)
        except Exception:  # noqa: BLE001
            logger.exception("dispatcher metrics not published")


async def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    d = Dispatcher()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, d.stop)
        except NotImplementedError:
            pass
    await d.run()


if __name__ == "__main__":
    asyncio.run(main())
