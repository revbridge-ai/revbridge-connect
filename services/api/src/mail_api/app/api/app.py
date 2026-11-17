"""
RevBridge API Service - Main Application

Unified API service providing:
- Email sending and tracking
- Webhook management
- Tracking statistics
- Suppression list management
- Deliverability metrics
- Webhook events
- Email configuration
- Approved senders management
- Health monitoring
- Rate limiting protection
"""

import os
from contextlib import asynccontextmanager
from datetime import datetime

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Centralized settings
from mail_api.common.config import (
    RATE_LIMIT_BURST_MODE,
    RATE_LIMIT_BURST_MULTIPLIER,
    RATE_LIMIT_ENABLED,
    RATE_LIMIT_KEY_TYPE,
    RATE_LIMIT_REQUESTS,
    RATE_LIMIT_WINDOW,
    REDIS_URL,
    setup_logging,
)

# Routers
from .routers import (
    admin_health_router,
    api_key_domain_router,
    approved_sender_router,
    deliverability_router,
    dkim_identity_router,
    dkim_key_router,
    email_config_router,
    email_router,
    events_router,
    sender_validation_router,
    stats_router,
    suppressions_router,
    volume_router,
    webhook_router,
    webhook_stats_router,
)

# Load environment variables
load_dotenv()

# Configure observability (OTel) - must be before logging
from connect_lib.observability import instrument_fastapi, setup_observability

setup_observability(service_name="mail-api-service")

# Centralized logging
logger = setup_logging("mail-api-service")
# Startup message, printed every time the application loads
logger.info("Starting RevBridge API Service...")

# Port (environment variable, default 8001)
API_SERVICE_HOST = os.getenv("API_SERVICE_HOST", "0.0.0.0")
API_SERVICE_PORT = int(os.getenv("API_SERVICE_PORT", os.getenv("API_PORT", "8001")))


# --- DEBUG ATTACH (enabled by env) --------------------------------------------
def _enable_debug_attach_if_needed():
    import os

    flag = os.getenv("DEBUG_ATTACH", "").lower() in {"1", "true", "yes"}
    if not flag:
        return
    try:
        import debugpy

        host = os.getenv("DEBUG_HOST", "127.0.0.1")  # keep it on loopback outside development
        port = int(os.getenv("DEBUG_PORT", "5680"))

        # Avoid crashing if something is already listening (e.g. a Uvicorn reload)
        try:
            debugpy.listen((host, port))
            print(f"debugpy waiting for a debugger to attach on {host}:{port}")
        except OSError as e:
            print(
                f"debugpy.listen failed ({e}). Continuing without reopening the port..."
            )

        if os.getenv("DEBUG_WAIT", "").lower() in {"1", "true", "yes"}:
            print("Waiting for the debugger (e.g. VS Code) to attach before continuing...")
            debugpy.wait_for_client()

    except Exception as e:
        print(f"Failed to enable debug attach: {e}")


# Rate Limiting - lazy initialization
_rate_limiter = None
_rate_limit_config = None
_login_attempt_service = None


def _get_rate_limit_config():
    """Returns the rate limiting configuration (lazy)."""
    global _rate_limit_config

    if _rate_limit_config is not None:
        return _rate_limit_config

    if not RATE_LIMIT_ENABLED:
        return None

    try:
        from connect_lib.rate_limit import RateLimitConfig, RateLimitKeyType

        key_type_map = {
            "ip": RateLimitKeyType.IP,
            "api_key": RateLimitKeyType.API_KEY,
            "user_id": RateLimitKeyType.USER_ID,
            "account_id": RateLimitKeyType.ACCOUNT_ID,
        }
        key_type = key_type_map.get(RATE_LIMIT_KEY_TYPE.lower(), RateLimitKeyType.API_KEY)

        _rate_limit_config = RateLimitConfig(
            requests=RATE_LIMIT_REQUESTS,
            window=RATE_LIMIT_WINDOW,
            key_type=key_type,
            key_prefix="connect:ratelimit:api",
            exclude_paths=["/health", "/docs", "/redoc", "/openapi.json", "/info"],
            include_headers=True,
            enforce=True,
            error_message="Rate limit exceeded. Please slow down your requests.",
            burst_mode=RATE_LIMIT_BURST_MODE,
            burst_multiplier=RATE_LIMIT_BURST_MULTIPLIER,
        )

        logger.info(
            f"Rate limiting configured: {RATE_LIMIT_REQUESTS} req/{RATE_LIMIT_WINDOW}s "
            f"(key_type={RATE_LIMIT_KEY_TYPE}, burst={RATE_LIMIT_BURST_MODE})"
        )

        return _rate_limit_config
    except ImportError as e:
        logger.warning(f"Rate limiting not available: {e}")
        return None


async def _get_rate_limiter():
    """Returns the rate limiter (lazy async initialization)."""
    global _rate_limiter

    if _rate_limiter is not None:
        return _rate_limiter

    config = _get_rate_limit_config()
    if config is None:
        return None

    try:
        import redis.asyncio as aioredis
        from connect_lib.rate_limit import RateLimiter, get_storage, set_decorator_storage

        # Redis client with resilience settings
        # (similar to AsyncRedisClient in redis_client.py)
        redis_client = aioredis.from_url(
            REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
            health_check_interval=30,
            retry_on_timeout=True,
        )

        storage = await get_storage(redis_client=redis_client)
        set_decorator_storage(storage)
        _rate_limiter = RateLimiter(storage, config)

        return _rate_limiter
    except Exception as e:
        logger.error(f"Error creating the rate limiter: {e}")
        return None


async def _setup_login_attempt_protection():
    """Sets up brute-force protection for login."""
    global _login_attempt_service

    try:
        import redis.asyncio as aioredis
        from connect_lib.login_attempt import (
            LoginAttemptService,
            RedisLoginAttemptStorage,
            set_service,
        )

        # Redis client with resilience settings
        redis_client = aioredis.from_url(
            REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
            health_check_interval=30,
            retry_on_timeout=True,
        )
        storage = RedisLoginAttemptStorage(redis_client, key_prefix="connect:login_attempt:api")
        _login_attempt_service = LoginAttemptService(storage)
        set_service(_login_attempt_service)

        logger.info("Login attempt protection configured with Redis storage")
        return _login_attempt_service
    except ImportError as e:
        logger.warning(f"Login attempt protection not available: {e}")
        return None
    except Exception as e:
        logger.error(f"Error configuring login attempt protection: {e}")
        return None


# Lifespan context manager for startup/shutdown events
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    _enable_debug_attach_if_needed()

    # Setup login attempt protection (brute force protection)
    await _setup_login_attempt_protection()

    # Pre-initialize the rate limiter (the middleware is not added here)
    await _get_rate_limiter()

    yield

    # Shutdown - close the Redis pool
    try:
        from mail_api.common.redis_client import redis_client
        if redis_client:
            await redis_client.close()
            logger.info("Redis pool closed on shutdown")
    except Exception as e:
        logger.warning(f"Error closing the Redis pool on shutdown: {e}")


# FastAPI application
app = FastAPI(
    title="RevBridge Mail API",
    description="""
## RevBridge Mail API

HTTP API of a self-hosted RevBridge Connect installation: send email, track engagement and manage deliverability.

### Email Sending

- Transactional and marketing email delivery
- Custom template support with dynamic personalization
- High-performance queue-based processing
- Real-time delivery tracking

### Webhooks

- Real-time event notifications
- Automatic retry with exponential backoff
- HMAC signature verification
- Complete performance monitoring

### Analytics & Reporting

- Detailed delivery statistics
- Engagement metrics (opens, clicks)
- Deliverability reports
- Performance dashboards

### Suppression Management

- Automatic suppression lists
- Unsubscribe handling
- Spam prevention
- Regulatory compliance

### Security

- JWT authentication
- Role-based access control
- API key management
- Domain verification

### Advanced Features

- DKIM signing configuration
- Approved sender management
- Multi-domain support
- Email configuration per domain

---

**RevBridge Connect** | **Version 1.0.0** | Licensed under Apache-2.0
    """,
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_tags=[
        {
            "name": "emails",
            "description": "**Email Sending & Management**\n\nEndpoints for sending transactional and marketing emails with template support, queue-based delivery, and real-time tracking.",
        },
        {
            "name": "webhooks",
            "description": "**Webhook Management**\n\nComplete webhook management for real-time email event notifications, with automatic retry and performance monitoring.",
        },
        {
            "name": "webhook-stats",
            "description": "**Webhook Statistics**\n\nDetailed webhook performance metrics including success rates, response times, processed events, and system analytics.",
        },
        {
            "name": "webhook-events",
            "description": "**Webhook Events**\n\nWebhook event processing and management, delivery logs, and real-time notification system.",
        },
        {
            "name": "webhook-admin",
            "description": "**Webhook Administration**\n\nAdministrative endpoints for webhook configuration, monitoring, and system maintenance.",
        },
        {
            "name": "statistics",
            "description": "**Tracking Statistics**\n\nEndpoints for detailed engagement statistics, performance metrics, and tracking reports.",
        },
        {
            "name": "suppressions",
            "description": "**Suppression Management**\n\nComplete suppression list management including unsubscribes, bounces, and spam complaints for compliance and deliverability.",
        },
        {
            "name": "deliverability",
            "description": "**Deliverability Metrics**\n\nDomain reputation monitoring, delivery rates, bounce analysis, and quality metrics for deliverability optimization.",
        },
        {
            "name": "authentication",
            "description": "**Authentication & Authorization**\n\nJWT authentication system with role-based access control, user management, and advanced security features.",
        },
        {
            "name": "health",
            "description": "**System Health & Monitoring**\n\nEndpoints for system health checks, status information, and performance monitoring.",
        },
    ],
    license_info={"name": "Apache-2.0", "url": "https://www.apache.org/licenses/LICENSE-2.0"},
    servers=[
        {"url": "http://localhost:8001", "description": "Local development"},
    ],
    lifespan=lifespan,
)
instrument_fastapi(app)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # the panel is same-origin (Caddy); API clients are servers
    allow_credentials=False,  # auth is a Bearer header, never a cookie
    allow_methods=["*"],
    allow_headers=["*"],
)

# Hybrid authentication middleware (JWT + API Key)
from mail_api.shared.auth.api_key_adapter import setup_auth
from connect_lib.auth.hybrid import hybrid_auth_middleware

# Configure authentication before adding the middleware
setup_auth()

app.middleware("http")(hybrid_auth_middleware)


# Rate limiting middleware (function-based, to avoid the add_middleware-after-startup error)
@app.middleware("http")
async def rate_limit_middleware(request, call_next):
    """Function-based rate limiting middleware."""
    config = _get_rate_limit_config()

    # Rate limiting disabled: pass through
    if config is None:
        return await call_next(request)

    # No header-based exemption: every request counts against the limit.

    # Excluded path?
    if any(request.url.path.startswith(p) for p in config.exclude_paths):
        return await call_next(request)

    try:
        limiter = await _get_rate_limiter()
        if limiter is None:
            return await call_next(request)

        # Build the key and check the rate limit
        key = limiter.build_key(request)
        result = await limiter.check(key)

        if not result.allowed:
            from fastapi.responses import JSONResponse
            response = JSONResponse(
                status_code=429,
                content={
                    "error": "rate_limit_exceeded",
                    "message": config.error_message,
                    "limit": result.limit,
                    "window": config.window,
                    "retry_after": result.retry_after,
                },
            )
            if config.include_headers:
                for header, value in result.to_headers().items():
                    response.headers[header] = value
                response.headers["Retry-After"] = str(result.retry_after)
            return response

        # Process the request
        response = await call_next(request)

        # Add the rate limit headers
        if config.include_headers:
            for header, value in result.to_headers().items():
                response.headers[header] = value

        return response
    except Exception as e:
        # Redis connection errors are transient: log them as warnings
        error_msg = str(e)
        if "Connection" in error_msg or "reset by peer" in error_msg or "timeout" in error_msg.lower():
            logger.warning(f"Rate limiting temporarily unavailable (Redis connection): {e}")
        else:
            logger.error(f"Error in the rate limiting middleware: {e}")
        # On error, let the request through (fail open)
        return await call_next(request)


# Routers
# NOTE: routes have no prefix; a reverse proxy may rewrite /email/* -> /*
app.include_router(admin_health_router)  # Administrative health checks
app.include_router(webhook_router)  # Webhook CRUD
app.include_router(webhook_stats_router)  # Webhook statistics
app.include_router(email_router)  # Email sending
app.include_router(email_config_router)  # Email configurations
app.include_router(approved_sender_router)  # Approved Senders
app.include_router(stats_router)  # Tracking statistics
app.include_router(suppressions_router)  # Suppression management
app.include_router(volume_router)  # Volume governor allowance (ramp-up)
app.include_router(deliverability_router)  # Deliverability metrics
app.include_router(events_router)  # Webhook events
app.include_router(dkim_identity_router)  # DKIM identities
app.include_router(dkim_key_router)  # DKIM keys
app.include_router(api_key_domain_router)  # Domains authorized per API Key (multi-domain)
app.include_router(sender_validation_router)  # from_address validation (/me/validate-sender)

# RevBridge Connect: panel login (JWT) and API key management, served by this API.
# Can be turned off with CONNECT_ADMIN_API_ENABLED.
if os.getenv("CONNECT_ADMIN_API_ENABLED", "true").lower() in ("1", "true", "yes"):
    from .routers.api_key_router import api_key_router
    from .routers.auth_router import auth_router

    app.include_router(auth_router)  # /auth/login, /auth/me, ...
    app.include_router(api_key_router)  # /api-keys (create, list, revoke)
    from .routers.connect_domains_router import router as connect_domains_router

    app.include_router(connect_domains_router)  # /domains/{domain}/dns-info (panel)

# RevBridge Connect: deliverability job metrics in Prometheus format (internal network).
if os.getenv("CONNECT_METRICS_SINK", "").lower() == "redis":
    from connect_lib.auth.hybrid import hybrid_auth as _hybrid_auth

    from .routers.metrics_router import metrics_router

    app.include_router(metrics_router)
    # /metrics is scraped by Prometheus on the internal network, without a token; Caddy
    # blocks the path on the public hosts. The auth middleware only knows its fixed list
    # of public routes.
    if "/metrics" not in _hybrid_auth.public_routes:
        _hybrid_auth.public_routes.append("/metrics")

# JWT security scheme (after the routers are included)
from fastapi.openapi.utils import get_openapi
from fastapi.security import HTTPBearer

security = HTTPBearer()


def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema

    openapi_schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )

    # JWT security scheme
    openapi_schema["components"]["securitySchemes"] = {
        "BearerAuth": {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
        }
    }

    # Apply security to every route except health, root, docs and authentication
    excluded_paths = [
        "/health",
        "/",
        "/info",
        "/docs",
        "/redoc",
        "/openapi.json",
        "/auth/login",
    ]

    for path in openapi_schema["paths"]:
        # Exact match with an excluded route?
        is_excluded_exact = path in excluded_paths

        # The route is public only on an exact match
        is_public = is_excluded_exact

        if not is_public:
            # Apply security to every method of the route
            for method in openapi_schema["paths"][path]:
                if method != "parameters" and isinstance(
                    openapi_schema["paths"][path][method], dict
                ):
                    openapi_schema["paths"][path][method]["security"] = [
                        {"BearerAuth": []}
                    ]

    app.openapi_schema = openapi_schema
    return app.openapi_schema


app.openapi = custom_openapi


# -----------------------------------------------------------------------------


# Public health check
@app.get("/health", tags=["health"])
async def health_check():
    """Service health check."""
    return {
        "status": "healthy",
        "service": "api-service",
        "timestamp": datetime.utcnow().isoformat(),
    }


# Info endpoint with rate limit and login protection settings
@app.get("/info", tags=["health"])
async def info():
    """Service information, including the rate limit and login protection settings."""
    # Is login attempt protection enabled?
    login_protection_enabled = False
    try:
        from connect_lib.login_attempt import get_service
        login_protection_enabled = get_service() is not None
    except ImportError:
        pass

    return {
        "service": "api-service",
        "version": "1.0.0",
        "rate_limit": {
            "enabled": RATE_LIMIT_ENABLED,
            "requests": RATE_LIMIT_REQUESTS,
            "window_seconds": RATE_LIMIT_WINDOW,
            "key_type": RATE_LIMIT_KEY_TYPE,
            "burst_mode": RATE_LIMIT_BURST_MODE,
        },
        "login_protection": {
            "enabled": login_protection_enabled,
            "max_attempts": 5,
            "window_minutes": 15,
            "block_minutes": 15,
        },
        "timestamp": datetime.utcnow().isoformat(),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=API_SERVICE_HOST,
        port=API_SERVICE_PORT,
        log_level=os.getenv("LOG_LEVEL", "INFO"),
    )
