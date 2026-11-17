"""
Shared authentication module

Uses the shared connect_lib.auth library for authentication.
The api_key_adapter sets up the API key validator for connect_lib.
"""

from .api_key_adapter import setup_auth, TrackingApiKeyValidator

__all__ = [
    "TrackingApiKeyValidator",
    "setup_auth",
]
