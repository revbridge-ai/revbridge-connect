"""
Routers of the tracking service

This package holds the tracking router:
- tracking_router: email pixels and clicks (open, click, unsubscribe)
"""

from .tracking_router import tracking_router

__all__ = [
    "tracking_router",
]
