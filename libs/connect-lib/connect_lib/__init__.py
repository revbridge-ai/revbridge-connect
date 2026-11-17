"""
Connect library — the shared code of RevBridge Connect (authentication, API keys, events).

Available modules:
- auth: JWT, API Key and hybrid authentication
- rate_limit: rate limiting for FastAPI APIs
- api_key: API Key management
- user: user management
- login_attempt: login brute force protection
- messaging: normalized events and the event outbox format
- logging: standardized logging with JSON support
- observability: OpenTelemetry (traces, metrics, logs), opt-in
- redis_client: Redis client factory with hardened defaults
"""

__version__ = "0.1.0"

from . import auth  # noqa: F401
from . import rate_limit  # noqa: F401
from . import logging  # noqa: F401
from . import observability  # noqa: F401
from . import redis_client  # noqa: F401
