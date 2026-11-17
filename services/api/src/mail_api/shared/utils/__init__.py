"""
Utilities shared across the services

Functions and constants used by more than one service:
- Date and time functions
- Metric calculation functions
- Pagination functions
- Validation functions
"""

from .date_utils import (
    parse_date_range,
    get_default_period,
    calculate_period_halves,
    _to_date,
    _date_iso,
    _split_period_in_halves
)

from .metrics_utils import (
    calculate_rate,
    determine_trend,
    calculate_pagination,
    build_pagination_metadata
)

from .validation_utils import (
    validate_suppression_type
)

from .logging_utils import (
    log_operation_start,
    log_operation_success,
    log_operation_error
)

from .webhook_utils import (
    generate_webhook_signature,
    verify_webhook_signature,
    format_webhook_payload,
    calculate_retry_delay,
    remove_surrogate_chars
)

__all__ = [
    # Date utilities
    "parse_date_range", "get_default_period", "calculate_period_halves",
    "_to_date", "_date_iso", "_split_period_in_halves",
    # Metrics utilities
    "calculate_rate", "determine_trend", "calculate_pagination", "build_pagination_metadata",
    # Validation utilities
    "validate_suppression_type",
    # Logging utilities
    "log_operation_start", "log_operation_success", "log_operation_error",
    # Webhook utilities
    "generate_webhook_signature", "verify_webhook_signature",
    "format_webhook_payload", "calculate_retry_delay", "remove_surrogate_chars"
]
