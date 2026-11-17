"""
API key service

Uses the full implementation from connect-lib.
ApiKeyService uses connect-lib's own database functions.
"""

from connect_lib.api_key.service_impl import ApiKeyService

# Global service instance (it uses connect-lib's own database functions)
api_key_service = ApiKeyService()
