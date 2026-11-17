"""
Webhook Statistics Router

Endpoints for webhook statistics and monitoring:
- General webhook statistics
- Performance metrics
- Usage reports
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from mail_api.models.webhook_models import WebhookStats
from mail_api.services.webhook_service import webhook_service
from connect_lib.auth.hybrid import require_read_webhooks_hybrid

# Router
webhook_stats_router = APIRouter(prefix="/webhooks", tags=["webhook-stats"])


async def _caller_stats(current_user: dict, webhook_id: int | None = None) -> list[WebhookStats]:
    """Statistics of the caller's client's webhooks only.

    RevBridge Connect: /webhooks/stats* never aggregates other clients' webhooks
    (their names, URLs and event volumes).
    """
    from mail_api.models.webhook_models import WebhookFilter
    from mail_api.shared.domain_validation import get_user_domain_id

    own = await webhook_service.list_webhooks(
        WebhookFilter(),
        domain_id=get_user_domain_id(current_user),
    )
    own_ids = {w.id for w in own or []}
    if webhook_id is not None:
        if webhook_id not in own_ids:
            return []
        return await webhook_service.get_webhook_stats(webhook_id) or []
    return [st for st in (await webhook_service.get_webhook_stats() or []) if st.webhook_id in own_ids]
logger = logging.getLogger(__name__)


@webhook_stats_router.get(
    "/stats/overview",
    response_model=list[WebhookStats],
    summary="Get Webhook Statistics Overview",
    description="""
## Get Webhook Statistics Overview

Retrieve detailed performance statistics for your webhooks.

### Metrics Included

| Metric | Description |
|--------|-------------|
| `total_events` | Total number of events processed |
| `successful_events` | Events delivered successfully |
| `failed_events` | Events that failed delivery |
| `success_rate` | Percentage of successful deliveries |
| `avg_response_time` | Average response time in milliseconds |

### Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `webhook_id` | integer | Filter by specific webhook ID (optional) |

### Response

Returns an array of webhook statistics. If `webhook_id` is provided, returns statistics only for that webhook.

### Authentication

Requires a valid JWT token or API key in the `Authorization` header.
    """,
    responses={
        200: {
            "description": "Webhook statistics retrieved successfully",
            "content": {
                "application/json": {
                    "example": [
                        {
                            "webhook_id": 123,
                            "webhook_name": "Delivery Webhook",
                            "total_events": 1500,
                            "successful_events": 1425,
                            "failed_events": 75,
                            "success_rate": 95.0,
                            "avg_response_time": 150,
                            "last_event": "2025-01-21T16:30:00Z"
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
    tags=["webhook-stats"],
    operation_id="webhook_stats_overview",
)
async def get_webhook_stats(
    webhook_id: int | None = Query(
        None, description="Filter by specific webhook ID", examples=[123]
    ),
    current_user: dict = Depends(require_read_webhooks_hybrid),
):
    """Returns webhook statistics"""
    try:
        if webhook_id:
            # Statistics of one webhook
            stats = await _caller_stats(current_user, webhook_id)
            if not stats:
                raise HTTPException(status_code=404, detail="Webhook not found")
            return stats
        else:
            # Statistics of every webhook of the client
            stats = await _caller_stats(current_user)
            return stats
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error computing webhook statistics: {e}")
        raise HTTPException(status_code=500, detail="Internal server error") from e


@webhook_stats_router.get(
    "/stats/summary",
    summary="Get Webhook Statistics Summary",
    description="""
## Get Webhook Statistics Summary

Retrieve a consolidated statistical summary of your webhook system.

### Consolidated Metrics

| Metric | Description |
|--------|-------------|
| `total_webhooks` | Total number of active webhooks |
| `total_events` | Total events processed across all webhooks |
| `successful_events` | Total successfully delivered events |
| `failed_events` | Total failed events |
| `success_rate` | Overall success percentage |
| `failure_rate` | Overall failure percentage |

### Response

Returns aggregated statistics across all webhooks, plus individual webhook breakdowns.

### Use Cases

- Dashboard overview
- Executive reporting
- Quality monitoring
- Performance analysis

### Authentication

Requires a valid JWT token or API key in the `Authorization` header.
    """,
    responses={
        200: {
            "description": "Webhook summary retrieved successfully",
            "content": {
                "application/json": {
                    "example": {
                        "total_webhooks": 5,
                        "total_events": 15000,
                        "successful_events": 14250,
                        "failed_events": 750,
                        "success_rate": 95.0,
                        "failure_rate": 5.0,
                        "webhooks": [
                            {
                                "webhook_id": 123,
                                "webhook_name": "Delivery Webhook",
                                "total_events": 3000,
                                "successful_events": 2850,
                                "failed_events": 150,
                                "success_rate": 95.0
                            }
                        ]
                    }
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
    tags=["webhook-stats"],
    operation_id="webhook_stats_summary",
)
async def get_webhook_summary(
    current_user: dict = Depends(require_read_webhooks_hybrid),
):
    """Returns the overall statistical summary of the webhook system"""
    try:
        # General statistics
        all_stats = await _caller_stats(current_user)

        # Totals
        total_webhooks = len(all_stats)
        total_events = sum(stat.total_events for stat in all_stats)
        successful_events = sum(stat.successful_events for stat in all_stats)
        failed_events = sum(stat.failed_events for stat in all_stats)

        # Rates
        success_rate = (
            (successful_events / total_events * 100) if total_events > 0 else 0
        )
        failure_rate = (failed_events / total_events * 100) if total_events > 0 else 0

        summary = {
            "total_webhooks": total_webhooks,
            "total_events": total_events,
            "successful_events": successful_events,
            "failed_events": failed_events,
            "success_rate": round(success_rate, 2),
            "failure_rate": round(failure_rate, 2),
            "webhooks": all_stats,
        }

        return summary
    except Exception as e:
        logger.error(f"Error computing the statistical summary: {e}")
        raise HTTPException(status_code=500, detail="Internal server error") from e


@webhook_stats_router.get(
    "/stats/performance",
    summary="Get Webhook Performance Metrics",
    description="""
## Get Webhook Performance Metrics

Retrieve detailed performance metrics for your webhook system.

### Performance Metrics

| Metric | Description |
|--------|-------------|
| `period_hours` | Analysis period in hours |
| `total_webhooks` | Number of active webhooks |
| `average_response_time` | Average response time in milliseconds |
| `total_requests` | Total requests in the period |
| `successful_requests` | Successfully delivered requests |
| `failed_requests` | Failed requests |
| `requests_per_hour` | Average request volume per hour |
| `top_webhooks` | Top 5 webhooks by volume |

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `hours` | integer | 24 | Analysis period (1-168 hours) |

### Use Cases

- System capacity analysis
- Bottleneck identification
- Resource planning
- SLA monitoring

### Authentication

Requires a valid JWT token or API key in the `Authorization` header.
    """,
    responses={
        200: {
            "description": "Performance metrics retrieved successfully",
            "content": {
                "application/json": {
                    "example": {
                        "period_hours": 24,
                        "total_webhooks": 5,
                        "average_response_time": 150,
                        "total_requests": 5000,
                        "successful_requests": 4750,
                        "failed_requests": 250,
                        "requests_per_hour": 208.33,
                        "top_webhooks": [
                            {
                                "webhook_id": 123,
                                "webhook_name": "Delivery Webhook",
                                "total_events": 2000,
                                "success_rate": 96.0
                            }
                        ]
                    }
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
    tags=["webhook-stats"],
    operation_id="webhook_stats_performance",
)
async def get_webhook_performance(
    hours: int = Query(
        24, ge=1, le=168, description="Analysis period in hours", examples=[24]
    ),
    current_user: dict = Depends(require_read_webhooks_hybrid),
):
    """Returns webhook performance metrics"""
    try:
        # Statistics of the period
        stats = await _caller_stats(current_user)

        # Performance metrics
        performance_metrics = {
            "period_hours": hours,
            "total_webhooks": len(stats),
            "average_response_time": 0,  # TODO: compute the average response time
            "total_requests": sum(stat.total_events for stat in stats),
            "successful_requests": sum(stat.successful_events for stat in stats),
            "failed_requests": sum(stat.failed_events for stat in stats),
            "requests_per_hour": 0,  # TODO: compute requests per hour
            "top_webhooks": sorted(stats, key=lambda x: x.total_events, reverse=True)[
                :5
            ],
        }

        # Requests per hour
        if hours > 0:
            performance_metrics["requests_per_hour"] = round(
                performance_metrics["total_requests"] / hours, 2
            )

        return performance_metrics
    except Exception as e:
        logger.error(f"Error computing performance metrics: {e}")
        raise HTTPException(status_code=500, detail="Internal server error") from e
