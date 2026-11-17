"""
API Key router

Uses the base router of the connect-lib library.
"""

import os

from mail_api.services.api_key_service import api_key_service
from mail_api.shared.domain_validation import (
    get_user_domain_id,
    get_user_domain_name,
    validate_domain_id_exists,
)
from mail_api.shared.tenant_scope import caller_client_id, domain_owned_by_client
from connect_lib.api_key.models import ApiKeyCreateAdmin
from connect_lib.api_key.router import create_api_key_router


async def _domain_owned(domain_id: int, current_user: dict) -> bool:
    return await domain_owned_by_client(domain_id, caller_client_id(current_user))


# Build the router with the library (includes the /admin/create endpoint when configured)
api_key_router = create_api_key_router(
    api_key_service=api_key_service,
    prefix="/api-keys",
    tags=["api-keys"],
    get_user_domain_id=get_user_domain_id,
    validate_domain_id=validate_domain_id_exists,
    get_user_domain_name=get_user_domain_name,
    # Admin domain (may create API Keys for other clients): only if the operator sets
    # CONNECT_ADMIN_DOMAIN. Without it, the /admin/create endpoint does not exist.
    admin_domain_name=os.getenv("CONNECT_ADMIN_DOMAIN") or None,
    api_key_create_with_domain_model=ApiKeyCreateAdmin,  # Model with a required client_id and an optional domain_id
    domain_owned=_domain_owned,
)
