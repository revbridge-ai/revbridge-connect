"""
Suppressions Router

Endpoints for managing email suppression lists.
Note: the tracking service (unsubscribes) and the events updater (bounces) also write
to the suppressions table.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from mail_api.common.db import exec_sql, fetch_all, fetch_one
from mail_api.models.tracking_models import (
    SuppressionCreate,
    SuppressionEntry,
    SuppressionListResponse,
    SuppressionResponse,
)
from mail_api.shared.domain_validation import get_user_domain_name
from connect_lib.auth.hybrid import (
    require_read_suppressions_hybrid,
    require_write_suppressions_hybrid,
)

logger = logging.getLogger(__name__)

suppressions_router = APIRouter(prefix="/suppressions", tags=["suppressions"])


@suppressions_router.get(
    "",
    response_model=SuppressionListResponse,
    summary="List Suppressions",
    description="""
## List Suppressions

Retrieve suppression list entries with advanced filtering and pagination.

### Suppression Types

| Type | Description |
|------|-------------|
| `unsubscribe` | User unsubscribed via link |
| `complaint` | Spam complaint received |
| `bounce` | Permanent bounce (hard bounce) |
| `manual` | Manually added suppression |

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `page` | integer | 1 | Page number |
| `limit` | integer | 50 | Items per page (max: 100) |
| `email` | string | - | Filter by email address |
| `suppression_type` | string | - | Filter by suppression type |
| `is_active` | boolean | - | Filter by active status |

### Response

Returns paginated list of suppressions with total count.

### Authentication

Requires a valid JWT token or API key in the `Authorization` header.
    """,
    responses={
        200: {
            "description": "Suppressions retrieved successfully",
            "content": {
                "application/json": {
                    "example": {
                        "suppressions": [
                            {
                                "id": 1,
                                "email": "user@example.com",
                                "reason": "User unsubscribed via link",
                                "created_at": "2025-01-21T16:30:00Z",
                                "updated_at": "2025-01-21T16:30:00Z",
                            }
                        ],
                        "total": 150,
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
        500: {
            "description": "Internal server error",
            "content": {
                "application/json": {"example": {"detail": "Internal server error"}}
            },
        },
    },
    tags=["suppressions"],
    operation_id="suppressions_list",
)
async def list_suppressions(
    page: int = Query(1, ge=1, description="Page number"),
    limit: int = Query(50, ge=1, le=100, description="Items per page"),
    email: str | None = Query(None, description="Filter by email"),
    suppression_type: str | None = Query(None, description="Filter by type"),
    is_active: bool | None = Query(None, description="Filter by active status"),
    current_user: dict = Depends(require_read_suppressions_hybrid),
):
    """
    Lists the suppressions of the user's domain
    """
    try:
        # domain_name from the user's JWT
        domain_name = get_user_domain_name(current_user)

        # Base query
        where_conditions = []
        params = []

        if email:
            where_conditions.append("email LIKE %s")
            params.append(f"%{email}%")

        # Always filter by the user's domain. RevBridge Connect: the domain FAMILY, since
        # bounces and unsubscribes are recorded under the VERP domain (rbm<n>.<domain>),
        # not the bare domain.
        if domain_name:
            where_conditions.append("(domain_name = %s OR domain_name LIKE %s)")
            params += [domain_name, f"%.{domain_name}"]
        else:
            where_conditions.append("1 = 0")  # no domain in the token: list nothing

        if suppression_type:
            where_conditions.append("suppression_type = %s")
            params.append(suppression_type)

        if is_active is not None:
            where_conditions.append("is_active = %s")
            params.append(is_active)

        where_clause = " AND ".join(where_conditions) if where_conditions else "1=1"

        # Count the total
        count_query = f"""
            SELECT COUNT(*) as total
            FROM suppressions
            WHERE {where_clause}
        """
        total_result = fetch_one(count_query, params)
        total = total_result["total"] if total_result else 0

        # Fetch the suppressions
        offset = (page - 1) * limit
        query = f"""
            SELECT id, email, domain_name, suppression_type, reason,
                   is_active, suppressed_at, expires_at
            FROM suppressions
            WHERE {where_clause}
            ORDER BY suppressed_at DESC
            LIMIT %s OFFSET %s
        """
        params.extend([limit, offset])

        suppressions_data = fetch_all(query, params)

        suppressions = []
        for row in suppressions_data:
            suppressions.append(
                SuppressionEntry(
                    id=row["id"],
                    email=row["email"],
                    reason=row["reason"] or "",
                    created_at=row["suppressed_at"],
                    updated_at=row["suppressed_at"],
                    suppression_type=row.get("suppression_type"),
                    domain_name=row.get("domain_name"),
                    is_active=bool(row["is_active"]) if row.get("is_active") is not None else None,
                )
            )

        # Pagination metadata
        total_pages = (total + limit - 1) // limit if total > 0 else 1

        return SuppressionListResponse(
            suppressions=suppressions,
            total=total,
            page=page,
            per_page=limit,
            total_pages=total_pages,
        )

    except Exception as e:
        logger.error(f"Error listing suppressions: {e}")
        raise HTTPException(status_code=500, detail="Internal server error") from e


@suppressions_router.post(
    "",
    response_model=SuppressionResponse,
    summary="Create Suppression",
    description="""
## Create Suppression

Add a new entry to the suppression list.

### Suppression Types

| Type | Description |
|------|-------------|
| `unsubscribe` | User unsubscribed manually |
| `complaint` | Spam complaint received |
| `bounce` | Permanent bounce (hard bounce) |
| `manual` | Manual administrative suppression |

### Features

- **Validation**: Checks if suppression already exists
- **Auto-activation**: Suppression created as active
- **Audit**: Records who created the suppression

### Required Fields

| Field | Type | Description |
|-------|------|-------------|
| `email` | string | Email address to suppress |
| `domain_name` | string | Domain name |
| `suppression_type` | string | Type of suppression |
| `reason` | string | Reason for suppression (optional) |

### Authentication

Requires a valid JWT token or API key in the `Authorization` header.
    """,
    responses={
        200: {
            "description": "Suppression created successfully",
            "content": {
                "application/json": {
                    "example": {
                        "success": True,
                        "message": "Suppression created successfully",
                        "suppression": {
                            "id": 1,
                            "email": "user@example.com",
                            "reason": "User unsubscribed via link",
                            "created_at": "2025-01-21T16:30:00Z",
                            "updated_at": "2025-01-21T16:30:00Z",
                        },
                    }
                }
            },
        },
        400: {
            "description": "Invalid request data",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Suppression already exists for this email and domain"
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
        500: {
            "description": "Internal server error",
            "content": {
                "application/json": {"example": {"detail": "Internal server error"}}
            },
        },
    },
    tags=["suppressions"],
    operation_id="suppressions_create",
)
async def create_suppression(
    suppression: SuppressionCreate,
    current_user: dict = Depends(require_write_suppressions_hybrid),
):
    """
    Creates a new suppression
    """
    try:
        # RevBridge Connect: the domain always comes from the token (the domain_name sent
        # in the body is ignored). Default type 'manual'.
        domain_name = get_user_domain_name(current_user)
        if not domain_name:
            raise HTTPException(status_code=403, detail="Token has no domain")
        suppression.domain_name = domain_name
        if not getattr(suppression, "suppression_type", None):
            suppression.suppression_type = "manual"
        # Does it already exist?
        existing = fetch_one(
            """
            SELECT id FROM suppressions
            WHERE email = %s AND domain_name = %s
        """,
            (suppression.email, suppression.domain_name),
        )

        if existing:
            raise HTTPException(
                status_code=400,
                detail="Suppression already exists for this email and domain",
            )

        # Create the suppression
        exec_sql(
            """
            INSERT INTO suppressions (email, domain_name, suppression_type, reason, is_active)
            VALUES (%s, %s, %s, %s, %s)
        """,
            (
                suppression.email,
                suppression.domain_name,
                suppression.suppression_type,
                suppression.reason,
                True,
            ),
        )

        # Fetch the created suppression. RevBridge Connect: by (email, domain), because
        # exec_sql does not return the inserted id
        created_suppression = fetch_one(
            """
            SELECT id, email, domain_name, suppression_type, reason,
                   is_active, suppressed_at, expires_at
            FROM suppressions
            WHERE email = %s AND domain_name = %s
            ORDER BY id DESC LIMIT 1
        """,
            (suppression.email, suppression.domain_name),
        )

        if not created_suppression:
            raise HTTPException(status_code=500, detail="Error creating the suppression")

        return SuppressionResponse(
            success=True,
            message="Suppression created successfully",
            suppression=SuppressionEntry(
                id=created_suppression["id"],
                email=created_suppression["email"],
                reason=created_suppression["reason"] or "",
                created_at=created_suppression["suppressed_at"],
                updated_at=created_suppression["suppressed_at"],
                suppression_type=created_suppression.get("suppression_type"),
                domain_name=created_suppression.get("domain_name"),
                is_active=bool(created_suppression.get("is_active")),
            ),
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating the suppression: {e}")
        raise HTTPException(status_code=500, detail="Internal server error") from e


@suppressions_router.delete(
    "/{suppression_id}",
    summary="Delete Suppression",
    description="""
## Delete Suppression

Remove a suppression entry from the list.

### Important

- **Irreversible**: The suppression will be permanently removed
- **Validation**: Checks if suppression exists before deletion
- **Impact**: The email address will be able to receive emails again

### Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `suppression_id` | integer | Suppression ID to delete |

### Authentication

Requires a valid JWT token or API key in the `Authorization` header.
    """,
    responses={
        200: {
            "description": "Suppression deleted successfully",
            "content": {
                "application/json": {
                    "example": {
                        "success": True,
                        "message": "Suppression deleted successfully",
                    }
                }
            },
        },
        404: {
            "description": "Suppression not found",
            "content": {
                "application/json": {"example": {"detail": "Suppression not found"}}
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
    tags=["suppressions"],
    operation_id="suppressions_delete",
)
async def delete_suppression(
    suppression_id: int = Path(..., description="Suppression ID", examples=[1], ge=1),
    current_user: dict = Depends(require_write_suppressions_hybrid),
):
    """
    Deletes a suppression
    """
    try:
        # Does it exist, and does it belong to the caller's domain? (RevBridge Connect: a
        # token can only delete suppressions of its own domain family)
        domain_name = get_user_domain_name(current_user)
        suppression = fetch_one(
            """
            SELECT id FROM suppressions WHERE id = %s AND (domain_name = %s OR domain_name LIKE %s)
        """,
            (suppression_id, domain_name or "", f"%.{domain_name}" if domain_name else ""),
        )

        if not suppression:
            raise HTTPException(status_code=404, detail="Suppression not found")

        # Delete the suppression
        exec_sql("DELETE FROM suppressions WHERE id = %s", (suppression_id,))

        return {"success": True, "message": "Suppression deleted successfully"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting the suppression: {e}")
        raise HTTPException(status_code=500, detail="Internal server error") from e
