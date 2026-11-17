"""
Utilities for generating and hashing API Keys
"""

import hashlib
import secrets
from passlib.hash import sha512_crypt

# API Key prefixes
SECRET_KEY_PREFIX = "rbak_"  # Secret keys (server-side)


def generate_api_key(prefix: str = SECRET_KEY_PREFIX) -> str:
    """
    Generates a new API Key

    Args:
        prefix: API Key prefix (default: "rbak_")

    Returns:
        API Key in the format {prefix}{64 hex characters}
    """
    # 32 random bytes as hex (64 characters)
    random_bytes = secrets.token_bytes(32)
    key_suffix = random_bytes.hex()
    return f"{prefix}{key_suffix}"


def hash_api_key(api_key: str) -> str:
    """
    Hashes the API Key for safe storage (SHA512-CRYPT, compatible with Dovecot)

    Args:
        api_key: The API key to hash

    Returns:
        Hash in the format {SHA512-CRYPT}<crypt_hash>
    """
    # SHA512-CRYPT through passlib (the same scheme Dovecot uses)
    # Dovecot usually expects the format {SHA512-CRYPT}<crypt_hash>
    crypt_hash = sha512_crypt.using(rounds=5000).hash(api_key)
    return f"{{SHA512-CRYPT}}{crypt_hash}"


def verify_api_key_hash(api_key: str, stored_hash: str) -> bool:
    """
    Checks whether an API Key matches the stored hash

    Args:
        api_key: The API key to check
        stored_hash: The hash stored in the database

    Returns:
        True if the key matches the hash, False otherwise
    """
    # Remove the {SHA512-CRYPT} prefix if present
    if stored_hash.startswith("{SHA512-CRYPT}"):
        stored_hash = stored_hash[14:]  # Remove "{SHA512-CRYPT}"

    try:
        return sha512_crypt.verify(api_key, stored_hash)
    except Exception:
        return False


def generate_api_key_id() -> str:
    """
    Generates a unique ID for the API Key

    Returns:
        ID in the format ak_{32 hex characters}
    """
    return f"ak_{secrets.token_hex(16)}"


def generate_lookup_hash(api_key: str) -> str:
    """
    Generates a fast hash (SHA256) for a direct database lookup.

    This hash is used for an O(1) lookup in the WHERE clause, instead of iterating over every
    key and checking SHA512-CRYPT.

    Args:
        api_key: The full API key (e.g. rbak_abc123...)

    Returns:
        SHA256 hash in hexadecimal (64 characters)
    """
    return hashlib.sha256(api_key.encode()).hexdigest()
