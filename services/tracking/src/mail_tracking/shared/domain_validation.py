"""
Domain lookups for the tracking service

Converts a domain_name into its domain_id (virtual_domains).
"""

import logging

from mail_tracking.common.db import fetch_one

logger = logging.getLogger(__name__)


def get_domain_id_from_name(domain_name: str) -> int | None:
    """
    Get the domain_id from the domain name

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
        print(f"[DOMAIN_VALIDATION] Error looking up the domain_id of {domain_name}: {e}")
        return None
