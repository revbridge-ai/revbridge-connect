"""
API Key Authorized Domains Router (multi-domain)

Manages the domains an API Key may send email from, IN ADDITION to the primary
domain (email channel / api_key_channels.config). Reads/writes the
`api_key_domains` table and invalidates the Redis cache used by
`validate_authorized_sender`, so ONE api_key can send from several domains of the
same customer.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Path, status
from mail_api.common.db import exec_sql_async, fetch_all_async, fetch_one_async
from pydantic import BaseModel, Field
from connect_lib.auth.hybrid import (
    require_read_emails_hybrid,
    require_write_emails_hybrid,
)

router = APIRouter(prefix="/authorized-domains", tags=["authorized-domains"])
logger = logging.getLogger(__name__)


class AuthorizedDomainCreate(BaseModel):
    domain_id: int | None = Field(None, description="ID of the virtual_domain to authorize")
    domain: str | None = Field(None, description="Domain name (resolved to domain_id when domain_id is missing)")
    include_subdomains: bool = Field(True, description="Also authorize the domain's subdomains")


async def _invalidate_cache(api_key_id: str) -> None:
    try:
        from mail_api.common.redis_client import redis_client
        if redis_client:
            await redis_client.delete(f"api_key_domains:{api_key_id}")
    except Exception as e:  # cache best-effort
        logger.debug(f"[AUTHORIZED_DOMAINS] error invalidating the cache: {e}")


async def _resolve_api_key_pk(api_key_id: str, current_user: dict | None = None) -> int:
    row = await fetch_one_async("SELECT id, client_id FROM api_keys WHERE api_key_id = %s", (api_key_id,))
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"API Key '{api_key_id}' not found")
    # RevBridge Connect: the key in the path must belong to the SAME client as the caller
    # (this also covers a JWT, which has no api_key_id and is not checked by
    # _assert_self_scope). 404, not 403: it does not confirm that another client's key exists.
    caller_client = (current_user or {}).get("client_id")
    if current_user is not None and (not caller_client or caller_client != row.get("client_id")):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"API Key '{api_key_id}' not found")
    return int(row["id"])


async def _assert_domain_owned(domain_id: int, current_user: dict) -> None:
    """RevBridge Connect: only authorizes a domain that already belongs to the caller's client.

    A key must never be able to send as another client's domain (signed with that
    owner's DKIM key). "Belongs" = already linked to the client through a key (email
    channel or api_key_domains) or through one of the client's users.
    """
    client_id = current_user.get("client_id")
    row = await fetch_one_async(
        """
        SELECT 1 AS ok FROM api_keys ak
         WHERE ak.client_id = %s AND ak.domain_id = %s
        UNION ALL
        SELECT 1 FROM api_key_domains akd JOIN api_keys ak ON ak.id = akd.api_key_id
         WHERE ak.client_id = %s AND akd.domain_id = %s
        UNION ALL
        SELECT 1 FROM api_key_channels akc JOIN api_keys ak ON ak.id = akc.api_key_id
         WHERE ak.client_id = %s AND JSON_EXTRACT(akc.config, '$.domain_id') = %s
        UNION ALL
        SELECT 1 FROM users u JOIN customers c ON c.id = u.customer_id
         WHERE c.client_id = %s AND u.domain_id = %s
        LIMIT 1
        """,
        (client_id, domain_id, client_id, domain_id, client_id, domain_id, client_id, domain_id),
    )
    if not row:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Domain does not belong to this client",
        )


def _assert_self_scope(api_key_id: str, current_user: dict) -> None:
    """An API Key can only manage its OWN authorized domains.

    Keeps a key from authorizing/deauthorizing the domains of ANOTHER key.
    Provisioning calls it with the bearer of the customer's own key (the same key
    as in the path).
    """
    caller = current_user.get("api_key_id")
    if caller and caller != api_key_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="An API Key can only manage its own authorized domains",
        )


@router.get("/{api_key_id}", summary="List the authorized domains of an API Key")
async def list_authorized_domains(
    api_key_id: str = Path(..., description="Public api_key_id (e.g. ak_...)"),
    current_user: dict = Depends(require_read_emails_hybrid),
):
    _assert_self_scope(api_key_id, current_user)
    pk = await _resolve_api_key_pk(api_key_id, current_user)
    rows = await fetch_all_async(
        "SELECT akd.domain_id, vd.name AS domain, akd.include_subdomains "
        "FROM api_key_domains akd JOIN virtual_domains vd ON vd.id = akd.domain_id "
        "WHERE akd.api_key_id = %s ORDER BY vd.name",
        (pk,),
    )
    return {"api_key_id": api_key_id, "domains": rows or []}


@router.post("/{api_key_id}", status_code=status.HTTP_201_CREATED, summary="Authorize a domain for the API Key")
async def add_authorized_domain(
    data: AuthorizedDomainCreate,
    api_key_id: str = Path(..., description="Public api_key_id (e.g. ak_...)"),
    current_user: dict = Depends(require_write_emails_hybrid),
):
    _assert_self_scope(api_key_id, current_user)
    pk = await _resolve_api_key_pk(api_key_id, current_user)

    domain_id = data.domain_id
    if not domain_id and data.domain:
        row = await fetch_one_async("SELECT id FROM virtual_domains WHERE name = %s", (data.domain.lower().strip(),))
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Domain '{data.domain}' is not registered in virtual_domains")
        domain_id = int(row["id"])
    if not domain_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Provide domain_id or domain")
    await _assert_domain_owned(int(domain_id), current_user)

    await exec_sql_async(
        "INSERT INTO api_key_domains (api_key_id, domain_id, include_subdomains) VALUES (%s, %s, %s) "
        "ON DUPLICATE KEY UPDATE include_subdomains = VALUES(include_subdomains)",
        (pk, domain_id, 1 if data.include_subdomains else 0),
    )
    await _invalidate_cache(api_key_id)
    logger.info(f"[AUTHORIZED_DOMAINS] api_key={api_key_id} authorized for domain_id={domain_id}")
    return {"api_key_id": api_key_id, "domain_id": domain_id, "include_subdomains": data.include_subdomains, "status": "authorized"}


@router.delete("/{api_key_id}/{domain_id}", summary="Remove a domain authorization")
async def remove_authorized_domain(
    api_key_id: str = Path(...),
    domain_id: int = Path(...),
    current_user: dict = Depends(require_write_emails_hybrid),
):
    _assert_self_scope(api_key_id, current_user)
    pk = await _resolve_api_key_pk(api_key_id, current_user)
    await exec_sql_async("DELETE FROM api_key_domains WHERE api_key_id = %s AND domain_id = %s", (pk, domain_id))
    await _invalidate_cache(api_key_id)
    return {"api_key_id": api_key_id, "domain_id": domain_id, "status": "removed"}
