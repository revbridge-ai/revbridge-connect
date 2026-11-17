"""
Full implementation of the API Key service

Uses connect-lib's own database functions by default.
They can be overridden with custom functions if needed.
"""

import logging
import json
import asyncio
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any, Callable, Awaitable

from .service import ApiKeyServiceBase
from .models import (
    ApiKeyCreate,
    ApiKeyUpdate,
    ApiKeyResponse,
    ApiKeyCreateResponse,
    ApiKeyListResponse,
    ApiKeyFilter,
    ApiKeyUsageStats,
    ChannelCreate,
    ChannelResponse,
    ChannelUpdate,
    ChannelType,
    ChannelBulkResult,
    ChannelBulkResponse,
)
from .utils import verify_api_key_hash, generate_lookup_hash
from .cache import get_cached_api_key, set_cached_api_key
from ..db import fetch_one_async, fetch_all_async, exec_sql_async

logger = logging.getLogger(__name__)


class ApiKeyService(ApiKeyServiceBase):
    """
    Full implementation of the API Key service.

    Uses connect-lib's own database functions by default.
    They can be overridden with custom functions if needed.
    """

    def __init__(
        self,
        fetch_one: Optional[
            Callable[[str, List[Any]], Awaitable[Optional[Dict[str, Any]]]]
        ] = None,
        fetch_all: Optional[
            Callable[[str, List[Any]], Awaitable[List[Dict[str, Any]]]]
        ] = None,
        execute: Optional[Callable[[str, List[Any]], Awaitable[Any]]] = None,
        key_prefix: str = "rbak_",
    ):
        """
        Initializes the API Key service

        Args:
            fetch_one: Optional function that fetches a single row
                If None, uses connect-lib's fetch_one_async
                Signature: (query: str, params: List[Any]) -> Awaitable[Optional[Dict]]
            fetch_all: Optional function that fetches several rows
                If None, uses connect-lib's fetch_all_async
                Signature: (query: str, params: List[Any]) -> Awaitable[List[Dict]]
            execute: Optional function that executes queries
                If None, uses connect-lib's exec_sql_async
                Signature: (query: str, params: List[Any]) -> Awaitable[Any]
            key_prefix: API Key prefix (default: "rbak_")
        """
        super().__init__(key_prefix)
        # connect-lib's functions by default
        self.fetch_one = fetch_one or fetch_one_async
        self.fetch_all = fetch_all or fetch_all_async
        self.execute = execute or exec_sql_async

    def _parse_permissions(self, permissions_str: Any) -> List[str]:
        """Converts permissions from a JSON string to a list"""
        if isinstance(permissions_str, str):
            try:
                return json.loads(permissions_str)
            except (json.JSONDecodeError, ValueError):
                # legacy rows stored a Python list literal: parse it as data, never as code
                import ast

                try:
                    value = ast.literal_eval(permissions_str)
                except (ValueError, SyntaxError):
                    return []
                return [str(v) for v in value] if isinstance(value, (list, tuple)) else []
        return permissions_str if isinstance(permissions_str, list) else []

    def _format_permissions(self, permissions: List[str]) -> str:
        """Converts a list of permissions to a JSON string"""
        return json.dumps(permissions)

    async def create_api_key(
        self,
        api_key_data: ApiKeyCreate,
        user_id: str,
        domain_id: Optional[int] = None,
        client_id: Optional[str] = None,
    ) -> ApiKeyCreateResponse:
        """Creates a new API Key"""

        # client_id is optional - it can be linked later through an update
        # domain_id is optional (only needed when the email channel is used)
        if domain_id is not None and isinstance(domain_id, str):
            try:
                domain_id = int(domain_id)
            except (ValueError, TypeError):
                logger.error(f"Could not convert domain_id '{domain_id}' to int")
                raise ValueError(f"Invalid domain_id: {domain_id}")

        logger.debug(f"Creating API key: user_id={user_id}, domain_id={domain_id}")

        api_key = self._generate_api_key()
        api_key_id = self._generate_api_key_id()
        key_hash = self._hash_api_key(api_key)
        key_lookup_hash = generate_lookup_hash(api_key)

        permissions_list = [perm.value for perm in api_key_data.permissions]

        query = """
        INSERT INTO api_keys (
            api_key_id, name, key_hash, key_lookup_hash, user_id, service_account_id,
            client_id, domain_id, permissions, expires_at, is_active
        ) VALUES (
            %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s
        )
        """

        params = [
            api_key_id,
            api_key_data.name,
            key_hash,
            key_lookup_hash,
            user_id,
            None,
            client_id,
            domain_id,
            self._format_permissions(permissions_list),
            api_key_data.expires_at,
            True,
        ]

        try:
            logger.debug(
                f"Inserting API key into the database: api_key_id={api_key_id}, user_id={user_id}, domain_id={domain_id}"
            )
            result = await self.execute(query, params)
            api_key_db_id = result

            logger.info(f"API key inserted. Returned ID: {api_key_db_id}")

            if not api_key_db_id or api_key_db_id == 0:
                # Row was inserted (api_key_id is unique) but lastrowid came back
                # 0 — recover the id via a tenant-scoped lookup. client_id is the
                # tenant boundary (see get_api_key_by_id docstring); for admin-
                # provisioned keys with client_id=None this still works because
                # the unique api_key_id pins the row.
                logger.error(f"INSERT returned an invalid ID: {api_key_db_id}")
                fallback_key = await self.get_api_key_by_key_id(api_key_id, client_id)
                if fallback_key:
                    logger.warning(
                        f"API key was inserted (found by api_key_id), but lastrowid returned {api_key_db_id}"
                    )
                    api_key_db_id = fallback_key.id
                else:
                    raise Exception(
                        f"Error inserting API key: invalid ID returned ({api_key_db_id}) and row not found"
                    )

            # Build the response from inputs we already have: every field below is
            # known by this function (or generated here), so there is no need to
            # read the row back with a SELECT.
            now = datetime.now(timezone.utc)
            logger.info(
                f"API key created: id={api_key_db_id}, api_key_id={api_key_id}, client_id={client_id}"
            )
            return ApiKeyCreateResponse(
                id=api_key_db_id,
                api_key_id=api_key_id,
                name=api_key_data.name,
                api_key=api_key,
                permissions=permissions_list,
                is_active=True,
                expires_at=api_key_data.expires_at,
                created_at=now,
                owner_id=user_id,
                client_id=client_id,
                domain_id=domain_id,
            )

        except Exception as e:
            logger.error(f"Error creating API key: {e}")
            raise Exception(f"Error creating API key: {str(e)}")

    async def get_api_key_by_id(
        self, api_key_id: int, client_id: str
    ) -> Optional[ApiKeyResponse]:
        """Gets an API Key by ID (scope: customer/client_id).

        SECURITY: filter is by client_id, not user_id. user_id identifies
        who CREATED the row — for keys provisioned via the admin endpoint,
        that's a shared admin user across many customers, so user_id would
        leak rows across tenants. client_id is the real tenant boundary.
        """
        query = """
        SELECT id, api_key_id, name, permissions, is_active, last_used_at,
               expires_at, created_at, updated_at, user_id, service_account_id, client_id, domain_id
        FROM api_keys
        WHERE id = %s AND client_id = %s
        """

        result = await self.fetch_one(query, [api_key_id, client_id])
        if not result:
            return None

        permissions = self._parse_permissions(result["permissions"])

        return ApiKeyResponse(
            id=result["id"],
            api_key_id=result["api_key_id"],
            name=result["name"],
            permissions=permissions,
            is_active=bool(result["is_active"]),
            last_used_at=result["last_used_at"],
            expires_at=result["expires_at"],
            created_at=result["created_at"],
            updated_at=result["updated_at"],
            owner_id=result.get("user_id"),
            service_account_id=result.get("service_account_id"),
            client_id=result.get("client_id"),
            domain_id=result.get("domain_id"),
        )

    async def get_api_key_by_key_id(
        self, api_key_id: str, client_id: str
    ) -> Optional[ApiKeyResponse]:
        """Gets an API Key by api_key_id (scope: customer/client_id). See get_api_key_by_id for rationale."""
        query = """
        SELECT id, api_key_id, name, permissions, is_active, last_used_at,
               expires_at, created_at, updated_at, user_id, service_account_id, client_id, domain_id
        FROM api_keys
        WHERE api_key_id = %s AND client_id = %s
        """

        result = await self.fetch_one(query, [api_key_id, client_id])
        if not result:
            return None

        permissions = self._parse_permissions(result["permissions"])

        return ApiKeyResponse(
            id=result["id"],
            api_key_id=result["api_key_id"],
            name=result["name"],
            permissions=permissions,
            is_active=bool(result["is_active"]),
            last_used_at=result["last_used_at"],
            expires_at=result["expires_at"],
            created_at=result["created_at"],
            updated_at=result["updated_at"],
            owner_id=result.get("user_id"),
            service_account_id=result.get("service_account_id"),
            client_id=result.get("client_id"),
            domain_id=result.get("domain_id"),
        )

    async def list_api_keys(
        self,
        client_id: str,
        page: int = 1,
        limit: int = 50,
        filters: Optional[ApiKeyFilter] = None,
    ) -> ApiKeyListResponse:
        """Lists the customer's API Keys (scope: client_id). See get_api_key_by_id for rationale."""

        base_query = """
        FROM api_keys
        WHERE client_id = %s
        """

        params = [client_id]

        if filters:
            if filters.name:
                base_query += " AND name LIKE %s"
                params.append(f"%{filters.name}%")

            if filters.is_active is not None:
                base_query += " AND is_active = %s"
                params.append(filters.is_active)

            if filters.domain_id:
                base_query += " AND domain_id = %s"
                params.append(filters.domain_id)

            if filters.permission:
                base_query += " AND JSON_CONTAINS(permissions, %s)"
                params.append(f'"{filters.permission.value}"')

        count_query = f"SELECT COUNT(*) as total {base_query}"
        count_result = await self.fetch_one(count_query, params)
        total = count_result["total"] if count_result else 0

        offset = (page - 1) * limit
        data_query = f"""
        SELECT id, api_key_id, name, permissions, is_active, last_used_at,
               expires_at, created_at, updated_at, user_id, service_account_id, client_id, domain_id
        {base_query}
        ORDER BY created_at DESC
        LIMIT %s OFFSET %s
        """

        params.extend([limit, offset])
        results = await self.fetch_all(data_query, params)

        api_keys = []
        for result in results:
            permissions = self._parse_permissions(result["permissions"])

            api_keys.append(
                ApiKeyResponse(
                    id=result["id"],
                    api_key_id=result["api_key_id"],
                    name=result["name"],
                    permissions=permissions,
                    is_active=bool(result["is_active"]),
                    last_used_at=result["last_used_at"],
                    expires_at=result["expires_at"],
                    created_at=result["created_at"],
                    updated_at=result["updated_at"],
                    owner_id=result.get("user_id"),
                    service_account_id=result.get("service_account_id"),
                    client_id=result.get("client_id"),
                    domain_id=result.get("domain_id"),
                )
            )

        return ApiKeyListResponse(
            api_keys=api_keys, total=total, page=page, limit=limit
        )

    async def update_api_key(
        self, api_key_id: int, client_id: str, update_data: ApiKeyUpdate
    ) -> Optional[ApiKeyResponse]:
        """Updates an API Key (scope: client_id). See get_api_key_by_id for rationale."""

        update_fields = []
        params = [api_key_id, client_id]

        if update_data.name is not None:
            update_fields.append("name = %s")
            params.insert(-2, update_data.name)

        if update_data.permissions is not None:
            update_fields.append("permissions = %s")
            permissions_list = [perm.value for perm in update_data.permissions]
            params.insert(-2, self._format_permissions(permissions_list))

        if update_data.is_active is not None:
            update_fields.append("is_active = %s")
            params.insert(-2, update_data.is_active)

        if update_data.expires_at is not None:
            update_fields.append("expires_at = %s")
            params.insert(-2, update_data.expires_at)

        if update_data.domain_id is not None:
            update_fields.append("domain_id = %s")
            params.insert(-2, update_data.domain_id)

        if update_data.client_id is not None:
            update_fields.append("client_id = %s")
            params.insert(-2, update_data.client_id)

        if not update_fields:
            return await self.get_api_key_by_id(api_key_id, client_id)

        update_fields.append("updated_at = CURRENT_TIMESTAMP")

        query = f"""
        UPDATE api_keys
        SET {', '.join(update_fields)}
        WHERE id = %s AND client_id = %s
        """

        try:
            await self.execute(query, params)
            return await self.get_api_key_by_id(api_key_id, client_id)
        except Exception as e:
            logger.error(f"Error updating API key: {e}")
            raise Exception(f"Error updating API key: {str(e)}")

    async def delete_api_key(self, api_key_id: int, client_id: str) -> bool:
        """Deletes an API Key (scope: client_id). See get_api_key_by_id for rationale."""
        query = "DELETE FROM api_keys WHERE id = %s AND client_id = %s"

        try:
            result = await self.execute(query, [api_key_id, client_id])
            if hasattr(result, "rowcount"):
                return result.rowcount > 0
            return True
        except Exception as e:
            logger.error(f"Error deleting API key: {e}")
            raise Exception(f"Error deleting API key: {str(e)}")

    async def validate_api_key(self, api_key: str) -> Optional[Dict[str, Any]]:
        """
        Validates an API Key and returns the data of the user or service account.

        Uses a cache to avoid repeated database queries.

        Returns a dict with:
            - api_key_id, name, permissions
            - user_id/username/email/role OR service_account_id/service_account_name
            - client_id (the customer's identifier)
            - domain_id (DEPRECATED: use channels["email"]["config"]["domain_id"])
            - enabled_channels: list of enabled channels (["email"])
            - channels: dict with the config of each enabled channel
        """

        if not api_key.startswith(self.key_prefix):
            return None

        # Try the cache first
        cached_data = await get_cached_api_key(api_key)
        if cached_data is not None:
            logger.debug(
                f"API key found in the cache: {cached_data.get('api_key_id', 'unknown')}"
            )
            # Update last_used in the background (does not block)
            if cached_data.get("api_key_db_id"):
                asyncio.create_task(
                    self._safe_update_last_used(cached_data["api_key_db_id"])
                )
            return cached_data

        # Not in the cache: look it up in the database by the lookup hash
        lookup_hash = generate_lookup_hash(api_key)

        query = """
        SELECT ak.id, ak.api_key_id, ak.name, ak.key_hash, ak.permissions,
               ak.is_active, ak.expires_at, ak.user_id, ak.service_account_id,
               ak.domain_id, ak.client_id, ak.last_used_at, u.username, u.email, u.role,
               u.is_active as user_active, sa.name as service_account_name,
               sa.is_active as service_account_active
        FROM api_keys ak
        LEFT JOIN users u ON ak.user_id = u.user_id
        LEFT JOIN service_accounts sa ON ak.service_account_id = sa.service_account_id
        WHERE ak.is_active = TRUE
          AND ak.key_lookup_hash = %s
          AND (ak.expires_at IS NULL OR ak.expires_at > %s)
        """

        current_time = datetime.now(timezone.utc)
        result = await self.fetch_one(query, [lookup_hash, current_time])

        # Fallback for old keys without a lookup_hash (slow lookup)
        if not result:
            logger.debug("Lookup hash not found, falling back to slow verification")
            fallback_query = """
            SELECT ak.id, ak.api_key_id, ak.name, ak.key_hash, ak.permissions,
                   ak.is_active, ak.expires_at, ak.user_id, ak.service_account_id,
                   ak.domain_id, ak.client_id, ak.last_used_at, u.username, u.email, u.role,
                   u.is_active as user_active, sa.name as service_account_name,
                   sa.is_active as service_account_active
            FROM api_keys ak
            LEFT JOIN users u ON ak.user_id = u.user_id
            LEFT JOIN service_accounts sa ON ak.service_account_id = sa.service_account_id
            WHERE ak.is_active = TRUE
              AND ak.key_lookup_hash IS NULL
              AND (ak.expires_at IS NULL OR ak.expires_at > %s)
            """
            results = await self.fetch_all(fallback_query, [current_time])

            for row in results:
                if verify_api_key_hash(api_key, row["key_hash"]):
                    result = row
                    # Store the lookup_hash for future lookups
                    try:
                        await self.execute(
                            "UPDATE api_keys SET key_lookup_hash = %s WHERE id = %s",
                            [lookup_hash, row["id"]]
                        )
                        logger.info(f"Updated lookup_hash for api_key_id={row['api_key_id']}")
                    except Exception as e:
                        logger.warning(f"Failed to update lookup_hash: {e}")
                    break

        if not result:
            return None

        if result["user_id"]:
            if not result["user_active"]:
                return None
        elif result["service_account_id"]:
            if not result["service_account_active"]:
                return None
        else:
            return None

        await self._safe_update_last_used(result["id"])

        permissions = self._parse_permissions(result["permissions"])

        # Enabled channels
        api_key_db_id = result["id"]
        enabled_channels = await self.get_enabled_channel_names(api_key_db_id)

        # Configuration of each channel
        channels_config = {}
        for channel_name in enabled_channels:
            config = await self.get_channel_config(api_key_db_id, channel_name)
            channels_config[channel_name] = {"enabled": True, "config": config or {}}

        # Compatibility: with no email channel but a domain_id, use the legacy domain_id
        domain_id = result["domain_id"]
        if "email" in channels_config and channels_config["email"]["config"].get(
            "domain_id"
        ):
            domain_id = channels_config["email"]["config"]["domain_id"]

        base_data = {
            "api_key_id": result["api_key_id"],
            "api_key_db_id": api_key_db_id,  # Internal ID for channel operations
            "name": result["name"],
            "permissions": permissions,
            "client_id": result.get("client_id"),
            "domain_id": domain_id,  # Compatibility
            "last_used_at": result["last_used_at"],
            "enabled_channels": enabled_channels,
            "channels": channels_config,
        }

        if result["user_id"]:
            api_key_data = {
                **base_data,
                "user_id": result["user_id"],
                "username": result["username"],
                "email": result["email"],
                "role": result["role"],
                "is_service_account": False,
            }
        else:
            api_key_data = {
                **base_data,
                "service_account_id": result["service_account_id"],
                "service_account_name": result["service_account_name"],
                "is_service_account": True,
            }

        # Store in the cache
        await set_cached_api_key(api_key, api_key_data)
        logger.debug(
            f"API key stored in the cache: {api_key_data.get('api_key_id', 'unknown')}"
        )

        return api_key_data

    async def _update_last_used(self, api_key_id: int):
        """Updates the last-used timestamp"""
        query = """
        UPDATE api_keys
        SET last_used_at = CURRENT_TIMESTAMP
        WHERE id = %s
        """
        await self.execute(query, [api_key_id])

    async def _safe_update_last_used(self, api_key_id: int) -> None:
        # last_used_at is best-effort telemetry; a DB outage here must not
        # take down the auth path nor leak "Task exception was never retrieved"
        # from the fire-and-forget cache-hit branch.
        try:
            await self._update_last_used(api_key_id)
        except Exception as e:
            logger.warning(
                "best-effort _update_last_used failed (api_key_id=%s): %s",
                api_key_id,
                e,
            )

    async def get_usage_stats(self, client_id: str) -> List[ApiKeyUsageStats]:
        """Returns the API Keys' usage statistics (scope: client_id). See get_api_key_by_id for rationale."""
        query = """
        SELECT ak.api_key_id, ak.name, ak.is_active, ak.last_used_at, ak.created_at,
               COUNT(akl.id) as total_requests
        FROM api_keys ak
        LEFT JOIN api_key_logs akl ON ak.id = akl.api_key_id
        WHERE ak.client_id = %s
        GROUP BY ak.id, ak.api_key_id, ak.name, ak.is_active, ak.last_used_at, ak.created_at
        ORDER BY total_requests DESC
        """

        results = await self.fetch_all(query, [client_id])

        stats = []
        for result in results:
            stats.append(
                ApiKeyUsageStats(
                    api_key_id=result["api_key_id"],
                    name=result["name"],
                    total_requests=result["total_requests"] or 0,
                    last_used_at=result["last_used_at"],
                    created_at=result["created_at"],
                    is_active=bool(result["is_active"]),
                )
            )

        return stats

    # =====================================================
    # CHANNEL MANAGEMENT METHODS
    # =====================================================

    async def get_channels_for_api_key(
        self, api_key_db_id: int
    ) -> List[ChannelResponse]:
        """
        Gets every channel enabled for an API Key.

        Args:
            api_key_db_id: ID of the API Key row (api_keys.id)

        Returns:
            List of ChannelResponse with the enabled channels
        """
        query = """
        SELECT akc.id, akc.is_active, akc.config, akc.created_at, akc.updated_at,
               c.name as channel_name, c.display_name
        FROM api_key_channels akc
        JOIN channels c ON akc.channel_id = c.id
        WHERE akc.api_key_id = %s
        ORDER BY c.name
        """

        results = await self.fetch_all(query, [api_key_db_id])

        channels = []
        for result in results:
            config = result.get("config")
            if isinstance(config, str):
                try:
                    config = json.loads(config)
                except (json.JSONDecodeError, ValueError):
                    config = None

            channels.append(
                ChannelResponse(
                    id=result["id"],
                    channel=result["channel_name"],
                    display_name=result["display_name"],
                    is_active=bool(result["is_active"]),
                    config=config,
                    created_at=result.get("created_at"),
                    updated_at=result.get("updated_at"),
                )
            )

        return channels

    async def get_enabled_channel_names(self, api_key_db_id: int) -> List[str]:
        """
        Returns the names of an API Key's active channels.

        Args:
            api_key_db_id: ID of the API Key row

        Returns:
            Names of the enabled channels (e.g. ["email"])
        """
        query = """
        SELECT c.name
        FROM api_key_channels akc
        JOIN channels c ON akc.channel_id = c.id
        WHERE akc.api_key_id = %s AND akc.is_active = TRUE
        ORDER BY c.name
        """

        results = await self.fetch_all(query, [api_key_db_id])
        return [r["name"] for r in results]

    async def add_channel_to_api_key(
        self, api_key_db_id: int, channel_data: ChannelCreate
    ) -> ChannelResponse:
        """
        Adds a channel to an API Key.

        Args:
            api_key_db_id: ID of the API Key row
            channel_data: Data of the channel to add

        Returns:
            ChannelResponse with the added channel
        """
        # Look up the channel's ID
        channel_query = "SELECT id, display_name FROM channels WHERE name = %s"
        channel_result = await self.fetch_one(
            channel_query, [channel_data.channel.value]
        )

        if not channel_result:
            raise ValueError(f"Channel not found: {channel_data.channel.value}")

        channel_id = channel_result["id"]
        display_name = channel_result["display_name"]

        # Check whether it already exists
        existing_query = """
        SELECT id FROM api_key_channels
        WHERE api_key_id = %s AND channel_id = %s
        """
        existing = await self.fetch_one(existing_query, [api_key_db_id, channel_id])

        if existing:
            raise ValueError(
                f"Channel {channel_data.channel.value} is already linked to this API key"
            )

        # Insert the channel
        config_json = json.dumps(channel_data.config) if channel_data.config else None

        insert_query = """
        INSERT INTO api_key_channels (api_key_id, channel_id, is_active, config)
        VALUES (%s, %s, %s, %s)
        """

        result = await self.execute(
            insert_query,
            [api_key_db_id, channel_id, channel_data.is_active, config_json],
        )

        logger.info(
            f"Channel {channel_data.channel.value} added to API key {api_key_db_id}"
        )

        return ChannelResponse(
            id=result if isinstance(result, int) else 0,
            channel=channel_data.channel.value,
            display_name=display_name,
            is_active=channel_data.is_active,
            config=channel_data.config,
            created_at=datetime.now(timezone.utc),
            updated_at=None,
        )

    async def add_channels_to_api_key(
        self, api_key_db_id: int, channels: List[ChannelCreate]
    ) -> ChannelBulkResponse:
        """
        Adds several channels to an API Key.

        Args:
            api_key_db_id: ID of the API Key row
            channels: List of channels to add

        Returns:
            ChannelBulkResponse with the result of each operation
        """
        results: List[ChannelBulkResult] = []
        success_count = 0
        error_count = 0

        for channel_data in channels:
            try:
                channel_response = await self.add_channel_to_api_key(
                    api_key_db_id, channel_data
                )
                results.append(
                    ChannelBulkResult(
                        channel=channel_data.channel.value,
                        success=True,
                        error=None,
                        data=channel_response,
                    )
                )
                success_count += 1
            except ValueError as e:
                results.append(
                    ChannelBulkResult(
                        channel=channel_data.channel.value,
                        success=False,
                        error=str(e),
                        data=None,
                    )
                )
                error_count += 1
            except Exception as e:
                logger.error(
                    f"Error adding channel {channel_data.channel.value}: {e}"
                )
                results.append(
                    ChannelBulkResult(
                        channel=channel_data.channel.value,
                        success=False,
                        error=f"Internal error: {str(e)}",
                        data=None,
                    )
                )
                error_count += 1

        return ChannelBulkResponse(
            total=len(channels),
            success_count=success_count,
            error_count=error_count,
            results=results,
        )

    async def update_api_key_channel(
        self, api_key_db_id: int, channel_name: str, update_data: ChannelUpdate
    ) -> Optional[ChannelResponse]:
        """
        Updates the configuration of an API Key's channel.

        Args:
            api_key_db_id: ID of the API Key row
            channel_name: Channel name (email)
            update_data: Data to update

        Returns:
            The updated ChannelResponse, or None if not found
        """
        # Current channel
        query = """
        SELECT akc.id, akc.is_active, akc.config, akc.created_at,
               c.name as channel_name, c.display_name, c.id as channel_id
        FROM api_key_channels akc
        JOIN channels c ON akc.channel_id = c.id
        WHERE akc.api_key_id = %s AND c.name = %s
        """

        result = await self.fetch_one(query, [api_key_db_id, channel_name])

        if not result:
            return None

        # Prepare the update
        update_fields = []
        params = []

        if update_data.is_active is not None:
            update_fields.append("is_active = %s")
            params.append(update_data.is_active)

        if update_data.config is not None:
            update_fields.append("config = %s")
            params.append(json.dumps(update_data.config))

        if not update_fields:
            # Nothing to update, return the current state
            config = result.get("config")
            if isinstance(config, str):
                try:
                    config = json.loads(config)
                except (json.JSONDecodeError, ValueError):
                    config = None

            return ChannelResponse(
                id=result["id"],
                channel=result["channel_name"],
                display_name=result["display_name"],
                is_active=bool(result["is_active"]),
                config=config,
                created_at=result.get("created_at"),
                updated_at=None,
            )

        update_fields.append("updated_at = CURRENT_TIMESTAMP")
        params.extend([api_key_db_id, channel_name])

        update_query = f"""
        UPDATE api_key_channels akc
        JOIN channels c ON akc.channel_id = c.id
        SET {', '.join(update_fields)}
        WHERE akc.api_key_id = %s AND c.name = %s
        """

        await self.execute(update_query, params)

        # Read the updated state
        updated = await self.fetch_one(query, [api_key_db_id, channel_name])

        config = updated.get("config")
        if isinstance(config, str):
            try:
                config = json.loads(config)
            except (json.JSONDecodeError, ValueError):
                config = None

        return ChannelResponse(
            id=updated["id"],
            channel=updated["channel_name"],
            display_name=updated["display_name"],
            is_active=bool(updated["is_active"]),
            config=config,
            created_at=updated.get("created_at"),
            updated_at=datetime.now(timezone.utc),
        )

    async def remove_channel_from_api_key(
        self, api_key_db_id: int, channel_name: str
    ) -> bool:
        """
        Removes a channel from an API Key.

        Args:
            api_key_db_id: ID of the API Key row
            channel_name: Name of the channel to remove

        Returns:
            True if removed, False if not found
        """
        query = """
        DELETE akc FROM api_key_channels akc
        JOIN channels c ON akc.channel_id = c.id
        WHERE akc.api_key_id = %s AND c.name = %s
        """

        try:
            result = await self.execute(query, [api_key_db_id, channel_name])
            if hasattr(result, "rowcount"):
                return result.rowcount > 0
            return True
        except Exception as e:
            logger.error(f"Error removing channel: {e}")
            return False

    async def get_channel_config(
        self, api_key_db_id: int, channel_name: str
    ) -> Optional[Dict[str, Any]]:
        """
        Gets the configuration of a specific channel of an API Key.

        Args:
            api_key_db_id: ID of the API Key row
            channel_name: Channel name

        Returns:
            Dict with the configuration, or None if the channel is not enabled
        """
        query = """
        SELECT akc.config, akc.is_active
        FROM api_key_channels akc
        JOIN channels c ON akc.channel_id = c.id
        WHERE akc.api_key_id = %s AND c.name = %s AND akc.is_active = TRUE
        """

        result = await self.fetch_one(query, [api_key_db_id, channel_name])

        if not result:
            return None

        config = result.get("config")
        if isinstance(config, str):
            try:
                return json.loads(config)
            except (json.JSONDecodeError, ValueError):
                return {}

        return config if isinstance(config, dict) else {}

    # =====================================================
    # CREATION WITH CHANNELS
    # =====================================================

    async def create_api_key_with_channels(
        self,
        api_key_data: ApiKeyCreate,
        user_id: str,
        client_id: str,
        channels: Optional[List[ChannelCreate]] = None,
    ) -> ApiKeyCreateResponse:
        """
        Creates a new API Key with its channels configured.

        Args:
            api_key_data: API Key data
            user_id: ID of the owning user
            client_id: Customer ID
            channels: List of channels to enable (optional)

        Returns:
            ApiKeyCreateResponse with the created API Key and its channels
        """
        logger.debug(f"Creating API key: user_id={user_id}, client_id={client_id}")

        api_key = self._generate_api_key()
        api_key_id = self._generate_api_key_id()
        key_hash = self._hash_api_key(api_key)
        key_lookup_hash = generate_lookup_hash(api_key)

        permissions_list = [perm.value for perm in api_key_data.permissions]

        # Take the domain_id from the email channel if present (compatibility)
        domain_id = None
        if channels:
            for ch in channels:
                if ch.channel == ChannelType.EMAIL and ch.config:
                    domain_id = ch.config.get("domain_id")
                    break

        query = """
        INSERT INTO api_keys (
            api_key_id, name, key_hash, key_lookup_hash, user_id, service_account_id,
            client_id, domain_id, permissions, expires_at, is_active
        ) VALUES (
            %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s
        )
        """

        params = [
            api_key_id,
            api_key_data.name,
            key_hash,
            key_lookup_hash,
            user_id,
            None,
            client_id,
            domain_id,
            self._format_permissions(permissions_list),
            api_key_data.expires_at,
            True,
        ]

        try:
            result = await self.execute(query, params)
            api_key_db_id = result

            if not api_key_db_id or api_key_db_id == 0:
                # Fallback: look it up by api_key_id
                fallback = await self.fetch_one(
                    "SELECT id FROM api_keys WHERE api_key_id = %s", [api_key_id]
                )
                if fallback:
                    api_key_db_id = fallback["id"]
                else:
                    raise Exception("Error inserting API key: invalid ID returned")

            # Add the channels
            channel_responses = []
            if channels:
                for ch in channels:
                    try:
                        ch_response = await self.add_channel_to_api_key(
                            api_key_db_id, ch
                        )
                        channel_responses.append(ch_response)
                    except Exception as e:
                        logger.warning(f"Error adding channel {ch.channel}: {e}")

            # Read the created API Key
            created = await self.fetch_one(
                """SELECT id, api_key_id, name, permissions, is_active, expires_at,
                          created_at, user_id, client_id, domain_id
                   FROM api_keys WHERE id = %s""",
                [api_key_db_id],
            )

            if not created:
                raise Exception("API key not found after insertion")

            enabled_channels = [ch.channel for ch in channel_responses if ch.is_active]

            return ApiKeyCreateResponse(
                id=created["id"],
                api_key_id=created["api_key_id"],
                name=created["name"],
                api_key=api_key,
                permissions=self._parse_permissions(created["permissions"]),
                is_active=bool(created["is_active"]),
                expires_at=created["expires_at"],
                created_at=created["created_at"],
                owner_id=created["user_id"],
                service_account_id=None,
                client_id=created["client_id"],
                domain_id=created["domain_id"],
                channels=channel_responses if channel_responses else None,
                enabled_channels=enabled_channels if enabled_channels else None,
            )

        except Exception as e:
            logger.error(f"Error creating API key: {e}")
            raise Exception(f"Error creating API key: {str(e)}")
