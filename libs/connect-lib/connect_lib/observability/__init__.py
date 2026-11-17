"""
Connect library - Observability module (OpenTelemetry)

Sets up traces, metrics and logs through OpenTelemetry with OTLP export. Exporting is OPT-IN:
nothing is set up (and nothing tries to reach a collector) unless OTEL_EXPORTER_OTLP_ENDPOINT
is set. The backend (Jaeger, SigNoz, Grafana Tempo, etc.) is chosen by the OTel Collector,
with no change in the services' code.

Basic usage:
    from connect_lib.observability import setup_observability

    setup_observability(service_name="my-service")

Configuration through env vars (OTel standard):
    OTEL_EXPORTER_OTLP_ENDPOINT  - Collector endpoint. REQUIRED to turn exporting on: without it
                                   setup_observability() does nothing
    OTEL_ENABLED                 - Enable/disable (default: true; false wins over the endpoint)
    OTEL_TRACES_ENABLED          - Enable traces (default: true)
    OTEL_METRICS_ENABLED         - Enable metrics (default: true)
    OTEL_LOGS_ENABLED            - Enable the OTLP log bridge (default: true)
    OTEL_ENV                     - Environment (default: dev)
    OTEL_SERVICE_VERSION         - Service version
"""

from __future__ import annotations

import logging
import os
from typing import Dict, Optional

__all__ = [
    "setup_observability",
    "instrument_fastapi",
    "get_tracer",
    "trace_span",
    "create_span_from_remote_context",
]

DEFAULT_EXCLUDED_URLS = "health,healthz,ready,alive,docs,redoc,openapi.json"

logger = logging.getLogger(__name__)

_initialized = False
_resolved_excluded_urls: Optional[str] = None


def _env_bool(key: str, default: bool = True) -> bool:
    """Reads an env var as a boolean."""
    return os.getenv(key, str(default)).lower() in ("true", "1", "yes")


def setup_observability(
    service_name: str,
    service_version: Optional[str] = None,
    environment: Optional[str] = None,
    endpoint: Optional[str] = None,
    enable_traces: bool = True,
    enable_metrics: bool = True,
    enable_logs: bool = True,
    extra_resource_attributes: Optional[Dict[str, str]] = None,
    excluded_urls: Optional[str] = None,
) -> None:
    """
    Sets up OpenTelemetry for a RevBridge service.

    It is a no-op unless an OTLP endpoint is configured (OTEL_EXPORTER_OTLP_ENDPOINT, or the
    `endpoint` argument), so a default installation never tries to reach a collector.

    It must be called ONCE when the service starts, BEFORE setup_logging().

    Args:
        service_name: Service name (e.g. "mail-api-service").
        service_version: Service version. If None, uses OTEL_SERVICE_VERSION.
        environment: Environment (dev/prod). If None, uses OTEL_ENV (or DD_ENV).
        endpoint: OTLP endpoint. If None, the exporters use OTEL_EXPORTER_OTLP_ENDPOINT.
        enable_traces: Enable tracing. Can be overridden by OTEL_TRACES_ENABLED.
        enable_metrics: Enable metrics. Can be overridden by OTEL_METRICS_ENABLED.
        enable_logs: Enable the OTLP log bridge. Can be overridden by OTEL_LOGS_ENABLED.
        extra_resource_attributes: Extra attributes for the Resource.
        excluded_urls: URLs excluded from FastAPI tracing (comma-separated).
            Priority: parameter > env OTEL_PYTHON_FASTAPI_EXCLUDED_URLS > DEFAULT_EXCLUDED_URLS.

    Example:
        from connect_lib.observability import setup_observability
        from connect_lib.logging import setup_logging

        setup_observability(service_name="my-service")
        logger = setup_logging("my-service")
    """
    global _initialized, _resolved_excluded_urls

    if _initialized:
        logger.debug("OTel already initialized, ignoring the duplicate call")
        return

    if not _env_bool("OTEL_ENABLED"):
        logger.info("OTel disabled via OTEL_ENABLED=false")
        _initialized = True
        return

    # Opt-in: without an endpoint the OTLP exporters would fall back to localhost:4317, so
    # nothing is set up at all. The original `endpoint` argument is still what is passed to
    # the exporters below, so the SDK keeps reading its own OTEL_EXPORTER_OTLP_* variables.
    if not (endpoint or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()):
        logger.debug("OTel not configured (OTEL_EXPORTER_OTLP_ENDPOINT is not set); skipping")
        _initialized = True
        return

    try:
        from ._resource import build_resource
    except ImportError:
        logger.warning(
            "OpenTelemetry is not installed. Install it with: "
            "pip install connect-lib[observability]"
        )
        _initialized = True
        return

    resource = build_resource(
        service_name=service_name,
        service_version=service_version,
        environment=environment,
        extra_attributes=extra_resource_attributes,
    )

    # Resolve excluded_urls: parameter > env > default
    _resolved_excluded_urls = (
        excluded_urls
        or os.getenv("OTEL_PYTHON_FASTAPI_EXCLUDED_URLS")
        or DEFAULT_EXCLUDED_URLS
    )

    # Traces
    if enable_traces and _env_bool("OTEL_TRACES_ENABLED"):
        try:
            from ._traces import setup_traces

            setup_traces(
                resource,
                endpoint=endpoint,
                excluded_urls=_resolved_excluded_urls,
            )
        except Exception as exc:
            logger.warning("Could not set up OTel traces: %s", exc)

    # Metrics
    if enable_metrics and _env_bool("OTEL_METRICS_ENABLED"):
        try:
            from ._metrics import setup_metrics

            setup_metrics(resource, endpoint=endpoint)
        except Exception as exc:
            logger.warning("Could not set up OTel metrics: %s", exc)

    # Log bridge (sends logs through OTLP in addition to stdout)
    if enable_logs and _env_bool("OTEL_LOGS_ENABLED"):
        try:
            from ._logs import setup_log_bridge

            setup_log_bridge(resource, endpoint=endpoint)
        except Exception as exc:
            logger.warning("Could not set up the OTel log bridge: %s", exc)

    _initialized = True
    logger.info("OpenTelemetry initialized for %s", service_name)


def get_tracer(name: str):
    """Wrapper - see _helpers.get_tracer."""
    from ._helpers import get_tracer as _get_tracer

    return _get_tracer(name)


def trace_span(name: str, attributes=None):
    """Wrapper - see _helpers.trace_span."""
    from ._helpers import trace_span as _trace_span

    return _trace_span(name, attributes)


def create_span_from_remote_context(tracer, trace_id_str, span_id_str, span_name, attributes=None):
    """Wrapper - see _helpers.create_span_from_remote_context."""
    from ._helpers import create_span_from_remote_context as _create

    return _create(tracer, trace_id_str, span_id_str, span_name, attributes)


def instrument_fastapi(app) -> None:
    """
    Instruments a FastAPI instance with OTel tracing.

    Must be called AFTER the FastAPI app is created and AFTER setup_observability().
    Respects the excluded_urls configured in setup_observability(). Without a configured
    exporter the spans are non-recording and nothing is sent anywhere.

    The FastAPIInstrumentor's global instrument() does not work when FastAPI is imported
    with 'from fastapi import FastAPI' (the local binding captures the class before the
    monkey-patch). That is why each service must call instrument_fastapi(app) explicitly.

    Example:
        from connect_lib.observability import setup_observability, instrument_fastapi

        setup_observability(service_name="my-service")
        app = FastAPI(...)
        instrument_fastapi(app)
    """
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        excluded = _resolved_excluded_urls or DEFAULT_EXCLUDED_URLS
        FastAPIInstrumentor.instrument_app(app, excluded_urls=excluded)
        logger.debug("FastAPI app instrumented (excluded_urls=%s)", excluded)
    except ImportError:
        pass
    except Exception as exc:
        logger.warning("Could not instrument the FastAPI app: %s", exc)
