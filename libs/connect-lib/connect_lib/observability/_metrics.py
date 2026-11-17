"""
MeterProvider setup with an OTLP exporter.

IMPORTANT: uses DELTA temporality for Counters/UpDownCounters, so backends that treat every
exported value as an increment do not add up cumulative totals over and over (which would
inflate counters).
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def _get_delta_temporality_preference():
    """
    Returns the DELTA temporality preference for Counter and UpDownCounter.

    This is needed because:
    - With CUMULATIVE (the default), the SDK exports the accumulated total on every export
    - A backend that treats each value as an increment would then double-count
    - With DELTA, the SDK exports only the increment since the last export
    """
    from opentelemetry.sdk.metrics import Counter, Histogram, UpDownCounter
    from opentelemetry.sdk.metrics.export import AggregationTemporality

    return {
        Counter: AggregationTemporality.DELTA,
        UpDownCounter: AggregationTemporality.DELTA,
        # Histograms use DELTA too.
        # The OTel Collector exporter drops the first CUMULATIVE point (it uses it as the
        # baseline), which loses data in short-lived processes.
        # With DELTA, every export sends its data right away.
        Histogram: AggregationTemporality.DELTA,
    }


def setup_metrics(resource, endpoint: Optional[str] = None) -> None:
    """
    Sets up a MeterProvider with an OTLP exporter.

    Uses DELTA temporality for Counter/UpDownCounter to avoid double-counting.
    """
    from opentelemetry import metrics
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

    exporter = _create_exporter(endpoint)

    reader = PeriodicExportingMetricReader(
        exporter,
        export_interval_millis=30000,
    )

    provider = MeterProvider(resource=resource, metric_readers=[reader])
    metrics.set_meter_provider(provider)
    logger.info("OTel MeterProvider configured with DELTA temporality")


def _create_exporter(endpoint: Optional[str] = None):
    """
    Creates the OTLP metric exporter (gRPC, falling back to HTTP).

    Sets DELTA temporality for Counter/UpDownCounter.
    """
    temporality = _get_delta_temporality_preference()

    try:
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
            OTLPMetricExporter,
        )

        kwargs = {"preferred_temporality": temporality}
        if endpoint:
            kwargs["endpoint"] = endpoint
        return OTLPMetricExporter(**kwargs)
    except ImportError:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            OTLPMetricExporter,
        )

        kwargs = {"preferred_temporality": temporality}
        if endpoint:
            kwargs["endpoint"] = f"{endpoint}/v1/metrics"
        return OTLPMetricExporter(**kwargs)
