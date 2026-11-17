"""
Base FastAPI router for the API Key endpoints

This router can be used as is or extended by the projects.
"""

from fastapi import APIRouter, HTTPException, Depends, Query, Path, status, Request
from typing import Any, Awaitable, Callable, Dict, List, Optional
import logging

from .models import (
    ApiKeyCreate,
    ApiKeyUpdate,
    ApiKeyResponse,
    ApiKeyCreateResponse,
    ApiKeyListResponse,
    ApiKeyFilter,
    ApiKeyUsageStats,
    ApiKeyPermission,
    ChannelCreate,
    ChannelResponse,
    ChannelUpdate,
    ChannelBulkCreate,
    ChannelBulkResponse,
)  # noqa: F401
from .service import ApiKeyServiceBase
from ..auth.hybrid import get_current_user_hybrid, get_auth_type

logger = logging.getLogger(__name__)


def create_api_key_router(
    api_key_service: ApiKeyServiceBase,
    prefix: str = "/api-keys",
    tags: Optional[List[str]] = None,
    get_user_domain_id: Optional[Callable[[Dict[str, Any]], Optional[int]]] = None,
    validate_domain_id: Optional[Callable[[int], bool]] = None,
    get_user_domain_name: Optional[Callable[[Dict[str, Any]], Optional[str]]] = None,
    admin_domain_name: Optional[str] = None,
    api_key_create_with_domain_model: Optional[type] = None,
    domain_owned: Optional[Callable[[int, Dict[str, Any]], Awaitable[bool]]] = None,
) -> APIRouter:
    """
    Creates a FastAPI router for API Key management

    Args:
        api_key_service: API Key service instance
        prefix: Route prefix (default: "/api-keys")
        tags: OpenAPI documentation tags (default: ["api-keys"])
        get_user_domain_id: Function that gets the user's domain_id (optional)
        validate_domain_id: Function that checks whether a domain_id exists (optional)
        get_user_domain_name: Function that gets the name of the user's domain (optional)
        admin_domain_name: Name of the admin domain that may create API Keys for other domains (optional)
        api_key_create_with_domain_model: Pydantic model for creating an API Key with a domain_id (optional)
        domain_owned: async (domain_id, current_user) -> bool. When given, every domain_id
            stored in a key or in a channel must belong to one of the caller's customer's domains.

    Returns:
        Configured APIRouter

    Note:
        If `admin_domain_name` and `api_key_create_with_domain_model` are provided,
        the `/admin/create` endpoint for administrative API Key creation is added.
    """
    router = APIRouter(prefix=prefix, tags=tags or ["api-keys"])

    def _get_user_id(current_user: Dict[str, Any]) -> Optional[str]:
        """Extracts the user_id from current_user (supports JWT and API Key)"""
        return current_user.get("sub") or current_user.get("user_id")

    def _get_domain_id(current_user: Dict[str, Any]) -> Optional[int]:
        """Gets the user's domain_id"""
        if get_user_domain_id:
            return get_user_domain_id(current_user)
        return current_user.get("domain_id")

    def _get_client_id(current_user: Dict[str, Any]) -> Optional[str]:
        """Gets the user's client_id (the customer's UUID)"""
        return current_user.get("client_id")

    def _is_api_key_principal(current_user: Dict[str, Any]) -> bool:
        return bool(current_user.get("api_key_id") or current_user.get("api_key_db_id"))

    def _assert_can_manage_keys(current_user: Dict[str, Any]) -> None:
        """RevBridge Connect: only an admin creates, changes or revokes keys and manages their channels.

        Admin = a panel user (JWT) with the admin role, or a key with the admin permission. A key
        without the admin permission can neither raise its own permissions nor create other keys.
        """
        if _is_api_key_principal(current_user):
            if "admin" in (current_user.get("permissions") or []):
                return
        elif current_user.get("role") == "admin":
            return
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Managing keys requires the admin role",
        )

    async def _assert_domain_owned(domain_id: Any, current_user: Dict[str, Any]) -> None:
        """A domain_id stored in a key or a channel must belong to one of the same customer's domains."""
        if domain_id is None or domain_owned is None:
            return
        try:
            did = int(domain_id)
        except (TypeError, ValueError):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid domain_id")
        if not await domain_owned(did, current_user):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="The domain does not belong to this customer",
            )

    def _config_domain_id(config: Any) -> Any:
        if isinstance(config, dict):
            return config.get("domain_id")
        return getattr(config, "domain_id", None)

    @router.post(
        "",
        response_model=ApiKeyCreateResponse,
        status_code=201,
        summary="🔑 Create API key",
        description="""
## Create a New API Key

Endpoint that creates a new API key for programmatic authentication.

### ⚠️ Important
- The actual key is shown **only at creation**
- Store the key in a safe place
- Use HTTPS for every request
        """,
    )
    async def create_api_key(
        api_key_data: ApiKeyCreate,
        current_user: dict = Depends(get_current_user_hybrid),
    ):
        """Create a new API key"""
        try:
            _assert_can_manage_keys(current_user)
            user_id = _get_user_id(current_user)
            if not user_id:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Incomplete user data",
                )

            domain_id = _get_domain_id(current_user)
            if not domain_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="User is not associated with a domain",
                )

            client_id = _get_client_id(current_user)
            if not client_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="User is not associated with a customer. Contact support.",
                )

            # Validate the domain_id if a function was provided
            if validate_domain_id and not validate_domain_id(domain_id):
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Domain not found (domain_id: {domain_id})",
                )

            api_key = await api_key_service.create_api_key(
                api_key_data=api_key_data,
                user_id=user_id,
                domain_id=domain_id,
                client_id=client_id,
            )

            logger.info(f"API key created: {api_key.api_key_id} for user {user_id}")
            return api_key

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error creating API key: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Internal server error",
            )

    @router.get(
        "",
        response_model=ApiKeyListResponse,
        summary="📋 List API keys",
    )
    async def list_api_keys(
        page: int = Query(1, ge=1, description="Page number"),
        limit: int = Query(50, ge=1, le=100, description="Items per page"),
        name: Optional[str] = Query(None, description="Filter by name"),
        is_active: Optional[bool] = Query(None, description="Filter by active status"),
        domain_id: Optional[int] = Query(None, description="Filter by domain"),
        permission: Optional[ApiKeyPermission] = Query(
            None, description="Filter by permission"
        ),
        current_user: dict = Depends(get_current_user_hybrid),
    ):
        """List the API keys of the current customer"""
        try:
            client_id = _get_client_id(current_user)
            if not client_id:
                # SECURITY: filtering by client_id is the only tenant boundary
                # we trust. Refuse when the caller has no client_id (e.g.
                # JWT-only auth without customer linkage).
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="User is not associated with a customer.",
                )

            filters = ApiKeyFilter(
                name=name,
                is_active=is_active,
                domain_id=domain_id,
                permission=permission,
            )

            api_keys = await api_key_service.list_api_keys(
                client_id=client_id, page=page, limit=limit, filters=filters
            )

            return api_keys

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error listing API keys: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Internal server error",
            )

    # =====================================================
    # /me ENDPOINTS (the API key used to authenticate)
    # They must come BEFORE /{api_key_id} to avoid a conflict
    # =====================================================

    @router.get(
        "/me",
        response_model=ApiKeyResponse,
        summary="🔑 Current API key",
        description="Returns the data of the API key used in the current authentication.",
    )
    async def get_current_api_key(
        current_user: dict = Depends(get_current_user_hybrid),
    ):
        """Get the data of the API key used to authenticate"""
        try:
            api_key_db_id = current_user.get("api_key_db_id")
            if not api_key_db_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="This endpoint requires API key authentication",
                )

            client_id = _get_client_id(current_user)
            if not client_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="User is not associated with a customer.",
                )

            api_key = await api_key_service.get_api_key_by_id(api_key_db_id, client_id)
            if not api_key:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="API key not found",
                )

            return api_key

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error fetching the current API key: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Internal server error",
            )

    @router.put(
        "/me",
        response_model=ApiKeyResponse,
        summary="✏️ Update the current API key",
        description="Updates the API key used in the current authentication.",
    )
    async def update_current_api_key(
        request: Request,
        update_data: ApiKeyUpdate = ...,
        current_user: dict = Depends(get_current_user_hybrid),
    ):
        """Update the API key used to authenticate"""
        try:
            api_key_db_id = current_user.get("api_key_db_id")
            if not api_key_db_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="This endpoint requires API key authentication",
                )

            client_id = _get_client_id(current_user)
            if not client_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="User is not associated with a customer.",
                )
            _assert_can_manage_keys(current_user)
            # the key's customer never changes, and the domain must belong to the customer
            if update_data.client_id and update_data.client_id != client_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="client_id does not belong to this credential",
                )
            await _assert_domain_owned(update_data.domain_id, current_user)

            api_key = await api_key_service.update_api_key(
                api_key_db_id, client_id, update_data
            )
            if not api_key:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="API key not found",
                )

            logger.info(f"API key updated via /me: {api_key.api_key_id}")

            return api_key

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error updating the current API key: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Internal server error",
            )

    # =====================================================
    # /{api_key_id} ENDPOINTS
    # =====================================================

    @router.get(
        "/{api_key_id}",
        response_model=ApiKeyResponse,
        summary="🔍 Get API key",
    )
    async def get_api_key(
        api_key_id: int = Path(..., description="API key ID", ge=1),
        current_user: dict = Depends(get_current_user_hybrid),
    ):
        """Get an API key by ID"""
        try:
            client_id = _get_client_id(current_user)
            if not client_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="User is not associated with a customer.",
                )

            api_key = await api_key_service.get_api_key_by_id(api_key_id, client_id)
            if not api_key:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="API key not found",
                )

            return api_key

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error fetching API key: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Internal server error",
            )

    @router.put(
        "/{api_key_id}",
        response_model=ApiKeyResponse,
        summary="✏️ Update API key",
    )
    async def update_api_key(
        api_key_id: int = Path(..., description="API key ID", ge=1),
        update_data: ApiKeyUpdate = ...,
        current_user: dict = Depends(get_current_user_hybrid),
    ):
        """Update an API key"""
        try:
            client_id = _get_client_id(current_user)
            if not client_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="User is not associated with a customer.",
                )
            _assert_can_manage_keys(current_user)
            # the key's customer never changes, and the domain must belong to the customer
            if update_data.client_id and update_data.client_id != client_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="client_id does not belong to this credential",
                )
            await _assert_domain_owned(update_data.domain_id, current_user)

            api_key = await api_key_service.update_api_key(
                api_key_id, client_id, update_data
            )
            if not api_key:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="API key not found",
                )

            logger.info(f"API key updated: {api_key.api_key_id}")
            return api_key

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error updating API key: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Internal server error",
            )

    @router.delete(
        "/{api_key_id}",
        status_code=204,
        summary="🗑️ Delete API key",
    )
    async def delete_api_key(
        api_key_id: int = Path(..., description="API key ID", ge=1),
        current_user: dict = Depends(get_current_user_hybrid),
    ):
        """Delete an API key"""
        try:
            client_id = _get_client_id(current_user)
            if not client_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="User is not associated with a customer.",
                )
            _assert_can_manage_keys(current_user)

            success = await api_key_service.delete_api_key(api_key_id, client_id)
            if not success:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="API key not found",
                )

            user_id_for_log = _get_user_id(current_user)
            logger.info(f"API key deleted: {api_key_id} by user {user_id_for_log} (client_id={client_id})")
            return None

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error deleting API key: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Internal server error",
            )

    @router.get(
        "/stats/usage",
        response_model=List[ApiKeyUsageStats],
        summary="📊 Usage statistics",
    )
    async def get_api_key_stats(current_user: dict = Depends(get_current_user_hybrid)):
        """Get the API keys' usage statistics"""
        try:
            client_id = _get_client_id(current_user)
            if not client_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="User is not associated with a customer.",
                )

            stats = await api_key_service.get_usage_stats(client_id)
            return stats

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error getting statistics: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Internal server error",
            )

    # =====================================================
    # CHANNEL MANAGEMENT ENDPOINTS
    # =====================================================

    @router.get(
        "/{api_key_id}/channels",
        response_model=List[ChannelResponse],
        summary="📡 List the API key's channels",
        description="""
## List Enabled Channels

Returns the list of channels enabled for a specific API key.

Each channel includes:
- The channel's name and display name
- Active/inactive status
- The channel's specific configuration
        """,
    )
    async def list_api_key_channels(
        api_key_id: str = Path(
            ..., description="API key ID (e.g. ak_xxx)", min_length=1
        ),
        current_user: dict = Depends(get_current_user_hybrid),
    ):
        """List the API key's enabled channels"""
        try:
            client_id = _get_client_id(current_user)
            if not client_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="User is not associated with a customer.",
                )

            # Look up the API key by its api_key_id string (scope: client_id)
            api_key = await api_key_service.get_api_key_by_key_id(api_key_id, client_id)
            if not api_key:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="API key not found",
                )

            channels = await api_key_service.get_channels_for_api_key(api_key.id)
            return channels

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error listing channels: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Internal server error",
            )

    @router.post(
        "/{api_key_id}/channels",
        response_model=ChannelResponse,
        status_code=201,
        summary="➕ Add a channel to the API key",
        description="""
## Add a Channel

Enables a new channel for an API key.

### Available channels
- **email**: requires `domain_id` in the configuration

### Email configuration example
```json
{
    "channel": "email",
    "config": {
        "domain_id": 1,
        "include_subdomains": true
    }
}
```
        """,
    )
    async def add_channel_to_api_key(
        channel_data: ChannelCreate,
        api_key_id: str = Path(
            ..., description="API key ID (e.g. ak_xxx)", min_length=1
        ),
        current_user: dict = Depends(get_current_user_hybrid),
    ):
        """Add a channel to the API key"""
        try:
            client_id = _get_client_id(current_user)
            if not client_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="User is not associated with a customer.",
                )
            _assert_can_manage_keys(current_user)
            await _assert_domain_owned(_config_domain_id(channel_data.config), current_user)

            # Look up the API key by its api_key_id string (scope: client_id)
            api_key = await api_key_service.get_api_key_by_key_id(api_key_id, client_id)
            if not api_key:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="API key not found",
                )

            channel = await api_key_service.add_channel_to_api_key(
                api_key.id, channel_data
            )
            logger.info(
                f"Channel {channel_data.channel.value} added to API key {api_key_id}"
            )
            return channel

        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(e),
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error adding channel: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Internal server error",
            )

    @router.post(
        "/{api_key_id}/channels/bulk",
        response_model=ChannelBulkResponse,
        status_code=201,
        summary="➕ Add several channels to the API key",
        description="""
## Add Several Channels

Enables several channels for an API key in a single request.

### Available channels
- **email**: requires `domain_id` in the configuration

### Behavior
- Processes every channel, even if some fail
- Returns the individual result of each channel
- Channels that already exist return an error (they are not duplicated)

### Request example
```json
{
    "channels": [
        {
            "channel": "email",
            "config": {"domain_id": 1, "include_subdomains": true}
        }
    ]
}
```
        """,
    )
    async def add_channels_to_api_key(
        bulk_data: ChannelBulkCreate,
        api_key_id: str = Path(
            ..., description="API key ID (e.g. ak_xxx)", min_length=1
        ),
        current_user: dict = Depends(get_current_user_hybrid),
    ):
        """Add several channels to the API key"""
        try:
            client_id = _get_client_id(current_user)
            if not client_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="User is not associated with a customer.",
                )
            _assert_can_manage_keys(current_user)
            for ch in bulk_data.channels:
                await _assert_domain_owned(_config_domain_id(ch.config), current_user)

            # Look up the API key by its api_key_id string (scope: client_id)
            api_key = await api_key_service.get_api_key_by_key_id(api_key_id, client_id)
            if not api_key:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="API key not found",
                )

            result = await api_key_service.add_channels_to_api_key(
                api_key.id, bulk_data.channels
            )

            logger.info(
                f"Bulk add channels: {result.success_count} succeeded, "
                f"{result.error_count} failed for API key {api_key_id}"
            )
            return result

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error adding channels in bulk: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Internal server error",
            )

    @router.put(
        "/{api_key_id}/channels/{channel_name}",
        response_model=ChannelResponse,
        summary="✏️ Update an API key channel",
        description="""
## Update a Channel's Configuration

Updates the configuration or the status of a specific channel.

### Updatable fields
- `is_active`: enable/disable the channel
- `config`: the channel's new configuration
        """,
    )
    async def update_api_key_channel(
        update_data: ChannelUpdate,
        api_key_id: str = Path(
            ..., description="API key ID (e.g. ak_xxx)", min_length=1
        ),
        channel_name: str = Path(
            ..., description="Channel name (`email`)"
        ),
        current_user: dict = Depends(get_current_user_hybrid),
    ):
        """Update an API key channel"""
        try:
            client_id = _get_client_id(current_user)
            if not client_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="User is not associated with a customer.",
                )
            _assert_can_manage_keys(current_user)
            await _assert_domain_owned(_config_domain_id(update_data.config), current_user)

            # Look up the API key by its api_key_id string (scope: client_id)
            api_key = await api_key_service.get_api_key_by_key_id(api_key_id, client_id)
            if not api_key:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="API key not found",
                )

            channel = await api_key_service.update_api_key_channel(
                api_key.id, channel_name, update_data
            )

            if not channel:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Channel '{channel_name}' not found for this API key",
                )

            logger.info(f"Channel {channel_name} updated for API key {api_key_id}")
            return channel

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error updating channel: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Internal server error",
            )

    @router.delete(
        "/{api_key_id}/channels/{channel_name}",
        status_code=204,
        summary="🗑️ Remove a channel from the API key",
        description="""
## Remove a Channel

Removes an enabled channel from an API key.
The API key can no longer be used for this channel's operations.
        """,
    )
    async def remove_channel_from_api_key(
        api_key_id: str = Path(
            ..., description="API key ID (e.g. ak_xxx)", min_length=1
        ),
        channel_name: str = Path(
            ..., description="Channel name (`email`)"
        ),
        current_user: dict = Depends(get_current_user_hybrid),
    ):
        """Remove a channel from the API key"""
        try:
            client_id = _get_client_id(current_user)
            if not client_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="User is not associated with a customer.",
                )
            _assert_can_manage_keys(current_user)

            # Look up the API key by its api_key_id string (scope: client_id)
            api_key = await api_key_service.get_api_key_by_key_id(api_key_id, client_id)
            if not api_key:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="API key not found",
                )

            success = await api_key_service.remove_channel_from_api_key(
                api_key.id, channel_name
            )

            if not success:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Channel '{channel_name}' not found for this API key",
                )

            logger.info(f"Channel {channel_name} removed from API key {api_key_id}")
            return None

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error removing channel: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Internal server error",
            )

    # Administrative endpoint that creates an API key for a specific domain (optional)
    if admin_domain_name and api_key_create_with_domain_model:

        @router.post(
            "/admin/create",
            response_model=ApiKeyCreateResponse,
            status_code=201,
            summary="🔑 Create API key (admin)",
            description=f"""
## Create an Administrative API Key

Administrative endpoint that creates an API key for a specific customer.
**Only users of the {admin_domain_name} domain with the admin permission can use this endpoint.**

### 🔐 Requirements
- The user must belong to the **{admin_domain_name}** domain
- The user must have the **admin** permission (role "admin" for JWT or the "admin" permission for an API key)

### ✨ Features
- Creates API keys for any customer of the installation
- domain_id is optional (only needed when the email channel is used)
- Useful for provisioning and centralized management

### ⚠️ Important
- The actual key is shown **only at creation**
- Store the key in a safe place
- Use HTTPS for every request
            """,
        )
        async def create_api_key_for_domain(
            api_key_data: api_key_create_with_domain_model,
            request: Request,
            current_user: dict = Depends(get_current_user_hybrid),
        ):
            """Create an API key for a specific customer (admins only)"""
            try:
                # Check that the user belongs to the admin domain
                if get_user_domain_name:
                    user_domain_name = get_user_domain_name(current_user)
                    if user_domain_name != admin_domain_name:
                        logger.warning(
                            f"Attempt to create an API key for another domain by an unauthorized user: "
                            f"user_domain={user_domain_name}, user_id={current_user.get('sub') or current_user.get('user_id')}"
                        )
                        raise HTTPException(
                            status_code=status.HTTP_403_FORBIDDEN,
                            detail=f"Only users of the {admin_domain_name} domain with the admin permission can create API keys for other domains",
                        )

                # Check that the user has the admin permission
                auth_type = get_auth_type(request)
                is_admin = False

                if auth_type == "api_key":
                    is_admin = "admin" in current_user.get("permissions", [])
                elif auth_type == "jwt":
                    is_admin = current_user.get("role") == "admin"

                if not is_admin:
                    logger.warning(
                        f"Attempt to create an API key for another domain by a user without the admin permission: "
                        f"user_id={current_user.get('sub') or current_user.get('user_id')}, "
                        f"role={current_user.get('role')}, permissions={current_user.get('permissions')}"
                    )
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail=f"Only users of the {admin_domain_name} domain with the admin permission can create API keys for other domains",
                    )

                # Validate that the domain_id exists (only if provided)
                domain_id = getattr(api_key_data, "domain_id", None)
                if (
                    domain_id is not None
                    and validate_domain_id
                    and not validate_domain_id(domain_id)
                ):
                    logger.error(f"Domain ID {domain_id} not found")
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=f"Domain not found (domain_id: {domain_id})",
                    )

                user_id = _get_user_id(current_user)
                if not user_id:
                    logger.error("The authenticated session has no user ID")
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        detail="Incomplete user data",
                    )

                # Use the client_id from the body if provided, otherwise the admin user's (may be None)
                client_id = getattr(api_key_data, "client_id", None) or _get_client_id(
                    current_user
                )

                # Create the API key with the provided domain_id (or None)
                api_key = await api_key_service.create_api_key(
                    api_key_data=api_key_data,
                    user_id=user_id,
                    domain_id=domain_id,
                    client_id=client_id,
                )

                logger.info(
                    f"API key created: {api_key.api_key_id} (client_id={client_id}) by admin user {user_id}"
                )
                return api_key

            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error creating an API key for a specific domain: {e}")
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Internal server error",
                )

    return router
