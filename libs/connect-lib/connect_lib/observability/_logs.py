"""
Python logging -> OpenTelemetry bridge through a LoggingHandler.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def setup_log_bridge(resource, endpoint: Optional[str] = None) -> None:
    """
    Sets up the Python logging bridge to OTLP.

    Adds a LoggingHandler to the root logger that sends logs to the OTel Collector through
    OTLP, without interfering with the existing handlers (JSON on stdout).
    """
    from opentelemetry._logs import set_logger_provider
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

    exporter = _create_exporter(endpoint)
    log_provider = LoggerProvider(resource=resource)
    log_provider.add_log_record_processor(BatchLogRecordProcessor(exporter))

    set_logger_provider(log_provider)

    # Add the bridge handler to the root logger
    otel_handler = LoggingHandler(
        level=logging.NOTSET,
        logger_provider=log_provider,
    )
    logging.getLogger().addHandler(otel_handler)
    logger.info("OTel log bridge configured")


def _create_exporter(endpoint: Optional[str] = None):
    """Creates the OTLP log exporter (gRPC, falling back to HTTP)."""
    try:
        from opentelemetry.exporter.otlp.proto.grpc._log_exporter import (
            OTLPLogExporter,
        )

        kwargs = {}
        if endpoint:
            kwargs["endpoint"] = endpoint
        return OTLPLogExporter(**kwargs)
    except ImportError:
        from opentelemetry.exporter.otlp.proto.http._log_exporter import (
            OTLPLogExporter,
        )

        kwargs = {}
        if endpoint:
            kwargs["endpoint"] = f"{endpoint}/v1/logs"
        return OTLPLogExporter(**kwargs)
