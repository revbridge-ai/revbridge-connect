"""Client (tenant) scope for the API routes.

Every route that reads or changes data of a domain checks that the domain belongs to the
caller's client. "Belongs to the client" means:
- by name: the token's domain, its subdomains (for example the rbm<n>.<domain> of the
  Return-Path) and the extra domains authorized for the key (api_key_domains);
- by id: a domain already linked to the client through a key, a channel, api_key_domains
  or one of the client's users.

A client_id or domain_id given in the body or the query never defines the scope: it is
only accepted when it falls inside the token's scope.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status

from mail_api.common.db import fetch_all_async, fetch_one_async
from mail_api.shared.domain_validation import (
    get_authorized_domain_ids,
    get_user_domain_name_async,
)

FORBIDDEN_DOMAIN = "Domain does not belong to this client"


def caller_client_id(current_user: dict[str, Any] | None) -> str:
    """client_id of the token. Without one, 403: it never means "every client"."""
    cid = (current_user or {}).get("client_id")
    if not cid:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Credential has no associated client",
        )
    return str(cid)


def _bare_domain(value: str | None) -> str:
    d = (value or "").strip().lower().rstrip(".")
    if "@" in d:
        d = d.rsplit("@", 1)[1]
    return d


def name_in_family(name: str | None, roots: set[str]) -> bool:
    """`name` (a domain or an email address) is one of the roots or a subdomain of one."""
    d = _bare_domain(name)
    return bool(d) and any(d == r or d.endswith("." + r) for r in roots)


async def caller_domain_names(current_user: dict[str, Any]) -> set[str]:
    """Name roots of the token: the primary domain and the extra domains authorized for the key."""
    names: set[str] = set()
    try:
        primary = await get_user_domain_name_async(current_user)
    except HTTPException:
        primary = None
    if primary:
        names.add(_bare_domain(primary))
    ids = await get_authorized_domain_ids(current_user)
    if ids:
        placeholders = ",".join(["%s"] * len(ids))
        rows = await fetch_all_async(
            f"SELECT name FROM virtual_domains WHERE id IN ({placeholders})",
            tuple(int(i) for i in ids),
        )
        names |= {_bare_domain(r.get("name")) for r in rows or [] if r.get("name")}
    names.discard("")
    return names


async def assert_names_in_family(current_user: dict[str, Any], *names: str | None) -> None:
    """403 if any of the given names (empty ones are ignored) is outside the scope."""
    given = [n for n in names if n]
    if not given:
        return
    roots = await caller_domain_names(current_user)
    for n in given:
        if not name_in_family(n, roots):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=FORBIDDEN_DOMAIN)


_OWNED_IDS_SQL = """
    SELECT ak.domain_id AS id FROM api_keys ak
     WHERE ak.client_id = %s AND ak.domain_id IS NOT NULL
    UNION
    SELECT akd.domain_id FROM api_key_domains akd JOIN api_keys ak ON ak.id = akd.api_key_id
     WHERE ak.client_id = %s
    UNION
    SELECT CAST(JSON_UNQUOTE(JSON_EXTRACT(akc.config, '$.domain_id')) AS UNSIGNED)
      FROM api_key_channels akc JOIN api_keys ak ON ak.id = akc.api_key_id
     WHERE ak.client_id = %s AND JSON_EXTRACT(akc.config, '$.domain_id') IS NOT NULL
    UNION
    SELECT u.domain_id FROM users u JOIN customers c ON c.id = u.customer_id
     WHERE c.client_id = %s AND u.domain_id IS NOT NULL
"""


async def owned_domain_ids(current_user: dict[str, Any]) -> set[int]:
    """Every domain_id linked to the caller's client (used to filter lists)."""
    client_id = caller_client_id(current_user)
    rows = await fetch_all_async(_OWNED_IDS_SQL, (client_id,) * 4)
    out: set[int] = set()
    for r in rows or []:
        try:
            if r.get("id") is not None:
                out.add(int(r["id"]))
        except (TypeError, ValueError):
            continue
    return out


async def domain_owned_by_client(domain_id: int, client_id: str) -> bool:
    row = await fetch_one_async(
        f"SELECT 1 AS ok FROM ({_OWNED_IDS_SQL}) owned WHERE owned.id = %s LIMIT 1",
        (client_id,) * 4 + (int(domain_id),),
    )
    return bool(row)


async def domain_registered_to_other_client(name: str, current_user: dict[str, Any]) -> bool:
    """The domain (or one of its parents) is registered and does NOT belong to the caller's client."""
    d = _bare_domain(name)
    if not d:
        return False
    if name_in_family(d, await caller_domain_names(current_user)):
        return False
    labels = d.split(".")
    candidates = [".".join(labels[i:]) for i in range(len(labels) - 1)]
    if not candidates:
        return False
    placeholders = ",".join(["%s"] * len(candidates))
    rows = await fetch_all_async(
        f"SELECT id FROM virtual_domains WHERE name IN ({placeholders})", tuple(candidates)
    )
    if not rows:
        return False
    owned = await owned_domain_ids(current_user)
    return any(int(r["id"]) not in owned for r in rows)
