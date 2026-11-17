"""
Hybrid authentication (JWT + API Key)
"""

import logging
from typing import Optional, Dict, Any, List
from fastapi import HTTPException, Request, status
from fastapi.responses import JSONResponse

from .jwt import jwt_manager
from .api_key import validate_api_key_async

logger = logging.getLogger(__name__)


class HybridAuth:
    """Hybrid authentication class (JWT + API Key) for FastAPI"""

    def __init__(
        self, jwt_manager_instance=None, public_routes: Optional[List[str]] = None
    ):
        """
        Initializes the hybrid authentication

        Args:
            jwt_manager_instance: JWTManager instance (default: the global jwt_manager)
            public_routes: List of public routes that do not require authentication
        """
        self.jwt_manager = jwt_manager_instance or jwt_manager
        self.public_routes = public_routes or [
            "/",
            "/health",
            "/info",  # Service metadata (name, endpoints) — public like /health
            "/docs",
            "/openapi.json",
            "/redoc",
            "/auth/login",  # User login - no authentication required
            # Tracking routes are public (pixels, clicks, unsubscribe)
            "/o/",  # Open tracking
            "/c/",  # Click tracking
            "/u/",  # Unsubscribe tracking
        ]

    async def __call__(self, request: Request):
        """Validates the JWT or the API Key of the request"""
        # RevBridge Connect: every request needs a JWT or an API key; no environment
        # variable or header can turn authentication off.
        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Access token or API key required",
                headers={"WWW-Authenticate": "Bearer"},
            )

        token = auth_header.split(" ")[1]

        # Try JWT authentication first
        jwt_error = None
        try:
            jwt_payload = self.jwt_manager.verify_token(token)
            if jwt_payload:
                request.state.user = jwt_payload
                request.state.auth_type = "jwt"
                logger.debug(
                    f"JWT authentication successful for user: {jwt_payload.get('sub') or jwt_payload.get('user_id')}"
                )
                return
        except HTTPException as e:
            # Keep the JWT error to include it in the final message if the API Key also fails
            jwt_error = e.detail
            logger.debug(f"JWT authentication failed: {e.detail}")
        except Exception as e:
            # Other errors are logged too
            jwt_error = str(e)
            logger.debug(f"JWT authentication error: {e}")

        # Try API Key authentication (the validator has a Redis cache, AUTH_CACHE_TTL).
        api_key_error = None
        try:
            api_key_data = await validate_api_key_async(token)
            if api_key_data:
                request.state.user = api_key_data
                request.state.auth_type = "api_key"
                logger.debug(
                    f"API Key authentication successful for user: {api_key_data.get('user_id') or api_key_data.get('service_account_id')}"
                )
                return
        except RuntimeError as e:
            # Validator not configured - not an authentication error, just log it
            logger.warning(f"API Key validator not configured: {e}")
            api_key_error = "API Key authentication not available"
        except HTTPException as e:
            api_key_error = e.detail
            logger.debug(f"API Key authentication failed: {e.detail}")
        except Exception as e:
            api_key_error = str(e)
            logger.debug(f"API Key authentication error: {e}")

        # Both authentications failed - build a detailed error message
        error_details = []
        if jwt_error:
            error_details.append(f"JWT: {jwt_error}")
        if api_key_error:
            error_details.append(f"API Key: {api_key_error}")

        error_message = "Invalid token or API key"
        if error_details:
            error_message = "; ".join(error_details)

        logger.warning(f"Authentication failed for {request.url.path}: {error_message}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=error_message,
            headers={"WWW-Authenticate": "Bearer"},
        )

    def is_public_route(self, path: str) -> bool:
        """
        Checks whether a route is public

        Args:
            path: Route path

        Returns:
            True if the route is public, False otherwise
        """
        for route in self.public_routes:
            if route == "/":
                # Exact match only for the root path "/"
                if path == "/":
                    return True
            else:
                # Other routes match by prefix
                if path.startswith(route):
                    return True
        return False


# Global hybrid authentication instance
hybrid_auth = HybridAuth()


def get_current_user_hybrid(request: Request) -> Dict[str, Any]:
    """
    Gets the data of the user authenticated through JWT or API Key

    Args:
        request: FastAPI Request object

    Returns:
        Authenticated user data

    Raises:
        HTTPException: If the user is not authenticated
    """

    # Check that request.state exists
    if not hasattr(request, "state"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Request state not found",
        )

    # Check that request.state.user exists and has a value
    if not hasattr(request.state, "user") or request.state.user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="User not authenticated"
        )

    return request.state.user


def get_auth_type(request: Request) -> str:
    """
    Returns the authentication type used

    Args:
        request: FastAPI Request object

    Returns:
        Authentication type: 'jwt' or 'api_key'
    """
    return getattr(request.state, "auth_type", "unknown")


def check_hybrid_permission(required_permission: str = None, required_role: str = None):
    """
    Checks a permission considering both JWT (roles) and API Key (permissions)

    Args:
        required_permission: Permission required for API Keys (e.g. "read:email")
        required_role: Role required for JWT (e.g. "admin")

    Returns:
        FastAPI dependency function
    """

    def permission_checker(request: Request) -> Dict[str, Any]:
        user_data = get_current_user_hybrid(request)
        auth_type = get_auth_type(request)

        # API Keys: check permissions
        if auth_type == "api_key":
            if not required_permission:
                return user_data

            # Check for the admin permission (full access)
            if "admin" in user_data.get("permissions", []):
                return user_data

            # Check the specific permission
            if required_permission not in user_data.get("permissions", []):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Insufficient permission. Required: {required_permission}",
                )

        # JWT: check roles
        elif auth_type == "jwt":
            if not required_role:
                return user_data

            # Role hierarchy
            role_hierarchy = {"viewer": 1, "user": 2, "admin": 3}

            user_role_level = role_hierarchy.get(user_data.get("role", "viewer"), 0)
            required_role_level = role_hierarchy.get(required_role, 0)

            if user_role_level < required_role_level:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Insufficient role. Required: {required_role}",
                )

        return user_data

    return permission_checker


async def hybrid_auth_middleware(request: Request, call_next):
    """
    Hybrid middleware that accepts both JWT and API Key

    Args:
        request: FastAPI Request
        call_next: Next function in the pipeline

    Returns:
        The processed response
    """
    # Check whether this is a public route
    path = request.url.path
    if hybrid_auth.is_public_route(path):
        return await call_next(request)

    # Protected routes ALWAYS go through hybrid authentication
    try:
        await hybrid_auth(request)

        # Make sure the user data is set before continuing
        if not hasattr(request.state, "user") or request.state.user is None:
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "Authentication failed"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)
    except HTTPException as e:
        # On failure, return the authentication error
        return JSONResponse(
            status_code=e.status_code,
            content={"detail": e.detail},
            headers=e.headers or {},
        )
    except Exception as e:
        logger.error(
            f"Unexpected error in the authentication middleware: {e}", exc_info=True
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Internal server error"},
            headers={},
        )


# Dependencies for each kind of permission (examples)
def require_read_emails_hybrid(request: Request) -> Dict[str, Any]:
    """Dependency for the email read permission (hybrid)"""
    return check_hybrid_permission(
        required_permission="read:email", required_role="viewer"
    )(request)


def require_write_emails_hybrid(request: Request) -> Dict[str, Any]:
    """Dependency for the email write permission (hybrid)"""
    return check_hybrid_permission(
        required_permission="write:email", required_role="user"
    )(request)


def require_read_stats_hybrid(request: Request) -> Dict[str, Any]:
    """Dependency for the statistics read permission (hybrid)"""
    return check_hybrid_permission(
        required_permission="read:stat", required_role="viewer"
    )(request)


def require_read_webhooks_hybrid(request: Request) -> Dict[str, Any]:
    """Dependency for the webhook read permission (hybrid)"""
    return check_hybrid_permission(
        required_permission="read:webhook", required_role="viewer"
    )(request)


def require_write_webhooks_hybrid(request: Request) -> Dict[str, Any]:
    """Dependency for the webhook write permission (hybrid)"""
    return check_hybrid_permission(
        required_permission="write:webhook", required_role="user"
    )(request)


def require_read_suppressions_hybrid(request: Request) -> Dict[str, Any]:
    """Dependency for the suppression read permission (hybrid)"""
    return check_hybrid_permission(
        required_permission="read:suppression", required_role="viewer"
    )(request)


def require_write_suppressions_hybrid(request: Request) -> Dict[str, Any]:
    """Dependency for the suppression write permission (hybrid)"""
    return check_hybrid_permission(
        required_permission="write:suppression", required_role="user"
    )(request)
