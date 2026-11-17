import logging
import os

# Load a .env file when python-dotenv is installed (optional)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    # python-dotenv is optional: use the process environment as is
    pass

REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
REDIS_TTL_SEC = int(os.getenv("REDIS_TTL_SEC", "259200"))

# Short TTL for the email_config cache (the milter's DB -> Redis fallback).
# Without a TTL, a direct UPDATE in the database would leave the cache stale forever.
EMAIL_CONFIG_CACHE_TTL_SEC = int(os.getenv("EMAIL_CONFIG_CACHE_TTL_SEC", "3600"))

DB_DSN = os.getenv("DB_DSN", "")

# Installation base domain (set by docker compose from connect.toml).
_INSTALL_DOMAIN = os.getenv("DOMAIN", "").strip()

# Base URL of the tracking service (open pixel, click and unsubscribe links).
TRACK_BASE = os.getenv("TRACK_BASE", f"https://t.{_INSTALL_DOMAIN}" if _INSTALL_DOMAIN else "")
REWRITE_LINKS = os.getenv("REWRITE_LINKS", "1") == "1"
INJECT_PIXEL = os.getenv("INJECT_PIXEL", "1") == "1"

# Domain used to generate Message-IDs (must be a valid FQDN)
MESSAGE_ID_DOMAIN = os.getenv("MESSAGE_ID_DOMAIN", _INSTALL_DOMAIN)

EVENTS_UPDATER_HOST = os.getenv("EVENTS_UPDATER_HOST", "0.0.0.0")
EVENTS_UPDATER_PORT = int(os.getenv("EVENTS_UPDATER_PORT", "8081"))

# Logging configuration
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_DIR = os.getenv("LOG_DIR", "./logs")


def setup_logging(service_name: str = "mail-milters") -> logging.Logger:
    """
    Configure the standard service logging.
    Uses the shared connect_lib.logging module.

    Args:
        service_name: Service name, used for the log file

    Returns:
        Configured logger
    """
    from connect_lib.logging import setup_logging as _setup_logging

    return _setup_logging(
        service_name=service_name,
        log_level=LOG_LEVEL,
        json_format=True,
        log_dir=LOG_DIR,
        log_to_file=True,
    )

# Shadow address for monitoring/auditing (a copy of every email sent).
# When set, a BCC copy is sent to this address.
SHADOW_EMAIL = os.getenv("SHADOW_EMAIL", None)  # Example: "monitor@example.com"

# Encryption key for tracking tokens.
# Required for tracking: without it the milter neither rewrites links nor adds the pixel.
# Generate one with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
TOKEN_ENCRYPTION_KEY = os.getenv("TOKEN_ENCRYPTION_KEY", None)
