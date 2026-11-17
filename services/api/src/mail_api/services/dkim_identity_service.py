"""
RevBridge DKIM Identity service

The main DKIM identity service:
- DKIM identity management in the database
- Full identity CRUD
- Filters and search
"""

import logging

from mail_api.common.db import (
    exec_sql_async,
    fetch_all_async,
    fetch_one_async,
)
from mail_api.models.dkim_models import (
    DkimIdentityCreate,
    DkimIdentityFilter,
    DkimIdentityResponse,
    DkimIdentityUpdate,
)


class DkimIdentityService:
    """Main service for DKIM identity management"""

    def __init__(self):
        self.logger = logging.getLogger(__name__)

    async def create_identity(
        self, identity_data: DkimIdentityCreate
    ) -> DkimIdentityResponse:
        """Creates a new DKIM identity"""
        try:
            # Require at least from_email or from_domain
            if not identity_data.from_email and not identity_data.from_domain:
                raise ValueError("Provide at least from_email or from_domain")

            # Idempotency: if an identity with the same parameters exists, return it.
            existing = await self._get_identity_by_params(
                cluster_id=identity_data.cluster_id,
                selector=identity_data.selector,
                dkim_domain=identity_data.dkim_domain,
                from_email=identity_data.from_email,
                from_domain=identity_data.from_domain,
            )
            if existing:
                self.logger.info(
                    f"DKIM identity already exists for cluster_id={identity_data.cluster_id}, "
                    f"selector={identity_data.selector}, dkim_domain={identity_data.dkim_domain}, "
                    f"from_email={identity_data.from_email}, from_domain={identity_data.from_domain}; "
                    f"returning the existing ID {existing.id}"
                )
                return existing

            # Insert into the database
            identity_id = await self._insert_identity(identity_data)

            # Fetch the created identity
            identity = await self._get_identity_by_id(identity_id)
            if not identity:
                raise Exception("Error creating the DKIM identity")

            self.logger.info(f"DKIM identity created: ID {identity_id}")
            return identity

        except Exception as e:
            self.logger.error(f"Error creating the DKIM identity: {e}")
            raise

    async def get_identity(self, identity_id: int) -> DkimIdentityResponse | None:
        """Fetches a DKIM identity by ID"""
        return await self._get_identity_by_id(identity_id)

    async def list_identities(
        self, filters: DkimIdentityFilter | None = None
    ) -> list[DkimIdentityResponse]:
        """Lists DKIM identities with optional filters"""
        try:
            query = """
                SELECT id, cluster_id, from_email, from_domain, wildcard_subdomains,
                       selector, dkim_domain, created_at, updated_at
                FROM dkim_identities
                WHERE 1=1
            """
            params = []

            if filters:
                if filters.cluster_id:
                    query += " AND cluster_id = %s"
                    params.append(filters.cluster_id)

                if filters.from_email:
                    query += " AND from_email = %s"
                    params.append(filters.from_email)

                if filters.from_domain:
                    query += " AND from_domain = %s"
                    params.append(filters.from_domain)

                if filters.selector:
                    query += " AND selector = %s"
                    params.append(filters.selector)

                if filters.dkim_domain:
                    query += " AND dkim_domain = %s"
                    params.append(filters.dkim_domain)

            query += " ORDER BY created_at DESC"

            rows = await fetch_all_async(query, params)
            identities = []

            for row in rows:
                identity = DkimIdentityResponse(
                    id=row["id"],
                    cluster_id=row["cluster_id"],
                    from_email=row["from_email"],
                    from_domain=row["from_domain"],
                    wildcard_subdomains=bool(row["wildcard_subdomains"]),
                    selector=row["selector"],
                    dkim_domain=row["dkim_domain"],
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )
                identities.append(identity)

            return identities

        except Exception as e:
            self.logger.error(f"Error listing DKIM identities: {e}")
            raise

    async def update_identity(
        self, identity_id: int, identity_data: DkimIdentityUpdate
    ) -> DkimIdentityResponse | None:
        """Updates a DKIM identity"""
        try:
            # Does the identity exist?
            existing = await self._get_identity_by_id(identity_id)
            if not existing:
                return None

            # Update the database
            await self._update_identity_db(identity_id, identity_data)

            # Fetch the updated identity
            updated_identity = await self._get_identity_by_id(identity_id)
            self.logger.info(
                f"DKIM identity updated: ID {identity_id}"
            )
            return updated_identity

        except Exception as e:
            self.logger.error(f"Error updating DKIM identity {identity_id}: {e}")
            raise

    async def delete_identity(self, identity_id: int) -> bool:
        """Removes a DKIM identity"""
        try:
            # Does the identity exist?
            existing = await self._get_identity_by_id(identity_id)
            if not existing:
                return False

            # Remove it from the database
            await exec_sql_async("DELETE FROM dkim_identities WHERE id = %s", (identity_id,))

            self.logger.info(f"DKIM identity removed: ID {identity_id}")
            return True

        except Exception as e:
            self.logger.error(f"Error removing DKIM identity {identity_id}: {e}")
            raise

    # Private methods

    async def _get_identity_by_id(self, identity_id: int) -> DkimIdentityResponse | None:
        """Fetches an identity by ID"""
        row = await fetch_one_async(
            "SELECT * FROM dkim_identities WHERE id = %s", (identity_id,)
        )

        if not row:
            return None

        return DkimIdentityResponse(
            id=row["id"],
            cluster_id=row["cluster_id"],
            from_email=row["from_email"],
            from_domain=row["from_domain"],
            wildcard_subdomains=bool(row["wildcard_subdomains"]),
            selector=row["selector"],
            dkim_domain=row["dkim_domain"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def _get_identity_by_params(
        self,
        cluster_id: str,
        selector: str,
        dkim_domain: str,
        from_email: str | None = None,
        from_domain: str | None = None,
    ) -> DkimIdentityResponse | None:
        """Fetches an identity by its unique parameters"""
        query = """
            SELECT * FROM dkim_identities
            WHERE cluster_id = %s AND selector = %s AND dkim_domain = %s
        """
        params = [cluster_id, selector, dkim_domain]

        if from_email:
            query += " AND from_email = %s"
            params.append(from_email)
        else:
            query += " AND from_email IS NULL"

        if from_domain:
            query += " AND from_domain = %s"
            params.append(from_domain)
        else:
            query += " AND from_domain IS NULL"

        row = await fetch_one_async(query, tuple(params))

        if not row:
            return None

        return DkimIdentityResponse(
            id=row["id"],
            cluster_id=row["cluster_id"],
            from_email=row["from_email"],
            from_domain=row["from_domain"],
            wildcard_subdomains=bool(row["wildcard_subdomains"]),
            selector=row["selector"],
            dkim_domain=row["dkim_domain"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def _insert_identity(
        self, identity_data: DkimIdentityCreate
    ) -> int:
        """Inserts the identity into the database"""
        identity_id = await exec_sql_async(
            """
            INSERT INTO dkim_identities (cluster_id, from_email, from_domain, wildcard_subdomains,
                                       selector, dkim_domain)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                identity_data.cluster_id,
                identity_data.from_email,
                identity_data.from_domain,
                identity_data.wildcard_subdomains,
                identity_data.selector,
                identity_data.dkim_domain,
            ),
        )

        if identity_id == 0:
            self.logger.error(
                "Error: the inserted DKIM identity returned ID 0!"
            )

        return identity_id

    async def _update_identity_db(self, identity_id: int, identity_data: DkimIdentityUpdate):
        """Updates the identity in the database"""
        updates = []
        params = []

        if identity_data.cluster_id is not None:
            updates.append("cluster_id = %s")
            params.append(identity_data.cluster_id)

        if identity_data.from_email is not None:
            updates.append("from_email = %s")
            params.append(identity_data.from_email)

        if identity_data.from_domain is not None:
            updates.append("from_domain = %s")
            params.append(identity_data.from_domain)

        if identity_data.wildcard_subdomains is not None:
            updates.append("wildcard_subdomains = %s")
            params.append(identity_data.wildcard_subdomains)

        if identity_data.selector is not None:
            updates.append("selector = %s")
            params.append(identity_data.selector)

        if identity_data.dkim_domain is not None:
            updates.append("dkim_domain = %s")
            params.append(identity_data.dkim_domain)

        if updates:
            updates.append("updated_at = CURRENT_TIMESTAMP")
            params.append(identity_id)

            query = f"UPDATE dkim_identities SET {', '.join(updates)} WHERE id = %s"
            await exec_sql_async(query, tuple(params))
        else:
            self.logger.warning(f"No update given for identity {identity_id}")


# Singleton service instance
dkim_identity_service = DkimIdentityService()
