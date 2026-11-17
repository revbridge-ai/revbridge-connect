"""
Connect Rate Limiting Module

Rate limiting for FastAPI APIs, with support for:
- Sliding window algorithm
- Storage backends: Redis (distributed) and Memory (local)
- Configuration per endpoint, IP, API Key or User
- Decorators and FastAPI dependencies for easy integration
"""

from .limiter import (
    RateLimiter,
    RateLimitConfig,
    RateLimitExceeded,
    RateLimitKeyType,
    RateLimitResult,
)
from .storage import (
    RateLimitStorage,
    RedisStorage,
    MemoryStorage,
    get_storage,
)
from .decorator import rate_limit, RateLimitDependency, set_decorator_storage

__all__ = [
    # Core
    "RateLimiter",
    "RateLimitConfig",
    "RateLimitExceeded",
    "RateLimitKeyType",
    "RateLimitResult",
    # Storage
    "RateLimitStorage",
    "RedisStorage",
    "MemoryStorage",
    "get_storage",
    # Decorator
    "rate_limit",
    "RateLimitDependency",
    "set_decorator_storage",
]
