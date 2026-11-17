"""
Core Rate Limiter - Sliding Window Algorithm
"""

import time
from dataclasses import dataclass, field
from typing import Optional, Dict, Any
from enum import Enum


class RateLimitExceeded(Exception):
    """Exception raised when the rate limit is exceeded"""

    def __init__(
        self,
        message: str = "Rate limit exceeded",
        limit: int = 0,
        window: int = 0,
        retry_after: int = 0,
    ):
        self.message = message
        self.limit = limit
        self.window = window
        self.retry_after = retry_after
        super().__init__(self.message)


class RateLimitKeyType(str, Enum):
    """Key types used to identify the client"""

    IP = "ip"
    API_KEY = "api_key"
    USER_ID = "user_id"
    ACCOUNT_ID = "account_id"
    CUSTOM = "custom"


@dataclass
class RateLimitConfig:
    """Rate limiting configuration"""

    # Maximum number of requests allowed
    requests: int = 100

    # Time window in seconds
    window: int = 60

    # Key type used for identification
    key_type: RateLimitKeyType = RateLimitKeyType.IP

    # Custom key prefix
    key_prefix: str = "connect:ratelimit"

    # Paths ignored by the rate limiter
    exclude_paths: list = field(
        default_factory=lambda: ["/health", "/docs", "/redoc", "/openapi.json"]
    )

    # Rate limit info headers
    include_headers: bool = True

    # If True, blocks; if False, only logs
    enforce: bool = True

    # Custom error message
    error_message: str = "Too many requests. Please try again later."

    # If True, uses burst mode (allows small peaks)
    burst_mode: bool = False
    burst_multiplier: float = 1.5

    def get_burst_limit(self) -> int:
        """Returns the limit, taking burst mode into account"""
        if self.burst_mode:
            return int(self.requests * self.burst_multiplier)
        return self.requests


@dataclass
class RateLimitResult:
    """Result of a rate limit check"""

    allowed: bool
    remaining: int
    limit: int
    reset_at: float
    retry_after: int = 0

    def to_headers(self) -> Dict[str, str]:
        """Converts to the standard HTTP headers"""
        return {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(max(0, self.remaining)),
            "X-RateLimit-Reset": str(int(self.reset_at)),
        }


class RateLimiter:
    """
    Rate Limiter using the Sliding Window Log algorithm.

    The algorithm keeps a log of request timestamps and counts how many requests happened
    within the current time window.
    """

    def __init__(self, storage, config: Optional[RateLimitConfig] = None):
        """
        Initializes the rate limiter.

        Args:
            storage: Storage backend (Redis or Memory)
            config: Rate limiter configuration
        """
        self.storage = storage
        self.config = config or RateLimitConfig()

    async def check(self, key: str) -> RateLimitResult:
        """
        Checks whether a request is allowed for the given key.

        Args:
            key: Unique client identifier (IP, API Key, etc.)

        Returns:
            RateLimitResult with the rate limit information
        """
        now = time.time()
        window_start = now - self.config.window

        # Full key with the prefix
        full_key = f"{self.config.key_prefix}:{key}"

        # Get the current count and add the new request
        count = await self.storage.increment(
            key=full_key,
            window_start=window_start,
            window_size=self.config.window,
            timestamp=now,
        )

        limit = self.config.get_burst_limit()
        remaining = limit - count
        reset_at = now + self.config.window
        allowed = count <= limit

        retry_after = 0
        if not allowed:
            retry_after = int(self.config.window)

        return RateLimitResult(
            allowed=allowed,
            remaining=remaining,
            limit=limit,
            reset_at=reset_at,
            retry_after=retry_after,
        )

    async def reset(self, key: str) -> None:
        """Removes every request recorded for a key"""
        full_key = f"{self.config.key_prefix}:{key}"
        await self.storage.reset(full_key)

    def build_key(self, request: Any) -> str:
        """
        Builds the rate limiting key from the request.

        Args:
            request: FastAPI request object

        Returns:
            Unique client identifier string
        """
        key_type = self.config.key_type

        if key_type == RateLimitKeyType.IP:
            # Try to get the real IP from the proxy headers
            forwarded = request.headers.get("X-Forwarded-For")
            if forwarded:
                return forwarded.split(",")[0].strip()
            real_ip = request.headers.get("X-Real-IP")
            if real_ip:
                return real_ip
            return request.client.host if request.client else "unknown"

        elif key_type == RateLimitKeyType.API_KEY:
            # Extract the API key from the header or the query param
            api_key = request.headers.get("X-API-Key") or request.headers.get(
                "Authorization", ""
            ).replace("Bearer ", "")
            if not api_key:
                api_key = request.query_params.get("api_key", "")
            return api_key or "anonymous"

        elif key_type == RateLimitKeyType.USER_ID:
            # Try to get the user_id from the request state
            user = getattr(request.state, "user", None)
            if user and hasattr(user, "user_id"):
                return str(user.user_id)
            return self.build_key_by_ip(request)

        elif key_type == RateLimitKeyType.ACCOUNT_ID:
            # Try to get the account_id from the request state
            user = getattr(request.state, "user", None)
            if user and hasattr(user, "account_id"):
                return str(user.account_id)
            return self.build_key_by_ip(request)

        elif key_type == RateLimitKeyType.CUSTOM:
            # For custom, return a combination of IP + path
            ip = self.build_key_by_ip(request)
            return f"{ip}:{request.url.path}"

        return "default"

    def build_key_by_ip(self, request: Any) -> str:
        """Helper that extracts the request's IP"""
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
        real_ip = request.headers.get("X-Real-IP")
        if real_ip:
            return real_ip
        return request.client.host if request.client else "unknown"
