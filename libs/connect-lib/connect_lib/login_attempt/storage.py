"""
Storage Backends for Login Attempt Tracking

Supports:
- Redis (shared by every instance)
- Memory (for development/tests)
"""

import time
import json
import asyncio
import uuid
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Optional, Dict, List
from collections import defaultdict

from .types import IdentifierType, AttemptType, LoginAttempt

logger = logging.getLogger(__name__)


class LoginAttemptStorage(ABC):
    """Abstract interface for storage backends"""

    @abstractmethod
    async def save_attempt(
        self,
        identifier: str,
        identifier_type: IdentifierType,
        attempt_type: AttemptType,
        success: bool,
        metadata: Optional[dict] = None,
    ) -> str:
        """
        Saves an authentication attempt.

        Returns:
            ID of the recorded attempt
        """
        pass

    @abstractmethod
    async def count_failed_attempts(
        self,
        identifier: str,
        identifier_type: IdentifierType,
        attempt_type: AttemptType,
        window_minutes: int,
    ) -> int:
        """Counts the failed attempts within the time window"""
        pass

    @abstractmethod
    async def delete_failed_attempts(
        self,
        identifier: str,
        identifier_type: IdentifierType,
        attempt_type: AttemptType,
    ) -> int:
        """
        Removes an identifier's failed attempts.

        Returns:
            Number of records removed
        """
        pass

    @abstractmethod
    async def delete_old_attempts(self, days_to_keep: int = 7) -> int:
        """
        Removes old attempts.

        Returns:
            Number of records removed
        """
        pass

    @abstractmethod
    async def get_recent_attempts(
        self,
        identifier: str,
        identifier_type: IdentifierType,
        attempt_type: AttemptType,
        limit: int = 10,
    ) -> List[LoginAttempt]:
        """Returns the recent attempts of an identifier"""
        pass


class MemoryLoginAttemptStorage(LoginAttemptStorage):
    """
    Local in-memory storage.

    Useful for development and tests, but it does not persist data and does not work
    across several instances.
    """

    def __init__(self):
        self._storage: Dict[str, List[LoginAttempt]] = defaultdict(list)
        self._lock = asyncio.Lock()

    def _make_key(
        self,
        identifier: str,
        identifier_type: IdentifierType,
        attempt_type: AttemptType,
    ) -> str:
        """Builds a unique key for the identifier"""
        return f"{identifier_type.value}:{attempt_type.value}:{identifier}"

    async def save_attempt(
        self,
        identifier: str,
        identifier_type: IdentifierType,
        attempt_type: AttemptType,
        success: bool,
        metadata: Optional[dict] = None,
    ) -> str:
        async with self._lock:
            attempt_id = str(uuid.uuid4())
            attempt = LoginAttempt(
                id=attempt_id,
                identifier=identifier,
                identifier_type=identifier_type,
                attempt_type=attempt_type,
                success=success,
                created_at=datetime.utcnow(),
                metadata=metadata,
            )
            key = self._make_key(identifier, identifier_type, attempt_type)
            self._storage[key].append(attempt)
            return attempt_id

    async def count_failed_attempts(
        self,
        identifier: str,
        identifier_type: IdentifierType,
        attempt_type: AttemptType,
        window_minutes: int,
    ) -> int:
        async with self._lock:
            key = self._make_key(identifier, identifier_type, attempt_type)
            cutoff = datetime.utcnow() - timedelta(minutes=window_minutes)

            count = 0
            for attempt in self._storage[key]:
                if not attempt.success and attempt.created_at >= cutoff:
                    count += 1
            return count

    async def delete_failed_attempts(
        self,
        identifier: str,
        identifier_type: IdentifierType,
        attempt_type: AttemptType,
    ) -> int:
        async with self._lock:
            key = self._make_key(identifier, identifier_type, attempt_type)
            original_count = len(self._storage[key])
            self._storage[key] = [a for a in self._storage[key] if a.success]
            return original_count - len(self._storage[key])

    async def delete_old_attempts(self, days_to_keep: int = 7) -> int:
        async with self._lock:
            cutoff = datetime.utcnow() - timedelta(days=days_to_keep)
            total_removed = 0

            for key in list(self._storage.keys()):
                original_count = len(self._storage[key])
                self._storage[key] = [
                    a for a in self._storage[key] if a.created_at >= cutoff
                ]
                total_removed += original_count - len(self._storage[key])

                # Remove the empty key
                if not self._storage[key]:
                    del self._storage[key]

            return total_removed

    async def get_recent_attempts(
        self,
        identifier: str,
        identifier_type: IdentifierType,
        attempt_type: AttemptType,
        limit: int = 10,
    ) -> List[LoginAttempt]:
        async with self._lock:
            key = self._make_key(identifier, identifier_type, attempt_type)
            attempts = self._storage[key]
            # Sort by date, newest first, and apply the limit
            sorted_attempts = sorted(attempts, key=lambda a: a.created_at, reverse=True)
            return sorted_attempts[:limit]


class RedisLoginAttemptStorage(LoginAttemptStorage):
    """
    Redis storage.

    Shared by every instance. Uses Sorted Sets for efficient time-window queries.
    """

    def __init__(self, redis_client, key_prefix: str = "connect:login_attempt"):
        """
        Initializes the Redis storage.

        Args:
            redis_client: Redis client (redis.asyncio.Redis)
            key_prefix: Prefix for the Redis keys
        """
        self._redis = redis_client
        self._prefix = key_prefix

    def _make_key(
        self,
        identifier: str,
        identifier_type: IdentifierType,
        attempt_type: AttemptType,
    ) -> str:
        """Builds a unique key for the identifier"""
        return (
            f"{self._prefix}:{identifier_type.value}:{attempt_type.value}:{identifier}"
        )

    async def save_attempt(
        self,
        identifier: str,
        identifier_type: IdentifierType,
        attempt_type: AttemptType,
        success: bool,
        metadata: Optional[dict] = None,
    ) -> str:
        attempt_id = str(uuid.uuid4())
        now = time.time()

        attempt_data = {
            "id": attempt_id,
            "identifier": identifier,
            "identifier_type": identifier_type.value,
            "attempt_type": attempt_type.value,
            "success": success,
            "created_at": now,
            "metadata": metadata,
        }

        key = self._make_key(identifier, identifier_type, attempt_type)
        member = json.dumps(attempt_data)

        pipe = self._redis.pipeline()
        # Sorted set with the timestamp as the score
        pipe.zadd(key, {member: now})
        # 7-day TTL for automatic cleanup
        pipe.expire(key, 7 * 24 * 60 * 60)
        await pipe.execute()

        return attempt_id

    async def count_failed_attempts(
        self,
        identifier: str,
        identifier_type: IdentifierType,
        attempt_type: AttemptType,
        window_minutes: int,
    ) -> int:
        key = self._make_key(identifier, identifier_type, attempt_type)
        cutoff = time.time() - (window_minutes * 60)

        # Every attempt within the time window
        attempts = await self._redis.zrangebyscore(key, cutoff, "+inf")

        count = 0
        for attempt_json in attempts:
            try:
                attempt = json.loads(attempt_json)
                if not attempt.get("success", True):
                    count += 1
            except json.JSONDecodeError:
                continue

        return count

    async def delete_failed_attempts(
        self,
        identifier: str,
        identifier_type: IdentifierType,
        attempt_type: AttemptType,
    ) -> int:
        key = self._make_key(identifier, identifier_type, attempt_type)

        # Every attempt
        attempts = await self._redis.zrange(key, 0, -1)

        removed = 0
        for attempt_json in attempts:
            try:
                attempt = json.loads(attempt_json)
                if not attempt.get("success", True):
                    await self._redis.zrem(key, attempt_json)
                    removed += 1
            except json.JSONDecodeError:
                continue

        return removed

    async def delete_old_attempts(self, days_to_keep: int = 7) -> int:
        """
        Removes attempts older than days_to_keep days.

        Note: since the keys have a 7-day TTL, this is mostly for manual cleanup.
        """
        cutoff = time.time() - (days_to_keep * 24 * 60 * 60)

        # Every key with the prefix
        keys = []
        async for key in self._redis.scan_iter(f"{self._prefix}:*"):
            keys.append(key)

        total_removed = 0
        for key in keys:
            removed = await self._redis.zremrangebyscore(key, "-inf", cutoff)
            total_removed += removed

        return total_removed

    async def get_recent_attempts(
        self,
        identifier: str,
        identifier_type: IdentifierType,
        attempt_type: AttemptType,
        limit: int = 10,
    ) -> List[LoginAttempt]:
        key = self._make_key(identifier, identifier_type, attempt_type)

        # Last N attempts (sorted by score, newest first)
        attempts_json = await self._redis.zrevrange(key, 0, limit - 1)

        attempts = []
        for attempt_json in attempts_json:
            try:
                data = json.loads(attempt_json)
                attempt = LoginAttempt(
                    id=data["id"],
                    identifier=data["identifier"],
                    identifier_type=IdentifierType(data["identifier_type"]),
                    attempt_type=AttemptType(data["attempt_type"]),
                    success=data["success"],
                    created_at=datetime.fromtimestamp(data["created_at"]),
                    metadata=data.get("metadata"),
                )
                attempts.append(attempt)
            except (json.JSONDecodeError, KeyError, ValueError):
                continue

        return attempts
