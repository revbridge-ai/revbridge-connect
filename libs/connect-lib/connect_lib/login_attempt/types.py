"""
Types for Login Attempt Module
"""

from enum import Enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


class IdentifierType(str, Enum):
    """Type of identifier used to track attempts"""

    IP = "ip"
    EMAIL = "email"
    USERNAME = "username"
    API_KEY = "api_key"
    PHONE = "phone"


class AttemptType(str, Enum):
    """Type of authentication operation"""

    LOGIN = "login"
    SIGNUP = "signup"
    FORGOT_PASSWORD = "forgot_password"
    RESET_PASSWORD = "reset_password"
    UPDATE_PASSWORD = "update_password"
    API_KEY_VALIDATION = "api_key_validation"
    TWO_FACTOR = "two_factor"


@dataclass
class LoginAttempt:
    """An authentication attempt"""

    id: str
    identifier: str  # IP, email, username, etc.
    identifier_type: IdentifierType
    attempt_type: AttemptType
    success: bool
    created_at: datetime = field(default_factory=datetime.utcnow)
    metadata: Optional[dict] = None  # Additional information (user agent, etc.)

    def to_dict(self) -> dict:
        """Converts to a dict"""
        return {
            "id": self.id,
            "identifier": self.identifier,
            "identifier_type": self.identifier_type.value,
            "attempt_type": self.attempt_type.value,
            "success": self.success,
            "created_at": self.created_at.isoformat(),
            "metadata": self.metadata,
        }


@dataclass
class BlockedResult:
    """Result of the block check"""

    blocked: bool
    message: Optional[str] = None
    failed_attempts: int = 0
    max_attempts: int = 0
    retry_after_minutes: int = 0

    def to_dict(self) -> dict:
        """Converts to a dict for the API response"""
        result = {
            "blocked": self.blocked,
            "failed_attempts": self.failed_attempts,
            "max_attempts": self.max_attempts,
        }
        if self.blocked:
            result["message"] = self.message
            result["retry_after_minutes"] = self.retry_after_minutes
        return result
