import logging
import os

from dotenv import load_dotenv

load_dotenv()

REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
REDIS_TTL_SEC = int(os.getenv("REDIS_TTL_SEC", "259200"))

# Short TTL for the email_config cache (email_config:{id} / email_config_domain:{id}).
# A direct UPDATE in the database does not bust the cache; the TTL bounds the window
# during which the worker sends to the old smtp_host. Read-through reloads from the
# database on a miss.
EMAIL_CONFIG_CACHE_TTL_SEC = int(os.getenv("EMAIL_CONFIG_CACHE_TTL_SEC", "3600"))

# Email Queue Configuration
EMAIL_QUEUE_PREFIX = os.getenv("EMAIL_QUEUE_PREFIX", "e")
EMAIL_QUEUE_KEY = os.getenv("EMAIL_QUEUE_KEY", "e:queue")
EMAIL_BRPOP_TIMEOUT = int(os.getenv("EMAIL_BRPOP_TIMEOUT", "5"))

# --- Queue fairness (round-robin per client) ---------------------------------
# Without fairness, e:queue is a single global list, so one large campaign can keep
# every worker busy for hours while the other clients wait.
# Design: with the flag on, the PRODUCER writes the item to e:q:c:{customer} and
# pushes a TOKEN to e:queue (1 token per item). e:queue becomes a semaphore: the
# worker keeps its blocking BRPOP and the queue length still reflects the backlog.
# When the CONSUMER gets a token, it picks WHICH client to pull from via
# round-robin, so each active client gets about 1/N of the throughput.
# The consumer ALWAYS understands BOTH formats (token and inline payload), so
# turning the producer flag off is a safe rollback: whatever is already in the
# client queues is drained by the remaining tokens.
QUEUE_FAIRNESS_ENABLED = os.getenv("QUEUE_FAIRNESS_ENABLED", "false").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
# Round-robin quantum: how many CONSECUTIVE items of the same client the worker
# serves before rotating. 1 = pure alternation (it would defeat SMTP connection
# reuse: every client switch closes/reopens the session). 25 = the connection pays
# off within the turn (and the multi-slot pool keeps the other clients' connections
# between turns), while fairness keeps a granularity of seconds.
QUEUE_FAIRNESS_QUANTUM = int(os.getenv("QUEUE_FAIRNESS_QUANTUM", "25"))
EMAIL_CUSTOMER_QUEUE_PREFIX = os.getenv("EMAIL_CUSTOMER_QUEUE_PREFIX", "e:q:c:")
EMAIL_CUSTOMER_REGISTRY_KEY = os.getenv("EMAIL_CUSTOMER_REGISTRY_KEY", "e:q:customers")
# Token = a JSON dict with only "_t" (distinct from items: no message_id/_ref).
EMAIL_QUEUE_TOKEN = '{"_t": 1}'

# --- Suppression filter at send time ----------------------------------------
# Removes recipients that are on the sender's suppression list (unsubscribes,
# bounces, complaints). It runs in the API, the last point every send goes
# through (transactional, direct API calls, scheduled jobs).
# Modes: "off" | "dryrun" (logs what it would remove) | "enforce" (removes).
# Code default dryrun: measures without changing what is sent; RevBridge Connect
# sets "enforce" in its configuration.
SUPPRESSION_CHECK_MODE = os.getenv("SUPPRESSION_CHECK_MODE", "dryrun").strip().lower()
# Batch size of the IN (...) query: one /send can carry thousands of recipients.
SUPPRESSION_CHECK_CHUNK = int(os.getenv("SUPPRESSION_CHECK_CHUNK", "500"))

# --- Per-domain volume governor (ramp-up) -----------------------------------
# Enforces the daily quota of `domain_volume_ramp` (maintained by the volume ramp
# controller in services/infra). Excess is DEFERRED to tomorrow via scheduled_at
# (never dropped); exempt=1 is never held; fail-open on Redis/MySQL errors. See
# docs/deliverability.md and volume_governor.py.
# Modes: "off" | "dryrun" (logs what it would defer) | "enforce" (really defers).
# Code default off; RevBridge Connect sets "enforce" in its configuration.
VOLUME_GOVERNOR_MODE = os.getenv("VOLUME_GOVERNOR_MODE", "off").strip().lower()
VOLUME_SENT_KEY_PREFIX = os.getenv("VOLUME_SENT_KEY_PREFIX", "volume:sent:")
VOLUME_SENT_TTL_SEC = int(os.getenv("VOLUME_SENT_TTL_SEC", str(2 * 24 * 3600)))
# UTC hour when tomorrow's window starts for deferred traffic (default 11:00 UTC).
VOLUME_DEFER_UTC_HOUR = int(os.getenv("VOLUME_DEFER_UTC_HOUR", "11"))
# 429 only for a request larger than factor x quota (abuse or a caller bug, not a
# legitimate peak).
VOLUME_HARD_REJECT_FACTOR = float(os.getenv("VOLUME_HARD_REJECT_FACTOR", "10"))
# Bulk requests (> threshold recipients) only see (100-reserve)% of the quota, so a
# large campaign does not starve transactional mail (password resets etc).
VOLUME_TXN_RESERVE_PCT = float(os.getenv("VOLUME_TXN_RESERVE_PCT", "20"))
VOLUME_BULK_THRESHOLD = int(os.getenv("VOLUME_BULK_THRESHOLD", "10"))
VOLUME_QUOTA_CACHE_TTL_SEC = int(os.getenv("VOLUME_QUOTA_CACHE_TTL_SEC", "60"))

DB_DSN = os.getenv("DB_DSN", "")

# Database Configuration
DB_CONNECTION_TIMEOUT = int(
    os.getenv("DB_CONNECTION_TIMEOUT", "5")
)  # Short 5s timeout (fails faster)
DB_AUTOCOMMIT = (
    os.getenv("DB_AUTOCOMMIT", "false").lower() == "true"
)  # Autocommit off by default (explicit transactions)

# API Service Configuration
API_HOST = os.getenv("API_HOST", "0.0.0.0")
API_PORT = int(os.getenv("API_PORT", "8001"))

# Track Service Configuration
TRACKER_HOST = os.getenv("TRACKER_HOST", "0.0.0.0")
TRACKER_PORT = int(os.getenv("TRACKER_PORT", "8002"))

# JWT Configuration
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "")  # no default: `make env` generates it
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")

# Validate and cap the JWT token expiration time
# If JWT_ACCESS_TOKEN_EXPIRE_MINUTES is 0 or negative, the token never expires
_raw_expire_minutes = int(os.getenv("JWT_ACCESS_TOKEN_EXPIRE_MINUTES", "0"))
if _raw_expire_minutes <= 0:
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES = None  # Token never expires
else:
    # Cap at a reasonable value (at most 1 week = 10080 minutes)
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES = min(_raw_expire_minutes, 10080)

# Webhook Configuration
WEBHOOK_QUEUE_PREFIX = os.getenv("WEBHOOK_QUEUE_PREFIX", "w")

# Webhook Dispatcher Configuration
# Note: max_workers is forced to 1 in the code (sequential processing)
# Scale by running more dispatcher processes instead of more workers per process
WEBHOOK_MAX_WORKERS = int(os.getenv("WEBHOOK_MAX_WORKERS", "1"))
WEBHOOK_POLL_INTERVAL = float(os.getenv("WEBHOOK_POLL_INTERVAL", "1.0"))
WEBHOOK_BATCH_SIZE = int(
    os.getenv("WEBHOOK_BATCH_SIZE", "1")
)  # Process 1 message at a time
WEBHOOK_RETRY_DELAY = int(os.getenv("WEBHOOK_RETRY_DELAY", "5"))

# Webhook Settings
WEBHOOK_DEFAULT_TIMEOUT = int(os.getenv("WEBHOOK_DEFAULT_TIMEOUT", "30"))
WEBHOOK_DEFAULT_RETRY = int(os.getenv("WEBHOOK_DEFAULT_RETRY", "3"))
WEBHOOK_MAX_RETRY_DELAY = int(os.getenv("WEBHOOK_MAX_RETRY_DELAY", "300"))
WEBHOOK_MAX_PAYLOAD_SIZE = int(os.getenv("WEBHOOK_MAX_PAYLOAD_SIZE", "10000"))

# Webhook Monitoring Configuration
WEBHOOK_STATS_INTERVAL = int(os.getenv("WEBHOOK_STATS_INTERVAL", "60"))
WEBHOOK_HEALTH_INTERVAL = int(os.getenv("WEBHOOK_HEALTH_INTERVAL", "30"))
WEBHOOK_LOG_ROTATION_SIZE = int(
    os.getenv("WEBHOOK_LOG_ROTATION_SIZE", "10485760")
)  # 10MB

# Webhook Logging Configuration
WEBHOOK_LOG_DIR = os.getenv("WEBHOOK_LOG_DIR", "./logs")

# SMTP Configuration
SMTP_HOST = os.getenv("SMTP_HOST", "localhost")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_SECURE = os.getenv("SMTP_SECURE", "false").lower() == "true"
SMTP_USE_STARTTLS = os.getenv("SMTP_USE_STARTTLS", "true").lower() == "true"  # Disable for local testing without TLS
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
SMTP_BASE_PORT = int(os.getenv("SMTP_BASE_PORT", "587"))
SMTP_SKIP_DB_CONFIG = os.getenv("SMTP_SKIP_DB_CONFIG", "false").lower() == "true"  # Skip DB lookup, use env vars only

# Feature flag: reuse the SMTP connection across consecutive emails of the SAME
# customer/config, instead of opening + authenticating + closing a new connection
# per email. OFF by default in the code.
#
# Why: without it the worker does connect -> login -> TLS -> send -> quit for EVERY
# email. That connection churn is exactly what Postfix's anvil limits, throttling
# throughput to a few emails per minute and returning the rest as deferred.
# Reusing the session sends N messages per connection, raising throughput AND
# lowering the pressure on anvil.
#
# SECURITY (isolation between customers): an SMTP connection is authenticated as ONE
# customer (a specific smtp_user/relay). The pool is keyed by
# (smtp_host, smtp_port, secure, starttls, smtp_user) and ONLY reuses a connection for
# messages with the identical key. A different key => close and open a new connection.
# A connection never spans two configs, so a cross-customer leak is impossible by
# construction.
SMTP_CONNECTION_REUSE = os.getenv("SMTP_CONNECTION_REUSE", "false").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
# Maximum messages sent per connection before it is recycled (anvil hygiene, and
# avoids very long sessions). Once reached, the connection is closed and reopened.
SMTP_REUSE_MAX_MESSAGES = int(os.getenv("SMTP_REUSE_MAX_MESSAGES", "50"))
# Maximum simultaneous SMTP connections in the multi-slot pool (one per key/customer;
# LRU eviction above that). With queue fairness the worker alternates between the
# active clients, and the pool keeps each one's connection between turns.
SMTP_REUSE_MAX_CONNS = int(os.getenv("SMTP_REUSE_MAX_CONNS", "8"))
# Maximum time (s) a connection may sit idle in the pool before it is discarded.
# Protects against the server's smtpd_timeout closing the socket (Postfix default
# 300s) and against dead sockets between bursts.
SMTP_REUSE_IDLE_TIMEOUT_SEC = float(os.getenv("SMTP_REUSE_IDLE_TIMEOUT_SEC", "30"))

# Shadow Email Configuration (for audit BCC)
# Emails sent to this address will be filtered from webhook events and engagement tracking
SHADOW_EMAIL = os.getenv("SHADOW_EMAIL", "").strip().lower()

# Encryption key for the tracking tokens (used by the tracking service and the milter).
# Generate one with:
#   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
TOKEN_ENCRYPTION_KEY = os.getenv("TOKEN_ENCRYPTION_KEY", None)

# Feature flag (kill switch): when True (default), the worker re-raises TEMPORARY SMTP
# failures (4xx, disconnects, rate limiting) so the queue re-enqueues them with a
# retry, instead of treating them as a success and dropping the payload. PERMANENT
# failures (5xx, invalid payload) stay terminal. Set "false" to treat temporary
# failures as terminal too, for example when a misbehaving relay causes excessive
# retries.
WORKER_REQUEUE_DEFERRED = os.getenv("WORKER_REQUEUE_DEFERRED", "true").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)

# Lease duration (seconds) of the atomic claim in send_ledger. After this time
# without a terminal state, a 'sending' claim is treated as orphaned (dead worker)
# and can be claimed again. Must exceed the longest SMTP send (including fallbacks).
# Operational tuning, NOT a feature gate.
SEND_LEDGER_LEASE_SEC = int(os.getenv("SEND_LEDGER_LEASE_SEC", "300"))

# --- send_ledger reaper ------------------------------------------------------
# The ledger-reaper process closes what the ledger says was lost (smtp_accepted
# without a delivery event before the deadline, sending with an expired lease) or
# whose backoff expired (deferred): delivered when `deliveries` confirms it,
# otherwise deadletter (Connect keeps no copy to resend).
LEDGER_REAPER_INTERVAL_SEC = int(os.getenv("LEDGER_REAPER_INTERVAL_SEC", "60"))
LEDGER_REAPER_BATCH = int(os.getenv("LEDGER_REAPER_BATCH", "200"))
# Minimum age for a never-claimed 'queued' row (attempt=0) to be treated as
# ORPHANED by the reaper (the API process was killed in the middle of the
# enqueue). Only evaluated with the Redis queue EMPTY: with a backlog, old
# queued rows are legitimate.
LEDGER_QUEUED_ORPHAN_GRACE_SEC = int(os.getenv("LEDGER_QUEUED_ORPHAN_GRACE_SEC", "7200"))
# Window for the first delivery event after the submission was accepted (see
# send_ledger.DEFAULT_DELIVERY_DEADLINE_SEC; deferred events push it forward).
# 3600, not 600: during a campaign burst the `deliveries` update lags minutes
# behind the real send, and a deadline shorter than that lag makes the reaper
# re-inject emails that were ALREADY delivered (a duplicate for the recipient).
# The deadline must be much larger than the worst lag of the event pipeline.
LEDGER_DELIVERY_DEADLINE_SEC = int(os.getenv("LEDGER_DELIVERY_DEADLINE_SEC", "3600"))

# Logging Configuration
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# Rate Limiting Configuration
# RATE_LIMIT_ENABLED: turns rate limiting on/off (default: True)
RATE_LIMIT_ENABLED = os.getenv("RATE_LIMIT_ENABLED", "true").lower() == "true"
# RATE_LIMIT_REQUESTS: maximum number of requests per time window
RATE_LIMIT_REQUESTS = int(os.getenv("RATE_LIMIT_REQUESTS", "100"))
# RATE_LIMIT_WINDOW: time window in seconds (default: 60 seconds = 1 minute)
RATE_LIMIT_WINDOW = int(os.getenv("RATE_LIMIT_WINDOW", "60"))
# RATE_LIMIT_KEY_TYPE: key type used for identification (ip, api_key, account_id)
RATE_LIMIT_KEY_TYPE = os.getenv("RATE_LIMIT_KEY_TYPE", "api_key")
# RATE_LIMIT_BURST_MODE: allows small request peaks
RATE_LIMIT_BURST_MODE = os.getenv("RATE_LIMIT_BURST_MODE", "false").lower() == "true"
# RATE_LIMIT_BURST_MULTIPLIER: burst mode multiplier (e.g. 1.5 = 50% more)
RATE_LIMIT_BURST_MULTIPLIER = float(os.getenv("RATE_LIMIT_BURST_MULTIPLIER", "1.5"))


async def get_email_config_for_domain(domain_id: int):
    """
    Fetches the email configuration of a domain from the database

    Args:
        domain_id: Domain ID (virtual_domains)

    Returns:
        dict: Email configuration, or None if not found
    """
    try:
        from mail_api.common.db import fetch_one_async

        # Query the database directly so smtp_password is included
        # (EmailConfigResponse leaves smtp_password out for security)
        query = """
            SELECT id, config_id, domain_id, name,
                smtp_host, smtp_port, smtp_secure, smtp_user, smtp_password, smtp_base_port, smtp_timeout, envelope_prefix,
                tracking_enabled, inject_pixel, rewrite_links, pixel_tracking_url, click_tracking_url, tracking_domain,
                default_from_name, default_from_email, default_reply_to,
                bounce_email, complaint_email, shadow_email,
                rate_limit_per_minute, rate_limit_per_hour, rate_limit_per_day,
                max_retries, retry_delay_seconds,
                default_webhook_url, default_webhook_secret,
                additional_config, is_active, is_default, priority, notes,
                created_by, created_at, updated_at
            FROM email_configs
            WHERE domain_id = %s AND is_default = TRUE AND is_active = TRUE
            ORDER BY priority DESC, created_at DESC
            LIMIT 1
        """
        result = await fetch_one_async(query, (domain_id,))
        if result:
            return dict(result)
        return None
    except Exception as e:
        logger = logging.getLogger(__name__)
        logger.warning(
            f"Error fetching the configuration from the database for domain {domain_id}: {e}"
        )
        return None


async def get_smtp_config_for_email(
    from_email: str, domain_id: int | None = None, use_cache: bool = True
):
    """
    Fetches the SMTP configuration for a sender email from Redis (cache) or the database

    Args:
        from_email: Sender email (e.g. user@domain.com)
        domain_id: Domain ID (optional; looked up when not given)
        use_cache: If True, tries Redis first (default: True)

    Returns:
        dict: SMTP configuration, or None if not found
    """
    try:
        # Try Redis first when the cache is enabled
        if use_cache:

            from mail_api.common.redis_client import redis_client

            # No domain_id given: look it up from the email's domain
            if not domain_id and "@" in from_email:
                domain = from_email.split("@")[1]
                # Try Redis first
                if redis_client:
                    try:
                        # Try the domain_id from Redis or the database
                        domain_cache_key = f"domain_id:{domain}"
                        domain_id_str = await redis_client.get(domain_cache_key)
                        if domain_id_str:
                            domain_id = int(domain_id_str)
                    except Exception:
                        pass

                # Not in Redis: query the database
                if not domain_id:
                    from mail_api.common.db import fetch_one_async

                    domain_row = await fetch_one_async(
                        "SELECT id FROM virtual_domains WHERE name = %s LIMIT 1",
                        (domain,),
                    )
                    if domain_row:
                        domain_id = domain_row["id"]
                        # Cache the domain_id in Redis
                        if redis_client:
                            try:
                                await redis_client.setex(
                                    domain_cache_key, REDIS_TTL_SEC, str(domain_id)
                                )
                            except Exception:
                                pass

            # Fetch the domain configuration from Redis (falls back to the database on a miss)
            if domain_id and redis_client:
                try:
                    from mail_api.common.email_config_cache import get_domain_config_from_redis
                    cached_config = await get_domain_config_from_redis(domain_id)
                    if cached_config:
                        return {
                            "smtp_host": cached_config.get("smtp_host"),
                            "smtp_port": cached_config.get("smtp_port"),
                            "smtp_secure": cached_config.get("smtp_secure"),
                            "smtp_user": cached_config.get("smtp_user"),
                            "smtp_password": cached_config.get(
                                "smtp_password"
                            ),  # Note: the password must already be decrypted
                            "smtp_base_port": cached_config.get("smtp_base_port"),
                            "smtp_timeout": cached_config.get("smtp_timeout"),
                        }
                except Exception as e:
                    logger = logging.getLogger(__name__)
                    logger.debug(f"Error fetching the configuration from Redis: {e}")

        # Fallback: query the database
        if not domain_id and "@" in from_email:
            domain = from_email.split("@")[1]
            from mail_api.common.db import fetch_one_async

            domain_row = await fetch_one_async(
                "SELECT id FROM virtual_domains WHERE name = %s LIMIT 1", (domain,)
            )
            if domain_row:
                domain_id = domain_row["id"]

        if not domain_id:
            return None

        config = await get_email_config_for_domain(domain_id)
        if config:
            return {
                "smtp_host": config.get("smtp_host"),
                "smtp_port": config.get("smtp_port"),
                "smtp_secure": config.get("smtp_secure"),
                "smtp_user": config.get("smtp_user"),
                "smtp_password": config.get(
                    "smtp_password"
                ),  # Note: the password must already be decrypted
                "smtp_base_port": config.get("smtp_base_port"),
                "smtp_timeout": config.get("smtp_timeout"),
            }
        return None
    except Exception as e:
        logger = logging.getLogger(__name__)
        logger.warning(f"Error fetching the SMTP configuration for {from_email}: {e}")
        return None


async def get_smtp_credentials_for_email(
    from_email: str, api_key: str | None = None
) -> tuple[str | None, str | None]:
    """
    Returns the SMTP credentials for a sender email.

    If api_key is given (from the JWT), that api_key is used directly.
    Otherwise the credentials are read from the database.

    If smtp_password or api_key is an API key (JWT), the API key is validated
    to obtain the api_key_id used as smtp_user.

    Args:
        from_email: Sender email (e.g. user@domain.com)
        api_key: API key from the JWT (optional; used when given)

    Returns:
        tuple: (api_key_id, api_key), or (None, None) if not found
    """
    logger = logging.getLogger(__name__)

    try:
        # api_key given (from the JWT): use it directly
        if api_key:
            logger.info(f"Using the JWT's API key for {from_email}")
            smtp_password = api_key
            smtp_user = None  # Obtained through validation
        else:
            # Query the database
            from mail_api.common.db import fetch_one_async

            # First get the domain_id to look up the configuration
            domain = from_email.split("@")[1] if "@" in from_email else None
            if not domain:
                logger.warning(f"Invalid email: {from_email}")
                return None, None

            domain_row = await fetch_one_async(
                "SELECT id FROM virtual_domains WHERE name = %s LIMIT 1", (domain,)
            )
            if not domain_row:
                logger.warning(f"Domain not found: {domain}")
                return None, None

            domain_id = domain_row["id"]

            # Look up credentials specific to this email
            credential_query = """
                SELECT sc.smtp_user, sc.smtp_password, ec.id as email_config_id
                FROM smtp_credentials sc
                INNER JOIN email_configs ec ON sc.email_config_id = ec.id
                WHERE sc.from_email = %s
                  AND sc.is_active = TRUE
                  AND ec.domain_id = %s
                  AND ec.is_active = TRUE
                ORDER BY sc.priority DESC, sc.created_at DESC
                LIMIT 1
            """
            credential_row = await fetch_one_async(
                credential_query, (from_email, domain_id)
            )

            if credential_row:
                # Email-specific credentials found
                smtp_user = credential_row.get("smtp_user")
                smtp_password = credential_row.get("smtp_password")
                logger.debug(f"Email-specific credentials found for {from_email}")
            else:
                # Use the domain's general configuration
                config = await get_smtp_config_for_email(from_email)

                if not config:
                    logger.warning(
                        f"SMTP configuration not found in the database for {from_email}"
                    )
                    return None, None

                # Configuration found in the database
                smtp_user = config.get("smtp_user")
                smtp_password = config.get("smtp_password")

        if not smtp_password:
            logger.warning(f"SMTP configuration without a password for {from_email}")
            return None, None

        logger.debug(
            f"Credentials found for {from_email}: "
            f"user={'***' if smtp_user else None}, "
            f"password={'***' if smtp_password else None}"
        )

        # Is smtp_password an API key (JWT)?
        # API keys start with a prefix (e.g. rbak_)
        from mail_api.services.api_key_service import api_key_service

        api_key_prefix = api_key_service.key_prefix

        logger.debug(
            f"Checking whether the password is an API key (prefix={api_key_prefix}) "
            f"for {from_email}"
        )

        if smtp_password.startswith(api_key_prefix):
            # smtp_password is an API key (JWT): validate it to get the api_key_id
            logger.info(f"Password is an API key, validating it for {from_email}")
            try:
                api_key_data = await api_key_service.validate_api_key(smtp_password)
                if api_key_data:
                    # api_key_id from the validation
                    api_key_id = api_key_data.get("api_key_id")
                    if api_key_id:
                        # Return (api_key_id, api_key)
                        logger.info(
                            f"API key validated for {from_email}, "
                            f"api_key_id={api_key_id}"
                        )
                        return api_key_id, smtp_password
                    # No api_key_id: use the smtp_user when available
                    if smtp_user:
                        logger.debug(f"Using the database smtp_user for {from_email}")
                        return smtp_user, smtp_password
                    logger.warning(
                        f"Valid API key without an api_key_id for {from_email}"
                    )
                    return None, None
            except Exception as e:
                logger.warning(f"Error validating the api_key for {from_email}: {e}")
                # Validation failed: use the smtp_user when available
                if smtp_user:
                    return smtp_user, smtp_password
                return None, None

        # Not an API key: use traditional credentials
        # smtp_user may already be the api_key_id or another value
        if smtp_user:
            return smtp_user, smtp_password

        # No smtp_user but a password that is not an API key
        logger.warning(
            f"SMTP configuration without smtp_user for {from_email}, "
            f"and the password is not an api_key"
        )
        return None, None

    except Exception as e:
        logger.error(
            f"Error fetching the SMTP credentials from the database for {from_email}: {e}",
            exc_info=True,
        )
        return None, None


# Grouped settings, for convenience
DISPATCHER_CONFIG = {
    "max_workers": WEBHOOK_MAX_WORKERS,
    "poll_interval": WEBHOOK_POLL_INTERVAL,
    "batch_size": WEBHOOK_BATCH_SIZE,
    "retry_delay": WEBHOOK_RETRY_DELAY,
}


def setup_logging(
    service_name: str = "revbridge", log_level: str = None
) -> logging.Logger:
    """
    Configures standard logging for every service.
    Uses the centralized connect_lib.logging module.

    Args:
        service_name: Service name, used for the log file
        log_level: Log level (DEBUG, INFO, WARNING, ERROR); if None, uses the environment variable

    Returns:
        The configured logger
    """
    log_dir = os.getenv("LOG_DIR", "./logs")
    log_level = log_level or LOG_LEVEL

    from connect_lib.logging import setup_logging as _setup_logging

    return _setup_logging(
        service_name=service_name,
        log_level=log_level,
        json_format=True,
        log_dir=log_dir,
        log_to_file=True,
    )
