"""
Creates the OTel Resource with the standard RevBridge attributes.
"""

from __future__ import annotations

import os
from typing import Dict, Optional


def build_resource(
    service_name: str,
    service_version: Optional[str] = None,
    environment: Optional[str] = None,
    extra_attributes: Optional[Dict[str, str]] = None,
):
    """
    Creates an OpenTelemetry Resource with the standard attributes.

    Priority: parameter > env var > default.
    """
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.semconv.resource import ResourceAttributes

    attrs = {
        ResourceAttributes.SERVICE_NAME: service_name,
        ResourceAttributes.DEPLOYMENT_ENVIRONMENT: (
            environment or os.getenv("OTEL_ENV", os.getenv("DD_ENV", "dev"))
        ),
    }

    version = service_version or os.getenv(
        "OTEL_SERVICE_VERSION", os.getenv("DD_VERSION")
    )
    if version:
        attrs[ResourceAttributes.SERVICE_VERSION] = version

    if extra_attributes:
        attrs.update(extra_attributes)

    return Resource.create(attrs)
