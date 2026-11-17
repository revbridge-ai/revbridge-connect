"""
Abstract interface for API Key validation

This module defines an interface that the projects using this library must implement,
which allows different database implementations.
"""

from abc import ABC, abstractmethod
from typing import Optional, Dict, Any
import logging

logger = logging.getLogger(__name__)


class ApiKeyValidator(ABC):
    """
    Abstract interface for API Key validation.

    Each project must implement this interface with its own database access logic
    (asynchronous or synchronous).
    """

    def __init__(self, key_prefix: str = "rbak_"):
        """
        Initializes the API Key validator

        Args:
            key_prefix: Prefix expected in the API Keys (default: "rbak_")
        """
        self.key_prefix = key_prefix

    @abstractmethod
    def validate_api_key(self, api_key: str) -> Optional[Dict[str, Any]]:
        """
        Validates an API Key and returns the data of the user or service account

        Args:
            api_key: The API key to validate

        Returns:
            Dict with the user's data if valid, None otherwise.
            The dict must contain at least:
            - user_id or service_account_id
            - permissions (list of strings)
            - domain_id (optional)
            - username/email (for users) or service_account_name (for service accounts)
            - is_service_account (bool)
        """
        pass

# Global validator instance (must be configured by the project)
_api_key_validator: Optional[ApiKeyValidator] = None


def set_api_key_validator(validator: ApiKeyValidator):
    """
    Configures the API Key validator to use

    Args:
        validator: ApiKeyValidator instance
    """
    global _api_key_validator
    _api_key_validator = validator


def validate_api_key(api_key: str) -> Optional[Dict[str, Any]]:
    """
    Validates an API Key with the configured validator (synchronous)

    Args:
        api_key: The API key to validate

    Returns:
        Dict with the user's data if valid, None otherwise

    Raises:
        RuntimeError: If no validator was configured
    """
    if _api_key_validator is None:
        raise RuntimeError(
            "ApiKeyValidator is not configured. Use set_api_key_validator() to configure it."
        )

    return _api_key_validator.validate_api_key(api_key)


async def validate_api_key_async(api_key: str) -> Optional[Dict[str, Any]]:
    """
    Validates an API Key with the configured validator (asynchronous)

    Args:
        api_key: The API key to validate

    Returns:
        Dict with the user's data if valid, None otherwise

    Raises:
        RuntimeError: If no validator was configured
    """
    if _api_key_validator is None:
        raise RuntimeError(
            "ApiKeyValidator is not configured. Use set_api_key_validator() to configure it."
        )

    # If the validator has an async method, await it
    import inspect

    if inspect.iscoroutinefunction(_api_key_validator.validate_api_key):
        return await _api_key_validator.validate_api_key(api_key)
    else:
        # If it is synchronous, call it directly
        return _api_key_validator.validate_api_key(api_key)
