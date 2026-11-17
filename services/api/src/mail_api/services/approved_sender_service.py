"""
Approved Sender service
"""

import logging

from mail_api.common.config import REDIS_TTL_SEC
from mail_api.common.db import exec_sql_async, fetch_all_async, fetch_one_async
from mail_api.common.redis_client import redis_client, safe_setex
from mail_api.models.approved_sender_models import (
    ApprovedSenderCreate,
    ApprovedSenderFilter,
    ApprovedSenderListResponse,
    ApprovedSenderResponse,
    ApprovedSenderUpdate,
)

logger = logging.getLogger(__name__)


class ApprovedSenderService:
    """Approved Sender management service"""

    # Prefix of the approved sender Redis keys
    REDIS_APPROVED_SENDER_PREFIX = "approved_sender"

    def _get_redis_key_for_sender(
        self, sender_email: str, principal_domain_id: int
    ) -> str:
        """Builds the Redis key of an approved sender"""
        # Normalize sender_email (lowercase, no spaces)
        normalized_email = sender_email.lower().strip()
        return (
            f"{self.REDIS_APPROVED_SENDER_PREFIX}:"
            f"{normalized_email}:{principal_domain_id}"
        )

    async def _cache_approved_sender_in_redis(
        self, approved_sender: ApprovedSenderResponse
    ):
        """Caches the approved sender in Redis"""
        try:
            if not redis_client:
                return

            # Only cache it when it is active
            if not approved_sender.is_active:
                await self._invalidate_approved_sender_cache(
                    approved_sender.sender_email,
                    approved_sender.principal_domain_id,
                )
                return

            # Data to cache (the fields the milter needs)
            cache_data = {
                "id": approved_sender.id,
                "sender_email": approved_sender.sender_email,
                "principal_domain_id": approved_sender.principal_domain_id,
                "is_active": approved_sender.is_active,
            }

            # Cache it under the normalized key with the domain_id
            sender_key = self._get_redis_key_for_sender(
                approved_sender.sender_email,
                approved_sender.principal_domain_id,
            )
            await safe_setex(
                sender_key,
                cache_data,
                ttl=REDIS_TTL_SEC,
                redis_instance=redis_client,
            )

            logger.debug(
                f"Approved Sender cached in Redis: "
                f"sender_email={approved_sender.sender_email}, "
                f"principal_domain_id={approved_sender.principal_domain_id}"
            )
        except Exception as e:
            logger.warning(
                f"Error caching the Approved Sender in Redis: {e}"
            )

    async def _invalidate_approved_sender_cache(
        self, sender_email: str, principal_domain_id: int
    ):
        """Invalidates the Redis cache of an approved sender"""
        try:
            if not redis_client:
                return

            sender_key = self._get_redis_key_for_sender(
                sender_email, principal_domain_id
            )
            await redis_client.delete(sender_key)
            logger.debug(
                f"Approved Sender cache invalidated: "
                f"sender_email={sender_email}, "
                f"principal_domain_id={principal_domain_id}"
            )
        except Exception as e:
            logger.warning(
                f"Error invalidating the Approved Sender cache: {e}"
            )

    async def create_approved_sender(
        self,
        approved_sender_data: ApprovedSenderCreate,
        domain_id: int,
    ) -> ApprovedSenderResponse:
        """Creates a new Approved Sender"""

        # domain_id is required
        if domain_id is None:
            raise ValueError(
                "domain_id is required to create an Approved Sender"
            )

        # Make sure domain_id is an int
        if isinstance(domain_id, str):
            try:
                domain_id = int(domain_id)
            except (ValueError, TypeError):
                logger.error(
                    f"Error converting domain_id '{domain_id}' to int"
                )
                raise ValueError(f"Invalid domain_id: {domain_id}") from None

        # Always use the domain_id of the user's JWT as principal_domain_id
        principal_domain_id = domain_id

        logger.debug(
            f"Creating approved sender: "
            f"sender_email={approved_sender_data.sender_email}, "
            f"principal_domain_id={principal_domain_id}"
        )

        # Is there already an approved sender with the same
        # sender_email and principal_domain_id?
        existing = await fetch_one_async(
            "SELECT id FROM approved_senders "
            "WHERE sender_email = %s AND principal_domain_id = %s",
            (approved_sender_data.sender_email, principal_domain_id),
        )
        if existing:
            raise ValueError(
                f"An approved sender with the email "
                f"{approved_sender_data.sender_email} already exists for this domain"
            )

        # Does the principal_domain_id exist?
        domain_exists = await fetch_one_async(
            "SELECT id FROM virtual_domains WHERE id = %s",
            (principal_domain_id,),
        )
        if not domain_exists:
            raise ValueError(
                f"Main domain not found: {principal_domain_id}"
            )

        # Insert into the database
        query = """
        INSERT INTO approved_senders (
            sender_email, principal_domain_id, is_active
        ) VALUES (
            %s, %s, %s
        )
        """

        params = [
            approved_sender_data.sender_email,
            principal_domain_id,
            approved_sender_data.is_active,
        ]

        try:
            result = await exec_sql_async(query, params)
            db_id = result

            # Fetch the full data of the created Approved Sender
            created_approved_sender = await self.get_approved_sender_by_id(
                db_id, domain_id
            )
            if not created_approved_sender:
                raise Exception("Error fetching the created Approved Sender")

            logger.info(
                f"Approved Sender created: {created_approved_sender.id}"
            )

            # Cache it in Redis
            await self._cache_approved_sender_in_redis(created_approved_sender)

            return created_approved_sender

        except Exception as e:
            logger.error(f"Error creating the Approved Sender: {e}")
            raise Exception(f"Error creating the Approved Sender: {e!s}") from e

    async def get_approved_sender_by_id(
        self, approved_sender_id: int, domain_id: int
    ) -> ApprovedSenderResponse | None:
        """Fetches an Approved Sender by ID"""
        query = """
        SELECT id, sender_email, principal_domain_id, is_active,
               created_at, updated_at
        FROM approved_senders
        WHERE id = %s AND principal_domain_id = %s
        """

        result = await fetch_one_async(query, [approved_sender_id, domain_id])
        if not result:
            return None

        return ApprovedSenderResponse(
            id=result["id"],
            sender_email=result["sender_email"],
            principal_domain_id=result["principal_domain_id"],
            is_active=bool(result["is_active"]),
            created_at=result["created_at"],
            updated_at=result["updated_at"],
        )

    async def list_approved_senders(
        self,
        domain_id: int,
        page: int = 1,
        limit: int = 50,
        filters: ApprovedSenderFilter | None = None,
    ) -> ApprovedSenderListResponse:
        """Lists the domain's Approved Senders"""

        # Base query
        base_query = """
        FROM approved_senders
        WHERE principal_domain_id = %s
        """

        params = [domain_id]

        # Apply the filters
        if filters:
            if filters.sender_email:
                base_query += " AND sender_email LIKE %s"
                params.append(f"%{filters.sender_email}%")

            if filters.is_active is not None:
                base_query += " AND is_active = %s"
                params.append(filters.is_active)

            if filters.principal_domain_id:
                base_query += " AND principal_domain_id = %s"
                params.append(filters.principal_domain_id)

        # Count the total
        count_query = f"SELECT COUNT(*) as total {base_query}"
        count_result = await fetch_one_async(count_query, params)
        total = count_result["total"] if count_result else 0

        # Fetch the page
        offset = (page - 1) * limit
        data_query = f"""
        SELECT id, sender_email, principal_domain_id, is_active,
               created_at, updated_at
        {base_query}
        ORDER BY created_at DESC
        LIMIT %s OFFSET %s
        """

        params.extend([limit, offset])
        results = await fetch_all_async(data_query, params)

        # Convert the results
        approved_senders = []
        for result in results:
            approved_senders.append(
                ApprovedSenderResponse(
                    id=result["id"],
                    sender_email=result["sender_email"],
                    principal_domain_id=result["principal_domain_id"],
                    is_active=bool(result["is_active"]),
                    created_at=result["created_at"],
                    updated_at=result["updated_at"],
                )
            )

        return ApprovedSenderListResponse(
            approved_senders=approved_senders,
            total=total,
            page=page,
            limit=limit,
        )

    async def update_approved_sender(
        self,
        approved_sender_id: int,
        domain_id: int,
        update_data: ApprovedSenderUpdate,
    ) -> ApprovedSenderResponse | None:
        """Updates an Approved Sender"""

        # Does the approved sender exist and belong to the domain?
        existing = await self.get_approved_sender_by_id(
            approved_sender_id, domain_id
        )
        if not existing:
            return None

        # Build the update query
        update_fields = []
        params = []

        if update_data.sender_email is not None:
            # Is there another approved sender with the same
            # sender_email and principal_domain_id?
            if update_data.sender_email != existing.sender_email:
                duplicate = await fetch_one_async(
                    "SELECT id FROM approved_senders "
                    "WHERE sender_email = %s "
                    "AND principal_domain_id = %s AND id != %s",
                    (update_data.sender_email, domain_id, approved_sender_id),
                )
                if duplicate:
                    raise ValueError(
                        f"An approved sender with the email "
                        f"{update_data.sender_email} already exists for this domain"
                    )

            update_fields.append("sender_email = %s")
            params.append(update_data.sender_email)

        # principal_domain_id cannot be changed: it is always the
        # domain_id of the user's JWT

        if update_data.is_active is not None:
            update_fields.append("is_active = %s")
            params.append(update_data.is_active)

        if not update_fields:
            # Nothing to update: return the existing one
            return existing

        # WHERE clause parameters
        params.extend([approved_sender_id, domain_id])

        query = f"""
        UPDATE approved_senders
        SET {', '.join(update_fields)}, updated_at = CURRENT_TIMESTAMP
        WHERE id = %s AND principal_domain_id = %s
        """

        try:
            await exec_sql_async(query, params)

            # Fetch the updated data
            updated = await self.get_approved_sender_by_id(
                approved_sender_id, domain_id
            )
            if updated:
                logger.info(
                    f"Approved Sender updated: {approved_sender_id}"
                )
                # Invalidate the old cache entry if sender_email changed
                old_sender_email = existing.sender_email
                if (
                    update_data.sender_email
                    and update_data.sender_email != old_sender_email
                ):
                    await self._invalidate_approved_sender_cache(
                        old_sender_email, existing.principal_domain_id
                    )
                # Cache the updated data (or invalidate it if is_active=False)
                await self._cache_approved_sender_in_redis(updated)
            return updated

        except Exception as e:
            logger.error(f"Error updating the Approved Sender: {e}")
            raise

    async def delete_approved_sender(
        self, approved_sender_id: int, domain_id: int
    ) -> bool:
        """Deletes an Approved Sender"""

        # Does the approved sender exist and belong to the domain?
        existing = await self.get_approved_sender_by_id(
            approved_sender_id, domain_id
        )
        if not existing:
            return False

        query = """
        DELETE FROM approved_senders
        WHERE id = %s AND principal_domain_id = %s
        """

        try:
            # Keep the data before deleting, to invalidate the cache
            sender_email = existing.sender_email
            principal_domain_id = existing.principal_domain_id

            await exec_sql_async(query, [approved_sender_id, domain_id])
            logger.info(f"Approved Sender deleted: {approved_sender_id}")

            # Invalidate the Redis cache
            await self._invalidate_approved_sender_cache(
                sender_email, principal_domain_id
            )

            return True

        except Exception as e:
            logger.error(f"Error deleting the Approved Sender: {e}")
            raise


# Singleton service instance
approved_sender_service = ApprovedSenderService()
