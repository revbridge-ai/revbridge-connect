"""Exceptions of the deliverability jobs."""

from __future__ import annotations


class InfraBackendError(RuntimeError):
    """Base exception for infra backend errors."""


class DatabaseConnectionError(InfraBackendError):
    """Raised when database connection fails."""
