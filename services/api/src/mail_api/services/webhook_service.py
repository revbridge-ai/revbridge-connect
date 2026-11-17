"""
RevBridge Webhook service

The main webhook service:
- Webhook management in the database
- Async queueing with Redis (the dispatcher delivers the events)
- Automatic retry and failure handling (in the dispatcher)
- Logging and monitoring
"""

import json
import logging
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from mail_api.common.db import (
    exec_sql_async,
    fetch_all_async,
    fetch_one_async,
)
from mail_api.common.redis_client import get_prefixed_key
from mail_api.common.redis_client import redis_client as redis
from mail_api.models.webhook_models import (
    WebhookCreate,
    WebhookEventResponse,
    WebhookEventType,
    WebhookFilter,
    WebhookQueueMessage,
    WebhookResponse,
    WebhookStats,
    WebhookUpdate,
    create_event_payload,
)
from mail_api.shared.utils import remove_surrogate_chars


class WebhookService:
    """Main service for webhook management"""

    def __init__(self):
        self.logger = logging.getLogger(__name__)

    async def create_webhook(
        self, webhook_data: WebhookCreate, created_by: str | None = None
    ) -> WebhookResponse:
        """Creates a new webhook"""
        try:
            # Validate the URL
            if not self._validate_webhook_url(str(webhook_data.url)):
                raise ValueError("Invalid webhook URL")

            # Does the name already exist for this domain?
            domain_name = webhook_data.domain_name if hasattr(webhook_data, 'domain_name') and webhook_data.domain_name else None
            self.logger.debug(f"Checking for a duplicate webhook: name={webhook_data.name}, domain_name={domain_name}")
            existing = await self._get_webhook_by_name(webhook_data.name, domain_name)
            if existing:
                existing_domain = existing.domain_name if hasattr(existing, 'domain_name') else None
                self.logger.warning(f"Duplicate webhook found: name={webhook_data.name}, domain_name={domain_name}, existing_id={existing.id}, existing_domain={existing_domain}")
                raise ValueError("A webhook with this name already exists")

            # Insert into the database
            webhook_id = await self._insert_webhook(webhook_data, created_by)

            # Fetch the created webhook
            webhook = await self._get_webhook_by_id(webhook_id)
            if not webhook:
                raise Exception("Error creating the webhook")

            self.logger.info(f"Webhook created: {webhook.name} (ID: {webhook_id})")
            return webhook

        except Exception as e:
            self.logger.error(f"Error creating the webhook: {e}")
            raise

    async def get_webhook(self, webhook_id: int) -> WebhookResponse | None:
        """Fetches a webhook by ID"""
        return await self._get_webhook_by_id(webhook_id)

    async def list_webhooks(
        self, filters: WebhookFilter = None, domain_id: int | None = None
    ) -> list[WebhookResponse]:
        """Lists webhooks with optional filters and mandatory tenant scoping."""
        # SECURITY: refuse to list anything without the caller's domain; a query
        # without a tenant clause would return other tenants' webhooks.
        if not domain_id:
            self.logger.warning(
                "[WEBHOOK] list_webhooks called without domain_id; refusing"
            )
            return []

        try:
            # Tenant scope: every returned webhook MUST belong to the caller's domain.
            tenant_clauses = ["(w.service_type = 'email' AND w.domain_id = %s)"]
            tenant_params = [domain_id]

            query = f"""
                SELECT w.id, w.webhook_id, w.name, w.url, w.events, w.secret_key, w.is_active, w.retry_count,
                       w.timeout_seconds, w.headers, w.created_at, w.updated_at, w.created_by,
                       w.service_type, w.domain_name, w.domain_id, w.client_id
                FROM webhooks w
                WHERE ({" OR ".join(tenant_clauses)})
            """
            params = list(tenant_params)

            # Optional service_type filter applied AFTER tenant scope, never replacing it.
            if filters and filters.service_type:
                query += " AND w.service_type = %s"
                params.append(filters.service_type.value)

            if filters:
                if filters.is_active is not None:
                    query += " AND w.is_active = %s"
                    params.append(filters.is_active)

                if filters.event_type:
                    query += " AND JSON_CONTAINS(w.events, %s)"
                    params.append(json.dumps(filters.event_type.value))

                if filters.created_after:
                    query += " AND w.created_at >= %s"
                    params.append(filters.created_after)

                if filters.created_before:
                    query += " AND w.created_at <= %s"
                    params.append(filters.created_before)

                if filters.search:
                    query += " AND (w.name LIKE %s OR w.url LIKE %s)"
                    search_term = f"%{filters.search}%"
                    params.extend([search_term, search_term])

            query += " ORDER BY w.created_at DESC"

            rows = await fetch_all_async(query, params)
            webhooks = []

            for row in rows:
                # Determine the service_type
                service_type_value = row.get("service_type", "email")
                try:
                    from mail_api.models.webhook_models import ServiceType
                    service_type = ServiceType(service_type_value)
                except (ValueError, KeyError):
                    service_type = ServiceType.EMAIL

                webhook = WebhookResponse(
                    id=row["id"],
                    webhook_id=row["webhook_id"],
                    name=row["name"],
                    url=row["url"],
                    events=[WebhookEventType(e) for e in json.loads(row["events"])],
                    secret_key=row["secret_key"],
                    is_active=bool(row["is_active"]),
                    retry_count=row["retry_count"],
                    timeout_seconds=row["timeout_seconds"],
                    headers=json.loads(row["headers"]) if row["headers"] else None,
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                    created_by=row["created_by"],
                    service_type=service_type,
                    domain_name=row.get("domain_name"),
                    domain_id=row.get("domain_id"),
                    client_id=row.get("client_id"),
                )
                webhooks.append(webhook)

            return webhooks

        except Exception as e:
            self.logger.error(f"Error listing webhooks: {e}")
            raise

    async def update_webhook(
        self, webhook_id: int, webhook_data: WebhookUpdate
    ) -> WebhookResponse | None:
        """Updates a webhook"""
        try:
            # Does the webhook exist?
            existing = await self._get_webhook_by_id(webhook_id)
            if not existing:
                return None

            # Validate the URL when given
            if webhook_data.url and not self._validate_webhook_url(
                str(webhook_data.url)
            ):
                raise ValueError("Invalid webhook URL")

            # Check that the name stays unique if it changed (for the same domain)
            if webhook_data.name and webhook_data.name != existing.name:
                name_exists = await self._get_webhook_by_name(webhook_data.name, existing.domain_name)
                if name_exists and name_exists.id != webhook_id:
                    raise ValueError("A webhook with this name already exists")

            # Update the database
            await self._update_webhook_db(webhook_id, webhook_data)

            # Fetch the updated webhook
            updated_webhook = await self._get_webhook_by_id(webhook_id)
            self.logger.info(
                f"Webhook updated: {updated_webhook.name} (ID: {webhook_id})"
            )
            return updated_webhook

        except Exception as e:
            self.logger.error(f"Error updating webhook {webhook_id}: {e}")
            raise

    async def delete_webhook(self, webhook_id: int) -> bool:
        """Removes a webhook"""
        try:
            # Does the webhook exist?
            existing = await self._get_webhook_by_id(webhook_id)
            if not existing:
                return False

            # Remove it from the database (the cascade removes events and logs)
            await exec_sql_async("DELETE FROM webhooks WHERE id = %s", (webhook_id,))

            self.logger.info(f"Webhook removed: {existing.name} (ID: {webhook_id})")
            return True

        except Exception as e:
            self.logger.error(f"Error removing webhook {webhook_id}: {e}")
            raise

    async def toggle_webhook(
        self, webhook_id: int, is_active: bool
    ) -> WebhookResponse | None:
        """Enables or disables a webhook"""
        try:
            webhook = await self._get_webhook_by_id(webhook_id)
            if not webhook:
                return None

            await exec_sql_async(
                "UPDATE webhooks SET is_active = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                (is_active, webhook_id),
            )

            webhook.is_active = is_active
            webhook.updated_at = datetime.utcnow()

            action = "enabled" if is_active else "disabled"
            self.logger.info(f"Webhook {action}: {webhook.name} (ID: {webhook_id})")
            return webhook

        except Exception as e:
            self.logger.error(f"Error changing the status of webhook {webhook_id}: {e}")
            raise

    async def queue_webhook_event(
        self,
        event_type: WebhookEventType,
        message_id: str,
        payload_data: dict[str, Any],
        recipient_email: str = None,
        domain_name: str = None,
        client_id: str = None,
        service_type: str = "email",
    ) -> bool:
        """Adds a webhook event to the Redis queue"""
        self.logger.info(
            f"[WEBHOOK_QUEUE] Starting enqueue: event_type={event_type.value}, message_id={message_id}, service_type={service_type}, recipient={recipient_email}, domain_name={domain_name}, client_id={client_id}"
        )

        try:
            domain_id = None

            # Different logic per service_type
            if service_type == "email":
                # Email: domain_name/domain_id is required
                if domain_name:
                    self.logger.info(
                        f"[WEBHOOK] Looking up webhooks for event_type={event_type.value}, domain_name={domain_name}, message_id={message_id}"
                    )
                    from mail_api.shared.domain_validation import get_domain_id_from_name
                    domain_id = get_domain_id_from_name(domain_name)
                    if not domain_id:
                        self.logger.error(
                            f"[WEBHOOK_QUEUE] domain_id not found for domain_name={domain_name}"
                        )
                        return False
                else:
                    self.logger.error(
                        "[WEBHOOK_QUEUE] domain_name is required for email webhooks"
                    )
                    return False

                # Active email webhooks
                webhooks = await self._get_active_webhooks_for_event(
                    event_type, domain_name=domain_name, domain_id=domain_id, service_type="email"
                )

            else:
                self.logger.error(f"[WEBHOOK_QUEUE] Invalid service_type: {service_type}")
                return False

            if webhooks:
                identifier = domain_name if service_type == "email" else client_id
                self.logger.info(
                    f"[WEBHOOK] Found {len(webhooks)} webhook(s) for {service_type}: {identifier}, event_type={event_type.value}"
                )

            if not webhooks:
                identifier = domain_name if service_type == "email" else client_id
                self.logger.warning(
                    f"[WEBHOOK_QUEUE] No webhook found for event_type={event_type.value}, {service_type}={identifier}. Checking whether any webhooks are registered..."
                )
                # Are any webhooks registered at all?
                try:
                    from mail_api.common.db import fetch_one_async

                    total_webhooks = await fetch_one_async(
                        "SELECT COUNT(*) as total FROM webhooks WHERE service_type = %s",
                        (service_type,)
                    )
                    active_webhooks = await fetch_one_async(
                        "SELECT COUNT(*) as active FROM webhooks WHERE is_active = TRUE AND service_type = %s",
                        (service_type,)
                    )
                    self.logger.info(
                        f"[WEBHOOK_QUEUE] {service_type} webhooks in the database: total={total_webhooks.get('total', 0) if total_webhooks else 0}, active={active_webhooks.get('active', 0) if active_webhooks else 0}"
                    )
                except Exception as e:
                    self.logger.error(
                        f"[WEBHOOK_QUEUE] Error counting the webhooks: {e}"
                    )

                return True

            # Build the event payload
            from mail_api.models.webhook_models import ServiceType as ServiceTypeEnum
            service_type_enum = ServiceTypeEnum(service_type)

            payload = create_event_payload(
                event_type=event_type,
                message_id=message_id,
                service_type=service_type_enum,
                recipient_email=recipient_email,
                domain_name=domain_name,
                domain_id=domain_id,
                client_id=client_id,
                **payload_data,
            )

            # Add the event for each webhook
            webhooks_enqueued = 0
            for webhook in webhooks:
                self.logger.info(
                    f"[WEBHOOK_QUEUE] Processing webhook {webhook.id} ({webhook.name}): {webhook.url}"
                )

                # Serialize the payload with model_dump() and strip surrogate characters
                # to avoid JSON serialization errors with invalid characters
                payload_dict = payload.model_dump()
                payload_dict = remove_surrogate_chars(payload_dict)
                self.logger.info(
                    f"[WEBHOOK_QUEUE] Payload serialized: {len(str(payload_dict))} characters"
                )

                # Add it to the Redis queue
                # NOTE: the database events are created by the dispatcher, so the logic lives in one place
                queue_message = WebhookQueueMessage(
                    webhook_id=webhook.id,
                    event_type=event_type,
                    message_id=message_id,
                    payload=payload_dict,
                    max_attempts=webhook.retry_count,
                )

                # Serialize the message for Redis
                message_json = json.dumps(queue_message.dict(), default=str)
                self.logger.info(
                    f"[WEBHOOK_QUEUE] Message serialized: {len(message_json)} bytes"
                )

                # Safe write with retry
                from mail_api.common.redis_client import safe_setex

                # One unique key per webhook: {event_type}:{message_id}:{webhook_id}
                # so several webhooks for the same event do not overwrite each other
                redis_key = get_prefixed_key(
                    f"{event_type.value}:{message_id}:{webhook.id}",
                    domain_name=domain_name,
                )
                self.logger.info(
                    f"[WEBHOOK_QUEUE] Adding the event to Redis: key={redis_key}, webhook_id={webhook.id}, message_id={message_id}"
                )

                success = await safe_setex(
                    key=redis_key,
                    data=message_json,
                    redis_instance=redis,
                )

                if success:
                    webhooks_enqueued += 1
                    self.logger.info(
                        f"[WEBHOOK_QUEUE] Event enqueued for webhook {webhook.id} ({webhook.name}): message_id={message_id}, key={redis_key}"
                    )
                else:
                    self.logger.error(
                        f"[WEBHOOK_QUEUE] FAILED to enqueue the event for webhook {webhook.id} ({webhook.name}): message_id={message_id}, key={redis_key}"
                    )

            self.logger.info(
                f"[WEBHOOK_QUEUE] Enqueue complete: {webhooks_enqueued} webhook(s) enqueued out of {len(webhooks)} found"
            )
            return True

        except Exception as e:
            self.logger.error(f"Error adding the event to the queue: {e}")
            import traceback

            self.logger.error(f"Traceback: {traceback.format_exc()}")
            return False

    async def get_webhook_stats(self, webhook_id: int = None) -> list[WebhookStats]:
        """Returns webhook statistics"""
        try:
            if webhook_id:
                query = """
                    SELECT w.id, w.name, w.url, w.is_active,
                           COUNT(we.id) as total_events,
                           SUM(CASE WHEN we.status = 'success' THEN 1 ELSE 0 END) as successful_events,
                           SUM(CASE WHEN we.status = 'failed' THEN 1 ELSE 0 END) as failed_events,
                           SUM(CASE WHEN we.status = 'pending' THEN 1 ELSE 0 END) as pending_events,
                           SUM(CASE WHEN we.status = 'retrying' THEN 1 ELSE 0 END) as retrying_events,
                           ROUND((SUM(CASE WHEN we.status = 'success' THEN 1 ELSE 0 END) / COUNT(we.id)) * 100, 2) as success_rate_percent,
                           MAX(we.created_at) as last_event_at
                    FROM webhooks w
                    LEFT JOIN webhook_events we ON w.id = we.webhook_id
                    WHERE w.id = %s
                    GROUP BY w.id, w.name, w.url, w.is_active
                """
                params = (webhook_id,)
            else:
                query = """
                    SELECT w.id, w.name, w.url, w.is_active,
                           COUNT(we.id) as total_events,
                           SUM(CASE WHEN we.status = 'success' THEN 1 ELSE 0 END) as successful_events,
                           SUM(CASE WHEN we.status = 'failed' THEN 1 ELSE 0 END) as failed_events,
                           SUM(CASE WHEN we.status = 'pending' THEN 1 ELSE 0 END) as pending_events,
                           SUM(CASE WHEN we.status = 'retrying' THEN 1 ELSE 0 END) as retrying_events,
                           ROUND((SUM(CASE WHEN we.status = 'success' THEN 1 ELSE 0 END) / COUNT(we.id)) * 100, 2) as success_rate_percent,
                           MAX(we.created_at) as last_event_at
                    FROM webhooks w
                    LEFT JOIN webhook_events we ON w.id = we.webhook_id
                    GROUP BY w.id, w.name, w.url, w.is_active
                    ORDER BY w.created_at DESC
                """
                params = ()

            rows = await fetch_all_async(query, params)
            stats = []

            for row in rows:
                # Convert datetime to ISO string for proper JSON serialization
                last_event_at = row["last_event_at"]
                if last_event_at and hasattr(last_event_at, "isoformat"):
                    last_event_at = last_event_at.isoformat()
                else:
                    last_event_at = None

                stat = WebhookStats(
                    webhook_id=row["id"],
                    webhook_name=row["name"],
                    total_events=row["total_events"] or 0,
                    successful_events=row["successful_events"] or 0,
                    failed_events=row["failed_events"] or 0,
                    pending_events=row["pending_events"] or 0,
                    retrying_events=row["retrying_events"] or 0,
                    success_rate_percent=row["success_rate_percent"] or 0.0,
                    last_event_at=last_event_at,
                )
                stats.append(stat)

            return stats

        except Exception as e:
            self.logger.error(f"Error computing statistics: {e}")
            raise

    async def list_webhook_events(
        self, event_filter: dict[str, Any]
    ) -> list[WebhookEventResponse]:
        """Lists webhook events with filters"""
        try:
            query = """
                SELECT we.id, we.webhook_id, we.event_id, we.event_type, we.message_id,
                       we.recipient_email, we.domain_name, we.domain_id, we.payload, we.status, we.retry_count,
                       we.response_code, we.response_body, we.error_message, we.created_at,
                       we.delivered_at AS processed_at, we.sent_at, we.next_retry_at
                FROM webhook_events we
                WHERE 1=1
            """
            params = []

            # Apply the filters
            if event_filter.get("webhook_id"):
                query += " AND we.webhook_id = %s"
                params.append(event_filter["webhook_id"])

            # RevBridge Connect: tenant scope, only events of the caller's webhooks
            if "webhook_ids" in event_filter:
                ids = [int(i) for i in event_filter["webhook_ids"] or []]
                if not ids:
                    return []
                query += " AND we.webhook_id IN (" + ",".join(["%s"] * len(ids)) + ")"
                params.extend(ids)

            if event_filter.get("event_type"):
                query += " AND we.event_type = %s"
                params.append(
                    event_filter["event_type"].value
                    if hasattr(event_filter["event_type"], "value")
                    else event_filter["event_type"]
                )

            if event_filter.get("status"):
                query += " AND we.status = %s"
                params.append(event_filter["status"])

            if event_filter.get("message_id"):
                query += " AND we.message_id = %s"
                params.append(event_filter["message_id"])

            if event_filter.get("start_time"):
                query += " AND we.created_at >= %s"
                params.append(event_filter["start_time"])

            # Ordering
            query += " ORDER BY we.created_at DESC"

            # Pagination
            if event_filter.get("limit"):
                query += " LIMIT %s"
                params.append(event_filter["limit"])

            if event_filter.get("offset"):
                query += " OFFSET %s"
                params.append(event_filter["offset"])

            rows = await fetch_all_async(query, params)
            events = []

            for row in rows:
                # Convert datetime fields to ISO strings
                created_at = row["created_at"]
                if created_at and hasattr(created_at, "isoformat"):
                    created_at = created_at.isoformat()

                processed_at = row["processed_at"]
                if processed_at and hasattr(processed_at, "isoformat"):
                    processed_at = processed_at.isoformat()

                sent_at = row["sent_at"]
                if sent_at and hasattr(sent_at, "isoformat"):
                    sent_at = sent_at.isoformat()

                next_retry_at = row["next_retry_at"]
                if next_retry_at and hasattr(next_retry_at, "isoformat"):
                    next_retry_at = next_retry_at.isoformat()

                event = WebhookEventResponse(
                    id=row["id"],
                    webhook_id=row["webhook_id"],
                    event_id=row["event_id"],
                    event_type=WebhookEventType(row["event_type"]),
                    message_id=row["message_id"],
                    recipient_email=row["recipient_email"],
                    domain_name=row["domain_name"],
                    domain_id=row["domain_id"],
                    payload=json.loads(row["payload"]) if row["payload"] else None,
                    status=row["status"],
                    # the dispatcher stores retry_count = attempt - 1; a pending event has no attempt yet
                    attempt_count=(row["retry_count"] or 0) + (0 if row["status"] == "pending" else 1),
                    response_code=row["response_code"],
                    response_body=row["response_body"],
                    error_message=row["error_message"],
                    created_at=created_at,
                    processed_at=processed_at,
                    sent_at=sent_at,
                    next_retry_at=next_retry_at,
                )
                events.append(event)

            return events

        except Exception as e:
            self.logger.error(f"Error listing webhook events: {e}")
            raise

    async def list_webhook_logs(self, log_filter: dict[str, Any]) -> list:
        """Delivery attempts of a webhook (webhook_logs), most recent first.

        Used by GET /webhooks/{id}/logs. status: success (HTTP < 400) | failed.
        """
        from mail_api.models.webhook_models import WebhookLogResponse

        def _json(v):
            if v is None or isinstance(v, dict):
                return v
            try:
                out = json.loads(v)
                return out if isinstance(out, dict) else None
            except (TypeError, ValueError):
                return None

        query = (
            "SELECT wl.id, wl.webhook_event_id, wl.attempt_number, wl.request_url, wl.request_headers, "
            "wl.request_payload, wl.response_code, wl.response_headers, wl.response_body, wl.duration_ms, "
            "wl.error_message, wl.created_at "
            "FROM webhook_logs wl JOIN webhook_events we ON we.id = wl.webhook_event_id WHERE we.webhook_id = %s"
        )
        params: list[Any] = [log_filter["webhook_id"]]
        status = (log_filter.get("status") or "").lower()
        if status == "success":
            query += " AND wl.response_code IS NOT NULL AND wl.response_code < 400"
        elif status == "failed":
            query += " AND (wl.response_code IS NULL OR wl.response_code >= 400)"
        query += " ORDER BY wl.id DESC LIMIT %s OFFSET %s"
        params += [int(log_filter.get("limit") or 100), int(log_filter.get("offset") or 0)]
        rows = await fetch_all_async(query, tuple(params)) or []
        out = []
        for r in rows:
            headers = _json(r.get("request_headers")) or {}
            # the signing secret is never returned; the HMAC value is masked too
            headers = {k: ("***" if k.lower() == "x-webhook-signature" else str(v)) for k, v in headers.items()}
            out.append(WebhookLogResponse(
                id=r["id"], webhook_event_id=r["webhook_event_id"], attempt_number=r["attempt_number"],
                request_url=r["request_url"], request_headers=headers, request_payload=_json(r.get("request_payload")),
                response_code=r.get("response_code"),
                response_headers={k: str(v) for k, v in (_json(r.get("response_headers")) or {}).items()},
                response_body=r.get("response_body"), duration_ms=r.get("duration_ms"),
                error_message=r.get("error_message"), created_at=r["created_at"],
            ))
        return out

    # Private methods

    async def _get_webhook_by_id(self, webhook_id: int) -> WebhookResponse | None:
        """Fetches a webhook by ID"""
        from mail_api.models.webhook_models import ServiceType

        row = await fetch_one_async(
            "SELECT * FROM webhooks WHERE id = %s", (webhook_id,)
        )

        if not row:
            return None

        # Determine the service_type
        service_type_value = row.get("service_type", "email")
        try:
            service_type = ServiceType(service_type_value)
        except (ValueError, KeyError):
            service_type = ServiceType.EMAIL

        return WebhookResponse(
            id=row["id"],
            webhook_id=row["webhook_id"],
            name=row["name"],
            url=row["url"],
            events=[WebhookEventType(e) for e in json.loads(row["events"])],
            secret_key=row["secret_key"],
            is_active=bool(row["is_active"]),
            retry_count=row["retry_count"],
            timeout_seconds=row["timeout_seconds"],
            headers=json.loads(row["headers"]) if row["headers"] else None,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            created_by=row["created_by"],
            service_type=service_type,
            domain_name=row.get("domain_name"),
            domain_id=row.get("domain_id"),
            client_id=row.get("client_id"),
        )

    async def _get_webhook_by_name(self, name: str, domain_name: str | None = None) -> WebhookResponse | None:
        """Fetches a webhook by name and domain"""
        from mail_api.models.webhook_models import ServiceType

        # Normalize domain_name: treat empty strings as None
        if domain_name is not None and (not isinstance(domain_name, str) or domain_name.strip() == ""):
            domain_name = None

        if domain_name:
            self.logger.debug(f"Looking up webhook: name={name}, domain_name={domain_name}")
            row = await fetch_one_async(
                "SELECT * FROM webhooks WHERE name = %s AND domain_name = %s",
                (name, domain_name)
            )
        else:
            self.logger.debug(f"Looking up webhook: name={name}, domain_name=NULL (or empty)")
            # Webhooks without a domain_name (NULL or empty string)
            row = await fetch_one_async(
                "SELECT * FROM webhooks WHERE name = %s AND (domain_name IS NULL OR domain_name = '')",
                (name,)
            )

        if not row:
            return None

        # Determine the service_type
        service_type_value = row.get("service_type", "email")
        try:
            service_type = ServiceType(service_type_value)
        except (ValueError, KeyError):
            service_type = ServiceType.EMAIL

        return WebhookResponse(
            id=row["id"],
            webhook_id=row["webhook_id"],
            name=row["name"],
            url=row["url"],
            events=[WebhookEventType(e) for e in json.loads(row["events"])],
            secret_key=row["secret_key"],
            is_active=bool(row["is_active"]),
            retry_count=row["retry_count"],
            timeout_seconds=row["timeout_seconds"],
            headers=json.loads(row["headers"]) if row["headers"] else None,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            created_by=row["created_by"],
            service_type=service_type,
            domain_name=row.get("domain_name"),
            domain_id=row.get("domain_id"),
            client_id=row.get("client_id"),
        )

    async def _get_active_webhooks_for_event(
        self, event_type: WebhookEventType, domain_name: str | None = None, domain_id: int | None = None,
        client_id: str | None = None, service_type: str = "email"
    ) -> list[WebhookResponse]:
        """
        Fetches the active webhooks for an event type.

        Filters by the webhook's domain (domain_id, derived from domain_name if needed).
        """

        # Email: lookup by domain_id
        # Derive domain_id from domain_name if it was not given
        if not domain_id and domain_name:
            from mail_api.shared.domain_validation import get_domain_id_from_name
            domain_id = get_domain_id_from_name(domain_name)

        # domain_id is required for email
        if not domain_id:
            self.logger.error(
                f"[WEBHOOK_QUEUE] domain_id is required but was not given (domain_name={domain_name})"
            )
            return []

        # Every associated domain_id (main domain + subdomains)
        # so that events of subdomains find the main domain's webhooks
        from mail_api.shared.domain_validation import get_associated_domain_ids
        associated_domain_ids = get_associated_domain_ids(domain_id)

        self.logger.info(
            f"[WEBHOOK_QUEUE] Looking up active webhooks: event_type={event_type.value}, domain_name={domain_name}, domain_id={domain_id}, associated_ids={associated_domain_ids}"
        )
        self.logger.debug(
            f"[WEBHOOK_QUEUE] Domain IDs to search: {list(associated_domain_ids)}"
        )

        try:
            # JSON_CONTAINS takes the value directly to search JSON arrays
            event_json_value = json.dumps(event_type.value)
            self.logger.debug(
                f"[WEBHOOK_QUEUE] Looking for event: '{event_json_value}' in the JSON 'events' field"
            )

            # Webhooks whose domain_id is any of the associated domain_ids,
            # so the main domain's webhooks receive the subdomains' events
            if len(associated_domain_ids) == 1:
                # Optimization: a single domain_id uses a simple query
                rows = await fetch_all_async(
                    "SELECT * FROM webhooks WHERE is_active = TRUE AND JSON_CONTAINS(events, %s) AND domain_id = %s",
                    (event_json_value, domain_id),
                )
            else:
                # Several associated domain_ids: use an IN clause
                placeholders = ",".join(["%s"] * len(associated_domain_ids))
                query = f"SELECT * FROM webhooks WHERE is_active = TRUE AND JSON_CONTAINS(events, %s) AND domain_id IN ({placeholders})"
                params = [event_json_value] + list(associated_domain_ids)
                rows = await fetch_all_async(query, params)
            self.logger.info(
                f"[WEBHOOK_QUEUE] Query done: found {len(rows)} webhook(s)"
            )
            if rows:
                for row in rows:
                    self.logger.debug(
                        f"[WEBHOOK_QUEUE] Webhook found: id={row['id']}, name={row['name']}, domain_name={row.get('domain_name')}, domain_id={row.get('domain_id')}, events={row['events']}"
                    )
            else:
                # Extra log when no webhook is found
                self.logger.debug(
                    "[WEBHOOK_QUEUE] No webhook found. Checking the existing webhooks..."
                )
                all_webhooks = await fetch_all_async(
                    "SELECT id, name, domain_name, domain_id, is_active FROM webhooks WHERE is_active = TRUE"
                )
                if all_webhooks:
                    self.logger.debug(
                        f"[WEBHOOK_QUEUE] Existing active webhooks: {[(w['id'], w['name'], w.get('domain_name'), w.get('domain_id')) for w in all_webhooks]}"
                    )
        except (ValueError, TypeError) as e:
            self.logger.error(
                f"[WEBHOOK_QUEUE] Error processing domain_name '{domain_name}': {e}",
                exc_info=True,
            )

        webhooks = []
        for row in rows:
            webhook = WebhookResponse(
                id=row["id"],
                webhook_id=row["webhook_id"],
                name=row["name"],
                url=row["url"],
                events=[WebhookEventType(e) for e in json.loads(row["events"])],
                secret_key=row["secret_key"],
                is_active=bool(row["is_active"]),
                retry_count=row["retry_count"],
                timeout_seconds=row["timeout_seconds"],
                headers=json.loads(row["headers"]) if row["headers"] else None,
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                created_by=row["created_by"],
                domain_name=row.get("domain_name"),
                domain_id=row["domain_id"],
            )
            webhooks.append(webhook)

        return webhooks

    async def _insert_webhook(
        self, webhook_data: WebhookCreate, created_by: str | None = None
    ) -> int:
        """Inserts the webhook into the database"""
        import uuid


        # Generate a unique webhook_id
        webhook_uuid = str(uuid.uuid4())

        # service_type
        service_type = webhook_data.service_type.value if hasattr(webhook_data, 'service_type') and webhook_data.service_type else "email"

        # domain_id and client_id depending on the service_type
        domain_id = webhook_data.domain_id if hasattr(webhook_data, 'domain_id') else None
        client_id = webhook_data.client_id if hasattr(webhook_data, 'client_id') else None

        if service_type == "email":
            # Email: domain_id is required
            if not domain_id:
                # No domain_id: try to derive it from domain_name
                if hasattr(webhook_data, 'domain_name') and webhook_data.domain_name:
                    from mail_api.shared.domain_validation import get_domain_id_from_name
                    domain_id = get_domain_id_from_name(webhook_data.domain_name)

                if not domain_id:
                    raise ValueError("domain_id is required for email webhooks")

        webhook_id = await exec_sql_async(
            """
            INSERT INTO webhooks (service_type, webhook_id, name, url, events, secret_key, is_active, retry_count,
                                timeout_seconds, headers, created_by, domain_name, domain_id, client_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                service_type,
                webhook_uuid,
                webhook_data.name,
                str(webhook_data.url),
                json.dumps([e.value for e in webhook_data.events]),
                webhook_data.secret_key,
                webhook_data.is_active,
                webhook_data.retry_count,
                webhook_data.timeout_seconds,
                json.dumps(webhook_data.headers) if webhook_data.headers else None,
                created_by,
                webhook_data.domain_name if hasattr(webhook_data, 'domain_name') else None,
                domain_id,
                client_id,
            ),
        )

        if webhook_id == 0:
            self.logger.error(
                f"Error: the inserted webhook returned ID 0! UUID: {webhook_uuid}"
            )

        return webhook_id

    async def _update_webhook_db(self, webhook_id: int, webhook_data: WebhookUpdate):
        """Updates the webhook in the database"""
        updates = []
        params = []

        if webhook_data.name is not None:
            updates.append("name = %s")
            params.append(webhook_data.name)

        if webhook_data.url is not None:
            updates.append("url = %s")
            params.append(str(webhook_data.url))

        if webhook_data.events is not None:
            updates.append("events = %s")
            params.append(json.dumps([e.value for e in webhook_data.events]))

        if webhook_data.secret_key is not None:
            updates.append("secret_key = %s")
            params.append(webhook_data.secret_key)

        if webhook_data.is_active is not None:
            updates.append("is_active = %s")
            params.append(webhook_data.is_active)

        if webhook_data.retry_count is not None:
            updates.append("retry_count = %s")
            params.append(webhook_data.retry_count)

        if webhook_data.timeout_seconds is not None:
            updates.append("timeout_seconds = %s")
            params.append(webhook_data.timeout_seconds)

        if webhook_data.headers is not None:
            updates.append("headers = %s")
            params.append(json.dumps(webhook_data.headers))

        if updates:
            updates.append("updated_at = CURRENT_TIMESTAMP")
            params.append(webhook_id)

            query = f"UPDATE webhooks SET {', '.join(updates)} WHERE id = %s"
            await exec_sql_async(query, params)

    def _validate_webhook_url(self, url: str) -> bool:
        """Validates the webhook URL"""
        try:
            parsed = urlparse(url)
            return bool(
                parsed.scheme and parsed.netloc and parsed.scheme in ["http", "https"]
            )
        except Exception:
            return False


# Global service instance
webhook_service = WebhookService()
