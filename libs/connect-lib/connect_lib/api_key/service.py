"""
Abstract interface for API Key services

Each project must implement this interface with its own database access logic.
"""

from abc import ABC, abstractmethod
from typing import List, Optional
from .models import (
    ApiKeyCreate,
    ApiKeyUpdate,
    ApiKeyResponse,
    ApiKeyCreateResponse,
    ApiKeyListResponse,
    ApiKeyFilter,
    ApiKeyUsageStats,
)
from .utils import generate_api_key, hash_api_key, generate_api_key_id


class ApiKeyServiceBase(ABC):
    """
    Abstract interface for API Key services.

    Each project must implement this interface with its own database access logic
    (asynchronous or synchronous).
    """

    def __init__(self, key_prefix: str = "rbak_"):
        """
        Initializes the API Key service

        Args:
            key_prefix: API Key prefix (default: "rbak_")
        """
        self.key_prefix = key_prefix

    # Utility methods (implemented)
    def _generate_api_key(self) -> str:
        """Generates a new API Key"""
        return generate_api_key(self.key_prefix)

    def _hash_api_key(self, api_key: str) -> str:
        """Hashes the API Key for safe storage"""
        return hash_api_key(api_key)

    def _generate_api_key_id(self) -> str:
        """Generates a unique ID for the API Key"""
        return generate_api_key_id()

    # Abstract methods (the projects must implement them)
    @abstractmethod
    async def create_api_key(
        self, api_key_data: ApiKeyCreate, user_id: str, domain_id: Optional[int] = None
    ) -> ApiKeyCreateResponse:
        """
        Creates a new API Key

        Args:
            api_key_data: Data of the API Key to create
            user_id: ID of the owning user
            domain_id: Domain ID (optional)

        Returns:
            ApiKeyCreateResponse with the actual key (only at creation)
        """
        pass

    @abstractmethod
    async def get_api_key_by_id(
        self, api_key_id: int, client_id: str
    ) -> Optional[ApiKeyResponse]:
        """
        Gets an API Key by ID (scope: customer/client_id).

        SECURITY: filtering is by client_id, not user_id. user_id identifies
        the row's creator, which for admin-provisioned keys collapses across
        many customers and leaks rows across tenants. client_id is the real
        tenant boundary.

        Args:
            api_key_id: API Key ID
            client_id: Customer UUID (tenant boundary)

        Returns:
            ApiKeyResponse, or None if not found
        """
        pass

    @abstractmethod
    async def list_api_keys(
        self,
        client_id: str,
        page: int = 1,
        limit: int = 50,
        filters: Optional[ApiKeyFilter] = None,
    ) -> ApiKeyListResponse:
        """
        Lists the customer's API Keys (scope: client_id). See get_api_key_by_id.

        Args:
            client_id: Customer UUID (tenant boundary)
            page: Page number
            limit: Items per page
            filters: Optional filters

        Returns:
            ApiKeyListResponse with the paginated list
        """
        pass

    @abstractmethod
    async def update_api_key(
        self, api_key_id: int, client_id: str, update_data: ApiKeyUpdate
    ) -> Optional[ApiKeyResponse]:
        """
        Updates an API Key (scope: client_id). See get_api_key_by_id.

        Args:
            api_key_id: API Key ID
            client_id: Customer UUID (tenant boundary)
            update_data: Update data

        Returns:
            The updated ApiKeyResponse, or None if not found
        """
        pass

    @abstractmethod
    async def delete_api_key(self, api_key_id: int, client_id: str) -> bool:
        """
        Deletes an API Key (scope: client_id). See get_api_key_by_id.

        Args:
            api_key_id: API Key ID
            client_id: Customer UUID (tenant boundary)

        Returns:
            True if deleted, False otherwise
        """
        pass

    @abstractmethod
    async def validate_api_key(self, api_key: str) -> Optional[dict]:
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

    @abstractmethod
    async def get_usage_stats(self, client_id: str) -> List[ApiKeyUsageStats]:
        """
        Returns the API Keys' usage statistics (scope: client_id).
        See get_api_key_by_id for rationale.

        Args:
            client_id: Customer UUID (tenant boundary)

        Returns:
            List of ApiKeyUsageStats
        """
        pass
