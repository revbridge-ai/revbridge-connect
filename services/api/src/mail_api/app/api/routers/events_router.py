"""
Webhook Events Router

Endpoints for webhook event management:
- List webhook events
- View delivery attempt logs
- Recent and failed events
"""

import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from mail_api.models.webhook_models import WebhookEventResponse, WebhookEventType, WebhookLogResponse
from mail_api.services.webhook_service import webhook_service
from connect_lib.auth.hybrid import require_read_webhooks_hybrid

# Router
router = APIRouter(prefix="/webhooks", tags=["webhook-events"])
logger = logging.getLogger(__name__)


async def _owned_webhook_ids(current_user: dict) -> list[int]:
    """RevBridge Connect: ids of the caller's webhooks (recent/failed only list the events
    of these webhooks, never other clients' recipient emails and payloads)."""
    from mail_api.models.webhook_models import WebhookFilter
    from mail_api.shared.domain_validation import get_user_domain_id

    owned = await webhook_service.list_webhooks(
        WebhookFilter(), domain_id=get_user_domain_id(current_user)
    )
    return [w.id for w in owned]


@router.get(
    "/{webhook_id}/events",
    response_model=list[WebhookEventResponse],
    summary="List Webhook Events",
    description="""
## List Webhook Events

Retrieve events for a specific webhook with advanced filtering.

### Event Types

| Type | Description |
|------|-------------|
| `sent` | Email sent |
| `delivered` | Email delivered |
| `bounced` | Email bounced |
| `opened` | Email opened |
| `clicked` | Link clicked |
| `complaint` | Spam complaint |
| `unsubscribed` | Recipient unsubscribed |

### Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `event_type` | string | Filter by event type |
| `status` | string | Filter by status (success, failed, pending) |
| `message_id` | string | Filter by message ID |
| `limit` | integer | Maximum results (default: 100, max: 1000) |
| `offset` | integer | Pagination offset |

### Response

Returns array of events with metadata, timestamps, and delivery status.

### Authentication

Requires a valid JWT token or API key in the `Authorization` header.
    """,
    responses={
        200: {
            "description": "Events retrieved successfully",
            "content": {
                "application/json": {
                    "example": [
                        {
                            "id": 1,
                            "webhook_id": 123,
                            "event_type": "delivered",
                            "message_id": "msg_1234567890abcdef",
                            "status": "success",
                            "created_at": "2025-01-21T16:30:00Z",
                            "processed_at": "2025-01-21T16:30:05Z",
                            "retry_count": 0,
                            "response_code": 200,
                            "response_time_ms": 150
                        }
                    ]
                }
            }
        },
        404: {
            "description": "Webhook not found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Webhook not found"
                    }
                }
            }
        },
        401: {
            "description": "Invalid or expired token",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Invalid or expired token"
                    }
                }
            }
        },
        500: {
            "description": "Internal server error",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Internal server error"
                    }
                }
            }
        }
    },
    tags=["webhook-events"],
    operation_id="webhook_events_list"
)
async def get_webhook_events(
    webhook_id: int = Path(..., description="Webhook ID", examples=[123], ge=1),
    event_type: WebhookEventType | None = Query(None, description="Filter by event type", examples=["delivered"]),
    status: str | None = Query(None, description="Filter by status", examples=["success"]),
    message_id: str | None = Query(None, description="Filter by message ID", examples=["msg_1234567890abcdef"]),
    limit: int = Query(100, ge=1, le=1000, description="Maximum results", examples=[100]),
    offset: int = Query(0, ge=0, description="Pagination offset", examples=[0]),
    current_user: dict = Depends(require_read_webhooks_hybrid)
):
    """Lists the events of one webhook"""
    try:
        # Does the webhook exist?
        webhook = await webhook_service.get_webhook(webhook_id)
        if not webhook:
            raise HTTPException(status_code=404, detail="Webhook not found")
        # RevBridge Connect: only the owner sees the webhook's events
        from .webhook_router import _enforce_webhook_ownership

        _enforce_webhook_ownership(webhook, current_user)

        # Build the filter
        event_filter = {
            "webhook_id": webhook_id,
            "event_type": event_type,
            "status": status,
            "message_id": message_id,
            "limit": limit,
            "offset": offset
        }

        events = await webhook_service.list_webhook_events(event_filter)
        logger.info(f"Listed {len(events)} events for webhook {webhook_id}")
        return events
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing the events of webhook {webhook_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error") from e


@router.get(
    "/{webhook_id}/logs",
    response_model=list[WebhookLogResponse],
    summary="List Webhook Delivery Logs",
    description="""
## List Webhook Delivery Logs

Retrieve delivery attempt logs for a specific webhook.

### Log Types

| Status | Description |
|--------|-------------|
| `success` | Delivery successful |
| `failed` | Delivery failed |
| `retry` | Retry attempt |
| `timeout` | Delivery timeout |

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `status` | string | - | Filter by delivery status |
| `limit` | integer | 100 | Maximum results (max: 1000) |
| `offset` | integer | 0 | Pagination offset |

### Response

Returns detailed delivery attempt logs including HTTP response codes, response times, and error messages.

### Authentication

Requires a valid JWT token or API key in the `Authorization` header.
    """,
    responses={
        200: {
            "description": "Delivery logs retrieved successfully",
            "content": {
                "application/json": {
                    "example": [
                        {
                            "id": 1,
                            "webhook_id": 123,
                            "event_id": 456,
                            "attempt": 1,
                            "status": "success",
                            "response_code": 200,
                            "response_time_ms": 150,
                            "created_at": "2025-01-21T16:30:00Z",
                            "error_message": None
                        }
                    ]
                }
            }
        },
        404: {
            "description": "Webhook not found",
            "content": {
                "application/json": {
                    "example": {"detail": "Webhook not found"}
                }
            }
        },
        401: {
            "description": "Invalid or expired token",
            "content": {
                "application/json": {
                    "example": {"detail": "Invalid or expired token"}
                }
            }
        },
        500: {
            "description": "Internal server error",
            "content": {
                "application/json": {
                    "example": {"detail": "Internal server error"}
                }
            }
        }
    },
    tags=["webhook-events"],
    operation_id="webhook_logs_list"
)
async def get_webhook_logs(
    webhook_id: int = Path(..., description="Webhook ID", examples=[123], ge=1),
    status: str | None = Query(None, description="Filter by status", examples=["success"]),
    limit: int = Query(100, ge=1, le=1000, description="Maximum results", examples=[100]),
    offset: int = Query(0, ge=0, description="Pagination offset", examples=[0]),
    current_user: dict = Depends(require_read_webhooks_hybrid)
):
    """Lists the delivery attempt logs of one webhook"""
    try:
        # Does the webhook exist?
        webhook = await webhook_service.get_webhook(webhook_id)
        if not webhook:
            raise HTTPException(status_code=404, detail="Webhook not found")
        # RevBridge Connect: only the owner sees the webhook's logs
        from .webhook_router import _enforce_webhook_ownership

        _enforce_webhook_ownership(webhook, current_user)

        # Build the filter
        log_filter = {
            "webhook_id": webhook_id,
            "status": status,
            "limit": limit,
            "offset": offset
        }

        logs = await webhook_service.list_webhook_logs(log_filter)
        logger.info(f"Listed {len(logs)} logs for webhook {webhook_id}")
        return logs
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing the logs of webhook {webhook_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error") from e


@router.get(
    "/events/recent",
    response_model=list[WebhookEventResponse],
    summary="List Recent Events",
    description="""
## List Recent Events

Retrieve recent events from your webhooks.

### Time Range

| Setting | Value |
|---------|-------|
| Default | Last 24 hours |
| Maximum | 168 hours (7 days) |
| Minimum | 1 hour |

### Event Types

| Type | Description |
|------|-------------|
| `sent` | Email sent |
| `delivered` | Email delivered |
| `bounced` | Email bounced |
| `opened` | Email opened |
| `clicked` | Link clicked |
| `complaint` | Spam complaint |
| `unsubscribed` | Recipient unsubscribed |

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `hours` | integer | 24 | Time range in hours (1-168) |
| `limit` | integer | 100 | Maximum results (max: 1000) |

### Response

Returns events sorted by timestamp (newest first) with metadata and processing status.

### Authentication

Requires a valid JWT token or API key in the `Authorization` header.
    """,
    responses={
        200: {
            "description": "Recent events retrieved successfully",
            "content": {
                "application/json": {
                    "example": [
                        {
                            "id": 1,
                            "webhook_id": 123,
                            "event_type": "delivered",
                            "message_id": "msg_1234567890abcdef",
                            "status": "success",
                            "created_at": "2025-01-21T16:30:00Z",
                            "processed_at": "2025-01-21T16:30:05Z",
                            "retry_count": 0,
                            "response_code": 200,
                            "response_time_ms": 150
                        }
                    ]
                }
            }
        },
        401: {
            "description": "Invalid or expired token",
            "content": {
                "application/json": {
                    "example": {"detail": "Invalid or expired token"}
                }
            }
        },
        500: {
            "description": "Internal server error",
            "content": {
                "application/json": {
                    "example": {"detail": "Internal server error"}
                }
            }
        }
    },
    tags=["webhook-events"],
    operation_id="webhook_events_recent"
)
async def get_recent_events(
    hours: int = Query(24, ge=1, le=168, description="Time range in hours to search", examples=[24]),
    limit: int = Query(100, ge=1, le=1000, description="Maximum results", examples=[100]),
    current_user: dict = Depends(require_read_webhooks_hybrid)
):
    """Lists the recent events of the caller's webhooks"""
    try:
        # Start timestamp
        start_time = datetime.utcnow() - timedelta(hours=hours)

        # Build the filter
        event_filter = {
            "start_time": start_time,
            "limit": limit,
            "offset": 0,
            "webhook_ids": await _owned_webhook_ids(current_user),
        }

        events = await webhook_service.list_webhook_events(event_filter)
        logger.info(f"Listed {len(events)} recent events (last {hours}h)")
        return events
    except Exception as e:
        logger.error(f"Error listing recent events: {e}")
        raise HTTPException(status_code=500, detail="Internal server error") from e


@router.get(
    "/events/failed",
    response_model=list[WebhookEventResponse],
    summary="List Failed Events",
    description="""
## List Failed Events

Retrieve events that failed processing across your webhooks.

### Failure Types

| Type | Description |
|------|-------------|
| `timeout` | Webhook delivery timeout |
| `http_error` | HTTP error (4xx, 5xx) |
| `network_error` | Network connectivity error |
| `validation_error` | Payload validation error |
| `retry_exhausted` | All retry attempts exhausted |

### Time Range

| Setting | Value |
|---------|-------|
| Default | Last 24 hours |
| Maximum | 168 hours (7 days) |
| Minimum | 1 hour |

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `hours` | integer | 24 | Time range in hours (1-168) |
| `limit` | integer | 100 | Maximum results (max: 1000) |

### Response

Returns failed events with error details, retry counts, failure timestamps, and HTTP response codes.

### Use Cases

- Debug webhook issues
- Monitor delivery failures
- Analyze performance patterns
- Identify error trends

### Authentication

Requires a valid JWT token or API key in the `Authorization` header.
    """,
    responses={
        200: {
            "description": "Failed events retrieved successfully",
            "content": {
                "application/json": {
                    "example": [
                        {
                            "id": 1,
                            "webhook_id": 123,
                            "event_type": "delivered",
                            "message_id": "msg_1234567890abcdef",
                            "status": "failed",
                            "created_at": "2025-01-21T16:30:00Z",
                            "processed_at": "2025-01-21T16:30:05Z",
                            "retry_count": 3,
                            "response_code": 500,
                            "response_time_ms": 5000,
                            "error_message": "Internal Server Error"
                        }
                    ]
                }
            }
        },
        401: {
            "description": "Invalid or expired token",
            "content": {
                "application/json": {
                    "example": {"detail": "Invalid or expired token"}
                }
            }
        },
        500: {
            "description": "Internal server error",
            "content": {
                "application/json": {
                    "example": {"detail": "Internal server error"}
                }
            }
        }
    },
    tags=["webhook-events"],
    operation_id="webhook_events_failed"
)
async def get_failed_events(
    hours: int = Query(24, ge=1, le=168, description="Time range in hours to search", examples=[24]),
    limit: int = Query(100, ge=1, le=1000, description="Maximum results", examples=[100]),
    current_user: dict = Depends(require_read_webhooks_hybrid)
):
    """Lists the failed events of the caller's webhooks"""
    try:
        # Start timestamp
        start_time = datetime.utcnow() - timedelta(hours=hours)

        # Build the filter
        event_filter = {
            "start_time": start_time,
            "status": "failed",
            "limit": limit,
            "offset": 0,
            "webhook_ids": await _owned_webhook_ids(current_user),
        }

        events = await webhook_service.list_webhook_events(event_filter)
        logger.info(f"Listed {len(events)} failed events (last {hours}h)")
        return events
    except Exception as e:
        logger.error(f"Error listing failed events: {e}")
        raise HTTPException(status_code=500, detail="Internal server error") from e
