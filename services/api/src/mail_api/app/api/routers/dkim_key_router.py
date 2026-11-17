"""
DKIM Keys Router

Endpoints for managing DKIM signing keys.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from mail_api.models.dkim_models import (
    DkimKeyCreate,
    DkimKeyFilter,
    DkimKeyResponse,
    DkimKeyUpdate,
)
from mail_api.services.dkim_key_service import dkim_key_service
from connect_lib.auth.hybrid import get_current_user_hybrid

logger = logging.getLogger(__name__)

# Router for the DKIM Key endpoints
router = APIRouter(prefix="/dkim/keys", tags=["dkim-keys"])


def _in_family(dkim_domain: str | None, current_user: dict) -> bool:
    """RevBridge Connect: a DKIM key is only visible/changeable by the domain owner.

    A client must never be able to read, change or delete another domain's key (with it,
    they could sign as another client). Family = the token's domain and its subdomains
    (including the rbm<n>.<domain> of the Return-Path).
    """
    from mail_api.shared.domain_validation import get_user_domain_name

    dom = (get_user_domain_name(current_user) or "").strip().lower()
    d = (dkim_domain or "").strip().lower()
    return bool(dom) and (d == dom or d.endswith("." + dom))


async def _owned_key_or_404(key_id: int, current_user: dict):
    key = await dkim_key_service.get_key(key_id)
    if not key or not _in_family(key.dkim_domain, current_user):
        raise HTTPException(status_code=404, detail="DKIM key not found")
    return key


@router.post(
    "",
    response_model=DkimKeyResponse,
    status_code=201,
    summary="Create DKIM Key",
    description="""
## Create DKIM Key

Create a new DKIM signing key.

### Features

- Configure DKIM keys per cluster
- Custom selector and DKIM domain configuration
- Private key path storage
- Uniqueness validation (cluster_id, selector, dkim_domain)

### Required Fields

| Field | Type | Description |
|-------|------|-------------|
| `cluster_id` | string | Cluster identifier |
| `selector` | string | DKIM selector |
| `dkim_domain` | string | DKIM signing domain |
| `private_key_path` | string | Path to the private key file |

### Authentication

Requires a valid JWT token or API key in the `Authorization` header.
    """,
    responses={
        201: {"description": "DKIM key created successfully"},
        400: {"description": "Invalid request data"},
        401: {"description": "Invalid or expired token"},
        500: {"description": "Internal server error"},
    },
)
async def create_key(
    key_data: DkimKeyCreate,
    current_user: dict = Depends(get_current_user_hybrid),
):
    """Create a new DKIM key"""
    if not _in_family(key_data.dkim_domain, current_user):
        raise HTTPException(status_code=403, detail="DKIM domain does not belong to this client")
    try:
        key = await dkim_key_service.create_key(key_data)
        logger.info(f"DKIM key created: ID {key.id}")
        return key
    except ValueError as e:
        logger.warning(f"Validation error creating DKIM key: {e}")
        raise HTTPException(status_code=400, detail=str(e)) from e
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating DKIM key: {e}")
        raise HTTPException(status_code=500, detail="Internal server error") from e


@router.get(
    "",
    response_model=list[DkimKeyResponse],
    summary="List DKIM Keys",
    description="""
## List DKIM Keys

Retrieve DKIM keys with optional filters.

### Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `cluster_id` | string | Filter by cluster ID |
| `selector` | string | Filter by DKIM selector |
| `dkim_domain` | string | Filter by DKIM domain |

### Authentication

Requires a valid JWT token or API key in the `Authorization` header.
    """,
    responses={
        200: {"description": "DKIM keys retrieved successfully"},
        401: {"description": "Invalid or expired token"},
        500: {"description": "Internal server error"},
    },
)
async def list_keys(
    cluster_id: str | None = Query(None, description="Filter by cluster_id"),
    selector: str | None = Query(None, description="Filter by DKIM selector"),
    dkim_domain: str | None = Query(None, description="Filter by DKIM domain"),
    current_user: dict = Depends(get_current_user_hybrid),
):
    """List DKIM keys with optional filters"""
    try:
        filters = DkimKeyFilter(
            cluster_id=cluster_id,
            selector=selector,
            dkim_domain=dkim_domain,
        )
        keys = await dkim_key_service.list_keys(filters)
        # RevBridge Connect: only the keys of the caller's domain, with the TXT value filled in
        keys = [await dkim_key_service._with_dns_record(k) for k in keys if _in_family(k.dkim_domain, current_user)]
        logger.info(f"Listed {len(keys)} DKIM keys")
        return keys
    except Exception as e:
        logger.error(f"Error listing DKIM keys: {e}")
        raise HTTPException(status_code=500, detail="Internal server error") from e


@router.get(
    "/{key_id}",
    response_model=DkimKeyResponse,
    summary="Get DKIM Key",
    description="""
## Get DKIM Key

Retrieve a specific DKIM key by ID.

### Authentication

Requires a valid JWT token or API key in the `Authorization` header.
    """,
    responses={
        200: {"description": "DKIM key found"},
        401: {"description": "Invalid or expired token"},
        404: {"description": "DKIM key not found"},
        500: {"description": "Internal server error"},
    },
)
async def get_key(
    key_id: int = Path(..., description="DKIM key ID", examples=[1], ge=1),
    current_user: dict = Depends(get_current_user_hybrid),
):
    """Get a specific DKIM key"""
    try:
        key = await _owned_key_or_404(key_id, current_user)
        return await dkim_key_service._with_dns_record(key)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting DKIM key {key_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error") from e


@router.put(
    "/{key_id}",
    response_model=DkimKeyResponse,
    summary="Update DKIM Key",
    description="""
## Update DKIM Key

Update an existing DKIM key.

### Important

- Updates to unique fields (cluster_id, selector, dkim_domain) will be validated
- Duplicate combinations are not allowed

### Authentication

Requires a valid JWT token or API key in the `Authorization` header.
    """,
    responses={
        200: {"description": "DKIM key updated successfully"},
        400: {"description": "Invalid request data"},
        401: {"description": "Invalid or expired token"},
        404: {"description": "DKIM key not found"},
        500: {"description": "Internal server error"},
    },
)
async def update_key(
    key_id: int = Path(..., description="DKIM key ID", examples=[1], ge=1),
    key_data: DkimKeyUpdate = ...,
    current_user: dict = Depends(get_current_user_hybrid),
):
    """Update an existing DKIM key"""
    try:
        await _owned_key_or_404(key_id, current_user)
        if getattr(key_data, "dkim_domain", None) and not _in_family(key_data.dkim_domain, current_user):
            raise HTTPException(status_code=403, detail="DKIM domain does not belong to this client")
        key = await dkim_key_service.update_key(key_id, key_data)
        if not key:
            raise HTTPException(status_code=404, detail="DKIM key not found")
        logger.info(f"DKIM key updated: ID {key_id}")
        return key
    except HTTPException:
        raise
    except ValueError as e:
        logger.warning(f"Validation error updating DKIM key: {e}")
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.error(f"Error updating DKIM key {key_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error") from e


@router.delete(
    "/{key_id}",
    status_code=204,
    summary="Delete DKIM Key",
    description="""
## Delete DKIM Key

Permanently delete a DKIM key.

### Warning

- This action is **irreversible**
- Emails using this key will no longer be DKIM signed

### Authentication

Requires a valid JWT token or API key in the `Authorization` header.
    """,
    responses={
        204: {"description": "DKIM key deleted successfully"},
        401: {"description": "Invalid or expired token"},
        404: {"description": "DKIM key not found"},
        500: {"description": "Internal server error"},
    },
)
async def delete_key(
    key_id: int = Path(..., description="DKIM key ID", examples=[1], ge=1),
    current_user: dict = Depends(get_current_user_hybrid),
):
    """Delete a DKIM key"""
    try:
        await _owned_key_or_404(key_id, current_user)
        success = await dkim_key_service.delete_key(key_id)
        if not success:
            raise HTTPException(status_code=404, detail="DKIM key not found")
        logger.info(f"DKIM key deleted: ID {key_id}")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting DKIM key {key_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error") from e
