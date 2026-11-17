"""
Webhook Router

Endpoints for managing webhook configurations:
- Create, list, retrieve, update, and delete webhooks
- Enable/disable webhooks
- Test webhook delivery
"""

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Path, Query
from mail_api.models.webhook_models import (
    ServiceType,
    WebhookCreate,
    WebhookEventType,
    WebhookFilter,
    WebhookResponse,
    WebhookTestRequest,
    WebhookUpdate,
)
from mail_api.services.webhook_service import webhook_service
from mail_api.shared.domain_validation import get_user_domain_id, get_user_domain_name
from connect_lib.auth.hybrid import (
    require_read_webhooks_hybrid,
    require_write_webhooks_hybrid,
)

# Router
router = APIRouter(prefix="/webhooks", tags=["webhooks"])
logger = logging.getLogger(__name__)


async def _assert_webhook_url_allowed(url) -> None:
    """400 if the webhook URL points to an internal address (mail_api.webhook_dispatch.ssrf)."""
    import asyncio

    from mail_api.webhook_dispatch.ssrf import BlockedDestination, check_url

    if not url:
        return
    try:
        await asyncio.to_thread(check_url, str(url))
    except BlockedDestination as e:
        raise HTTPException(status_code=400, detail=f"Webhook URL not allowed: {e}") from e


def _mask_secret(webhook):
    """Copy of the webhook with the signing secret masked (never mutate the service's object:
    it may be cached and used to sign deliveries). Only the create response shows the secret
    the caller just chose; every other response, like the list, returns a masked value."""
    secret = getattr(webhook, "secret_key", None)
    if secret:
        return webhook.model_copy(update={"secret_key": secret[:4] + "***"})
    return webhook


def _enforce_webhook_ownership(
    webhook: WebhookResponse, current_user: dict
) -> None:
    """Raise 404 if the current_user does not own this webhook.

    Returns 404 (not 403) to avoid leaking which webhook IDs are valid for
    other tenants. Tenant scope: the webhook's domain_id must be the caller's.
    """
    user_domain_id = get_user_domain_id(current_user)

    if webhook.service_type == ServiceType.EMAIL:
        if not user_domain_id or webhook.domain_id != user_domain_id:
            raise HTTPException(status_code=404, detail="Webhook not found")
    else:
        # Unknown / unexpected service_type — fail closed.
        raise HTTPException(status_code=404, detail="Webhook not found")


@router.post(
    "",
    response_model=WebhookResponse,
    status_code=201,
    summary="Create Webhook",
    description="""
## Create Webhook

Create a new webhook to receive real-time notifications for messaging events.

### Features

- **Multiple Events**: Subscribe to multiple event types per webhook
- **HMAC Signature**: Secure payload validation with secret key
- **Custom Headers**: Add custom headers to webhook requests
- **Smart Retry**: Exponential backoff retry system
- **Configurable Timeout**: Per-webhook timeout settings

### Supported Events

| Event | Description |
|-------|-------------|
| `sent` | Message sent to mail server |
| `delivered` | Message delivered to recipient |
| `bounced` | Message bounced back |
| `opened` | Email opened by recipient |
| `clicked` | Link clicked in email |
| `complaint` | Spam complaint received |
| `unsubscribed` | Recipient unsubscribed |

### Authentication

Requires a valid JWT token or API key in the `Authorization` header.
    """,
    responses={
        201: {
            "description": "Webhook created successfully",
            "content": {
                "application/json": {
                    "example": {
                        "id": 1,
                        "webhook_id": "wh_1234567890abcdef",
                        "name": "Delivery Webhook",
                        "url": "https://hooks.example.com/webhook",
                        "events": ["delivered", "bounced"],
                        "secret_key": "sk_***",
                        "is_active": True,
                        "retry_count": 3,
                        "timeout_seconds": 30,
                        "headers": {"X-Custom": "value"},
                        "created_at": "2025-01-21T16:30:00Z",
                        "updated_at": "2025-01-21T16:30:00Z",
                        "created_by": "user@example.com",
                    }
                }
            },
        },
        400: {
            "description": "Invalid request data",
            "content": {
                "application/json": {
                    "example": {"detail": "At least one event must be specified"}
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
        422: {
            "description": "Validation error",
            "content": {
                "application/json": {
                    "example": {
                        "detail": [
                            {
                                "loc": ["body", "url"],
                                "msg": "invalid or missing URL scheme",
                                "type": "value_error.url.scheme",
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
    tags=["webhooks"],
    operation_id="webhooks_create",
)
async def create_webhook(
    webhook_data: WebhookCreate,
    current_user: dict = Depends(require_write_webhooks_hybrid),
):
    """Creates a new webhook"""
    try:
        # Get user_id from current user (works for both JWT and API Key)
        user_id = current_user.get("sub") or current_user.get("user_id")

        webhook_dict = webhook_data.dict(exclude_unset=True)
        service_type = webhook_data.service_type

        # RevBridge Connect: the URL cannot point to the internal network (the dispatcher
        # runs on the same VM as the database, Redis and the internal APIs)
        await _assert_webhook_url_allowed(webhook_data.url)

        # Validate the required fields for the service_type
        if service_type == ServiceType.EMAIL:
            # Email: domain_id is required.
            # RevBridge Connect: a domain_id/domain_name in the body is only accepted if it
            # belongs to the token's client, so a webhook can never be created on another
            # client's domain to receive its events. The domain_name is always the resolved
            # domain's, never the body's.
            user_domain_id = get_user_domain_id(current_user)
            domain_id = webhook_data.domain_id or user_domain_id
            if not domain_id:
                logger.error(f"Domain_id not found for user_id={user_id}")
                raise HTTPException(status_code=400, detail="Domain not found for the user")
            if domain_id != user_domain_id:
                from mail_api.shared.domain_validation import get_authorized_domain_ids

                allowed = await get_authorized_domain_ids(current_user)
                if domain_id not in allowed:
                    raise HTTPException(status_code=403, detail="Domain not authorized for this credential")

            # Does the domain_id exist in the virtual_domains table?
            from mail_api.shared.domain_validation import validate_domain_id_exists
            if not validate_domain_id_exists(domain_id):
                logger.error(f"Domain_id {domain_id} not found in virtual_domains")
                raise HTTPException(status_code=400, detail=f"Domain not found (domain_id: {domain_id})")

            if domain_id == user_domain_id:
                domain_name = get_user_domain_name(current_user)
            else:
                from mail_api.common.db import fetch_one_async

                row = await fetch_one_async("SELECT name FROM virtual_domains WHERE id = %s", (domain_id,))
                domain_name = row["name"] if row else None
            if not domain_name:
                raise HTTPException(status_code=400, detail="Webhook domain not found")

            webhook_dict["domain_id"] = domain_id
            webhook_dict["domain_name"] = domain_name
            logger.debug(f"Email webhook prepared: domain_id={domain_id}, domain_name={domain_name}")


        webhook_data = WebhookCreate(**webhook_dict)
        webhook = await webhook_service.create_webhook(webhook_data, created_by=user_id)
        logger.info(f"Webhook created: {webhook.name} (ID: {webhook.id}, service_type: {service_type.value})")
        return webhook
    except ValueError as e:
        logger.warning(f"Validation error creating the webhook: {e}")
        raise HTTPException(status_code=400, detail=str(e)) from e
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating the webhook: {e}")
        raise HTTPException(status_code=500, detail="Internal server error") from e


@router.get(
    "",
    response_model=list[WebhookResponse],
    summary="List Webhooks",
    description="""
## List Webhooks

Retrieve all webhooks with advanced filtering and pagination.

### Available Filters

| Parameter | Type | Description |
|-----------|------|-------------|
| `is_active` | boolean | Filter by active/inactive status |
| `event_type` | string | Filter by specific event type |
| `service_type` | string | Filter by service (`email`) |
| `search` | string | Search by webhook name or URL |
| `limit` | integer | Maximum results (default: 100, max: 1000) |
| `offset` | integer | Pagination offset |

### Response

Returns an array of webhook configurations including:
- Complete webhook settings
- Activation status
- Configured events
- Retry and timeout settings
- Creation and update timestamps

### Sorting

Results are sorted by creation date in descending order (most recent first).

### Authentication

Requires a valid JWT token or API key in the `Authorization` header.
    """,
    responses={
        200: {
            "description": "Webhooks retrieved successfully",
            "content": {
                "application/json": {
                    "example": [
                        {
                            "id": 1,
                            "webhook_id": "wh_1234567890abcdef",
                            "name": "Delivery Webhook",
                            "url": "https://hooks.example.com/webhook",
                            "events": ["delivered", "bounced"],
                            "secret_key": "sk_***",
                            "is_active": True,
                            "retry_count": 3,
                            "timeout_seconds": 30,
                            "headers": {"X-Custom": "value"},
                            "created_at": "2025-01-21T16:30:00Z",
                            "updated_at": "2025-01-21T16:30:00Z",
                            "created_by": "user@example.com",
                        }
                    ]
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
    tags=["webhooks"],
    operation_id="webhooks_list",
)
async def list_webhooks(
    is_active: bool | None = Query(
        None, description="Filter by active status", examples=[True]
    ),
    event_type: WebhookEventType | None = Query(
        None, description="Filter by event type", examples=["delivered"]
    ),
    service_type: ServiceType | None = Query(
        None, description="Filter by service type (`email`)", examples=["email"]
    ),
    search: str | None = Query(
        None, description="Search by name or URL", examples=["delivery"]
    ),
    limit: int = Query(
        100, ge=1, le=1000, description="Maximum results", examples=[50]
    ),
    offset: int = Query(0, ge=0, description="Pagination offset", examples=[0]),
    current_user: dict = Depends(require_read_webhooks_hybrid),
):
    """Lists webhooks with optional filters"""
    try:
        # RevBridge Connect: the scope is always the token's domain (never another
        # client's webhooks, URL and secret)
        user_domain_id = get_user_domain_id(current_user)

        # Build the filter
        webhook_filter = WebhookFilter(
            is_active=is_active,
            event_type=event_type,
            service_type=service_type,
            search=search
        )

        # Call the service with the right filters
        webhooks = await webhook_service.list_webhooks(webhook_filter, domain_id=user_domain_id)
        logger.info(f"Listed {len(webhooks)} webhooks")
        # RevBridge Connect: the list never returns the signing secret in clear text.
        # A copy (model_copy), never the service's object: it may be cached and used to
        # sign the delivery.
        return [
            w.model_copy(update={"secret_key": w.secret_key[:4] + "***"}) if getattr(w, "secret_key", None) else w
            for w in webhooks
        ]
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing webhooks: {e}")
        raise HTTPException(status_code=500, detail="Internal server error") from e


@router.get(
    "/{webhook_id}",
    response_model=WebhookResponse,
    summary="Get Webhook",
    description="""
## Get Webhook by ID

Retrieve a specific webhook configuration by its ID.

### Response Fields

- Complete webhook configuration
- Activation status
- Configured events
- Retry and timeout settings
- Custom headers
- Creation and update timestamps

### Authentication

Requires a valid JWT token or API key in the `Authorization` header.
    """,
    responses={
        200: {
            "description": "Webhook retrieved successfully",
            "content": {
                "application/json": {
                    "example": {
                        "id": 1,
                        "webhook_id": "wh_1234567890abcdef",
                        "name": "Delivery Webhook",
                        "url": "https://hooks.example.com/webhook",
                        "events": ["delivered", "bounced"],
                        "secret_key": "sk_***",
                        "is_active": True,
                        "retry_count": 3,
                        "timeout_seconds": 30,
                        "headers": {"X-Custom": "value"},
                        "created_at": "2025-01-21T16:30:00Z",
                        "updated_at": "2025-01-21T16:30:00Z",
                        "created_by": "user@example.com",
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
            "description": "Webhook not found",
            "content": {
                "application/json": {"example": {"detail": "Webhook not found"}}
            },
        },
        500: {
            "description": "Internal server error",
            "content": {
                "application/json": {"example": {"detail": "Internal server error"}}
            },
        },
    },
    tags=["webhooks"],
    operation_id="webhooks_get",
)
async def get_webhook(
    webhook_id: int = Path(..., description="Webhook ID", examples=[1], ge=1),
    current_user: dict = Depends(require_read_webhooks_hybrid),
):
    """Fetches one webhook"""
    try:
        webhook = await webhook_service.get_webhook(webhook_id)
        if not webhook:
            raise HTTPException(status_code=404, detail="Webhook not found")
        _enforce_webhook_ownership(webhook, current_user)
        return _mask_secret(webhook)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching webhook {webhook_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error") from e


@router.put("/{webhook_id}", response_model=WebhookResponse)
async def update_webhook(
    webhook_id: int,
    webhook_data: WebhookUpdate,
    current_user: dict = Depends(require_write_webhooks_hybrid),
):
    """Updates an existing webhook"""
    try:
        existing = await webhook_service.get_webhook(webhook_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Webhook not found")
        _enforce_webhook_ownership(existing, current_user)
        if webhook_data.url is not None:
            await _assert_webhook_url_allowed(webhook_data.url)

        webhook = await webhook_service.update_webhook(webhook_id, webhook_data)
        if not webhook:
            raise HTTPException(status_code=404, detail="Webhook not found")
        logger.info(f"Webhook updated: {webhook.name} (ID: {webhook.id})")
        return _mask_secret(webhook)
    except HTTPException:
        raise
    except ValueError as e:
        logger.warning(f"Validation error updating the webhook: {e}")
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.error(f"Error updating webhook {webhook_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error") from e


@router.delete("/{webhook_id}", status_code=204)
async def delete_webhook(
    webhook_id: int, current_user: dict = Depends(require_write_webhooks_hybrid)
):
    """Removes a webhook"""
    try:
        existing = await webhook_service.get_webhook(webhook_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Webhook not found")
        _enforce_webhook_ownership(existing, current_user)

        success = await webhook_service.delete_webhook(webhook_id)
        if not success:
            raise HTTPException(status_code=404, detail="Webhook not found")
        logger.info(f"Webhook removed: ID {webhook_id}")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error removing webhook {webhook_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error") from e


@router.patch("/{webhook_id}/toggle", response_model=WebhookResponse)
async def toggle_webhook(
    webhook_id: int,
    is_active: bool,
    current_user: dict = Depends(require_write_webhooks_hybrid),
):
    """Enables or disables a webhook"""
    try:
        existing = await webhook_service.get_webhook(webhook_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Webhook not found")
        _enforce_webhook_ownership(existing, current_user)

        webhook = await webhook_service.toggle_webhook(webhook_id, is_active)
        if not webhook:
            raise HTTPException(status_code=404, detail="Webhook not found")

        status = "enabled" if is_active else "disabled"
        logger.info(f"Webhook {status}: {webhook.name} (ID: {webhook.id})")
        return _mask_secret(webhook)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error changing the status of webhook {webhook_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error") from e


@router.post(
    "/{webhook_id}/test",
    status_code=200,
    summary="Test Webhook",
    description="""
## Test Webhook

Send a test payload to verify webhook configuration and connectivity.

### Features

- **Custom Payload**: Send custom test data
- **Async Processing**: Non-blocking test execution
- **Detailed Logging**: Track test results
- **Full Validation**: Tests the complete processing chain

### How It Works

1. Validates that the webhook exists
2. Prepares test payload (default or custom)
3. Adds to processing queue
4. Returns immediate confirmation
5. Processes webhook in background

### Authentication

Requires a valid JWT token or API key in the `Authorization` header.
    """,
    responses={
        200: {
            "description": "Webhook test scheduled successfully",
            "content": {
                "application/json": {
                    "example": {
                        "message": "Webhook test scheduled successfully",
                        "webhook_id": 1,
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
            "description": "Webhook not found",
            "content": {
                "application/json": {"example": {"detail": "Webhook not found"}}
            },
        },
        500: {
            "description": "Internal server error",
            "content": {
                "application/json": {"example": {"detail": "Internal server error"}}
            },
        },
    },
    tags=["webhooks"],
    operation_id="webhooks_test",
)
async def test_webhook(
    webhook_id: int = Path(..., description="Webhook ID", examples=[1], ge=1),
    request: WebhookTestRequest = None,
    background_tasks: BackgroundTasks = None,
    current_user: dict = Depends(require_read_webhooks_hybrid),
):
    """Tests a webhook by sending a test payload"""
    try:
        # Fetch the webhook
        webhook = await webhook_service.get_webhook(webhook_id)
        if not webhook:
            raise HTTPException(status_code=404, detail="Webhook not found")
        # RevBridge Connect: only the owner can test a webhook
        _enforce_webhook_ownership(webhook, current_user)

        # Test payload for the service_type
        service_type = webhook.service_type or ServiceType.EMAIL

        test_payload = request.test_payload if request else None
        if not test_payload:
            test_payload = {
                "test": True,
                "message": "Email webhook test",
                "timestamp": "2025-01-21T16:30:00Z",
                "webhook_id": webhook_id,
                "service_type": "email",
            }

        # Add it to the email processing queue
        success = await webhook_service.queue_webhook_event(
            event_type=WebhookEventType.TEST,
            message_id="test-message",
            payload_data=test_payload,
            recipient_email="test@example.com",
            domain_name=webhook.domain_name,
        )

        if success:
            logger.info(f"Webhook test scheduled: {webhook.name} (ID: {webhook_id}, service_type: {service_type.value})")
            return {
                "message": "Webhook test scheduled successfully",
                "webhook_id": webhook_id,
                "service_type": service_type.value,
            }
        else:
            raise HTTPException(
                status_code=500, detail="Error scheduling the webhook test"
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error testing webhook {webhook_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error") from e
