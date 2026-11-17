"""
Adapter between api_key_service and connect-lib

Connects the project's API key service to the abstract interface
that connect-lib expects.
"""
from typing import Any

from connect_lib.auth.api_key import ApiKeyValidator, set_api_key_validator

from mail_tracking.services.api_key_service import api_key_service


class TrackingApiKeyValidator(ApiKeyValidator):
    """API key validator of the tracking service"""

    async def validate_api_key(self, api_key: str) -> dict[str, Any] | None:
        """
        Validate an API key with the library service

        Args:
            api_key: The API key to validate

        Returns:
            Dict with the user data if valid, None otherwise
        """
        return await api_key_service.validate_api_key(api_key)


def setup_auth():
    """
    Configure authentication for the project

    Call this at application startup, before adding the
    authentication middleware.
    """
    set_api_key_validator(TrackingApiKeyValidator())
