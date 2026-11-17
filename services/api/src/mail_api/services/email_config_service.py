"""
Per customer/domain email configuration service

Service that manages email configurations, including:
- SMTP settings per domain
- Tracking settings
- SMTP credentials per sender email
- Change history
- Redis cache for fast access by the milter
"""

import json
import logging
import uuid
from datetime import datetime
from typing import Any

from mysql.connector.errors import IntegrityError

from mail_api.common.config import EMAIL_CONFIG_CACHE_TTL_SEC
from mail_api.common.db import (
    exec_sql_async,
    fetch_all_async,
    fetch_one_async,
)
from mail_api.common.redis_client import redis_client, safe_setex
from mail_api.models.email_config_models import (
    EmailConfigCreate,
    EmailConfigFilter,
    EmailConfigResponse,
    EmailConfigUpdate,
)


class EmailConfigService:
    """Email configuration management service"""

    # Prefixes of the configuration Redis keys
    REDIS_CONFIG_PREFIX = "email_config"
    REDIS_DOMAIN_PREFIX = "email_config_domain"

    def __init__(self):
        self.logger = logging.getLogger(__name__)

    def _get_redis_key_for_config(self, config_id: str) -> str:
        """Builds the Redis key of a configuration"""
        return f"{self.REDIS_CONFIG_PREFIX}:{config_id}"

    def _get_redis_key_for_domain(self, domain_id: int) -> str:
        """Builds the Redis key of a domain's default configuration"""
        return f"{self.REDIS_DOMAIN_PREFIX}:{domain_id}"

    async def _cache_config_in_redis(self, config: EmailConfigResponse):
        """Caches the configuration in Redis with a TTL (EMAIL_CONFIG_CACHE_TTL_SEC).

        Without a TTL, a direct UPDATE in the database that bypasses this service
        would leave the cache stale forever (the worker would keep sending to the
        old smtp_host)."""
        try:
            if not redis_client:
                return

            config_key = self._get_redis_key_for_config(config.config_id)
            await safe_setex(
                config_key,
                config.dict(),
                ttl=EMAIL_CONFIG_CACHE_TTL_SEC,
                redis_instance=redis_client
            )

            # Also cache the domain's default configuration when this is the default
            if config.is_default and config.is_active:
                domain_key = self._get_redis_key_for_domain(config.domain_id)
                await safe_setex(
                    domain_key,
                    config.dict(),
                    ttl=EMAIL_CONFIG_CACHE_TTL_SEC,
                    redis_instance=redis_client
                )
                self.logger.debug(f"Default configuration cached in Redis: domain_id={config.domain_id}")

            self.logger.debug(f"Configuration cached in Redis: config_id={config.config_id}")
        except Exception as e:
            self.logger.warning(f"Error caching the configuration in Redis: {e}")

    async def _invalidate_domain_cache(self, domain_id: int):
        """Invalidates the domain's Redis cache"""
        try:
            if not redis_client:
                return

            domain_key = self._get_redis_key_for_domain(domain_id)
            await redis_client.delete(domain_key)
            self.logger.debug(f"Domain cache invalidated: domain_id={domain_id}")
        except Exception as e:
            self.logger.warning(f"Error invalidating the domain cache: {e}")

    async def _invalidate_config_cache(self, config_id: str):
        """Invalidates the Redis cache of a configuration"""
        try:
            if not redis_client:
                return

            config_key = self._get_redis_key_for_config(config_id)
            await redis_client.delete(config_key)
            self.logger.debug(f"Configuration cache invalidated: config_id={config_id}")
        except Exception as e:
            self.logger.warning(f"Error invalidating the configuration cache: {e}")

    async def _get_config_from_redis(self, config_id: str) -> EmailConfigResponse | None:
        """Fetches a configuration from Redis"""
        try:
            if not redis_client:
                return None

            config_key = self._get_redis_key_for_config(config_id)
            cached_data = await redis_client.get(config_key)

            if cached_data:
                config_dict = json.loads(cached_data)
                return EmailConfigResponse(**config_dict)

            return None
        except Exception as e:
            self.logger.debug(f"Error reading the configuration from Redis: {e}")
            return None

    async def _get_domain_config_from_redis(self, domain_id: int) -> EmailConfigResponse | None:
        """Fetches the domain's default configuration from Redis"""
        try:
            if not redis_client:
                return None

            domain_key = self._get_redis_key_for_domain(domain_id)
            cached_data = await redis_client.get(domain_key)

            if cached_data:
                config_dict = json.loads(cached_data)
                return EmailConfigResponse(**config_dict)

            return None
        except Exception as e:
            self.logger.debug(f"Error reading the domain configuration from Redis: {e}")
            return None

    async def create_config(
        self, config_data: EmailConfigCreate, domain_id: int, created_by: str | None = None
    ) -> EmailConfigResponse:
        """Creates a new email configuration"""
        try:
            # domain_id is required
            if domain_id is None:
                raise ValueError("domain_id is required")

            # Does the domain exist?
            domain = await fetch_one_async(
                "SELECT id FROM virtual_domains WHERE id = %s",
                (domain_id,)
            )
            if not domain:
                raise ValueError(f"Domain ID {domain_id} not found")

            # If this is the default, unset the other defaults of the same domain
            # IMPORTANT: unset ALL the other default configurations (active and inactive)
            # to avoid violating the unique_domain_default constraint
            if config_data.is_default:
                await exec_sql_async(
                    "UPDATE email_configs SET is_default = FALSE WHERE domain_id = %s AND is_default = TRUE",
                    (domain_id,)
                )

                # Check again whether an active default configuration still exists
                # (there may be a race condition)
                existing_default = await fetch_one_async(
                    "SELECT id FROM email_configs WHERE domain_id = %s AND is_default = TRUE AND is_active = TRUE",
                    (domain_id,)
                )
                if existing_default:
                    # Still there: try to unset it again (possible race condition)
                    await exec_sql_async(
                        "UPDATE email_configs SET is_default = FALSE WHERE domain_id = %s AND is_default = TRUE AND is_active = TRUE",
                        (domain_id,)
                    )

            # Is there already an active default configuration (only when is_default = TRUE)?
            # The unique_domain_default constraint prevents several active defaults
            if config_data.is_default:
                existing_default_active = await fetch_one_async(
                    "SELECT id FROM email_configs WHERE domain_id = %s AND is_default = TRUE AND is_active = TRUE",
                    (domain_id,)
                )
                if existing_default_active:
                    # An active default still exists after the UPDATE: raise
                    raise ValueError(
                        f"An active default configuration already exists for domain_id={domain_id}. "
                        f"Deactivate the existing configuration before creating a new one."
                    )

            # Generate a unique config_id
            config_id = f"ec_{uuid.uuid4().hex[:16]}"

            # Prepare the data to insert
            additional_config_json = json.dumps(config_data.additional_config) if config_data.additional_config else None

            # Insert the configuration
            # is_active is always TRUE on creation (default value)
            insert_query = """
                INSERT INTO email_configs (
                    config_id, domain_id, name,
                    smtp_host, smtp_port, smtp_secure, smtp_user, smtp_password,
                    smtp_base_port, smtp_timeout, envelope_prefix,
                    tracking_enabled, inject_pixel, rewrite_links, pixel_tracking_url, click_tracking_url, tracking_domain,
                    default_from_name, default_from_email, default_reply_to,
                    bounce_email, complaint_email, shadow_email,
                    rate_limit_per_minute, rate_limit_per_hour, rate_limit_per_day,
                    max_retries, retry_delay_seconds,
                    default_webhook_url, default_webhook_secret,
                    additional_config, is_active, is_default, priority, notes, created_by
                ) VALUES (
                    %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, %s,
                    %s, %s,
                    %s, %s, %s, %s, %s, %s
                )
            """

            await exec_sql_async(
                insert_query,
                (
                    config_id, domain_id, config_data.name,
                    config_data.smtp_host, config_data.smtp_port, config_data.smtp_secure,
                    config_data.smtp_user, config_data.smtp_password,
                    config_data.smtp_base_port, config_data.smtp_timeout, config_data.envelope_prefix,
                    config_data.tracking_enabled, config_data.inject_pixel, config_data.rewrite_links,
                    config_data.pixel_tracking_url, config_data.click_tracking_url, config_data.tracking_domain,
                    config_data.default_from_name, config_data.default_from_email,
                    config_data.default_reply_to,
                    config_data.bounce_email, config_data.complaint_email,
                    config_data.shadow_email,
                    config_data.rate_limit_per_minute, config_data.rate_limit_per_hour,
                    config_data.rate_limit_per_day,
                    config_data.max_retries, config_data.retry_delay_seconds,
                    config_data.default_webhook_url, config_data.default_webhook_secret,
                    additional_config_json, True,  # is_active is always TRUE on creation
                    config_data.is_default, config_data.priority,
                    config_data.notes, created_by
                )
            )

            # Fetch the created configuration
            config = await self._get_config_by_config_id(config_id)
            if not config:
                raise Exception("Error creating the email configuration")

            # Record it in the history
            await self._log_config_history(
                config.id, 'created', created_by, None, config_data.dict()
            )

            # Invalidate the domain cache if this is the default (before caching)
            if config.is_default:
                await self._invalidate_domain_cache(config.domain_id)

            # Cache it in Redis
            await self._cache_config_in_redis(config)

            self.logger.info(f"Email configuration created: {config.name} (ID: {config_id})")
            return config

        except IntegrityError as e:
            # Handle duplicate key errors (unique constraint violations)
            error_msg = str(e)
            if 'unique_domain_default' in error_msg or 'idx_unique_active_default' in error_msg:
                self.logger.warning(f"Attempt to create a duplicate config for domain_id={domain_id}: {e}")
                raise ValueError(
                    f"An active default configuration already exists for domain_id={domain_id}. "
                    f"Deactivate the existing configuration before creating a new one."
                ) from e
            self.logger.error(f"Integrity error creating the configuration: {e}", exc_info=True)
            raise ValueError(f"Database integrity error: {e}") from e

        except Exception as e:
            self.logger.error(f"Error creating the email configuration: {e}", exc_info=True)
            raise

    async def get_config(self, config_id: str) -> EmailConfigResponse | None:
        """Fetches a configuration by config_id (Redis first)"""
        # Try Redis first
        cached_config = await self._get_config_from_redis(config_id)
        if cached_config:
            return cached_config

        # Not in Redis: read the database
        config = await self._get_config_by_config_id(config_id)
        if config:
            # Cache it in Redis for the next lookups
            await self._cache_config_in_redis(config)
        return config

    async def get_default_config_for_domain(
        self, domain_id: int, use_cache: bool = True
    ) -> EmailConfigResponse | None:
        """Fetches the default configuration of a domain (Redis first)"""
        # Try Redis first when the cache is enabled
        if use_cache:
            cached_config = await self._get_domain_config_from_redis(domain_id)
            if cached_config:
                return cached_config

        # Not in Redis: read the database
        try:
            query = """
                SELECT id, config_id, domain_id, name,
                    smtp_host, smtp_port, smtp_secure, smtp_user, smtp_password, smtp_base_port, smtp_timeout,
                    tracking_enabled, inject_pixel, rewrite_links, pixel_tracking_url, click_tracking_url, tracking_domain,
                    default_from_name, default_from_email, default_reply_to,
                    bounce_email, complaint_email, shadow_email,
                    rate_limit_per_minute, rate_limit_per_hour, rate_limit_per_day,
                    max_retries, retry_delay_seconds,
                    default_webhook_url, default_webhook_secret,
                    additional_config, is_active, is_default, priority, notes,
                    created_by, created_at, updated_at
                FROM email_configs
                WHERE domain_id = %s AND is_default = TRUE AND is_active = TRUE
                ORDER BY priority DESC, created_at DESC
                LIMIT 1
            """
            result = await fetch_one_async(query, (domain_id,))
            if not result:
                return None

            config = self._row_to_config_response(result)

            # Cache it in Redis for the next lookups
            if use_cache and config:
                await self._cache_config_in_redis(config)

            return config
        except Exception as e:
            self.logger.error(f"Error fetching the default configuration: {e}", exc_info=True)
            return None

    async def list_configs(
        self, filters: EmailConfigFilter | None = None, domain_id: int | None = None
    ) -> list[EmailConfigResponse]:
        """Lists configurations with optional filters"""
        try:
            query = """
                SELECT id, config_id, domain_id, name,
                    smtp_host, smtp_port, smtp_secure, smtp_user, smtp_base_port, smtp_timeout,
                    tracking_enabled, inject_pixel, rewrite_links, pixel_tracking_url, click_tracking_url, tracking_domain,
                    default_from_name, default_from_email, default_reply_to,
                    bounce_email, complaint_email, shadow_email,
                    rate_limit_per_minute, rate_limit_per_hour, rate_limit_per_day,
                    max_retries, retry_delay_seconds,
                    default_webhook_url, default_webhook_secret,
                    additional_config, is_active, is_default, priority, notes,
                    created_by, created_at, updated_at
                FROM email_configs
                WHERE 1=1
            """
            params = []

            # Filter by domain_id when given
            if domain_id:
                query += " AND domain_id = %s"
                params.append(domain_id)
            elif filters and filters.domain_id:
                query += " AND domain_id = %s"
                params.append(filters.domain_id)

            # Apply the filters
            if filters:
                if filters.is_active is not None:
                    query += " AND is_active = %s"
                    params.append(filters.is_active)
                if filters.is_default is not None:
                    query += " AND is_default = %s"
                    params.append(filters.is_default)
                if filters.smtp_host:
                    query += " AND smtp_host LIKE %s"
                    params.append(f"%{filters.smtp_host}%")
                if filters.tracking_enabled is not None:
                    query += " AND tracking_enabled = %s"
                    params.append(filters.tracking_enabled)

            query += " ORDER BY priority DESC, created_at DESC"

            results = await fetch_all_async(query, tuple(params))
            return [self._row_to_config_response(row) for row in results]

        except Exception as e:
            self.logger.error(f"Error listing configurations: {e}", exc_info=True)
            return []

    async def update_config(
        self, config_id: str, config_data: EmailConfigUpdate, updated_by: str | None = None
    ) -> EmailConfigResponse | None:
        """Updates an email configuration"""
        try:
            # Fetch the current configuration
            current_config = await self._get_config_by_config_id(config_id)
            if not current_config:
                raise ValueError(f"Configuration {config_id} not found")

            # Prepare the update data
            update_fields = []
            params = []

            # Build the query dynamically
            if config_data.name is not None:
                update_fields.append("name = %s")
                params.append(config_data.name)
            if config_data.smtp_host is not None:
                update_fields.append("smtp_host = %s")
                params.append(config_data.smtp_host)
            if config_data.smtp_port is not None:
                update_fields.append("smtp_port = %s")
                params.append(config_data.smtp_port)
            if config_data.smtp_secure is not None:
                update_fields.append("smtp_secure = %s")
                params.append(config_data.smtp_secure)
            if config_data.smtp_user is not None:
                update_fields.append("smtp_user = %s")
                params.append(config_data.smtp_user)
            if config_data.smtp_password is not None:
                update_fields.append("smtp_password = %s")
                params.append(config_data.smtp_password)
            if config_data.smtp_base_port is not None:
                update_fields.append("smtp_base_port = %s")
                params.append(config_data.smtp_base_port)
            if config_data.smtp_timeout is not None:
                update_fields.append("smtp_timeout = %s")
                params.append(config_data.smtp_timeout)
            if config_data.envelope_prefix is not None:
                update_fields.append("envelope_prefix = %s")
                params.append(config_data.envelope_prefix)
            if config_data.tracking_enabled is not None:
                update_fields.append("tracking_enabled = %s")
                params.append(config_data.tracking_enabled)
            if config_data.inject_pixel is not None:
                update_fields.append("inject_pixel = %s")
                params.append(config_data.inject_pixel)
            if config_data.rewrite_links is not None:
                update_fields.append("rewrite_links = %s")
                params.append(config_data.rewrite_links)
            if config_data.pixel_tracking_url is not None:
                update_fields.append("pixel_tracking_url = %s")
                params.append(config_data.pixel_tracking_url)
            if config_data.click_tracking_url is not None:
                update_fields.append("click_tracking_url = %s")
                params.append(config_data.click_tracking_url)
            if config_data.tracking_domain is not None:
                update_fields.append("tracking_domain = %s")
                params.append(config_data.tracking_domain)
            if config_data.default_from_name is not None:
                update_fields.append("default_from_name = %s")
                params.append(config_data.default_from_name)
            if config_data.default_from_email is not None:
                update_fields.append("default_from_email = %s")
                params.append(config_data.default_from_email)
            if config_data.default_reply_to is not None:
                update_fields.append("default_reply_to = %s")
                params.append(config_data.default_reply_to)
            if config_data.bounce_email is not None:
                update_fields.append("bounce_email = %s")
                params.append(config_data.bounce_email)
            if config_data.complaint_email is not None:
                update_fields.append("complaint_email = %s")
                params.append(config_data.complaint_email)
            if config_data.shadow_email is not None:
                update_fields.append("shadow_email = %s")
                params.append(config_data.shadow_email)
            if config_data.rate_limit_per_minute is not None:
                update_fields.append("rate_limit_per_minute = %s")
                params.append(config_data.rate_limit_per_minute)
            if config_data.rate_limit_per_hour is not None:
                update_fields.append("rate_limit_per_hour = %s")
                params.append(config_data.rate_limit_per_hour)
            if config_data.rate_limit_per_day is not None:
                update_fields.append("rate_limit_per_day = %s")
                params.append(config_data.rate_limit_per_day)
            if config_data.max_retries is not None:
                update_fields.append("max_retries = %s")
                params.append(config_data.max_retries)
            if config_data.retry_delay_seconds is not None:
                update_fields.append("retry_delay_seconds = %s")
                params.append(config_data.retry_delay_seconds)
            if config_data.default_webhook_url is not None:
                update_fields.append("default_webhook_url = %s")
                params.append(config_data.default_webhook_url)
            if config_data.default_webhook_secret is not None:
                update_fields.append("default_webhook_secret = %s")
                params.append(config_data.default_webhook_secret)
            if config_data.additional_config is not None:
                update_fields.append("additional_config = %s")
                params.append(json.dumps(config_data.additional_config))
            if config_data.is_active is not None:
                update_fields.append("is_active = %s")
                params.append(config_data.is_active)
            if config_data.is_default is not None:
                update_fields.append("is_default = %s")
                params.append(config_data.is_default)
                # Becoming the default: unset the other defaults of the same domain
                if config_data.is_default:
                    await exec_sql_async(
                        "UPDATE email_configs SET is_default = FALSE WHERE domain_id = %s AND id != %s AND is_default = TRUE",
                        (current_config.domain_id, current_config.id)
                    )
            if config_data.priority is not None:
                update_fields.append("priority = %s")
                params.append(config_data.priority)
            if config_data.notes is not None:
                update_fields.append("notes = %s")
                params.append(config_data.notes)

            if not update_fields:
                # Nothing to update
                return current_config

            # config_id for the WHERE clause
            params.append(config_id)

            # Run the update
            update_query = f"""
                UPDATE email_configs
                SET {', '.join(update_fields)}, updated_at = CURRENT_TIMESTAMP
                WHERE config_id = %s
            """
            await exec_sql_async(update_query, tuple(params))

            # Fetch the updated configuration
            updated_config = await self._get_config_by_config_id(config_id)
            if not updated_config:
                raise Exception("Error updating the configuration")

            # Record it in the history
            await self._log_config_history(
                updated_config.id, 'updated', updated_by,
                current_config.dict(), config_data.dict(exclude_unset=True)
            )

            # Invalidate and re-cache in Redis
            await self._invalidate_config_cache(config_id)

            # If is_default or the domain changed, invalidate the domain cache before re-caching
            if config_data.is_default is not None or current_config.domain_id != updated_config.domain_id:
                await self._invalidate_domain_cache(current_config.domain_id)
                if updated_config.domain_id != current_config.domain_id:
                    await self._invalidate_domain_cache(updated_config.domain_id)

            # Cache the configuration (also caches the domain default when is_default=True)
            await self._cache_config_in_redis(updated_config)

            self.logger.info(f"Configuration updated: {config_id}")
            return updated_config

        except Exception as e:
            self.logger.error(f"Error updating the configuration: {e}", exc_info=True)
            raise

    async def delete_config(self, config_id: str, deleted_by: str | None = None) -> bool:
        """Deletes an email configuration"""
        try:
            config = await self._get_config_by_config_id(config_id)
            if not config:
                return False

            # Record it in the history before deleting
            await self._log_config_history(
                config.id, 'deleted', deleted_by, config.dict(), None
            )

            # Invalidate the cache before deleting
            await self._invalidate_config_cache(config_id)
            await self._invalidate_domain_cache(config.domain_id)

            # Delete the configuration (the cascade deletes the related credentials)
            await exec_sql_async(
                "DELETE FROM email_configs WHERE config_id = %s",
                (config_id,)
            )

            self.logger.info(f"Configuration deleted: {config_id}")
            return True

        except Exception as e:
            self.logger.error(f"Error deleting the configuration: {e}", exc_info=True)
            raise

    # Private helper methods
    async def _get_config_by_config_id(self, config_id: str) -> EmailConfigResponse | None:
        """Fetches a configuration by config_id"""
        try:
            query = """
                SELECT id, config_id, domain_id, name,
                    smtp_host, smtp_port, smtp_secure, smtp_user, smtp_password, smtp_base_port, smtp_timeout,
                    tracking_enabled, inject_pixel, rewrite_links, pixel_tracking_url, click_tracking_url, tracking_domain,
                    default_from_name, default_from_email, default_reply_to,
                    bounce_email, complaint_email, shadow_email,
                    rate_limit_per_minute, rate_limit_per_hour, rate_limit_per_day,
                    max_retries, retry_delay_seconds,
                    default_webhook_url, default_webhook_secret,
                    additional_config, is_active, is_default, priority, notes,
                    created_by, created_at, updated_at
                FROM email_configs
                WHERE config_id = %s
            """
            result = await fetch_one_async(query, (config_id,))
            if not result:
                return None
            return self._row_to_config_response(result)
        except Exception as e:
            self.logger.error(f"Error fetching the configuration: {e}", exc_info=True)
            return None

    def _row_to_config_response(self, row: dict[str, Any]) -> EmailConfigResponse:
        """Converts a database row to EmailConfigResponse"""
        additional_config = None
        if row.get('additional_config'):
            try:
                additional_config = json.loads(row['additional_config']) if isinstance(row['additional_config'], str) else row['additional_config']
            except:
                additional_config = None

        return EmailConfigResponse(
            id=row['id'],
            config_id=row['config_id'],
            domain_id=row['domain_id'],
            name=row['name'],
            smtp_host=row['smtp_host'],
            smtp_port=row['smtp_port'],
            smtp_secure=bool(row['smtp_secure']),
            smtp_user=row.get('smtp_user'),
            smtp_base_port=row['smtp_base_port'],
            smtp_timeout=row['smtp_timeout'],
            tracking_enabled=bool(row['tracking_enabled']),
            inject_pixel=bool(row.get('inject_pixel', True)),
            rewrite_links=bool(row.get('rewrite_links', True)),
            pixel_tracking_url=row.get('pixel_tracking_url'),
            click_tracking_url=row.get('click_tracking_url'),
            tracking_domain=row.get('tracking_domain'),
            default_from_name=row.get('default_from_name'),
            default_from_email=row.get('default_from_email'),
            default_reply_to=row.get('default_reply_to'),
            bounce_email=row.get('bounce_email'),
            complaint_email=row.get('complaint_email'),
            shadow_email=row.get('shadow_email'),
            rate_limit_per_minute=row['rate_limit_per_minute'],
            rate_limit_per_hour=row['rate_limit_per_hour'],
            rate_limit_per_day=row['rate_limit_per_day'],
            max_retries=row['max_retries'],
            retry_delay_seconds=row['retry_delay_seconds'],
            default_webhook_url=row.get('default_webhook_url'),
            default_webhook_secret=row.get('default_webhook_secret'),
            additional_config=additional_config,
            is_active=bool(row['is_active']),
            is_default=bool(row['is_default']),
            priority=row['priority'],
            notes=row.get('notes'),
            created_by=row.get('created_by'),
            created_at=row['created_at'],
            updated_at=row['updated_at'],
        )

    def _serialize_for_history(self, data: dict | None) -> str | None:
        """Serializes data for the history, converting datetimes to strings"""
        if not data:
            return None

        def convert_datetime(obj):
            """Converts datetimes to ISO format strings"""
            if isinstance(obj, datetime):
                return obj.isoformat()
            elif isinstance(obj, dict):
                return {k: convert_datetime(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [convert_datetime(item) for item in obj]
            return obj

        try:
            serialized = convert_datetime(data)
            return json.dumps(serialized)
        except Exception as e:
            self.logger.warning(f"Error serializing data for the history: {e}")
            return None

    async def _log_config_history(
        self, config_id: int, action: str, changed_by: str | None,
        old_values: dict | None, new_values: dict | None
    ):
        """Records a change in the history"""
        try:
            await exec_sql_async(
                """
                INSERT INTO email_config_history
                (email_config_id, action, changed_by, old_values, new_values)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    config_id, action, changed_by,
                    self._serialize_for_history(old_values),
                    self._serialize_for_history(new_values)
                )
            )
        except Exception as e:
            self.logger.warning(f"Error recording the history: {e}")


# Global service instance
email_config_service = EmailConfigService()
