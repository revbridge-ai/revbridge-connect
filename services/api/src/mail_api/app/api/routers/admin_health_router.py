"""
Health and Info Router

Endpoints for service health checks and information.
"""

import logging
from datetime import datetime

from fastapi import APIRouter
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

# Router for the health and info endpoints
admin_health_router = APIRouter(prefix="", tags=["health"])


@admin_health_router.get(
    "/health",
    summary="Service Health Check",
    description="""
## Service Health Check

Public endpoint to verify the health status of the mail tracking service.

### Checks Performed

- Redis connectivity
- Database connectivity

### Response

Returns service status, version, and timestamp.

### Authentication

**Public Route** - No authentication required
    """,
    responses={
        200: {
            "description": "Service operating normally",
            "content": {
                "application/json": {
                    "example": {
                        "status": "healthy",
                        "version": "1.0.0",
                        "timestamp": "2025-01-21T16:30:00Z"
                    }
                }
            }
        },
        503: {
            "description": "Service has issues",
            "content": {
                "application/json": {
                    "example": {
                        "status": "unhealthy",
                        "timestamp": "2025-01-21T16:30:00Z"
                    }
                }
            }
        }
    },
    tags=["health"],
    dependencies=[],
)
async def health_check():
    """
    Health check endpoint

    **Public Route** - No authentication required
    """
    try:
        # Check the Redis connection
        from mail_api.common.redis_client import redis_client

        await redis_client.ping()

        # Check the database connection
        from mail_api.common.db import fetch_one

        fetch_one("SELECT 1")

        return {
            "status": "healthy",
            "version": "1.0.0",
            "timestamp": datetime.utcnow().isoformat(),
        }
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "timestamp": datetime.utcnow().isoformat()},
        )
