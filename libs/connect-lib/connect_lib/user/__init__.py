"""
User management module

Provides:
- Password hashing and verification (bcrypt)
- An abstract interface for user operations
- A base implementation that accepts database functions
"""

from .utils import verify_password, get_password_hash
from .service import UserServiceBase, UserService

__all__ = [
    "verify_password",
    "get_password_hash",
    "UserServiceBase",
    "UserService",
]
