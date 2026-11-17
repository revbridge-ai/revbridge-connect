"""
Types and enums for messaging events (RevBridge Connect: email).
"""

from enum import Enum


class ChannelType(str, Enum):
    """Communication channel type."""

    EMAIL = "email"


class ProviderType(str, Enum):
    """Who delivered the message."""

    POSTFIX = "postfix"  # Connect's own MTA (Postfix + milters)


class EngagementAction(str, Enum):
    """Normalized action/status of an email event."""

    SENT = "sent"
    FAILED = "failed"
    DELIVERED = "delivered"
    DEFERRED = "deferred"
    BOUNCED = "bounced"
    HARD_BOUNCED = "hard_bounced"
    SOFT_BOUNCED = "soft_bounced"
    SUPPRESSED = "suppressed"
    OPENED = "opened"
    CLICK = "click"
    UNSUBSCRIBED = "unsubscribed"


class IdentifierType(str, Enum):
    """Contact identifier type."""

    EMAIL = "email"


class Environment(str, Enum):
    """Runtime environment."""

    PRODUCTION = "production"
    STAGING = "staging"
    DEVELOPMENT = "development"
