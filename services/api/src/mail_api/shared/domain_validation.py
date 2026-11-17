"""
Domain access validation

This module provides:
- Domain access validation based on the domain_id of the JWT or of the email channel
- Conversion from domain_name to domain_id
- Checks that the user has access to a given domain
- Checks that a sender is authorized to send email
- Checks that the email channel is enabled for the API Key

Works with the channel model (api_key_channels) and with the
legacy model (domain_id directly on api_keys).
"""

import asyncio
import logging
from typing import Any

from fastapi import HTTPException, status

from mail_api.common.db import fetch_all, fetch_one

logger = logging.getLogger(__name__)


def is_email_channel_enabled(current_user: dict[str, Any]) -> bool:
    """
    Checks whether the email channel is enabled for the user.

    For a JWT, email is assumed to be enabled.
    For API Keys, checks the list of enabled channels.

    Args:
        current_user: Authenticated user data

    Returns:
        bool: True if the email channel is enabled
    """
    # Non-empty enabled_channels: use it (channel model)
    enabled_channels = current_user.get("enabled_channels")
    if enabled_channels:  # Non-empty list
        return "email" in enabled_channels

    # A channels dict: check it
    channels = current_user.get("channels", {})
    if channels:
        email_channel = channels.get("email", {})
        if email_channel.get("enabled", False):
            return True

    # Fallback: with a domain_id, assume email is enabled (legacy)
    # Keeps API Keys that have a domain_id but no configured channels working
    if current_user.get("domain_id") is not None:
        logger.debug("[EMAIL_CHANNEL] Using fallback: domain_id present, assuming email is enabled")
        return True

    return False


def get_domain_id_from_name(domain_name: str) -> int | None:
    """
    Returns the domain_id for a domain name

    Args:
        domain_name: Domain name (e.g. "example.com")

    Returns:
        int: Domain ID, or None if not found
    """
    try:
        # The virtual_domains table only has the 'name' column, not 'domain'
        result = fetch_one(
            "SELECT id FROM virtual_domains WHERE name = %s",
            (domain_name,)
        )
        if result:
            return result.get("id")

        # Not found: the domain may not exist
        return None
    except Exception as e:
        print(f"[DOMAIN_VALIDATION] Error fetching domain_id for {domain_name}: {e}")
        return None


def get_user_domain_id(current_user: dict[str, Any]) -> int | None:
    """
    Returns the domain_id of the authenticated user.

    For API Keys on the channel model, reads the domain_id from the
    email channel configuration. Keeps the legacy domain_id working.

    Args:
        current_user: Authenticated user data (from the JWT or API Key)

    Returns:
        int: The user's domain ID, or None
    """
    # Try the email channel first (channel model)
    channels = current_user.get("channels", {})
    if channels:
        email_channel = channels.get("email", {})
        if email_channel.get("enabled"):
            email_config = email_channel.get("config", {})
            domain_id = email_config.get("domain_id")
            if domain_id is not None:
                try:
                    return int(domain_id)
                except (ValueError, TypeError):
                    pass

    # Fallback to the legacy domain_id
    domain_id = current_user.get("domain_id")
    # Convert to int when it is a string
    if domain_id and isinstance(domain_id, str):
        try:
            return int(domain_id)
        except (ValueError, TypeError):
            return None
    return domain_id


def validate_domain_id_exists(domain_id: int) -> bool:
    """
    Checks that the domain_id exists in the virtual_domains table

    Args:
        domain_id: Domain ID to check

    Returns:
        bool: True if the domain exists, False otherwise
    """
    try:
        result = fetch_one(
            "SELECT id FROM virtual_domains WHERE id = %s",
            (domain_id,)
        )
        return result is not None
    except Exception as e:
        print(f"[DOMAIN_VALIDATION] Error validating domain_id {domain_id}: {e}")
        return False


async def get_user_domain_name_async(current_user: dict[str, Any]) -> str | None:
    """
    Returns the domain_name of the authenticated user (async version, Redis cache).

    Args:
        current_user: Authenticated user data (from the JWT)

    Returns:
        str: The user's domain name, or None

    Raises:
        HTTPException: If the user is not associated with a domain
    """
    from mail_api.common.redis_client import redis_client

    # Try the domain_name straight from the JWT
    domain_name = current_user.get("domain")
    if domain_name:
        return domain_name

    # No domain_name: look it up by domain_id
    domain_id = get_user_domain_id(current_user)
    if not domain_id:
        logger.warning(
            "[DOMAIN_VALIDATION] Access denied: principal without domain_id "
            "(api_key_id=%s, user_id=%s, channels=%s)",
            current_user.get("api_key_id"),
            current_user.get("user_id") or current_user.get("sub"),
            list((current_user.get("channels") or {}).keys()),
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User is not associated with a domain"
        )

    # Try the Redis cache first
    cache_key = f"domain_name:{domain_id}"
    try:
        cached = await redis_client.get(cache_key)
        if cached:
            logger.debug(f"[DOMAIN_CACHE] Cache hit for domain_name, domain_id={domain_id}")
            return cached
    except Exception as e:
        logger.debug(f"[DOMAIN_CACHE] Error reading the cache: {e}")

    # Cache miss - query the database in an executor (does not block the event loop)
    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            fetch_one,
            "SELECT name FROM virtual_domains WHERE id = %s",
            (domain_id,)
        )
        if not result:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Domain not found"
            )
        domain_value = result.get("name") or None

        # Store in the cache (1 hour TTL)
        if domain_value:
            try:
                await redis_client.setex(cache_key, 3600, domain_value)
                logger.debug(f"[DOMAIN_CACHE] Cached domain_name={domain_value} for domain_id={domain_id}")
            except Exception as e:
                logger.debug(f"[DOMAIN_CACHE] Error writing the cache: {e}")

        return domain_value
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[DOMAIN_VALIDATION] Error fetching domain_name: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error fetching the user's domain"
        ) from e


def get_user_domain_name(current_user: dict[str, Any]) -> str | None:
    """
    Legacy synchronous version of get_user_domain_name.
    DEPRECATED: use get_user_domain_name_async in async code.
    """
    # Try the domain_name straight from the JWT
    domain_name = current_user.get("domain")
    if domain_name:
        return domain_name

    # No domain_name: look it up by domain_id
    domain_id = get_user_domain_id(current_user)
    if not domain_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User is not associated with a domain"
        )

    # Look up domain_name by domain_id (sync - slow)
    try:
        result = fetch_one(
            "SELECT name FROM virtual_domains WHERE id = %s",
            (domain_id,)
        )
        if not result:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Domain not found"
            )
        domain_value = result.get("name") or None
        return domain_value
    except HTTPException:
        raise
    except Exception as e:
        print(f"[DOMAIN_VALIDATION] Error fetching domain_name: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error fetching the user's domain"
        ) from e


async def get_associated_domain_ids_cached(domain_id: int) -> set[int]:
    """
    Redis-cached version of get_associated_domain_ids.
    1 hour cache TTL (the domain structure rarely changes).
    """
    import json

    from mail_api.common.redis_client import redis_client

    cache_key = f"associated_domains:{domain_id}"

    try:
        # Try the cache
        cached = await redis_client.get(cache_key)
        if cached:
            ids = json.loads(cached)
            logger.debug(f"[DOMAIN_CACHE] Cache hit for domain_id={domain_id}: {ids}")
            return set(ids)
    except Exception as e:
        logger.debug(f"[DOMAIN_CACHE] Error reading the cache: {e}")

    # Cache miss - run the synchronous query in an executor
    loop = asyncio.get_running_loop()
    associated_ids = await loop.run_in_executor(
        None, get_associated_domain_ids, domain_id
    )

    # Store in the cache (1 hour TTL)
    try:
        await redis_client.setex(cache_key, 3600, json.dumps(list(associated_ids)))
        logger.debug(f"[DOMAIN_CACHE] Cached domain_id={domain_id}")
    except Exception as e:
        logger.debug(f"[DOMAIN_CACHE] Error writing the cache: {e}")

    return associated_ids


def get_associated_domain_ids(domain_id: int) -> set[int]:
    """
    Returns every associated domain_id, including:
    - The domain_id itself
    - For a subdomain, its main domain
    - For a main domain, all of its subdomains

    Args:
        domain_id: Domain ID (main domain or subdomain)

    Returns:
        set[int]: Valid domain_ids (the domain itself, the main domain and the subdomains)
    """
    try:
        associated_ids = {domain_id}  # Always include the domain_id itself

        # Fetch the domain name
        result = fetch_one(
            "SELECT name FROM virtual_domains WHERE id = %s",
            (domain_id,)
        )
        if not result:
            return associated_ids

        domain_name = result.get('name', '').lower().strip()
        if not domain_name:
            return associated_ids

        # Is it a subdomain (more than 2 dot-separated labels)?
        # If so, look up the main domain
        parts = domain_name.split('.')
        if len(parts) > 2:
            # Subdomain: look up the main domain by dropping the leftmost labels
            # E.g. "sub.example.com" -> "example.com"
            # E.g. "rbm1001.mail.example.com" -> "mail.example.com"
            possible_main_domains = []
            for i in range(1, len(parts)):
                possible_main = '.'.join(parts[i:])
                possible_main_domains.append(possible_main)

            # Look up the main domain (first match in the loop order below)
            main_domain_id = None
            main_domain_name = None
            for possible_main in reversed(possible_main_domains):  # Loop from the shortest candidate to the longest
                main_result = fetch_one(
                    "SELECT id, name FROM virtual_domains WHERE name = %s",
                    (possible_main,)
                )
                if main_result:
                    main_domain_id = main_result.get('id')
                    main_domain_name = main_result.get('name', '').lower().strip()
                    break

            if main_domain_id and main_domain_id != domain_id:
                associated_ids.add(main_domain_id)
                # Now look up the main domain's subdomains as well
                domain_name = main_domain_name
            else:
                # No main domain found: use the domain itself as the main one
                main_domain_name = domain_name

        else:
            # Already the main domain (or not an obvious subdomain)
            main_domain_name = domain_name

        # Look up subdomains by domain suffix
        # Example: with main_domain = "example.com", look for "*.example.com"
        suffix_pattern = f"%.{main_domain_name}"
        subdomains_by_suffix = fetch_all(
            "SELECT id, name FROM virtual_domains WHERE name LIKE %s AND name != %s",
            (suffix_pattern, main_domain_name)
        )
        if subdomains_by_suffix:
            for subdomain in subdomains_by_suffix:
                subdomain_name = subdomain.get('name', '').lower().strip()
                subdomain_id = subdomain.get('id')
                # Make sure it really is a subdomain (ends with .main_domain)
                if subdomain_id and subdomain_name.endswith(f".{main_domain_name}"):
                    associated_ids.add(subdomain_id)

        return associated_ids
    except Exception as e:
        logger.error(f"Error fetching the associated domain_ids: {e}")
        # On error, return at least the domain_id itself
        return {domain_id}


def get_associated_subdomain_id(domain_id: int) -> int | None:
    """
    Returns the first subdomain associated with a main domain (excluding the domain_id itself).

    Args:
        domain_id: Main domain ID

    Returns:
        int: ID of the first subdomain found, or None if there are no subdomains
    """
    try:
        # Fetch the main domain name
        result = fetch_one(
            "SELECT name FROM virtual_domains WHERE id = %s",
            (domain_id,)
        )
        if not result:
            return None

        main_domain_name = result.get('name', '').lower().strip()
        if not main_domain_name:
            return None

        # Look up subdomains by domain suffix
        # Example: with main_domain = "example.com", look for "*.example.com"
        suffix_pattern = f"%.{main_domain_name}"
        subdomains_by_suffix = fetch_all(
            "SELECT id, name FROM virtual_domains WHERE name LIKE %s AND name != %s ORDER BY name ASC LIMIT 1",
            (suffix_pattern, main_domain_name)
        )
        if subdomains_by_suffix:
            for subdomain in subdomains_by_suffix:
                subdomain_name = subdomain.get('name', '').lower().strip()
                subdomain_id = subdomain.get('id')
                # Make sure it really is a subdomain (ends with .main_domain)
                if subdomain_id and subdomain_name.endswith(f".{main_domain_name}"):
                    return subdomain_id

        return None
    except Exception as e:
        logger.error(f"Error fetching the associated subdomain: {e}")
        return None


async def sender_belongs_to_api_key_domain(sender_email: str, api_key_domain_id: int) -> bool:
    """
    Checks whether sender_email belongs to the API key's domain_id or its subdomains.

    Args:
        sender_email: Sender email (e.g. user@example.com)
        api_key_domain_id: ID of the API key's domain

    Returns:
        bool: True if the sender belongs to the API key's domain or its subdomains, False otherwise
    """
    try:
        if not sender_email or "@" not in sender_email or not api_key_domain_id:
            logger.debug(f"[SENDER_BELONGS] Invalid parameters: sender={sender_email}, api_key_domain_id={api_key_domain_id}")
            return False

        # Make sure api_key_domain_id is an int
        try:
            api_key_domain_id = int(api_key_domain_id)
        except (ValueError, TypeError):
            logger.error(f"[SENDER_BELONGS] Invalid api_key_domain_id: {api_key_domain_id}")
            return False

        sender_domain_name = sender_email.split("@")[-1].lower().strip()
        logger.debug(f"[SENDER_BELONGS] sender_domain_name={sender_domain_name}, api_key_domain_id={api_key_domain_id}")

        # Look up the sender's domain_id
        from mail_api.common.email_config_cache import get_domain_id_from_redis
        domain_id = await get_domain_id_from_redis(sender_domain_name)
        logger.debug(f"[SENDER_BELONGS] domain_id from Redis={domain_id}")
        if not domain_id:
            # Fallback: query the database (in an executor, so the event loop is not blocked)
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None,
                fetch_one,
                "SELECT id FROM virtual_domains WHERE name = %s",
                (sender_domain_name,),
            )
            if result:
                domain_id = result.get('id')
            logger.debug(f"[SENDER_BELONGS] domain_id from DB={domain_id}")

        if not domain_id:
            logger.debug(f"[SENDER_BELONGS] Domain {sender_domain_name} not found")
            return False

        # Every domain_id associated with the API key's domain (including subdomains)
        # Uses the Redis-cached version to avoid slow database queries
        associated_domain_ids = await get_associated_domain_ids_cached(api_key_domain_id)
        logger.debug(f"[SENDER_BELONGS] associated_domain_ids={associated_domain_ids}, sender_domain_id={domain_id}")

        # Make sure domain_id is an int so the comparison is correct
        try:
            domain_id = int(domain_id)
        except (ValueError, TypeError):
            pass

        # Is the sender's domain_id among the associated domain_ids?
        result = domain_id in associated_domain_ids
        logger.debug(f"[SENDER_BELONGS] domain_id {domain_id} (type={type(domain_id)}) in associated_domain_ids: {result}")
        return result
    except Exception as e:
        logger.error(f"Error checking whether the sender belongs to the API key's domain: {e}")
        return False


async def _get_api_key_extra_domains_cached(api_key_id: str) -> set[int]:
    """Extra domains authorized for an API Key (api_key_domains table).

    Multi-domain: besides the primary domain (email channel), a key can have N
    domains authorized here. The result is cached in Redis (10 min TTL); the cache
    is invalidated when a domain is added to or removed from the key.
    """
    if not api_key_id:
        return set()

    import json

    from mail_api.common.redis_client import redis_client

    cache_key = f"api_key_domains:{api_key_id}"
    try:
        if redis_client:
            cached = await redis_client.get(cache_key)
            if cached is not None:
                return {int(x) for x in json.loads(cached)}
    except Exception as e:
        logger.debug(f"[API_KEY_DOMAINS] error reading the cache: {e}")

    loop = asyncio.get_running_loop()
    rows = await loop.run_in_executor(
        None,
        fetch_all,
        "SELECT akd.domain_id FROM api_key_domains akd "
        "JOIN api_keys ak ON ak.id = akd.api_key_id "
        "WHERE ak.api_key_id = %s",
        (api_key_id,),
    )
    ids = {int(r["domain_id"]) for r in (rows or []) if r.get("domain_id") is not None}

    try:
        if redis_client:
            await redis_client.setex(cache_key, 600, json.dumps(list(ids)))
    except Exception as e:
        logger.debug(f"[API_KEY_DOMAINS] error writing the cache: {e}")

    return ids


async def get_authorized_domain_ids(current_user: dict[str, Any]) -> set[int]:
    """The set of domain_ids this API Key may send from.

    = primary domain (get_user_domain_id, email channel/legacy) ∪ extra authorized
    domains (api_key_domains). With no rows in api_key_domains, returns only the
    primary domain.
    """
    ids: set[int] = set()
    primary = get_user_domain_id(current_user)
    if primary:
        try:
            ids.add(int(primary))
        except (ValueError, TypeError):
            pass
    api_key_id = current_user.get("api_key_id")
    if api_key_id:
        ids |= await _get_api_key_extra_domains_cached(str(api_key_id))
    return ids


async def validate_authorized_sender(sender_email: str, current_user: dict[str, Any]) -> bool:
    """
    Checks whether sender_email is authorized to send email.

    First checks that the email channel is enabled for the API Key.
    A sender is authorized if:
    1. The email channel is enabled for the API Key, AND
    2. It belongs to the domain configured on the channel (or its subdomains), OR
    3. It is an approved sender for the user's main domain

    Args:
        sender_email: Sender email (e.g. user@example.com)
        current_user: Authenticated user data (from the JWT or API Key)

    Returns:
        bool: True if the sender is authorized, False otherwise
    """
    try:
        if not sender_email or "@" not in sender_email:
            return False

        logger.debug(f"[SENDER_VALIDATION] Validating sender={sender_email}, current_user keys={list(current_user.keys())}")

        # Is the email channel enabled?
        email_enabled = is_email_channel_enabled(current_user)
        logger.debug(f"[SENDER_VALIDATION] is_email_channel_enabled={email_enabled}")
        if not email_enabled:
            logger.warning(
                "[SENDER_VALIDATION] The email channel is not enabled for this user/API Key"
            )
            return False

        # Domains authorized for this API Key:
        # primary domain (email channel/legacy) + extra domains (api_key_domains).
        # Multi-domain: one key can send from several domains of the same customer.
        authorized_domain_ids = await get_authorized_domain_ids(current_user)
        logger.debug(f"[SENDER_VALIDATION] authorized_domain_ids={authorized_domain_ids}")
        if not authorized_domain_ids:
            logger.warning("[SENDER_VALIDATION] The user has no authorized domain on the email channel")
            return False

        # 1) the sender belongs to SOME authorized domain (or its subdomains)
        for did in authorized_domain_ids:
            if await sender_belongs_to_api_key_domain(sender_email, did):
                logger.debug(
                    f"[SENDER_VALIDATION] Sender {sender_email} belongs to an authorized domain (ID: {did})"
                )
                return True

        # 2) the sender is an approved sender under SOME authorized domain
        from mail_api.common.approved_sender_cache import get_approved_sender_info

        for did in authorized_domain_ids:
            approved_sender_info = await get_approved_sender_info(sender_email, did)
            if approved_sender_info and approved_sender_info.get('principal_domain_id') == did:
                logger.debug(
                    f"[SENDER_VALIDATION] Sender {sender_email} is an approved sender for an authorized domain (ID: {did})"
                )
                return True

        logger.debug(
            f"[SENDER_VALIDATION] Sender {sender_email} is NOT authorized for any domain in {authorized_domain_ids}"
        )
        return False
    except Exception as e:
        logger.error(f"Error validating the authorized sender: {e}")
        return False
