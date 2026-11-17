"""
Shared JWT authentication module for the RevBridge services

This module provides:
- Generation and validation of JWT tokens
- An authentication middleware for FastAPI
- Exclusion of specific routes from authentication
"""

import jwt
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any
from fastapi import HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from ..config import JWT_SECRET_KEY, JWT_ALGORITHM, JWT_ACCESS_TOKEN_EXPIRE_MINUTES


class JWTManager:
    """JWT token manager"""

    def __init__(
        self,
        secret_key: Optional[str] = None,
        algorithm: Optional[str] = None,
        access_token_expire_minutes: Optional[int] = None,
    ):
        """
        Initializes the JWT manager

        Args:
            secret_key: Secret key used to sign tokens (default: JWT_SECRET_KEY from config)
            algorithm: Signing algorithm (default: JWT_ALGORITHM from config)
            access_token_expire_minutes: Minutes until expiration (default: JWT_ACCESS_TOKEN_EXPIRE_MINUTES from config)
        """
        self.secret_key = secret_key or JWT_SECRET_KEY
        self.algorithm = algorithm or JWT_ALGORITHM
        self.access_token_expire_minutes = (
            access_token_expire_minutes or JWT_ACCESS_TOKEN_EXPIRE_MINUTES
        )

    def _require_secret(self) -> None:
        """RevBridge Connect: without a secret (or with one that is too short) there are no sessions."""
        if not self.secret_key or len(self.secret_key) < 32:
            raise RuntimeError("JWT_SECRET_KEY is missing or too short (minimum 32 characters); run make env")

    def create_access_token(
        self, data: Dict[str, Any], expires_delta: Optional[timedelta] = None
    ) -> str:
        """
        Creates a JWT access token

        Args:
            data: Data to include in the token
            expires_delta: Custom expiration time

        Returns:
            Encoded JWT token
        """
        to_encode = data.copy()

        if expires_delta:
            expire = datetime.now(timezone.utc) + expires_delta
            to_encode.update({"exp": int(expire.timestamp())})
        elif self.access_token_expire_minutes is not None:
            # The value was already validated in the configuration; this is extra protection
            try:
                expire = datetime.now(timezone.utc) + timedelta(
                    minutes=self.access_token_expire_minutes
                )
                to_encode.update({"exp": int(expire.timestamp())})
            except OverflowError:
                # Fall back to 30 minutes if it still overflows
                expire = datetime.now(timezone.utc) + timedelta(minutes=30)
                to_encode.update({"exp": int(expire.timestamp())})
        # If access_token_expire_minutes is None, no 'exp' claim is added (verify_token then
        # rejects the token, since 'exp' is required)

        self._require_secret()
        encoded_jwt = jwt.encode(to_encode, self.secret_key, algorithm=self.algorithm)
        return encoded_jwt

    def verify_token(self, token: str) -> Dict[str, Any]:
        """
        Verifies and decodes a JWT token

        Args:
            token: JWT token to verify

        Returns:
            Decoded token data

        Raises:
            HTTPException: If the token is invalid or expired
        """
        if not self.secret_key or len(self.secret_key) < 32:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Sessions are disabled: JWT_SECRET_KEY is missing",
                headers={"WWW-Authenticate": "Bearer"},
            )
        try:
            # RevBridge Connect: always with an expiration, and "exp" is required
            payload = jwt.decode(
                token,
                self.secret_key,
                algorithms=[self.algorithm],
                options={"require": ["exp"]},
            )
            return payload
        except jwt.ExpiredSignatureError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token expired",
                headers={"WWW-Authenticate": "Bearer"},
            )
        except jwt.InvalidTokenError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token",
                headers={"WWW-Authenticate": "Bearer"},
            )


# Global JWT manager instance
jwt_manager = JWTManager()


class JWTAuth(HTTPBearer):
    """JWT authentication class for FastAPI"""

    def __init__(
        self, auto_error: bool = True, jwt_manager_instance: Optional[JWTManager] = None
    ):
        super(JWTAuth, self).__init__(auto_error=auto_error)
        self.jwt_manager = jwt_manager_instance or jwt_manager

    async def __call__(
        self, request: Request
    ) -> Optional[HTTPAuthorizationCredentials]:
        """Validates the JWT token of the request"""
        credentials: Optional[HTTPAuthorizationCredentials] = await super(
            JWTAuth, self
        ).__call__(request)

        if credentials:
            if not credentials.scheme == "Bearer":
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid authentication scheme. Use 'Bearer'",
                    headers={"WWW-Authenticate": "Bearer"},
                )

            # Check that the token is valid
            payload = self.jwt_manager.verify_token(credentials.credentials)

            # Add the user data to the request state
            request.state.user = payload
            request.state.auth_type = "jwt"

            return credentials
        else:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Access token required",
                headers={"WWW-Authenticate": "Bearer"},
            )


# Global authentication instance
jwt_auth = JWTAuth()


def get_current_user(request: Request) -> Dict[str, Any]:
    """
    Gets the current user from the JWT token

    Args:
        request: FastAPI Request object

    Returns:
        User data decoded from the token
    """
    if not hasattr(request.state, "user"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="User not authenticated"
        )
    return request.state.user


def create_token_for_user(user_id: str, username: str, **kwargs) -> str:
    """
    Creates a JWT token for a specific user

    Args:
        user_id: Unique user ID
        username: Username
        **kwargs: Additional data to include in the token

    Returns:
        Encoded JWT token
    """
    token_data = {
        "sub": user_id,
        "username": username,
        "iat": int(datetime.now(timezone.utc).timestamp()),
        **kwargs,
    }
    return jwt_manager.create_access_token(token_data)


def is_public_route(path: str, public_routes: Optional[list] = None) -> bool:
    """
    Checks whether a route is public (does not require authentication)

    Args:
        path: Route path
        public_routes: List of public routes (default: the common ones)

    Returns:
        True if the route is public, False otherwise
    """
    if public_routes is None:
        public_routes = [
            "/c",  # Click tracking
            "/o",  # Open tracking
            "/u",  # Unsubscribe tracking
            "/health",  # Health check
            "/docs",  # Swagger documentation
            "/redoc",  # ReDoc documentation
            "/openapi.json",  # OpenAPI schema
            "/auth/login",  # User login
            "/info",  # Info endpoint
        ]

    # Check whether the path is exactly "/" or starts with a public route
    if path == "/":
        return True

    for route in public_routes:
        if path.startswith(route):
            return True

    return False


# Alias kept for compatibility
create_access_token = jwt_manager.create_access_token
