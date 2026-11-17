"""
Rate Limit Decorator for specific endpoints
"""

import functools
import logging
from typing import Optional, Callable
from fastapi import HTTPException, Request

from .limiter import RateLimiter, RateLimitConfig
from .storage import RateLimitStorage, MemoryStorage, get_default_storage

logger = logging.getLogger(__name__)

# Global storage for the decorators
_decorator_storage: Optional[RateLimitStorage] = None
_decorator_limiters: dict = {}


def set_decorator_storage(storage: RateLimitStorage) -> None:
    """Sets the global storage for the decorators"""
    global _decorator_storage
    _decorator_storage = storage


def get_limiter_for_config(config: RateLimitConfig) -> RateLimiter:
    """Gets or creates a limiter for a specific configuration"""
    # Unique key for this configuration
    config_key = f"{config.requests}:{config.window}:{config.key_type}"

    if config_key not in _decorator_limiters:
        storage = _decorator_storage or get_default_storage() or MemoryStorage()
        _decorator_limiters[config_key] = RateLimiter(storage, config)

    return _decorator_limiters[config_key]


def rate_limit(
    requests: int = 100,
    window: int = 60,
    key_type: str = "ip",
    key_func: Optional[Callable[[Request], str]] = None,
    error_message: str = "Too many requests for this endpoint",
) -> Callable:
    """
    Decorator that applies rate limiting to specific endpoints.

    It lets each endpoint have its own rate limit, overriding the middleware's global
    rate limit.

    Usage example:
        from connect_lib.rate_limit import rate_limit

        @app.get("/expensive-operation")
        @rate_limit(requests=10, window=60)  # 10 req/min
        async def expensive_operation(request: Request):
            return {"result": "ok"}

        @app.post("/send-email")
        @rate_limit(requests=5, window=3600, key_type="api_key")  # 5 req/hour per API key
        async def send_email(request: Request):
            return {"sent": True}

    Args:
        requests: Maximum number of requests allowed
        window: Time window in seconds
        key_type: Key type ("ip", "api_key", "user_id", "account_id")
        key_func: Custom function that extracts the key
        error_message: Custom error message

    Returns:
        Decorator function
    """
    from .limiter import RateLimitKeyType

    # Map the string to the enum
    key_type_enum = {
        "ip": RateLimitKeyType.IP,
        "api_key": RateLimitKeyType.API_KEY,
        "user_id": RateLimitKeyType.USER_ID,
        "account_id": RateLimitKeyType.ACCOUNT_ID,
        "custom": RateLimitKeyType.CUSTOM,
    }.get(key_type, RateLimitKeyType.IP)

    config = RateLimitConfig(
        requests=requests,
        window=window,
        key_type=key_type_enum,
        key_prefix="connect:ratelimit:endpoint",
        error_message=error_message,
        exclude_paths=[],  # The decorator must not exclude paths
    )

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            # Find the Request object among the arguments
            request = None
            for arg in args:
                if isinstance(arg, Request):
                    request = arg
                    break
            if request is None:
                request = kwargs.get("request")

            if request is None:
                logger.warning(
                    f"Rate limit decorator on {func.__name__}: "
                    "Request object not found. Skipping rate limit check."
                )
                return await func(*args, **kwargs)

            # Get the limiter
            limiter = get_limiter_for_config(config)

            # Build a unique key that includes the endpoint
            if key_func:
                base_key = key_func(request)
            else:
                base_key = limiter.build_key(request)

            endpoint_key = f"{func.__name__}:{base_key}"

            # Check the rate limit
            result = await limiter.check(endpoint_key)

            if not result.allowed:
                logger.warning(
                    f"Rate limit exceeded for endpoint={func.__name__}, key={base_key}"
                )
                raise HTTPException(
                    status_code=429,
                    detail={
                        "error": "rate_limit_exceeded",
                        "message": error_message,
                        "limit": result.limit,
                        "window": window,
                        "retry_after": result.retry_after,
                    },
                    headers={
                        "X-RateLimit-Limit": str(result.limit),
                        "X-RateLimit-Remaining": str(max(0, result.remaining)),
                        "X-RateLimit-Reset": str(int(result.reset_at)),
                        "Retry-After": str(result.retry_after),
                    },
                )

            return await func(*args, **kwargs)

        return wrapper

    return decorator


class RateLimitDependency:
    """
    Dependency injection for rate limiting in FastAPI.

    Usage example:
        from connect_lib.rate_limit import RateLimitDependency

        rate_limiter = RateLimitDependency(requests=10, window=60)

        @app.get("/endpoint")
        async def endpoint(request: Request, _: None = Depends(rate_limiter)):
            return {"result": "ok"}
    """

    def __init__(
        self,
        requests: int = 100,
        window: int = 60,
        key_type: str = "ip",
        key_func: Optional[Callable[[Request], str]] = None,
        error_message: str = "Too many requests",
    ):
        from .limiter import RateLimitKeyType

        key_type_enum = {
            "ip": RateLimitKeyType.IP,
            "api_key": RateLimitKeyType.API_KEY,
            "user_id": RateLimitKeyType.USER_ID,
            "account_id": RateLimitKeyType.ACCOUNT_ID,
            "custom": RateLimitKeyType.CUSTOM,
        }.get(key_type, RateLimitKeyType.IP)

        self.config = RateLimitConfig(
            requests=requests,
            window=window,
            key_type=key_type_enum,
            key_prefix="connect:ratelimit:dep",
            error_message=error_message,
            exclude_paths=[],
        )
        self.key_func = key_func
        self.error_message = error_message
        self._limiter = None

    @property
    def limiter(self) -> RateLimiter:
        if self._limiter is None:
            self._limiter = get_limiter_for_config(self.config)
        return self._limiter

    async def __call__(self, request: Request) -> None:
        if self.key_func:
            key = self.key_func(request)
        else:
            key = self.limiter.build_key(request)

        result = await self.limiter.check(key)

        if not result.allowed:
            raise HTTPException(
                status_code=429,
                detail={
                    "error": "rate_limit_exceeded",
                    "message": self.error_message,
                    "limit": result.limit,
                    "window": self.config.window,
                    "retry_after": result.retry_after,
                },
                headers={
                    "X-RateLimit-Limit": str(result.limit),
                    "X-RateLimit-Remaining": str(max(0, result.remaining)),
                    "X-RateLimit-Reset": str(int(result.reset_at)),
                    "Retry-After": str(result.retry_after),
                },
            )
