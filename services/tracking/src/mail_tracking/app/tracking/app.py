"""
Tracking service - main application

The tracking service handles:
- email pixels and clicks
- open tracking
- click tracking
- unsubscribe tracking
- rate limiting (abuse protection)
"""

import os
from contextlib import asynccontextmanager
from datetime import datetime

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.security import HTTPBearer
from connect_lib.auth.hybrid import hybrid_auth_middleware

# The webhook dispatcher runs as a separate service
# Central configuration
from mail_tracking.common.config import (
    RATE_LIMIT_BURST_MODE,
    RATE_LIMIT_BURST_MULTIPLIER,
    RATE_LIMIT_ENABLED,
    RATE_LIMIT_REQUESTS,
    RATE_LIMIT_WINDOW,
    REDIS_URL,
    TRACKER_HOST,
    TRACKER_PORT,
    setup_logging,
)
from mail_tracking.shared.auth.api_key_adapter import setup_auth

# Routers
from .routers import (
    tracking_router,
)

# Load environment variables
load_dotenv()

# Configure observability (OTel) - must be before logging
from connect_lib.observability import instrument_fastapi, setup_observability

setup_observability(service_name="mail-tracking-service")

# Central logging
logger = setup_logging("mail-tracking-service")

# A single startup ops event is emitted in the FastAPI lifespan handler below
# once Redis pool, rate limiter, and the FastAPI instrumentation are ready.


# --- DEBUG ATTACH (enabled by environment) ------------------------------------
def _enable_debug_attach_if_needed():
    import os

    flag = os.getenv("DEBUG_ATTACH", "").lower() in {"1", "true", "yes"}
    if not flag:
        return
    try:
        import debugpy

        host = os.getenv("DEBUG_HOST", "127.0.0.1")  # keep it on loopback
        port = int(os.getenv("DEBUG_PORT", "5679"))

        # Avoid a crash when something is already listening (e.g. a Uvicorn reload)
        try:
            debugpy.listen((host, port))
            print(f"debugpy waiting for attach on {host}:{port}")
        except OSError as e:
            print(
                f"debugpy.listen failed ({e}). Continuing without reopening the port..."
            )

        if os.getenv("DEBUG_WAIT", "").lower() in {"1", "true", "yes"}:
            print("waiting for the debugger to connect before continuing...")
            debugpy.wait_for_client()

    except Exception as e:
        print(f"Failed to enable debug attach: {e}")


# Rate Limiting - lazy initialization
_rate_limiter = None
_rate_limit_config = None


def _get_rate_limit_config():
    """Return the rate limiting configuration for tracking (lazy)"""
    global _rate_limit_config

    if _rate_limit_config is not None:
        return _rate_limit_config

    if not RATE_LIMIT_ENABLED:
        return None

    try:
        from connect_lib.rate_limit import RateLimitConfig, RateLimitKeyType

        # Tracking uses 5x the default limit (high volume of pixels/clicks)
        tracking_requests = RATE_LIMIT_REQUESTS * 5

        _rate_limit_config = RateLimitConfig(
            requests=tracking_requests,
            window=RATE_LIMIT_WINDOW,
            key_type=RateLimitKeyType.IP,  # tracking always keys by IP
            key_prefix="connect:ratelimit:tracking",
            exclude_paths=["/health", "/docs", "/redoc", "/openapi.json", "/info"],
            include_headers=True,
            enforce=True,
            error_message="Rate limit exceeded. Too many tracking requests.",
            burst_mode=RATE_LIMIT_BURST_MODE,
            burst_multiplier=RATE_LIMIT_BURST_MULTIPLIER,
        )

        logger.debug(
            f"Rate limiting configured for tracking: {tracking_requests} req/{RATE_LIMIT_WINDOW}s "
            f"(key_type=ip, burst={RATE_LIMIT_BURST_MODE})"
        )

        return _rate_limit_config
    except ImportError as e:
        logger.warning(f"Rate limiting not available: {e}")
        return None


async def _get_rate_limiter():
    """Return the rate limiter for tracking (lazy async initialization)"""
    global _rate_limiter

    if _rate_limiter is not None:
        return _rate_limiter

    config = _get_rate_limit_config()
    if config is None:
        return None

    try:
        from connect_lib.rate_limit import RateLimiter, get_storage, set_decorator_storage
        from connect_lib.redis_client import create_async_client

        # The tracking service has a high concurrent volume of pixels/clicks:
        # the default max_connections=10 would raise MaxConnectionsError under load.
        redis_client = create_async_client(REDIS_URL, max_connections=50)
        storage = await get_storage(redis_client=redis_client)
        set_decorator_storage(storage)
        _rate_limiter = RateLimiter(storage, config)

        return _rate_limiter
    except Exception as e:
        logger.error(f"Error creating the rate limiter: {e}")
        return None


# Lifespan context manager for the startup/shutdown events
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    _enable_debug_attach_if_needed()

    # Pre-initialize rate limiter
    limiter = await _get_rate_limiter()

    # A single startup ops event (log_type=ops event=service_started).
    logger.info(
        f"log_type=ops event=service_started component=mail-tracking-service "
        f"host={TRACKER_HOST} port={TRACKER_PORT} "
        f"rate_limit_enabled={RATE_LIMIT_ENABLED} "
        f"rate_limiter={'ready' if limiter else 'disabled'}"
    )

    yield
    logger.info("log_type=ops event=service_stopped component=mail-tracking-service")


# FastAPI application
app = FastAPI(
    title="RevBridge Track Service API",
    description="""
## RevBridge Connect tracking service v1.0.0

Public endpoints that record email engagement:

- opens, through a transparent pixel
- clicks, with a redirect to the original link
- one-click unsubscribe (RFC 8058) and the unsubscribe link in the body

Events are written to the event outbox, which the webhook dispatcher delivers.
    """,
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_tags=[
        {
            "name": "tracking",
            "description": "**Email tracking**\n\nPublic endpoints that track opens, clicks and unsubscribes: a transparent pixel, a redirect to the original link and the collection of engagement metrics.",
        },
    ],
    license_info={"name": "Apache-2.0", "url": "https://www.apache.org/licenses/LICENSE-2.0"},
    servers=[
        {"url": "http://localhost:8002", "description": "Local development"},
    ],
    lifespan=lifespan,
)
instrument_fastapi(app)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # opens and clicks are plain image/redirect requests
    allow_credentials=False,  # no cookies
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configure authentication before adding the middleware
setup_auth()

# Hybrid authentication middleware (JWT + API key)
app.middleware("http")(hybrid_auth_middleware)




# Rate limiting middleware
@app.middleware("http")
async def rate_limit_middleware(request, call_next):
    """Rate limiting middleware for tracking"""
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

        # Rate limit headers
        if config.include_headers:
            for header, value in result.to_headers().items():
                response.headers[header] = value

        return response
    except Exception as e:
        logger.error(f"Error in the rate limiting middleware: {e}")
        return await call_next(request)


# Routers
app.include_router(tracking_router, prefix="")  # Tracking endpoints

# JWT security scheme (after including the routers)
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

    # Security on every route except health, root, tracking and authentication
    excluded_paths = [
        "/health",
        "/",
        "/info",
        "/docs",
        "/redoc",
        "/openapi.json",
        "/auth/login",
    ]
    tracking_paths = ["/c", "/o", "/u"]  # tracking routes, which must be public

    for path in openapi_schema["paths"]:
        # Exactly an excluded route?
        is_excluded_exact = path in excluded_paths

        # A tracking route (starts with /c, /o, /u)?
        is_tracking_route = any(
            path.startswith(tracking_path) for tracking_path in tracking_paths
        )

        # The route is public if it is an exact exclusion OR a tracking route
        is_public = is_excluded_exact or is_tracking_route

        if not is_public:
            # Security on every method of the route
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
    """Service health check"""
    return {
        "status": "healthy",
        "service": "track-service",
        "timestamp": datetime.utcnow().isoformat(),
    }


# Info endpoint with the rate limit settings
@app.get("/info", tags=["health"])
async def info():
    """Service information, including the rate limit configuration"""
    # The tracking rate limit is 5x the default
    tracking_requests = RATE_LIMIT_REQUESTS * 5

    return {
        "service": "track-service",
        "version": "1.0.0",
        "rate_limit": {
            "enabled": RATE_LIMIT_ENABLED,
            "requests": tracking_requests,
            "window_seconds": RATE_LIMIT_WINDOW,
            "key_type": "ip",  # tracking always keys by IP
            "burst_mode": RATE_LIMIT_BURST_MODE,
        },
        "timestamp": datetime.utcnow().isoformat(),
    }


# The webhook dispatcher runs as a separate service, so no startup/shutdown
# events are needed for it here

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=TRACKER_HOST,
        port=TRACKER_PORT,
        log_level=os.getenv("LOG_LEVEL", "INFO"),
    )
