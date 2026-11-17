"""
Login Attempt Service - brute force protection

Tracks authentication attempts and blocks after several consecutive failures.
"""

import logging
from dataclasses import dataclass
from typing import Optional, Dict

from .types import IdentifierType, AttemptType, BlockedResult
from .storage import LoginAttemptStorage, MemoryLoginAttemptStorage

logger = logging.getLogger(__name__)


@dataclass
class LoginAttemptConfig:
    """Configuration for one attempt type"""

    max_attempts: int = 5
    window_minutes: int = 15
    block_duration_minutes: int = 15


# Default configuration per operation type
DEFAULT_CONFIGS: Dict[AttemptType, LoginAttemptConfig] = {
    AttemptType.LOGIN: LoginAttemptConfig(
        max_attempts=5,
        window_minutes=15,
        block_duration_minutes=15,
    ),
    AttemptType.SIGNUP: LoginAttemptConfig(
        max_attempts=5,
        window_minutes=15,
        block_duration_minutes=15,
    ),
    AttemptType.FORGOT_PASSWORD: LoginAttemptConfig(
        max_attempts=5,
        window_minutes=60,
        block_duration_minutes=60,
    ),
    AttemptType.RESET_PASSWORD: LoginAttemptConfig(
        max_attempts=5,
        window_minutes=15,
        block_duration_minutes=15,
    ),
    AttemptType.UPDATE_PASSWORD: LoginAttemptConfig(
        max_attempts=5,
        window_minutes=15,
        block_duration_minutes=15,
    ),
    AttemptType.API_KEY_VALIDATION: LoginAttemptConfig(
        max_attempts=10,
        window_minutes=15,
        block_duration_minutes=30,
    ),
    AttemptType.TWO_FACTOR: LoginAttemptConfig(
        max_attempts=5,
        window_minutes=15,
        block_duration_minutes=30,
    ),
}


class LoginAttemptService:
    """
    Main service controlling authentication attempts.

    Usage example:
        from connect_lib.login_attempt import LoginAttemptService, RedisLoginAttemptStorage

        storage = RedisLoginAttemptStorage(redis_client)
        service = LoginAttemptService(storage)

        # Check for a block before the login
        blocked = await service.is_blocked("user@example.com", "email", "login")
        if blocked.blocked:
            raise HTTPException(429, blocked.message)

        # Record the attempt
        if login_success:
            await service.record_attempt("user@example.com", "email", "login", success=True)
            await service.clear_failed_attempts("user@example.com", "email", "login")
        else:
            await service.record_attempt("user@example.com", "email", "login", success=False)
    """

    def __init__(
        self,
        storage: Optional[LoginAttemptStorage] = None,
        configs: Optional[Dict[AttemptType, LoginAttemptConfig]] = None,
    ):
        """
        Initializes the service.

        Args:
            storage: Storage backend (default: MemoryLoginAttemptStorage)
            configs: Custom configuration per operation type
        """
        self.storage = storage or MemoryLoginAttemptStorage()
        self.configs = {**DEFAULT_CONFIGS, **(configs or {})}

    def get_config(self, attempt_type: AttemptType) -> LoginAttemptConfig:
        """Returns the configuration of an attempt type"""
        return self.configs.get(attempt_type, DEFAULT_CONFIGS[AttemptType.LOGIN])

    async def record_attempt(
        self,
        identifier: str,
        identifier_type: IdentifierType | str,
        attempt_type: AttemptType | str,
        success: bool,
        metadata: Optional[dict] = None,
    ) -> Optional[str]:
        """
        Records an authentication attempt.

        Args:
            identifier: Identifier (IP, email, username, etc.)
            identifier_type: Identifier type
            attempt_type: Operation type
            success: Whether the attempt succeeded
            metadata: Additional information (user agent, etc.)

        Returns:
            ID of the recorded attempt, or None on failure
        """
        try:
            # Convert strings to enums if needed
            if isinstance(identifier_type, str):
                identifier_type = IdentifierType(identifier_type)
            if isinstance(attempt_type, str):
                attempt_type = AttemptType(attempt_type)

            logger.debug(
                f"Recording auth attempt: {identifier_type.value}={identifier}, "
                f"type={attempt_type.value}, success={success}"
            )

            attempt_id = await self.storage.save_attempt(
                identifier=identifier,
                identifier_type=identifier_type,
                attempt_type=attempt_type,
                success=success,
                metadata=metadata,
            )

            logger.debug(f"Auth attempt recorded: id={attempt_id}")
            return attempt_id

        except Exception as e:
            logger.error(
                f"Failed to record auth attempt: {e}",
                exc_info=True,
                extra={
                    "identifier": identifier,
                    "identifier_type": str(identifier_type),
                    "attempt_type": str(attempt_type),
                    "success": success,
                },
            )
            # Do not raise, so the authentication flow is never blocked
            return None

    async def is_blocked(
        self,
        identifier: str,
        identifier_type: IdentifierType | str,
        attempt_type: AttemptType | str,
    ) -> BlockedResult:
        """
        Checks whether an identifier is blocked because of too many failed attempts.

        Args:
            identifier: Identifier (IP, email, username, etc.)
            identifier_type: Identifier type
            attempt_type: Operation type

        Returns:
            BlockedResult with the block status
        """
        try:
            # Convert strings to enums if needed
            if isinstance(identifier_type, str):
                identifier_type = IdentifierType(identifier_type)
            if isinstance(attempt_type, str):
                attempt_type = AttemptType(attempt_type)

            config = self.get_config(attempt_type)

            # Count the failed attempts in the window
            failed_attempts = await self.storage.count_failed_attempts(
                identifier=identifier,
                identifier_type=identifier_type,
                attempt_type=attempt_type,
                window_minutes=config.window_minutes,
            )

            if failed_attempts >= config.max_attempts:
                logger.warning(
                    f"Login blocked - too many failed attempts: "
                    f"{identifier_type.value}={identifier}, type={attempt_type.value}, "
                    f"attempts={failed_attempts}/{config.max_attempts}"
                )
                return BlockedResult(
                    blocked=True,
                    message=f"Too many failed attempts. Please try again in {config.block_duration_minutes} minutes.",
                    failed_attempts=failed_attempts,
                    max_attempts=config.max_attempts,
                    retry_after_minutes=config.block_duration_minutes,
                )

            return BlockedResult(
                blocked=False,
                failed_attempts=failed_attempts,
                max_attempts=config.max_attempts,
            )

        except Exception as e:
            logger.error(
                f"Failed to check if identifier is blocked: {e}",
                exc_info=True,
                extra={
                    "identifier": identifier,
                    "identifier_type": str(identifier_type),
                    "attempt_type": str(attempt_type),
                },
            )
            # On error, do not block (fail open) so legitimate logins are never prevented
            return BlockedResult(blocked=False)

    async def clear_failed_attempts(
        self,
        identifier: str,
        identifier_type: IdentifierType | str,
        attempt_type: AttemptType | str,
    ) -> int:
        """
        Clears an identifier's failed attempts after a successful login.

        Args:
            identifier: Identifier (IP, email, username, etc.)
            identifier_type: Identifier type
            attempt_type: Operation type

        Returns:
            Number of records removed
        """
        try:
            # Convert strings to enums if needed
            if isinstance(identifier_type, str):
                identifier_type = IdentifierType(identifier_type)
            if isinstance(attempt_type, str):
                attempt_type = AttemptType(attempt_type)

            removed = await self.storage.delete_failed_attempts(
                identifier=identifier,
                identifier_type=identifier_type,
                attempt_type=attempt_type,
            )

            if removed > 0:
                logger.debug(
                    f"Cleared {removed} failed attempts for "
                    f"{identifier_type.value}={identifier}, type={attempt_type.value}"
                )

            return removed

        except Exception as e:
            logger.error(
                f"Failed to clear failed attempts: {e}",
                exc_info=True,
                extra={
                    "identifier": identifier,
                    "identifier_type": str(identifier_type),
                    "attempt_type": str(attempt_type),
                },
            )
            # Do not raise, so the authentication flow is never blocked
            return 0

    async def cleanup_old_attempts(self, days_to_keep: int = 7) -> int:
        """
        Removes old attempts from the storage.

        Args:
            days_to_keep: Number of days to keep

        Returns:
            Number of records removed
        """
        try:
            removed = await self.storage.delete_old_attempts(days_to_keep)
            if removed > 0:
                logger.info(f"Cleaned up {removed} old auth attempts")
            return removed
        except Exception as e:
            logger.error(f"Failed to cleanup old auth attempts: {e}", exc_info=True)
            return 0

    async def get_recent_attempts(
        self,
        identifier: str,
        identifier_type: IdentifierType | str,
        attempt_type: AttemptType | str,
        limit: int = 10,
    ) -> list:
        """
        Returns the recent attempts of an identifier.

        Args:
            identifier: Identifier (IP, email, username, etc.)
            identifier_type: Identifier type
            attempt_type: Operation type
            limit: Maximum number of records

        Returns:
            List of LoginAttempt
        """
        try:
            # Convert strings to enums if needed
            if isinstance(identifier_type, str):
                identifier_type = IdentifierType(identifier_type)
            if isinstance(attempt_type, str):
                attempt_type = AttemptType(attempt_type)

            return await self.storage.get_recent_attempts(
                identifier=identifier,
                identifier_type=identifier_type,
                attempt_type=attempt_type,
                limit=limit,
            )
        except Exception as e:
            logger.error(f"Failed to get recent attempts: {e}", exc_info=True)
            return []


# Global service instance (for the convenience functions)
_global_service: Optional[LoginAttemptService] = None


def get_service() -> Optional[LoginAttemptService]:
    """Returns the global service instance"""
    return _global_service


def set_service(service: LoginAttemptService) -> None:
    """Sets the global service instance"""
    global _global_service
    _global_service = service
    logger.info("Login attempt service configured globally")


# Convenience functions that use the global service
async def record_attempt(
    identifier: str,
    identifier_type: IdentifierType | str,
    attempt_type: AttemptType | str,
    success: bool,
    metadata: Optional[dict] = None,
) -> Optional[str]:
    """
    Records an authentication attempt with the global service.

    Make sure set_service() was called before using this function.
    """
    service = get_service()
    if service is None:
        logger.warning(
            "Login attempt service not configured. Call set_service() first."
        )
        return None
    return await service.record_attempt(
        identifier, identifier_type, attempt_type, success, metadata
    )


async def is_blocked(
    identifier: str,
    identifier_type: IdentifierType | str,
    attempt_type: AttemptType | str,
) -> BlockedResult:
    """
    Checks whether an identifier is blocked, with the global service.

    Make sure set_service() was called before using this function.
    """
    service = get_service()
    if service is None:
        logger.warning(
            "Login attempt service not configured. Call set_service() first."
        )
        return BlockedResult(blocked=False)
    return await service.is_blocked(identifier, identifier_type, attempt_type)


async def clear_failed_attempts(
    identifier: str,
    identifier_type: IdentifierType | str,
    attempt_type: AttemptType | str,
) -> int:
    """
    Clears failed attempts with the global service.

    Make sure set_service() was called before using this function.
    """
    service = get_service()
    if service is None:
        logger.warning(
            "Login attempt service not configured. Call set_service() first."
        )
        return 0
    return await service.clear_failed_attempts(
        identifier, identifier_type, attempt_type
    )


async def cleanup_old_attempts(days_to_keep: int = 7) -> int:
    """
    Removes old attempts with the global service.

    Make sure set_service() was called before using this function.
    """
    service = get_service()
    if service is None:
        logger.warning(
            "Login attempt service not configured. Call set_service() first."
        )
        return 0
    return await service.cleanup_old_attempts(days_to_keep)
