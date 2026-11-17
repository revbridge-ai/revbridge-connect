"""
Approved Senders Router

Endpoints for managing approved email sender addresses.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from mail_api.shared.tenant_scope import FORBIDDEN_DOMAIN, domain_registered_to_other_client
from mail_api.models.approved_sender_models import (
    ApprovedSenderCreate,
    ApprovedSenderFilter,
    ApprovedSenderListResponse,
    ApprovedSenderResponse,
    ApprovedSenderUpdate,
)
from mail_api.services.approved_sender_service import approved_sender_service
from mail_api.shared.domain_validation import get_user_domain_id, validate_domain_id_exists
from connect_lib.auth.hybrid import get_current_user_hybrid

logger = logging.getLogger(__name__)

# Router for the Approved Sender endpoints
approved_sender_router = APIRouter(prefix="/approved-senders", tags=["approved-senders"])


@approved_sender_router.post(
    "",
    response_model=ApprovedSenderResponse,
    status_code=201,
    summary="Create Approved Sender",
    description="""
## Create Approved Sender

Create a new approved sender address that can be used as the FROM address for emails.

### Features

- **Domain Validation**: Verifies the principal domain exists
- **Uniqueness**: Ensures each sender email is unique per domain
- **Domain Isolation**: Users can only manage their own approved senders

### Required Fields

| Field | Type | Description |
|-------|------|-------------|
| `sender_email` | string | Complete sender email (e.g., newsletter@example.com) |
| `is_active` | boolean | Whether the sender is active (default: true) |

**Note**: The `principal_domain_id` is automatically extracted from the authenticated user's JWT.

### Authentication

Requires a valid JWT token or API key in the `Authorization` header.
    """,
    responses={
        201: {
            "description": "Approved sender created successfully",
            "content": {
                "application/json": {
                    "example": {
                        "id": 1,
                        "sender_email": "newsletter@example.com",
                        "principal_domain_id": 1,
                        "is_active": True,
                        "created_at": "2025-01-21T16:30:00Z",
                        "updated_at": "2025-01-21T16:30:00Z",
                    }
                }
            },
        },
        400: {
            "description": "Invalid request data",
            "content": {
                "application/json": {
                    "example": {"detail": "An approved sender with email newsletter@example.com already exists for this domain"}
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
    tags=["approved-senders"],
    operation_id="approved_senders_create",
)
async def create_approved_sender(
    approved_sender_data: ApprovedSenderCreate,
    current_user: dict = Depends(get_current_user_hybrid),
):
    """
    Creates a new Approved Sender

    **Authentication**: requires a valid JWT token
    """
    try:
        # domain_id from the authenticated user's JWT
        domain_id = get_user_domain_id(current_user)

        if not domain_id:
            logger.error("The authenticated session has no user domain_id")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="User is not associated with a domain",
            )

        # Does the domain_id exist in the virtual_domains table?
        if not validate_domain_id_exists(domain_id):
            logger.error(f"Domain ID {domain_id} not found in virtual_domains")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Domain not found (domain_id: {domain_id})",
            )

        # RevBridge Connect: an address in a domain registered to ANOTHER client cannot be
        # approved: OpenDKIM signs by the From domain, with that client's key, so client A
        # must never be able to send as client B.
        if await domain_registered_to_other_client(approved_sender_data.sender_email, current_user):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=FORBIDDEN_DOMAIN)

        approved_sender = await approved_sender_service.create_approved_sender(
            approved_sender_data=approved_sender_data,
            domain_id=domain_id,
        )

        logger.info(f"Approved Sender created: {approved_sender.id} for domain_id {domain_id}")
        return approved_sender

    except HTTPException:
        raise
    except ValueError as e:
        logger.error(f"Validation error creating the Approved Sender: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e
    except Exception as e:
        logger.error(f"Error creating the Approved Sender: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from e


@approved_sender_router.get(
    "",
    response_model=ApprovedSenderListResponse,
    summary="List Approved Senders",
    description="""
## List Approved Senders

Retrieve all approved senders for your domain with filtering and pagination.

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `page` | integer | 1 | Page number |
| `limit` | integer | 50 | Items per page (max: 100) |
| `sender_email` | string | - | Filter by sender email (partial match) |
| `is_active` | boolean | - | Filter by active status |

### Authentication

Requires a valid JWT token or API key in the `Authorization` header.
    """,
    responses={
        200: {
            "description": "Approved senders retrieved successfully",
            "content": {
                "application/json": {
                    "example": {
                        "approved_senders": [
                            {
                                "id": 1,
                                "sender_email": "newsletter@example.com",
                                "principal_domain_id": 1,
                                "is_active": True,
                                "created_at": "2025-01-21T16:30:00Z",
                                "updated_at": "2025-01-21T16:30:00Z",
                            }
                        ],
                        "total": 10,
                        "page": 1,
                        "limit": 50,
                    }
                }
            },
        },
        401: {"description": "Invalid or expired token"},
        500: {"description": "Internal server error"},
    },
    tags=["approved-senders"],
    operation_id="approved_senders_list",
)
async def list_approved_senders(
    page: int = Query(1, ge=1, description="Page number", examples=[1]),
    limit: int = Query(50, ge=1, le=100, description="Items per page", examples=[50]),
    sender_email: str | None = Query(
        None, description="Filter by sender_email", examples=["newsletter@example.com"]
    ),
    principal_domain_id: int | None = Query(
        None, description="Filter by principal domain", examples=[1]
    ),
    is_active: bool | None = Query(
        None, description="Filter by active status", examples=[True]
    ),
    current_user: dict = Depends(get_current_user_hybrid),
):
    """
    Lists the domain's Approved Senders

    **Authentication**: requires a valid JWT token
    """
    try:
        # domain_id from the authenticated user's JWT
        domain_id = get_user_domain_id(current_user)

        if not domain_id:
            logger.error("The authenticated session has no user domain_id")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="User is not associated with a domain",
            )

        # Build the filters
        filters = ApprovedSenderFilter(
            sender_email=sender_email,
            principal_domain_id=principal_domain_id,
            is_active=is_active,
        )

        approved_senders = await approved_sender_service.list_approved_senders(
            domain_id=domain_id,
            page=page,
            limit=limit,
            filters=filters,
        )

        return approved_senders

    except Exception as e:
        logger.error(f"Error listing Approved Senders: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from e


@approved_sender_router.get(
    "/{approved_sender_id}",
    response_model=ApprovedSenderResponse,
    summary="Get Approved Sender",
    description="""
## Get Approved Sender by ID

Retrieve a specific approved sender by its ID.

### Authentication

Requires a valid JWT token or API key in the `Authorization` header.
    """,
    responses={
        200: {
            "description": "Approved sender found",
            "content": {
                "application/json": {
                    "example": {
                        "id": 1,
                        "sender_email": "newsletter@example.com",
                        "principal_domain_id": 1,
                        "is_active": True,
                        "created_at": "2025-01-21T16:30:00Z",
                        "updated_at": "2025-01-21T16:30:00Z",
                    }
                }
            },
        },
        404: {
            "description": "Approved sender not found",
            "content": {
                "application/json": {"example": {"detail": "Approved sender not found"}}
            },
        },
        401: {"description": "Invalid or expired token"},
        500: {"description": "Internal server error"},
    },
    tags=["approved-senders"],
    operation_id="approved_senders_get",
)
async def get_approved_sender(
    approved_sender_id: int = Path(..., description="Approved Sender ID", examples=[1], ge=1),
    current_user: dict = Depends(get_current_user_hybrid),
):
    """
    Fetches an Approved Sender by ID

    **Authentication**: requires a valid JWT token
    """
    try:
        # domain_id from the authenticated user's JWT
        domain_id = get_user_domain_id(current_user)

        if not domain_id:
            logger.error("The authenticated session has no user domain_id")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="User is not associated with a domain",
            )

        approved_sender = await approved_sender_service.get_approved_sender_by_id(
            approved_sender_id, domain_id
        )
        if not approved_sender:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Approved Sender not found",
            )

        return approved_sender

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching the Approved Sender: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from e


@approved_sender_router.put(
    "/{approved_sender_id}",
    response_model=ApprovedSenderResponse,
    summary="Update Approved Sender",
    description="""
## Update Approved Sender

Update an existing approved sender.

### Updatable Fields

| Field | Type | Description |
|-------|------|-------------|
| `sender_email` | string | Change sender email address |
| `is_active` | boolean | Enable/disable the approved sender |

**Note**: The `principal_domain_id` cannot be changed.

### Authentication

Requires a valid JWT token or API key in the `Authorization` header.
    """,
    responses={
        200: {
            "description": "Approved sender updated successfully",
            "content": {
                "application/json": {
                    "example": {
                        "id": 1,
                        "sender_email": "newsletter.updated@example.com",
                        "principal_domain_id": 1,
                        "is_active": True,
                        "created_at": "2025-01-21T16:30:00Z",
                        "updated_at": "2025-01-21T17:00:00Z",
                    }
                }
            },
        },
        404: {"description": "Approved sender not found"},
        400: {"description": "Invalid request data"},
        401: {"description": "Invalid or expired token"},
        500: {"description": "Internal server error"},
    },
    tags=["approved-senders"],
    operation_id="approved_senders_update",
)
async def update_approved_sender(
    approved_sender_id: int = Path(..., description="Approved Sender ID", examples=[1], ge=1),
    update_data: ApprovedSenderUpdate = ...,
    current_user: dict = Depends(get_current_user_hybrid),
):
    """
    Updates an Approved Sender

    **Authentication**: requires a valid JWT token
    """
    try:
        # domain_id from the authenticated user's JWT
        domain_id = get_user_domain_id(current_user)

        if not domain_id:
            logger.error("The authenticated session has no user domain_id")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="User is not associated with a domain",
            )

        if update_data.sender_email and await domain_registered_to_other_client(
            update_data.sender_email, current_user
        ):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=FORBIDDEN_DOMAIN)

        approved_sender = await approved_sender_service.update_approved_sender(
            approved_sender_id, domain_id, update_data
        )
        if not approved_sender:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Approved Sender not found",
            )

        logger.info(f"Approved Sender updated: {approved_sender.id}")
        return approved_sender

    except ValueError as e:
        logger.error(f"Validation error updating the Approved Sender: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating the Approved Sender: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from e


@approved_sender_router.delete(
    "/{approved_sender_id}",
    status_code=204,
    summary="Delete Approved Sender",
    description="""
## Delete Approved Sender

Permanently delete an approved sender.

### Warning

- This action is **irreversible**
- The approved sender will be removed immediately
- Emails using this sender address will no longer be authorized

### Authentication

Requires a valid JWT token or API key in the `Authorization` header.
    """,
    responses={
        204: {"description": "Approved sender deleted successfully"},
        404: {
            "description": "Approved sender not found",
            "content": {
                "application/json": {"example": {"detail": "Approved sender not found"}}
            },
        },
        401: {"description": "Invalid or expired token"},
        500: {"description": "Internal server error"},
    },
    tags=["approved-senders"],
    operation_id="approved_senders_delete",
)
async def delete_approved_sender(
    approved_sender_id: int = Path(..., description="Approved Sender ID", examples=[1], ge=1),
    current_user: dict = Depends(get_current_user_hybrid),
):
    """
    Deletes an Approved Sender

    **Authentication**: requires a valid JWT token
    """
    try:
        # domain_id from the authenticated user's JWT
        domain_id = get_user_domain_id(current_user)

        if not domain_id:
            logger.error("The authenticated session has no user domain_id")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="User is not associated with a domain",
            )

        success = await approved_sender_service.delete_approved_sender(
            approved_sender_id, domain_id
        )
        if not success:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Approved Sender not found",
            )

        logger.info(f"Approved Sender deleted: {approved_sender_id} by domain_id {domain_id}")
        return None

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting the Approved Sender: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from e
