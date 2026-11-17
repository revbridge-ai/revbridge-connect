"""
TracerProvider setup and auto-instrumentation.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def setup_traces(
    resource,
    endpoint: Optional[str] = None,
    excluded_urls: Optional[str] = None,
) -> None:
    """
    Sets up a TracerProvider with an OTLP exporter and auto-instrumentation.

    Args:
        resource: OTel Resource.
        endpoint: OTLP endpoint.
        excluded_urls: URLs excluded from FastAPI tracing (comma-separated).
    """
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=resource)

    exporter = _create_exporter(endpoint)
    provider.add_span_processor(BatchSpanProcessor(exporter))

    trace.set_tracer_provider(provider)
    logger.info("OTel TracerProvider configured")

    _auto_instrument(excluded_urls=excluded_urls)


def _create_exporter(endpoint: Optional[str] = None):
    """Creates the OTLP exporter (gRPC, falling back to HTTP)."""
    try:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )

        kwargs = {}
        if endpoint:
            kwargs["endpoint"] = endpoint
        return OTLPSpanExporter(**kwargs)
    except ImportError:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )

        kwargs = {}
        if endpoint:
            kwargs["endpoint"] = f"{endpoint}/v1/traces"
        return OTLPSpanExporter(**kwargs)


def _auto_instrument(excluded_urls: Optional[str] = None) -> None:
    """Installs auto-instrumentation for common frameworks.

    Note: FastAPI is NOT instrumented here. The global instrument() does not work when the
    services do 'from fastapi import FastAPI' (the local binding captures the class before
    the monkey-patch). The services must call instrument_fastapi(app) explicitly after
    creating the app.
    """
    instrumentors = [
        ("opentelemetry.instrumentation.logging", "LoggingInstrumentor"),
        ("opentelemetry.instrumentation.httpx", "HTTPXClientInstrumentor"),
        ("opentelemetry.instrumentation.redis", "RedisInstrumentor"),
        ("opentelemetry.instrumentation.mysql", "MySQLInstrumentor"),
    ]

    for module_path, class_name in instrumentors:
        try:
            module = __import__(module_path, fromlist=[class_name])
            instrumentor_cls = getattr(module, class_name)
            instrumentor = instrumentor_cls()
            if not instrumentor.is_instrumented_by_opentelemetry:
                instrumentor.instrument()
                logger.debug("Auto-instrumented: %s", class_name)
        except ImportError:
            logger.debug("Instrumentor not available: %s", class_name)
        except Exception as exc:
            logger.warning("Could not instrument %s: %s", class_name, exc)
