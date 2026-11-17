"""
Shared models

Every shared Pydantic model:
- email_models: email sending models
- tracking_models: email tracking models
- webhook_models: webhook models
- api_key_models: API Key models
- approved_sender_models: Approved Sender models
- dkim_models: DKIM models (identities and keys)
"""

from .email_models import *
from .tracking_models import *
from .webhook_models import *
# API Key models come straight from connect-lib
from connect_lib.api_key.models import (
    ApiKeyCreate,
    ApiKeyCreateWithDomain,
    ApiKeyUpdate,
    ApiKeyResponse,
    ApiKeyCreateResponse,
    ApiKeyListResponse,
    ApiKeyFilter,
    ApiKeyUsageStats,
    ApiKeyPermission,
)
from .approved_sender_models import *
from .dkim_models import *

__all__ = [
    # Email models
    "SendEmailRequest", "EmailResponse", "EmailStatus", "EmailListResponse",
    "EmailStatsResponse", "EmailFilter", "EmailBulkActionRequest",

    # Tracking models
    "TrackingEventType", "TrackingStats", "TrackingEvent", "TrackingEventResponse",
    "TrackingFilter", "TrackingEventListResponse", "SuppressionEntry", "SuppressionCreate",
    "SuppressionResponse", "SuppressionListResponse", "DeliverabilityMetrics",
    "DeliverabilityResponse", "HealthCheckResponse",

    # Webhook models
    "WebhookCreate", "WebhookUpdate", "WebhookResponse", "WebhookEventResponse",
    "WebhookLogResponse", "WebhookStats", "WebhookFilter", "WebhookEventFilter",
    "WebhookTestRequest", "WebhookEventType",
    "WebhookQueueMessage", "generate_webhook_signature", "create_event_payload",

    # API Key models (from connect-lib)
    "ApiKeyCreate", "ApiKeyCreateWithDomain", "ApiKeyUpdate", "ApiKeyResponse",
    "ApiKeyCreateResponse", "ApiKeyListResponse", "ApiKeyFilter", "ApiKeyUsageStats",
    "ApiKeyPermission",

    # Approved Sender models
    "ApprovedSenderCreate", "ApprovedSenderUpdate", "ApprovedSenderResponse",
    "ApprovedSenderListResponse", "ApprovedSenderFilter",

    # DKIM models
    "DkimIdentityCreate", "DkimIdentityUpdate", "DkimIdentityResponse", "DkimIdentityFilter",
    "DkimKeyCreate", "DkimKeyUpdate", "DkimKeyResponse", "DkimKeyFilter",
]
