import logging
import os

from dotenv import load_dotenv

load_dotenv()

REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
REDIS_TTL_SEC = int(os.getenv("REDIS_TTL_SEC", "259200"))

DB_DSN = os.getenv("DB_DSN", "")

# Database Configuration
DB_CONNECTION_TIMEOUT = int(
    os.getenv("DB_CONNECTION_TIMEOUT", "5")
)  # short timeout (5s)
DB_AUTOCOMMIT = (
    os.getenv("DB_AUTOCOMMIT", "false").lower() == "true"
)  # autocommit off by default (explicit transactions)

# Track Service Configuration
TRACKER_HOST = os.getenv("TRACKER_HOST", "0.0.0.0")
TRACKER_PORT = int(os.getenv("TRACKER_PORT", "8002"))

# Minimum delta (s) between delivered and open for the open to count as engagement.
# Prefetches/scans at delivery time fire within 0-2 s; people opening from a push
# notification arrive from about 3 s on. Scanners slower than 3 s are caught by
# detect_bot (UA/IP).
OPEN_MIN_DELTA_SECONDS = int(os.getenv("OPEN_MIN_DELTA_SECONDS", "3"))

# Encryption key of the tracking tokens (the same key the milter uses).
# Required: without it, tracking tokens are refused.
# Generate one with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
TOKEN_ENCRYPTION_KEY = os.getenv("TOKEN_ENCRYPTION_KEY", None)

# Logging Configuration
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# Rate Limiting Configuration
# RATE_LIMIT_ENABLED: turns rate limiting on/off (default: True)
RATE_LIMIT_ENABLED = os.getenv("RATE_LIMIT_ENABLED", "true").lower() == "true"
# RATE_LIMIT_REQUESTS: maximum number of requests per time window
RATE_LIMIT_REQUESTS = int(os.getenv("RATE_LIMIT_REQUESTS", "100"))
# RATE_LIMIT_WINDOW: time window in seconds (default: 60 seconds = 1 minute)
RATE_LIMIT_WINDOW = int(os.getenv("RATE_LIMIT_WINDOW", "60"))
# RATE_LIMIT_BURST_MODE: allows small bursts of requests
RATE_LIMIT_BURST_MODE = os.getenv("RATE_LIMIT_BURST_MODE", "false").lower() == "true"
# RATE_LIMIT_BURST_MULTIPLIER: burst-mode multiplier (e.g. 1.5 = 50% more)
RATE_LIMIT_BURST_MULTIPLIER = float(os.getenv("RATE_LIMIT_BURST_MULTIPLIER", "1.5"))


def setup_logging(
    service_name: str = "revbridge", log_level: str = None
) -> logging.Logger:
    """
    Configure the standard logging of the service.
    Uses the shared connect_lib.logging module.

    Args:
        service_name: Service name, used for the log file
        log_level: Log level (DEBUG, INFO, WARNING, ERROR); None uses the environment variable

    Returns:
        Configured logger
    """
    from connect_lib.logging import setup_logging as _setup_logging

    log_dir = os.getenv("LOG_DIR", "./logs")

    return _setup_logging(
        service_name=service_name,
        log_level=log_level or LOG_LEVEL,
        json_format=True,
        log_dir=log_dir,
        log_to_file=True,
    )
