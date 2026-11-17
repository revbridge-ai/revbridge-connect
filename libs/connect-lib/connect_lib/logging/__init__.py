"""
Connect library - Centralized logging module

Provides a standardized logging setup for every RevBridge service.
Supports a text format (legacy) and structured JSON (recommended for observability).

Basic usage:
    from connect_lib.logging import setup_logging

    logger = setup_logging(service_name="my-service")
    logger.info("Service started")

Usage with JSON (recommended):
    logger = setup_logging(service_name="my-service", json_format=True)

OpenTelemetry integration:
    When there is an active OpenTelemetry span (see connect_lib.observability), the
    trace_id and span_id fields are added to the JSON logs automatically.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Iterable

__all__ = [
    "setup_logging",
    "JSONFormatter",
    "RevBridgeFormatter",
    "REVBRIDGE_LOG_FORMAT",
    "NOISY_LOGGERS",
    "HealthCheckFilter",
    "install_exception_handlers",
]

# Standard RevBridge text format
REVBRIDGE_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

# Loggers whose level is lowered
NOISY_LOGGERS: Iterable[str] = (
    "httpx",
    "httpcore",
    "urllib3",
    "mysql.connector",
    "aiomysql",
    "aioredis",
    "opentelemetry",
    "opentelemetry.sdk",
    "opentelemetry.exporter",
    "opentelemetry.instrumentation",
)

# Health check paths excluded from the access logs (same list as observability)
DEFAULT_HEALTH_CHECK_PATHS = (
    "/health",
    "/healthz",
    "/ready",
    "/alive",
    "/health/ready",
    "/health/live",
)


class HealthCheckFilter(logging.Filter):
    """
    Filters health check records out of the uvicorn access log.

    Drops requests to health check endpoints, which generate a lot of noise
    (container health probes every few seconds).

    Configurable through:
        - The excluded_paths constructor parameter
        - The LOG_EXCLUDED_PATHS env var (comma-separated)
        - Default: /health, /healthz, /ready, /alive, /health/ready, /health/live
    """

    def __init__(self, excluded_paths: Optional[tuple] = None):
        super().__init__()
        custom = os.getenv("LOG_EXCLUDED_PATHS")
        if excluded_paths:
            self.excluded_paths = excluded_paths
        elif custom:
            self.excluded_paths = tuple(
                p.strip() for p in custom.split(",") if p.strip()
            )
        else:
            self.excluded_paths = DEFAULT_HEALTH_CHECK_PATHS

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for path in self.excluded_paths:
            if path in message:
                return False
        return True


class RevBridgeFormatter(logging.Formatter):
    """Standard RevBridge formatter for text logs."""

    def __init__(self):
        super().__init__(REVBRIDGE_LOG_FORMAT)


class JSONFormatter(logging.Formatter):
    """
    Structured JSON formatter for observability.

    Works with any backend that consumes JSON logs. Uses OpenTelemetry for trace
    correlation when it is available.

    Included fields:
    - timestamp: ISO 8601 with timezone
    - level: Level name (DEBUG, INFO, WARNING, ERROR, CRITICAL)
    - logger: Logger name
    - message: Log message
    - service: Service name (optional)
    - trace_id: OpenTelemetry trace ID (hex, 32 chars)
    - span_id: OpenTelemetry span ID (hex, 16 chars)
    - error.stack: Stack trace for exceptions
    """

    def __init__(self, service_name: Optional[str] = None):
        super().__init__()
        self.service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        log_data: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Add the service name if configured
        if self.service_name:
            log_data["service"] = self.service_name

        # Add file/line information in DEBUG mode
        if record.levelno <= logging.DEBUG:
            log_data["file"] = record.filename
            log_data["line"] = record.lineno
            log_data["function"] = record.funcName

        # Inject the trace/span IDs (OpenTelemetry)
        self._inject_trace_context(log_data)

        # Add trace_flags if present (W3C trace context)
        if "trace_flags" in log_data and log_data["trace_flags"] == 0:
            del log_data["trace_flags"]

        # Add the stack trace for exceptions
        if record.exc_info:
            log_data["error.stack"] = "".join(
                traceback.format_exception(*record.exc_info)
            )

        # Add the record's extra fields
        for key, value in record.__dict__.items():
            if key not in (
                "name",
                "msg",
                "args",
                "created",
                "filename",
                "funcName",
                "levelname",
                "levelno",
                "lineno",
                "module",
                "msecs",
                "pathname",
                "process",
                "processName",
                "relativeCreated",
                "stack_info",
                "exc_info",
                "exc_text",
                "thread",
                "threadName",
                "message",
                "taskName",
            ):
                try:
                    json.dumps(value)  # Check that it is serializable
                    log_data[key] = value
                except (TypeError, ValueError):
                    log_data[key] = str(value)

        return json.dumps(log_data, ensure_ascii=False)

    @staticmethod
    def _inject_trace_context(log_data: Dict[str, Any]) -> None:
        """
        Injects trace_id and span_id into the logs through OpenTelemetry.
        """
        try:
            from opentelemetry import trace

            span = trace.get_current_span()
            ctx = span.get_span_context()
            if ctx and ctx.trace_id != 0:
                log_data["trace_id"] = format(ctx.trace_id, "032x")
                log_data["span_id"] = format(ctx.span_id, "016x")
        except ImportError:
            pass
        except Exception:
            pass


def setup_logging(
    service_name: str,
    log_level: Optional[str] = None,
    json_format: bool = True,
    log_dir: Optional[str] = None,
    log_to_file: bool = False,
    configure_uvicorn: bool = True,
    silence_noisy_loggers: bool = True,
    capture_exceptions: bool = True,
    filter_health_checks: bool = True,
    health_check_paths: Optional[tuple] = None,
) -> logging.Logger:
    """
    Sets up standardized logging for RevBridge services.

    Args:
        service_name: Service name (e.g. "mail-api-service")
        log_level: Log level (DEBUG, INFO, WARNING, ERROR). If None, uses the LOG_LEVEL env var or INFO.
        json_format: If True (default), uses the JSON format. If False, uses the text format.
        log_dir: Directory for log files. If None, uses the LOG_DIR env var or "./logs".
        log_to_file: If True, also writes logs to a file. Default False (stdout only).
        configure_uvicorn: If True (default), configures the Uvicorn loggers.
        silence_noisy_loggers: If True (default), lowers the level of noisy loggers.
        capture_exceptions: If True (default), logs unhandled exceptions as a single record.
        filter_health_checks: If True (default), filters health checks out of the uvicorn access logs.
        health_check_paths: Tuple of paths to filter. If None, uses the LOG_EXCLUDED_PATHS env var or the default.

    Returns:
        The service's configured logger.

    Example:
        from connect_lib.logging import setup_logging

        logger = setup_logging("my-service")
        logger.info("Service started", extra={"version": "1.0.0"})
    """
    # Determine the log level
    if log_level is None:
        log_level = os.getenv("LOG_LEVEL", "INFO")
    level = getattr(logging, log_level.upper(), logging.INFO)

    # Create the right formatter
    if json_format:
        formatter = JSONFormatter(service_name=service_name)
    else:
        formatter = RevBridgeFormatter()

    # Configure the handlers
    handlers: list[logging.Handler] = []

    # Console handler (stdout, which is where container log collectors read from)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    handlers.append(console_handler)

    # File handler (optional)
    if log_to_file:
        log_dir_path = log_dir or os.getenv("LOG_DIR", "./logs")
        try:
            Path(log_dir_path).mkdir(parents=True, exist_ok=True)
            log_file = os.path.join(log_dir_path, f"{service_name}.log")
            file_handler = logging.FileHandler(log_file)
            file_handler.setLevel(level)
            file_handler.setFormatter(formatter)
            handlers.append(file_handler)
        except (PermissionError, OSError):
            # Ignore it if the file cannot be created
            pass

    # Configure the root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Keep the OTel handlers (OTLP log bridge) while removing duplicates
    otel_handlers = [
        h for h in root_logger.handlers
        if type(h).__module__.startswith("opentelemetry")
    ]
    root_logger.handlers.clear()

    # Add the preserved OTel handlers back (with the health check filter)
    health_filter = HealthCheckFilter(excluded_paths=health_check_paths) if filter_health_checks else None
    for h in otel_handlers:
        if health_filter:
            h.addFilter(health_filter)
        root_logger.addHandler(h)

    for handler in handlers:
        root_logger.addHandler(handler)

    # Configure the service's logger
    logger = logging.getLogger(service_name)
    logger.setLevel(level)
    # Propagate to the root logger (avoids duplicate handlers)
    logger.propagate = True

    # Configure the Uvicorn loggers
    if configure_uvicorn:
        health_filter = (
            HealthCheckFilter(excluded_paths=health_check_paths)
            if filter_health_checks
            else None
        )
        _configure_uvicorn_loggers(formatter, level, health_filter=health_filter)

    # Silence noisy loggers
    if silence_noisy_loggers:
        _silence_noisy_loggers()

    # Log unhandled exceptions as a single record
    if capture_exceptions:
        install_exception_handlers(logger)

    return logger


def _configure_uvicorn_loggers(
    formatter: logging.Formatter,
    level: int,
    health_filter: Optional[HealthCheckFilter] = None,
) -> None:
    """
    Configures the Uvicorn loggers to use the standard format.

    Note: Uvicorn has two main loggers:
    - uvicorn.error: lifecycle events (startup, shutdown) - it does NOT mean errors!
    - uvicorn.access: HTTP access logs

    The name "uvicorn.error" is historical and confusing - these are not error logs.
    """
    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv_logger = logging.getLogger(logger_name)
        uv_logger.handlers.clear()
        uv_logger.filters.clear()

        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(level)
        handler.setFormatter(formatter)
        uv_logger.addHandler(handler)

        # Filter on the LOGGER (not on the handler): no handler at all (including the OTel
        # LoggingHandler) receives health check records. A logger filter is also more robust
        # against handler reconfiguration.
        if health_filter and logger_name == "uvicorn.access":
            uv_logger.addFilter(health_filter)

        uv_logger.setLevel(level)
        # IMPORTANT: propagate=False avoids duplicate logs
        uv_logger.propagate = False


def _silence_noisy_loggers() -> None:
    """Lowers the level of loggers that generate a lot of noise."""
    _info_loggers = {"opentelemetry"}
    for logger_name in NOISY_LOGGERS:
        noisy_logger = logging.getLogger(logger_name)
        if logger_name in _info_loggers:
            noisy_logger.setLevel(logging.INFO)
        else:
            noisy_logger.setLevel(logging.WARNING)


# Keep a reference to the logger and to the original excepthook
_exception_logger: Optional[logging.Logger] = None
_original_excepthook = sys.excepthook


def _exception_handler(exc_type, exc_value, exc_traceback):
    """
    Handler for unhandled exceptions.
    Logs the full exception as a single record instead of line by line.
    """
    if _exception_logger is None:
        # Fall back to the original excepthook if no logger is configured
        _original_excepthook(exc_type, exc_value, exc_traceback)
        return

    # Ignore KeyboardInterrupt so Ctrl+C keeps working
    if issubclass(exc_type, KeyboardInterrupt):
        _original_excepthook(exc_type, exc_value, exc_traceback)
        return

    # Format the full traceback as a single string
    tb_lines = traceback.format_exception(exc_type, exc_value, exc_traceback)
    tb_str = "".join(tb_lines)

    # Log it as an error with the full stack trace
    _exception_logger.error(
        f"Unhandled exception: {exc_type.__name__}: {exc_value}",
        extra={"error.stack": tb_str, "error.type": exc_type.__name__},
        exc_info=(exc_type, exc_value, exc_traceback),
    )


def _asyncio_exception_handler(loop, context):
    """
    Handler for unhandled asyncio exceptions.
    Logs the full exception as a single record.
    """
    if _exception_logger is None:
        # Fall back to the default handler
        loop.default_exception_handler(context)
        return

    exception = context.get("exception")
    message = context.get("message", "Unhandled exception in asyncio")

    if exception:
        tb_str = "".join(
            traceback.format_exception(
                type(exception), exception, exception.__traceback__
            )
        )
        _exception_logger.error(
            f"{message}: {type(exception).__name__}: {exception}",
            extra={
                "error.stack": tb_str,
                "error.type": type(exception).__name__,
                "asyncio.context": {
                    k: str(v) for k, v in context.items() if k != "exception"
                },
            },
            exc_info=(type(exception), exception, exception.__traceback__),
        )
    else:
        _exception_logger.error(
            message,
            extra={"asyncio.context": {k: str(v) for k, v in context.items()}},
        )


def install_exception_handlers(logger: Optional[logging.Logger] = None) -> None:
    """
    Installs global handlers that capture unhandled exceptions.

    This makes sure exceptions are logged as a single JSON record with the full stack trace
    in the 'error.stack' field, instead of several separate lines.

    Args:
        logger: Logger to use. If None, uses the root logger.

    Example:
        from connect_lib.logging import setup_logging, install_exception_handlers

        logger = setup_logging("my-service")
        install_exception_handlers(logger)
    """
    global _exception_logger

    _exception_logger = logger or logging.getLogger()

    # Handler for synchronous exceptions
    sys.excepthook = _exception_handler

    # Handler for asyncio exceptions
    try:
        import asyncio

        loop = asyncio.get_event_loop()
        loop.set_exception_handler(_asyncio_exception_handler)
    except RuntimeError:
        # No event loop yet - it is configured when the loop starts
        pass
