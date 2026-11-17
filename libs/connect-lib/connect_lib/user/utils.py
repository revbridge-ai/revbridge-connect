"""
User management utilities
"""

import bcrypt


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    Checks whether the given password matches the stored hash

    Args:
        plain_password: Plain-text password
        hashed_password: Stored password hash

    Returns:
        bool: True if the password is correct
    """
    try:
        password_bytes = plain_password.encode("utf-8")
        hash_bytes = hashed_password.encode("utf-8")
        return bcrypt.checkpw(password_bytes, hash_bytes)
    except Exception:
        return False


def get_password_hash(password: str, rounds: int = 12) -> str:
    """
    Hashes the password for safe storage

    Args:
        password: Plain-text password
        rounds: Number of bcrypt rounds (default: 12)

    Returns:
        str: Password hash
    """
    salt = bcrypt.gensalt(rounds=rounds)
    password_bytes = password.encode("utf-8")
    hash_bytes = bcrypt.hashpw(password_bytes, salt)
    return hash_bytes.decode("utf-8")
