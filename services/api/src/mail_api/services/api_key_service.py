"""
API Key service

Uses the full implementation from the connect-lib library.
ApiKeyService uses its own database functions.
"""

from connect_lib.api_key.service_impl import ApiKeyService

# Global service instance
# ApiKeyService uses connect-lib's own database functions
api_key_service = ApiKeyService()
