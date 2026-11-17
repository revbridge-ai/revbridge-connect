"""
Shared authentication module
"""

from .jwt import jwt_manager, JWTManager, create_access_token
from .hybrid import (
    HybridAuth,
    hybrid_auth,
    get_current_user_hybrid,
    get_auth_type,
    check_hybrid_permission,
    hybrid_auth_middleware,
)
from .api_key import ApiKeyValidator, validate_api_key, validate_api_key_async, set_api_key_validator
from .router import create_auth_router

__all__ = [
    # JWT
    "jwt_manager",
    "JWTManager",
    "create_access_token",
    # Hybrid Auth
    "HybridAuth",
    "hybrid_auth",
    "get_current_user_hybrid",
    "get_auth_type",
    "check_hybrid_permission",
    "hybrid_auth_middleware",
    # API Key
    "ApiKeyValidator",
    "validate_api_key",
    "validate_api_key_async",
    "set_api_key_validator",
    # Router
    "create_auth_router",
]
