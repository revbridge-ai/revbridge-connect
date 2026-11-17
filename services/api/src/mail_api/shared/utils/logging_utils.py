"""
Logging utilities

Common functions to log operations.
"""

import logging

logger = logging.getLogger(__name__)


def log_operation_start(operation: str, **kwargs):
    """Logs the start of an operation"""
    logger.info(f"Starting {operation}", extra=kwargs)


def log_operation_success(operation: str, **kwargs):
    """Logs that an operation succeeded"""
    logger.info(f"{operation} completed successfully", extra=kwargs)


def log_operation_error(operation: str, error: Exception, **kwargs):
    """Logs that an operation failed"""
    logger.error(f"Error in {operation}: {error!s}", extra=kwargs)
