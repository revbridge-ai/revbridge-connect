"""
API Key management module

This module provides shared functionality for:
- Generating and hashing API Keys
- Base Pydantic models
- An abstract interface for API Key services
- A base FastAPI router for the API Key endpoints
"""

from .utils import (
    generate_api_key,
    hash_api_key,
    generate_api_key_id,
    verify_api_key_hash,
    generate_lookup_hash,
    SECRET_KEY_PREFIX,
)
from .models import (
    ApiKeyPermission,
    ApiKeyCreate,
    ApiKeyCreateWithDomain,
    ApiKeyCreateAdmin,
    ApiKeyUpdate,
    ApiKeyResponse,
    ApiKeyCreateResponse,
    ApiKeyListResponse,
    ApiKeyFilter,
    ApiKeyUsageStats,
    # Channel models
    ChannelType,
    ChannelCreate,
    ChannelResponse,
    ChannelUpdate,
    ChannelBulkCreate,
    ChannelBulkResult,
    ChannelBulkResponse,
)
from .service import ApiKeyServiceBase
from .service_impl import ApiKeyService
from .router import create_api_key_router
from .cache import (
    init_api_key_cache,
    set_api_key_cache,
    get_api_key_cache,
    get_cached_api_key,
    set_cached_api_key,
    invalidate_cached_api_key,
    clear_api_key_cache,
    ApiKeyCacheStorage,
    MemoryCacheStorage,
    RedisCacheStorage,
    DEFAULT_CACHE_TTL,
)

__all__ = [
    # Utils
    "generate_api_key",
    "hash_api_key",
    "generate_api_key_id",
    "verify_api_key_hash",
    "generate_lookup_hash",
    "SECRET_KEY_PREFIX",
    # Models
    "ApiKeyPermission",
    "ApiKeyCreate",
    "ApiKeyCreateWithDomain",
    "ApiKeyCreateAdmin",
    "ApiKeyUpdate",
    "ApiKeyResponse",
    "ApiKeyCreateResponse",
    "ApiKeyListResponse",
    "ApiKeyFilter",
    "ApiKeyUsageStats",
    # Channel models
    "ChannelType",
    "ChannelCreate",
    "ChannelResponse",
    "ChannelUpdate",
    "ChannelBulkCreate",
    "ChannelBulkResult",
    "ChannelBulkResponse",
    # Service
    "ApiKeyServiceBase",
    "ApiKeyService",
    # Router
    "create_api_key_router",
    # Cache
    "init_api_key_cache",
    "set_api_key_cache",
    "get_api_key_cache",
    "get_cached_api_key",
    "set_cached_api_key",
    "invalidate_cached_api_key",
    "clear_api_key_cache",
    "ApiKeyCacheStorage",
    "MemoryCacheStorage",
    "RedisCacheStorage",
    "DEFAULT_CACHE_TTL",
]
