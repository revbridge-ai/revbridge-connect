"""
API authentication router

Uses the base router of the connect-lib library.
The library uses its own database functions.
"""

from typing import Any

from mail_api.shared.domain_validation import get_user_domain_id, validate_domain_id_exists
from connect_lib.auth.router import create_auth_router
from connect_lib.user.service import UserService

# Single instance of connect-lib's user service
user_service = UserService()


def validate_credentials(email: str, password: str) -> dict[str, Any] | None:
    """
    Validates credentials directly with connect-lib's UserService.

    Kept as a separate function only because create_auth_router
    expects a callable with this signature.
    """
    return user_service.authenticate_user(email, password)


# Build the router with the library, using connect-lib's functions directly
auth_router = create_auth_router(
    validate_credentials=validate_credentials,
    create_user_func=user_service.create_user,
    update_user_password_func=user_service.update_user_password,
    get_user_domain_id=get_user_domain_id,
    validate_domain_id=validate_domain_id_exists,
    prefix="/auth",
    tags=["authentication"],
)
