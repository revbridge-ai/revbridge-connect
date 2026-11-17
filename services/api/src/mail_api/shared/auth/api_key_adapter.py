"""
Adapter that plugs API Key validation into connect-lib

API keys are validated locally, against the installation's MySQL (mail_api.services.api_key_service).
"""
import logging
import os

logger = logging.getLogger(__name__)

try:
    from connect_lib.auth.api_key import ApiKeyValidator, set_api_key_validator

    from mail_api.services.api_key_service import api_key_service
    _LOCAL_VALIDATOR_AVAILABLE = True
except ImportError:
    _LOCAL_VALIDATOR_AVAILABLE = False


class TrackingApiKeyValidator(ApiKeyValidator):
    """Local API Key validator"""

    async def validate_api_key(self, api_key: str):
        """Validates the API Key with the local service"""
        return await api_key_service.validate_api_key(api_key)


def setup_auth():
    """
    Configures authentication for the project: the local API Key validator.
    """
    if _LOCAL_VALIDATOR_AVAILABLE:
        set_api_key_validator(TrackingApiKeyValidator())
        logger.info("Authentication configured (local TrackingApiKeyValidator)")
    else:
        logger.error("No API Key validator available!")
