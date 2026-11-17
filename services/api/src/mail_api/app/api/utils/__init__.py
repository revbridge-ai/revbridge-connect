"""
Tracking-specific utilities

Functions and constants specific to the tracking reports:
- SQL constants for the tracking queries
- Tracking data processing functions
- Re-exported webhook utilities
"""

# Import shared utilities
from mail_api.shared.utils import (
    parse_date_range, calculate_period_halves, calculate_rate, determine_trend,
    calculate_pagination, build_pagination_metadata, validate_suppression_type,
    log_operation_start, log_operation_success, log_operation_error,
    generate_webhook_signature, verify_webhook_signature,
    format_webhook_payload, calculate_retry_delay
)

# Import tracking-specific utilities

from .tracking_utils import (
    process_isp_performance,
    process_timeseries_data,
    process_bounce_breakdown
)

from .sql_queries import (
    DELIVERY_EVENTS_BASE_FROM,
    DELIVERY_COUNTS_SELECT,
    AVG_DELIVERY_TIME_SELECT_BASE,
    BOUNCE_BREAKDOWN_SELECT,
    ISP_PERFORMANCE_SELECT,
    TIMESERIES_SELECT
)

__all__ = [
    # Shared utilities (re-exported)
    "parse_date_range", "calculate_period_halves", "calculate_rate", "determine_trend",
    "calculate_pagination", "build_pagination_metadata", "validate_suppression_type",
    "log_operation_start", "log_operation_success", "log_operation_error",
    "generate_webhook_signature", "verify_webhook_signature",
    "format_webhook_payload", "calculate_retry_delay",
    # Tracking utilities
    "process_isp_performance", "process_timeseries_data", "process_bounce_breakdown",
    # SQL queries
    "DELIVERY_EVENTS_BASE_FROM", "DELIVERY_COUNTS_SELECT", "AVG_DELIVERY_TIME_SELECT_BASE",
    "BOUNCE_BREAKDOWN_SELECT", "ISP_PERFORMANCE_SELECT", "TIMESERIES_SELECT"
]
