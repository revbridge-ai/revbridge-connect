"""
Shared authentication module

Authentication uses the shared connect_lib.auth library.
The api_key_adapter adapter registers the API Key validator with connect_lib.
"""

from .api_key_adapter import setup_auth, TrackingApiKeyValidator

__all__ = [
    "TrackingApiKeyValidator",
    "setup_auth",
]
