"""
API Service routers

This module holds every router of the API:
- stats_router: tracking statistics
- suppressions_router: suppression management
- deliverability_router: deliverability metrics
- events_router: webhook events
- email_router: email sending
- webhook_router: webhook CRUD
- webhook_stats_router: webhook statistics
- email_config_router: email configurations
- approved_sender_router: Approved Sender management
- admin_health_router: administrative health checks
"""

from .stats_router import stats_router
from .suppressions_router import suppressions_router
from .deliverability_router import deliverability_router
from .events_router import router as events_router
from .email_router import email_router
from .volume_router import volume_router
from .webhook_router import router as webhook_router
from .webhook_stats_router import webhook_stats_router
from .email_config_router import router as email_config_router
from .approved_sender_router import approved_sender_router
from .admin_health_router import admin_health_router
from .dkim_identity_router import router as dkim_identity_router
from .dkim_key_router import router as dkim_key_router
from .sender_validation_router import router as sender_validation_router
from .api_key_domain_router import router as api_key_domain_router

__all__ = [
    "admin_health_router",
    "api_key_domain_router",
    "approved_sender_router",
    "deliverability_router",
    "dkim_identity_router",
    "dkim_key_router",
    "email_config_router",
    "email_router",
    "volume_router",
    "events_router",
    "sender_validation_router",
    "stats_router",
    "suppressions_router",
    "webhook_router",
    "webhook_stats_router",
]
