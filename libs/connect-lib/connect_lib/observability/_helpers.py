"""
Helpers for creating custom spans in application code.

Usage:
    from connect_lib.observability import get_tracer, trace_span

    tracer = get_tracer("my-service")

    @trace_span("operation.name", {"attr": "value"})
    async def my_function():
        ...
"""

from __future__ import annotations

import functools
import inspect
from contextlib import contextmanager
from typing import Any, Dict, Optional


class _NoOpSpanContext:
    """SpanContext that does nothing - fallback when OTel is not available."""

    trace_id = 0
    span_id = 0
    is_remote = False


class _NoOpSpan:
    """Span that does nothing - fallback when OTel is not available."""

    def set_attribute(self, key: str, value: Any) -> None:
        pass

    def set_status(self, status: Any) -> None:
        pass

    def record_exception(self, exception: BaseException) -> None:
        pass

    def end(self) -> None:
        pass

    def get_span_context(self) -> _NoOpSpanContext:
        return _NoOpSpanContext()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class _NoOpTracer:
    """Tracer that does nothing - fallback when OTel is not available."""

    def start_span(self, name: str, **kwargs) -> _NoOpSpan:
        return _NoOpSpan()

    @contextmanager
    def start_as_current_span(self, name: str, **kwargs):
        yield _NoOpSpan()


def get_tracer(name: str) -> Any:
    """
    Returns an OpenTelemetry Tracer, or _NoOpTracer if OTel is not installed.

    Args:
        name: Tracer name (usually the module or service name).
    """
    try:
        from opentelemetry import trace

        return trace.get_tracer(name)
    except ImportError:
        return _NoOpTracer()


def trace_span(
    name: str,
    attributes: Optional[Dict[str, Any]] = None,
):
    """
    Decorator that creates an OTel span around a function (sync or async).

    Records exceptions automatically and re-raises them.

    Args:
        name: Span name (e.g. "email.send").
        attributes: Static span attributes.

    Usage:
        @trace_span("email.send", {"email.channel": "smtp"})
        async def send_email(...):
            ...
    """

    def decorator(func):
        tracer = get_tracer(func.__module__)

        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            with tracer.start_as_current_span(name) as span:
                if attributes:
                    for k, v in attributes.items():
                        span.set_attribute(k, v)
                try:
                    return await func(*args, **kwargs)
                except Exception as exc:
                    span.record_exception(exc)
                    try:
                        from opentelemetry.trace import StatusCode

                        span.set_status(StatusCode.ERROR, str(exc))
                    except ImportError:
                        pass
                    raise

        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            with tracer.start_as_current_span(name) as span:
                if attributes:
                    for k, v in attributes.items():
                        span.set_attribute(k, v)
                try:
                    return func(*args, **kwargs)
                except Exception as exc:
                    span.record_exception(exc)
                    try:
                        from opentelemetry.trace import StatusCode

                        span.set_status(StatusCode.ERROR, str(exc))
                    except ImportError:
                        pass
                    raise

        if inspect.iscoroutinefunction(func):
            return async_wrapper
        return sync_wrapper

    return decorator


def create_span_from_remote_context(
    tracer,
    trace_id_str: str,
    span_id_str: Optional[str],
    span_name: str,
    attributes: Optional[Dict[str, Any]] = None,
):
    """
    Creates a span as a child of a remote context (trace_id/span_id stored in Redis).

    Used for cross-service correlation: the milter stores trace_id/span_id in Redis, and the
    events_updater creates child spans with those IDs.

    Args:
        tracer: OTel Tracer instance.
        trace_id_str: Trace ID as a string (decimal int or hex).
        span_id_str: Span ID as a string (decimal int or hex), may be None.
        span_name: Name of the span to create.
        attributes: Initial span attributes.

    Returns:
        An OTel span, or _NoOpSpan if it cannot be created.
    """
    if not trace_id_str:
        return _NoOpSpan()

    try:
        from opentelemetry import trace
        from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

        # Convert the trace_id (decimal or hex)
        try:
            trace_id_int = int(trace_id_str, 16) if _looks_hex(trace_id_str) else int(trace_id_str)
        except (ValueError, TypeError):
            return _NoOpSpan()

        # Convert the span_id
        span_id_int = 0
        if span_id_str:
            try:
                span_id_int = int(span_id_str, 16) if _looks_hex(span_id_str) else int(span_id_str)
            except (ValueError, TypeError):
                span_id_int = 0

        # Build the remote context
        span_context = SpanContext(
            trace_id=trace_id_int,
            span_id=span_id_int,
            is_remote=True,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        )
        parent_span = NonRecordingSpan(span_context)
        ctx = trace.set_span_in_context(parent_span)

        # Create the child span
        span = tracer.start_span(span_name, context=ctx)
        if attributes:
            for k, v in attributes.items():
                span.set_attribute(k, str(v) if v is not None else "")

        return span

    except ImportError:
        return _NoOpSpan()
    except Exception:
        return _NoOpSpan()


def _looks_hex(s: str) -> bool:
    """Checks whether a string looks hexadecimal (contains a-f/A-F)."""
    if not s:
        return False
    s = s.lower()
    return any(c in "abcdef" for c in s)
