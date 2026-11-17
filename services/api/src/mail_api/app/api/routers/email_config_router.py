"""
Email Configuration Router

Endpoints for managing email configuration settings:
- Create, list, retrieve, update, and delete configurations
- Manage SMTP credentials per domain
- Enable/disable configurations
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from mail_api.models.email_config_models import (
    EmailConfigCreate,
    EmailConfigFilter,
    EmailConfigResponse,
    EmailConfigUpdate,
)
from mail_api.services.email_config_service import email_config_service
from mail_api.shared.domain_validation import get_authorized_domain_ids, get_user_domain_id
from connect_lib.auth.hybrid import (
    require_read_emails_hybrid,
    require_write_emails_hybrid,
)

# Router
router = APIRouter(prefix="/email-configs", tags=["email-configs"])
logger = logging.getLogger(__name__)



async def _allowed_domain_ids(current_user: dict) -> set[int]:
    """Domains whose configuration the credential manages: primary ∪ api_key_domains ∪
    the key's own legacy domain_id.

    RevBridge Connect: a credential without a domain sees no configuration at all; the
    checks below never fall back to listing other clients' configurations (SMTP,
    sender, tracking).
    """
    allowed = await get_authorized_domain_ids(current_user)
    legacy = current_user.get("domain_id")
    if legacy:
        try:
            allowed.add(int(legacy))
        except (ValueError, TypeError):
            pass
    if not allowed:
        raise HTTPException(status_code=403, detail="User is not associated with a domain")
    return allowed


@router.post(
    "",
    response_model=EmailConfigResponse,
    status_code=201,
    summary="Create Email Configuration",
    description="""
## Create Email Configuration

Create a new email configuration for your domain.

### Features

- **SMTP Settings**: Complete SMTP configuration per domain
- **Tracking Options**: Enable/disable pixel tracking and click tracking
- **Rate Limiting**: Configurable sending rate limits
- **Retry Settings**: Customize retry behavior for failed deliveries
- **Default Webhook**: Set default webhook URL for events

### Required Fields

| Field | Type | Description |
|-------|------|-------------|
| `smtp_host` | string | SMTP server hostname |
| `smtp_port` | integer | SMTP server port |
| `smtp_username` | string | SMTP authentication username |
| `smtp_password` | string | SMTP authentication password |

### Optional Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `is_default` | boolean | false | Set as default configuration |
| `tracking_enabled` | boolean | true | Enable email tracking |
| `rate_limit` | integer | 100 | Maximum emails per minute |
| `retry_count` | integer | 3 | Number of retry attempts |

### Authentication

Requires a valid JWT token or API key in the `Authorization` header.
    """,
    responses={
        201: {
            "description": "Email configuration created successfully",
            "content": {
                "application/json": {
                    "example": {
                        "id": "cfg_abc123",
                        "domain_id": 1,
                        "smtp_host": "smtp.example.com",
                        "smtp_port": 587,
                        "is_active": True,
                        "is_default": True,
                        "tracking_enabled": True,
                        "created_at": "2025-01-21T16:30:00Z"
                    }
                }
            }
        },
        400: {
            "description": "Invalid request data",
            "content": {
                "application/json": {
                    "example": {"detail": "SMTP host is required"}
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
        403: {
            "description": "User not associated with a domain",
            "content": {
                "application/json": {
                    "example": {"detail": "User not associated with a domain"}
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
    tags=["email-configs"],
    operation_id="email_configs_create",
)
async def create_email_config(
    config_data: EmailConfigCreate,
    current_user: dict = Depends(require_write_emails_hybrid),
):
    """Creates a new email configuration"""
    try:
        # domain_id from the JWT
        user_domain_id = get_user_domain_id(current_user)

        # Explicit domain_id in the body: allows creating the config of a domain
        # JUST linked to the key (the primary/channel may still point to the
        # previous domain during provisioning). Authorization = primary ∪
        # api_key_domains ∪ the key's own legacy domain_id.
        target_domain_id = user_domain_id
        if config_data.domain_id and config_data.domain_id != user_domain_id:
            # only a domain other than the credential's own needs the authorization lookup
            allowed = await _allowed_domain_ids(current_user)
            if config_data.domain_id not in allowed:
                raise HTTPException(
                    status_code=403,
                    detail=f"Domain ID {config_data.domain_id} not authorized for this credential",
                )
            target_domain_id = config_data.domain_id

        if not target_domain_id:
            raise HTTPException(
                status_code=403,
                detail="User is not associated with a domain"
            )

        config = await email_config_service.create_config(
            config_data, domain_id=target_domain_id, created_by=current_user.get("user_id")
        )
        return config
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.error(f"Error creating the configuration: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal error creating the configuration") from e


@router.get(
    "",
    response_model=list[EmailConfigResponse],
    summary="List Email Configurations",
    description="""
## List Email Configurations

Retrieve all email configurations for your domain with optional filters.

### Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `domain_id` | integer | Filter by domain ID |
| `is_active` | boolean | Filter by active status |
| `is_default` | boolean | Filter by default configuration |
| `smtp_host` | string | Search by SMTP host |
| `tracking_enabled` | boolean | Filter by tracking status |

### Response

Returns an array of email configurations matching the specified filters.

### Authentication

Requires a valid JWT token or API key in the `Authorization` header.
    """,
    responses={
        200: {
            "description": "Email configurations retrieved successfully",
            "content": {
                "application/json": {
                    "example": [
                        {
                            "id": "cfg_abc123",
                            "domain_id": 1,
                            "smtp_host": "smtp.example.com",
                            "smtp_port": 587,
                            "is_active": True,
                            "is_default": True,
                            "tracking_enabled": True,
                            "created_at": "2025-01-21T16:30:00Z"
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
    tags=["email-configs"],
    operation_id="email_configs_list",
)
async def list_email_configs(
    domain_id: int | None = Query(None, description="Filter by domain ID"),
    is_active: bool | None = Query(None, description="Filter by active status"),
    is_default: bool | None = Query(None, description="Filter by default configuration"),
    smtp_host: str | None = Query(None, description="Search by SMTP host"),
    tracking_enabled: bool | None = Query(None, description="Filter by tracking status"),
    current_user: dict = Depends(require_read_emails_hybrid),
):
    """Lists email configurations"""
    try:
        # Filter by the user's domain
        user_domain_id = get_user_domain_id(current_user)
        allowed = await _allowed_domain_ids(current_user)
        if not domain_id:
            domain_id = user_domain_id or min(allowed)
        # RevBridge Connect: ?domain_id= is only accepted for the caller's own domains
        # (another client's SMTP, sender and tracking settings are never listed). Same rule
        # as create: primary ∪ api_key_domains ∪ legacy domain_id.
        if domain_id != user_domain_id:
            if domain_id not in allowed:
                raise HTTPException(
                    status_code=403,
                    detail=f"Domain ID {domain_id} not authorized for this credential",
                )

        filters = EmailConfigFilter(
            domain_id=domain_id,
            is_active=is_active,
            is_default=is_default,
            smtp_host=smtp_host,
            tracking_enabled=tracking_enabled,
        )

        configs = await email_config_service.list_configs(filters, domain_id)
        return configs
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing configurations: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal error listing the configurations") from e


@router.get(
    "/{config_id}",
    response_model=EmailConfigResponse,
    summary="Get Email Configuration",
    description="""
## Get Email Configuration

Retrieve a specific email configuration by its ID.

### Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `config_id` | string | Configuration ID |

### Authentication

Requires a valid JWT token or API key in the `Authorization` header.
    """,
    responses={
        200: {
            "description": "Email configuration found",
            "content": {
                "application/json": {
                    "example": {
                        "id": "cfg_abc123",
                        "domain_id": 1,
                        "smtp_host": "smtp.example.com",
                        "smtp_port": 587,
                        "is_active": True,
                        "is_default": True,
                        "tracking_enabled": True,
                        "created_at": "2025-01-21T16:30:00Z"
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
        403: {
            "description": "Access denied",
            "content": {
                "application/json": {
                    "example": {"detail": "You do not have permission to access this configuration"}
                }
            }
        },
        404: {
            "description": "Configuration not found",
            "content": {
                "application/json": {
                    "example": {"detail": "Configuration not found"}
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
    tags=["email-configs"],
    operation_id="email_configs_get",
)
async def get_email_config(
    config_id: str = Path(..., description="Configuration ID"),
    current_user: dict = Depends(require_read_emails_hybrid),
):
    """Fetches an email configuration by ID"""
    try:
        config = await email_config_service.get_config(config_id)
        if not config:
            raise HTTPException(status_code=404, detail="Configuration not found")

        # Check access to the domain
        if config.domain_id not in await _allowed_domain_ids(current_user):
            raise HTTPException(
                status_code=403,
                detail="You do not have permission to access this configuration"
            )

        return config
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching the configuration: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal error fetching the configuration") from e


@router.get(
    "/domain/{domain_id}/default",
    response_model=EmailConfigResponse,
    summary="Get Default Configuration",
    description="""
## Get Default Configuration

Retrieve the default active email configuration for a specific domain.

### Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `domain_id` | integer | Domain ID |

### Response

Returns the default email configuration for the specified domain. Only active configurations marked as default are returned.

### Authentication

Requires a valid JWT token or API key in the `Authorization` header.
    """,
    responses={
        200: {
            "description": "Default configuration found",
            "content": {
                "application/json": {
                    "example": {
                        "id": "cfg_abc123",
                        "domain_id": 1,
                        "smtp_host": "smtp.example.com",
                        "smtp_port": 587,
                        "is_active": True,
                        "is_default": True,
                        "tracking_enabled": True,
                        "created_at": "2025-01-21T16:30:00Z"
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
        403: {
            "description": "Access denied",
            "content": {
                "application/json": {
                    "example": {"detail": "You do not have permission to access this domain"}
                }
            }
        },
        404: {
            "description": "No default configuration found",
            "content": {
                "application/json": {
                    "example": {"detail": "No default configuration found for this domain"}
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
    tags=["email-configs"],
    operation_id="email_configs_get_default",
)
async def get_default_config_for_domain(
    domain_id: int = Path(..., description="Domain ID"),
    current_user: dict = Depends(require_read_emails_hybrid),
):
    """Fetches the default configuration of a domain"""
    try:
        # Check access to the domain
        if domain_id not in await _allowed_domain_ids(current_user):
            raise HTTPException(
                status_code=403,
                detail="You do not have permission to access this domain"
            )

        config = await email_config_service.get_default_config_for_domain(domain_id)
        if not config:
            raise HTTPException(
                status_code=404,
                detail="No default configuration found for this domain"
            )

        return config
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching the default configuration: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal error fetching the configuration") from e


@router.put(
    "/{config_id}",
    response_model=EmailConfigResponse,
    summary="Update Email Configuration",
    description="""
## Update Email Configuration

Update an existing email configuration. Only provided fields will be updated.

### Updatable Fields

| Field | Type | Description |
|-------|------|-------------|
| `smtp_host` | string | SMTP server hostname |
| `smtp_port` | integer | SMTP server port |
| `smtp_username` | string | SMTP authentication username |
| `smtp_password` | string | SMTP authentication password |
| `is_active` | boolean | Enable/disable configuration |
| `is_default` | boolean | Set as default configuration |
| `tracking_enabled` | boolean | Enable/disable email tracking |
| `rate_limit` | integer | Maximum emails per minute |
| `retry_count` | integer | Number of retry attempts |

### Important

- Only provided fields are updated; omitted fields remain unchanged
- Setting `is_default` to `true` will unset the previous default configuration

### Authentication

Requires a valid JWT token or API key in the `Authorization` header.
    """,
    responses={
        200: {
            "description": "Email configuration updated successfully",
            "content": {
                "application/json": {
                    "example": {
                        "id": "cfg_abc123",
                        "domain_id": 1,
                        "smtp_host": "smtp.example.com",
                        "smtp_port": 587,
                        "is_active": True,
                        "is_default": True,
                        "tracking_enabled": True,
                        "updated_at": "2025-01-21T17:00:00Z"
                    }
                }
            }
        },
        400: {
            "description": "Invalid request data",
            "content": {
                "application/json": {
                    "example": {"detail": "Invalid SMTP port"}
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
        403: {
            "description": "Access denied",
            "content": {
                "application/json": {
                    "example": {"detail": "You do not have permission to update this configuration"}
                }
            }
        },
        404: {
            "description": "Configuration not found",
            "content": {
                "application/json": {
                    "example": {"detail": "Configuration not found"}
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
    tags=["email-configs"],
    operation_id="email_configs_update",
)
async def update_email_config(
    config_id: str = Path(..., description="Configuration ID"),
    config_data: EmailConfigUpdate = ...,
    current_user: dict = Depends(require_write_emails_hybrid),
):
    """Updates an email configuration"""
    try:
        # Does the configuration exist, and may the caller access it?
        current_config = await email_config_service.get_config(config_id)
        if not current_config:
            raise HTTPException(status_code=404, detail="Configuration not found")

        # Config of another domain of the SAME key (multi-domain): allowed if the
        # domain is authorized (api_key_domains ∪ legacy), mirroring the create rule
        # with an explicit domain_id.
        if current_config.domain_id not in await _allowed_domain_ids(current_user):
            raise HTTPException(
                status_code=403,
                detail="You do not have permission to update this configuration"
            )

        updated_config = await email_config_service.update_config(
            config_id, config_data, updated_by=current_user.get("user_id")
        )
        return updated_config
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.error(f"Error updating the configuration: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal error updating the configuration") from e


@router.delete(
    "/{config_id}",
    status_code=204,
    summary="Delete Email Configuration",
    description="""
## Delete Email Configuration

Permanently delete an email configuration.

### Warning

- This action is **irreversible**
- The configuration will be permanently removed
- If this is the default configuration, you must set another as default

### Authentication

Requires a valid JWT token or API key in the `Authorization` header.
    """,
    responses={
        204: {
            "description": "Email configuration deleted successfully"
        },
        401: {
            "description": "Invalid or expired token",
            "content": {
                "application/json": {
                    "example": {"detail": "Invalid or expired token"}
                }
            }
        },
        403: {
            "description": "Access denied",
            "content": {
                "application/json": {
                    "example": {"detail": "You do not have permission to delete this configuration"}
                }
            }
        },
        404: {
            "description": "Configuration not found",
            "content": {
                "application/json": {
                    "example": {"detail": "Configuration not found"}
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
    tags=["email-configs"],
    operation_id="email_configs_delete",
)
async def delete_email_config(
    config_id: str = Path(..., description="Configuration ID"),
    current_user: dict = Depends(require_write_emails_hybrid),
):
    """Deletes an email configuration"""
    try:
        # Does the configuration exist, and may the caller access it?
        current_config = await email_config_service.get_config(config_id)
        if not current_config:
            raise HTTPException(status_code=404, detail="Configuration not found")

        if current_config.domain_id not in await _allowed_domain_ids(current_user):
            raise HTTPException(
                status_code=403,
                detail="You do not have permission to delete this configuration"
            )

        success = await email_config_service.delete_config(
            config_id, deleted_by=current_user.get("user_id")
        )
        if not success:
            raise HTTPException(status_code=404, detail="Configuration not found")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting the configuration: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal error deleting the configuration") from e


@router.patch(
    "/{config_id}/toggle",
    response_model=EmailConfigResponse,
    summary="Toggle Configuration Status",
    description="""
## Toggle Configuration Status

Toggle the active/inactive status of an email configuration.

### Behavior

- If the configuration is active, it will be deactivated
- If the configuration is inactive, it will be activated

### Important

Deactivating a configuration will prevent it from being used for sending emails.

### Authentication

Requires a valid JWT token or API key in the `Authorization` header.
    """,
    responses={
        200: {
            "description": "Configuration status toggled successfully",
            "content": {
                "application/json": {
                    "example": {
                        "id": "cfg_abc123",
                        "domain_id": 1,
                        "smtp_host": "smtp.example.com",
                        "is_active": False,
                        "updated_at": "2025-01-21T17:00:00Z"
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
        403: {
            "description": "Access denied",
            "content": {
                "application/json": {
                    "example": {"detail": "You do not have permission to modify this configuration"}
                }
            }
        },
        404: {
            "description": "Configuration not found",
            "content": {
                "application/json": {
                    "example": {"detail": "Configuration not found"}
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
    tags=["email-configs"],
    operation_id="email_configs_toggle",
)
async def toggle_email_config(
    config_id: str = Path(..., description="Configuration ID"),
    current_user: dict = Depends(require_write_emails_hybrid),
):
    """Toggles the active/inactive status of a configuration"""
    try:
        current_config = await email_config_service.get_config(config_id)
        if not current_config:
            raise HTTPException(status_code=404, detail="Configuration not found")

        if current_config.domain_id not in await _allowed_domain_ids(current_user):
            raise HTTPException(
                status_code=403,
                detail="You do not have permission to modify this configuration"
            )

        update_data = EmailConfigUpdate(is_active=not current_config.is_active)
        updated_config = await email_config_service.update_config(
            config_id, update_data, updated_by=current_user.get("user_id")
        )
        return updated_config
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error toggling the status: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal error toggling the status") from e
