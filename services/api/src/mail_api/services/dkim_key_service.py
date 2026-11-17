"""
RevBridge DKIM Key service

The main DKIM key service:
- DKIM key management in the database
- Full key CRUD
- Filters and search
"""

import base64
import logging

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from mail_api.common.db import (
    exec_sql_async,
    fetch_all_async,
    fetch_one_async,
)
from mail_api.models.dkim_models import (
    DkimKeyCreate,
    DkimKeyFilter,
    DkimKeyResponse,
    DkimKeyUpdate,
)


def generate_dkim_keypair(key_size: int = 2048) -> tuple[str, str]:
    """Generates a DKIM RSA key pair.

    Returns (private_key_pem, public_key_b64):
      * private_key_pem  — PKCS#1 PEM ("BEGIN RSA PRIVATE KEY"), identical to what
        opendkim-genkey produces; it is what the inline key table hands to OpenDKIM.
      * public_key_b64   — DER SubjectPublicKeyInfo as single-line base64, the ``p=``
        value of the DKIM TXT record.
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=key_size)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,  # PKCS#1, same as opendkim-genkey
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_der = key.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    public_b64 = base64.b64encode(public_der).decode()
    return private_pem, public_b64


def public_key_from_private_pem(private_pem: str) -> str:
    """Derives the public_key (``p=`` of the DKIM TXT record) from a PEM private key.

    Used on import to check that the imported PEM matches the DKIM record already
    published (same key → DNS does not change).
    """
    key = serialization.load_pem_private_key(private_pem.encode(), password=None)
    public_der = key.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return base64.b64encode(public_der).decode()


class DkimKeyService:
    """Main service for DKIM key management"""

    def __init__(self):
        self.logger = logging.getLogger(__name__)

    async def _with_dns_record(
        self, key: DkimKeyResponse | None
    ) -> DkimKeyResponse | None:
        """Fills public_key/dns_record_* by deriving them from the stored private key.

        This makes a repeated POST (re-provisioning) return the same DNS record as
        the creation, so the endpoint is truly idempotent: same input, same response,
        including on repeats. A response without these fields would lead a DNS
        automation to publish an empty TXT record, and the domain would sign without
        a publishable key (``dkim=permerror`` at the recipient).

        The PEM is fetched here instead of coming in ``DkimKeyResponse``: private
        material never goes into the response model (see the ``DkimKeyResponse``
        docstring).
        """
        if not key:
            return key
        row = await fetch_one_async(
            "SELECT private_key_pem FROM dkim_keys WHERE id = %s", (key.id,)
        )
        pem = row.get("private_key_pem") if row else None
        if not pem:
            return key
        try:
            pub = public_key_from_private_pem(pem)
        except Exception as err:  # corrupted key in the database: must not break the caller
            self.logger.warning(
                f"DKIM key ID {key.id} ({key.dkim_domain}): failed to derive the "
                f"public_key from the stored private_key_pem: {err}"
            )
            return key
        key.public_key = pub
        key.dns_record_name = f"{key.selector}._domainkey.{key.dkim_domain}"
        # Exactly the same format as on creation (below): re-provisioning must produce
        # a byte-for-byte identical TXT record, otherwise a DNS automation rewrites the
        # record on every run for no reason.
        key.dns_record_value = f"v=DKIM1; k=rsa; p={pub}"
        return key

    async def create_key(
        self, key_data: DkimKeyCreate
    ) -> DkimKeyResponse:
        """Creates a new DKIM key (idempotent: returns the existing one if there is one)."""
        try:
            # Idempotency: if a key with the same unique parameters exists, return it.
            existing = await self._get_key_by_unique_params(
                cluster_id=key_data.cluster_id,
                selector=key_data.selector,
                dkim_domain=key_data.dkim_domain,
            )
            if existing:
                # Import: the domain already has the key row (created with only a
                # private_key_path). Store the private_key_pem the domain ALREADY uses,
                # keeping the SAME selector → signature/DNS do not change.
                # Idempotent backfill.
                if key_data.private_key_pem:
                    pub = public_key_from_private_pem(key_data.private_key_pem)
                    await self._update_key_db(
                        existing.id,
                        DkimKeyUpdate(private_key_pem=key_data.private_key_pem),
                    )
                    self.logger.info(
                        f"DKIM key ID {existing.id} ({key_data.dkim_domain}): private_key_pem "
                        f"imported (selector {existing.selector} kept)."
                    )
                    updated = await self._get_key_by_id(existing.id)
                    if updated:
                        updated.public_key = pub
                        updated.dns_record_name = f"{existing.selector}._domainkey.{existing.dkim_domain}"
                        updated.dns_record_value = f"v=DKIM1; k=rsa; p={pub}"
                        return updated
                # Path upsert: if the private_key_path changed (domain re-provisioned),
                # UPDATE instead of just returning the old key. Otherwise OpenDKIM points
                # at a key file that does not exist on the server
                # → "KeyTable entry corrupt" → milter-reject 451.
                if (
                    key_data.private_key_path
                    and existing.private_key_path != key_data.private_key_path
                ):
                    self.logger.warning(
                        f"DKIM key ID {existing.id} ({key_data.dkim_domain}): private_key_path "
                        f"differs {existing.private_key_path!r} → {key_data.private_key_path!r}; "
                        f"updating it (re-provisioning)."
                    )
                    await self._update_key_db(
                        existing.id,
                        DkimKeyUpdate(private_key_path=key_data.private_key_path),
                    )
                    updated = await self._get_key_by_id(existing.id)
                    if updated:
                        return await self._with_dns_record(updated)
                self.logger.info(
                    f"DKIM key already exists for cluster_id={key_data.cluster_id}, "
                    f"selector={key_data.selector}, dkim_domain={key_data.dkim_domain}; "
                    f"returning the existing ID {existing.id}"
                )
                return await self._with_dns_record(existing)

            # Generate mode: create the RSA pair server-side, store the PEM (inline
            # signing, no key file) and return the public_key to publish in DNS.
            private_key_pem: str | None = None
            public_key_b64: str | None = None
            private_key_path = key_data.private_key_path
            if key_data.generate_key:
                private_key_pem, public_key_b64 = generate_dkim_keypair(key_data.key_size)
            elif key_data.private_key_pem:
                # Import without an existing row: store the given key.
                private_key_pem = key_data.private_key_pem
                public_key_b64 = public_key_from_private_pem(private_key_pem)
            if private_key_pem and not private_key_path:
                # Synthetic path following the usual OpenDKIM layout (the inline key
                # table does not use the path: it reads private_key_pem from the database).
                private_key_path = (
                    f"/etc/opendkim/keys/{key_data.dkim_domain}/{key_data.selector}.private"
                )

            # Insert into the database (path + key material when generated)
            key_id = await self._insert_key(key_data, private_key_path, private_key_pem)

            # Fetch the created key
            key = await self._get_key_by_id(key_id)
            if not key:
                raise Exception("Error creating the DKIM key")

            # Only on creation with a key (generated or imported): return the DNS record.
            if public_key_b64:
                key.public_key = public_key_b64
                key.dns_record_name = f"{key_data.selector}._domainkey.{key_data.dkim_domain}"
                key.dns_record_value = f"v=DKIM1; k=rsa; p={public_key_b64}"

            if key_data.generate_key:
                mode = " (generated server-side, inline key table)"
            elif key_data.private_key_pem:
                mode = " (imported)"
            else:
                mode = ""
            self.logger.info(f"DKIM key created: ID {key_id}{mode}")
            return key

        except Exception as e:
            self.logger.error(f"Error creating the DKIM key: {e}")
            raise

    async def get_key(self, key_id: int) -> DkimKeyResponse | None:
        """Fetches a DKIM key by ID"""
        return await self._get_key_by_id(key_id)

    async def list_keys(
        self, filters: DkimKeyFilter | None = None
    ) -> list[DkimKeyResponse]:
        """Lists DKIM keys with optional filters"""
        try:
            query = """
                SELECT id, cluster_id, selector, dkim_domain, private_key_path,
                       created_at, updated_at
                FROM dkim_keys
                WHERE 1=1
            """
            params = []

            if filters:
                if filters.cluster_id:
                    query += " AND cluster_id = %s"
                    params.append(filters.cluster_id)

                if filters.selector:
                    query += " AND selector = %s"
                    params.append(filters.selector)

                if filters.dkim_domain:
                    query += " AND dkim_domain = %s"
                    params.append(filters.dkim_domain)

            query += " ORDER BY created_at DESC"

            rows = await fetch_all_async(query, params)
            keys = []

            for row in rows:
                key = DkimKeyResponse(
                    id=row["id"],
                    cluster_id=row["cluster_id"],
                    selector=row["selector"],
                    dkim_domain=row["dkim_domain"],
                    private_key_path=row["private_key_path"],
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )
                keys.append(key)

            return keys

        except Exception as e:
            self.logger.error(f"Error listing DKIM keys: {e}")
            raise

    async def update_key(
        self, key_id: int, key_data: DkimKeyUpdate
    ) -> DkimKeyResponse | None:
        """Updates a DKIM key"""
        try:
            # Does the key exist?
            existing = await self._get_key_by_id(key_id)
            if not existing:
                return None

            # When the unique fields change, check for duplicates
            if (key_data.cluster_id or key_data.selector or key_data.dkim_domain):
                new_cluster_id = key_data.cluster_id or existing.cluster_id
                new_selector = key_data.selector or existing.selector
                new_dkim_domain = key_data.dkim_domain or existing.dkim_domain

                # Is there another key with the same unique parameters?
                duplicate = await self._get_key_by_unique_params(
                    cluster_id=new_cluster_id,
                    selector=new_selector,
                    dkim_domain=new_dkim_domain,
                )
                if duplicate and duplicate.id != key_id:
                    raise ValueError("A DKIM key with these parameters (cluster_id, selector, dkim_domain) already exists")

            # Update the database
            await self._update_key_db(key_id, key_data)

            # Fetch the updated key
            updated_key = await self._get_key_by_id(key_id)
            self.logger.info(
                f"DKIM key updated: ID {key_id}"
            )
            return updated_key

        except Exception as e:
            self.logger.error(f"Error updating DKIM key {key_id}: {e}")
            raise

    async def delete_key(self, key_id: int) -> bool:
        """Removes a DKIM key"""
        try:
            # Does the key exist?
            existing = await self._get_key_by_id(key_id)
            if not existing:
                return False

            # Remove it from the database
            await exec_sql_async("DELETE FROM dkim_keys WHERE id = %s", (key_id,))

            self.logger.info(f"DKIM key removed: ID {key_id}")
            return True

        except Exception as e:
            self.logger.error(f"Error removing DKIM key {key_id}: {e}")
            raise

    # Private methods

    async def _get_key_by_id(self, key_id: int) -> DkimKeyResponse | None:
        """Fetches a key by ID"""
        row = await fetch_one_async(
            "SELECT * FROM dkim_keys WHERE id = %s", (key_id,)
        )

        if not row:
            return None

        return DkimKeyResponse(
            id=row["id"],
            cluster_id=row["cluster_id"],
            selector=row["selector"],
            dkim_domain=row["dkim_domain"],
            private_key_path=row["private_key_path"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def _get_key_by_unique_params(
        self,
        cluster_id: str,
        selector: str,
        dkim_domain: str,
    ) -> DkimKeyResponse | None:
        """Fetches a key by its unique parameters (cluster_id, selector, dkim_domain)"""
        row = await fetch_one_async(
            """
            SELECT * FROM dkim_keys
            WHERE cluster_id = %s AND selector = %s AND dkim_domain = %s
            """,
            (cluster_id, selector, dkim_domain),
        )

        if not row:
            return None

        return DkimKeyResponse(
            id=row["id"],
            cluster_id=row["cluster_id"],
            selector=row["selector"],
            dkim_domain=row["dkim_domain"],
            private_key_path=row["private_key_path"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def _insert_key(
        self,
        key_data: DkimKeyCreate,
        private_key_path: str,
        private_key_pem: str | None = None,
    ) -> int:
        """Inserts the key into the database (always the path; the PEM when there is one)."""
        key_id = await exec_sql_async(
            """
            INSERT INTO dkim_keys (cluster_id, selector, dkim_domain, private_key_path, private_key_pem)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (
                key_data.cluster_id,
                key_data.selector,
                key_data.dkim_domain,
                private_key_path,
                private_key_pem,
            ),
        )

        if key_id == 0:
            self.logger.error(
                "Error: the inserted DKIM key returned ID 0!"
            )

        return key_id

    async def _update_key_db(self, key_id: int, key_data: DkimKeyUpdate):
        """Updates the key in the database"""
        updates = []
        params = []

        if key_data.cluster_id is not None:
            updates.append("cluster_id = %s")
            params.append(key_data.cluster_id)

        if key_data.selector is not None:
            updates.append("selector = %s")
            params.append(key_data.selector)

        if key_data.dkim_domain is not None:
            updates.append("dkim_domain = %s")
            params.append(key_data.dkim_domain)

        if key_data.private_key_path is not None:
            updates.append("private_key_path = %s")
            params.append(key_data.private_key_path)

        if key_data.private_key_pem is not None:
            updates.append("private_key_pem = %s")
            params.append(key_data.private_key_pem)

        if updates:
            updates.append("updated_at = CURRENT_TIMESTAMP")
            params.append(key_id)

            query = f"UPDATE dkim_keys SET {', '.join(updates)} WHERE id = %s"
            await exec_sql_async(query, tuple(params))
        else:
            self.logger.warning(f"No update given for key {key_id}")


# Singleton service instance
dkim_key_service = DkimKeyService()
