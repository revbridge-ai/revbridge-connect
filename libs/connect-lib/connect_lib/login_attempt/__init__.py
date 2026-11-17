"""
Connect Login Attempt Module

Brute force protection for authentication.
Tracks failed login attempts and blocks after several failures.

Features:
- Tracking by IP and/or email/identifier
- Flexible limits per operation type
- Storage backends: Redis (fast) and in-memory
- Automatic cleanup of old records
"""

from .service import (
    LoginAttemptService,
    LoginAttemptConfig,
    record_attempt,
    is_blocked,
    clear_failed_attempts,
    cleanup_old_attempts,
    get_service,
    set_service,
)
from .types import (
    IdentifierType,
    AttemptType,
    LoginAttempt,
    BlockedResult,
)
from .storage import (
    LoginAttemptStorage,
    RedisLoginAttemptStorage,
    MemoryLoginAttemptStorage,
)

__all__ = [
    # Service
    "LoginAttemptService",
    "LoginAttemptConfig",
    "record_attempt",
    "is_blocked",
    "clear_failed_attempts",
    "cleanup_old_attempts",
    "get_service",
    "set_service",
    # Types
    "IdentifierType",
    "AttemptType",
    "LoginAttempt",
    "BlockedResult",
    # Storage
    "LoginAttemptStorage",
    "RedisLoginAttemptStorage",
    "MemoryLoginAttemptStorage",
]
