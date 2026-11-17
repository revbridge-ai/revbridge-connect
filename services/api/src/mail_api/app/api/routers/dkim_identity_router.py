"""
DKIM Identities Router

Endpoints for managing DKIM signing identities.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from mail_api.models.dkim_models import (
    DkimIdentityCreate,
    DkimIdentityFilter,
    DkimIdentityResponse,
    DkimIdentityUpdate,
)
from mail_api.services.dkim_identity_service import dkim_identity_service
from mail_api.shared.tenant_scope import assert_names_in_family, caller_domain_names, name_in_family
from connect_lib.auth.hybrid import get_current_user_hybrid

logger = logging.getLogger(__name__)

# Router for the DKIM Identity endpoints
router = APIRouter(prefix="/dkim/identities", tags=["dkim-identities"])


def _identity_names(identity) -> list[str]:
    return [n for n in (identity.from_email, identity.from_domain, identity.dkim_domain) if n]


async def _owned_identity_or_404(identity_id: int, current_user: dict):
    """OpenDKIM uses this table to choose which key signs each sender: only the owner of
    the identity's domains (sender AND DKIM domain) can see or change it."""
    identity = await dkim_identity_service.get_identity(identity_id)
    if not identity:
        raise HTTPException(status_code=404, detail="DKIM identity not found")
    roots = await caller_domain_names(current_user)
    if not all(name_in_family(n, roots) for n in _identity_names(identity)):
        raise HTTPException(status_code=404, detail="DKIM identity not found")
    return identity


@router.post(
    "",
    response_model=DkimIdentityResponse,
    status_code=201,
    summary="Create DKIM Identity",
    description="""
## Create DKIM Identity

Create a new DKIM signing identity for email authentication.

### Features

- Configure DKIM identities per cluster
- Support for email or domain-based matching
- Wildcard subdomain support
- Custom selector and DKIM domain configuration

### Required Fields

| Field | Type | Description |
|-------|------|-------------|
| `cluster_id` | string | Cluster identifier |
| `from_email` | string | Source email address (optional) |
| `from_domain` | string | Source domain (optional) |
| `selector` | string | DKIM selector |
| `dkim_domain` | string | DKIM signing domain |

### Authentication

Requires a valid JWT token or API key in the `Authorization` header.
    """,
    responses={
        201: {"description": "DKIM identity created successfully"},
        400: {"description": "Invalid request data"},
        401: {"description": "Invalid or expired token"},
        500: {"description": "Internal server error"},
    },
)
async def create_identity(
    identity_data: DkimIdentityCreate,
    current_user: dict = Depends(get_current_user_hybrid),
):
    """Create a new DKIM identity"""
    try:
        await assert_names_in_family(
            current_user, identity_data.from_email, identity_data.from_domain, identity_data.dkim_domain
        )
        identity = await dkim_identity_service.create_identity(identity_data)
        logger.info(f"DKIM identity created: ID {identity.id}")
        return identity
    except ValueError as e:
        logger.warning(f"Validation error creating DKIM identity: {e}")
        raise HTTPException(status_code=400, detail=str(e)) from e
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating DKIM identity: {e}")
        raise HTTPException(status_code=500, detail="Internal server error") from e


@router.get(
    "",
    response_model=list[DkimIdentityResponse],
    summary="List DKIM Identities",
    description="""
## List DKIM Identities

Retrieve DKIM identities with optional filters.

### Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `cluster_id` | string | Filter by cluster ID |
| `from_email` | string | Filter by source email |
| `from_domain` | string | Filter by source domain |
| `selector` | string | Filter by DKIM selector |
| `dkim_domain` | string | Filter by DKIM domain |

### Authentication

Requires a valid JWT token or API key in the `Authorization` header.
    """,
    responses={
        200: {"description": "DKIM identities retrieved successfully"},
        401: {"description": "Invalid or expired token"},
        500: {"description": "Internal server error"},
    },
)
async def list_identities(
    cluster_id: str | None = Query(None, description="Filter by cluster_id"),
    from_email: str | None = Query(None, description="Filter by source email"),
    from_domain: str | None = Query(None, description="Filter by source domain"),
    selector: str | None = Query(None, description="Filter by DKIM selector"),
    dkim_domain: str | None = Query(None, description="Filter by DKIM domain"),
    current_user: dict = Depends(get_current_user_hybrid),
):
    """List DKIM identities with optional filters"""
    try:
        filters = DkimIdentityFilter(
            cluster_id=cluster_id,
            from_email=from_email,
            from_domain=from_domain,
            selector=selector,
            dkim_domain=dkim_domain,
        )
        identities = await dkim_identity_service.list_identities(filters)
        roots = await caller_domain_names(current_user)
        identities = [i for i in identities if all(name_in_family(n, roots) for n in _identity_names(i))]
        logger.info(f"Listed {len(identities)} DKIM identities")
        return identities
    except Exception as e:
        logger.error(f"Error listing DKIM identities: {e}")
        raise HTTPException(status_code=500, detail="Internal server error") from e


@router.get(
    "/{identity_id}",
    response_model=DkimIdentityResponse,
    summary="Get DKIM Identity",
    description="""
## Get DKIM Identity

Retrieve a specific DKIM identity by ID.

### Authentication

Requires a valid JWT token or API key in the `Authorization` header.
    """,
    responses={
        200: {"description": "DKIM identity found"},
        401: {"description": "Invalid or expired token"},
        404: {"description": "DKIM identity not found"},
        500: {"description": "Internal server error"},
    },
)
async def get_identity(
    identity_id: int = Path(..., description="DKIM identity ID", examples=[1], ge=1),
    current_user: dict = Depends(get_current_user_hybrid),
):
    """Get a specific DKIM identity"""
    try:
        return await _owned_identity_or_404(identity_id, current_user)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting DKIM identity {identity_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error") from e


@router.put(
    "/{identity_id}",
    response_model=DkimIdentityResponse,
    summary="Update DKIM Identity",
    description="""
## Update DKIM Identity

Update an existing DKIM identity.

### Updatable Fields

| Field | Type | Description |
|-------|------|-------------|
| `from_email` | string | Source email address |
| `from_domain` | string | Source domain |
| `selector` | string | DKIM selector |
| `dkim_domain` | string | DKIM signing domain |

### Authentication

Requires a valid JWT token or API key in the `Authorization` header.
    """,
    responses={
        200: {"description": "DKIM identity updated successfully"},
        400: {"description": "Invalid request data"},
        401: {"description": "Invalid or expired token"},
        404: {"description": "DKIM identity not found"},
        500: {"description": "Internal server error"},
    },
)
async def update_identity(
    identity_id: int = Path(..., description="DKIM identity ID", examples=[1], ge=1),
    identity_data: DkimIdentityUpdate = ...,
    current_user: dict = Depends(get_current_user_hybrid),
):
    """Update an existing DKIM identity"""
    try:
        await _owned_identity_or_404(identity_id, current_user)
        await assert_names_in_family(
            current_user, identity_data.from_email, identity_data.from_domain, identity_data.dkim_domain
        )
        identity = await dkim_identity_service.update_identity(identity_id, identity_data)
        if not identity:
            raise HTTPException(status_code=404, detail="DKIM identity not found")
        logger.info(f"DKIM identity updated: ID {identity_id}")
        return identity
    except HTTPException:
        raise
    except ValueError as e:
        logger.warning(f"Validation error updating DKIM identity: {e}")
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.error(f"Error updating DKIM identity {identity_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error") from e


@router.delete(
    "/{identity_id}",
    status_code=204,
    summary="Delete DKIM Identity",
    description="""
## Delete DKIM Identity

Permanently delete a DKIM identity.

### Warning

- This action is **irreversible**
- Emails matching this identity will no longer be DKIM signed

### Authentication

Requires a valid JWT token or API key in the `Authorization` header.
    """,
    responses={
        204: {"description": "DKIM identity deleted successfully"},
        401: {"description": "Invalid or expired token"},
        404: {"description": "DKIM identity not found"},
        500: {"description": "Internal server error"},
    },
)
async def delete_identity(
    identity_id: int = Path(..., description="DKIM identity ID", examples=[1], ge=1),
    current_user: dict = Depends(get_current_user_hybrid),
):
    """Delete a DKIM identity"""
    try:
        await _owned_identity_or_404(identity_id, current_user)
        success = await dkim_identity_service.delete_identity(identity_id)
        if not success:
            raise HTTPException(status_code=404, detail="DKIM identity not found")
        logger.info(f"DKIM identity deleted: ID {identity_id}")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting DKIM identity {identity_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error") from e
