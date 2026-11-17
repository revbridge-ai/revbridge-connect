"""
Base FastAPI router for the authentication endpoints

This router can be used as is or extended by the projects.
It includes brute force protection for logins.
"""

from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel, Field, validator
from typing import Optional, Callable, Dict, Any
import logging

from ..config import JWT_ACCESS_TOKEN_EXPIRE_MINUTES
from .jwt import get_current_user, create_token_for_user

logger = logging.getLogger(__name__)


def _get_client_ip(request: Request) -> str:
    """Extracts the client's real IP, taking proxies into account"""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip
    return request.client.host if request.client else "unknown"


# Base Pydantic models
class LoginRequest(BaseModel):
    """Login request model"""

    email: str = Field(
        ..., description="User's email", min_length=3, max_length=255
    )
    password: str = Field(
        ..., description="User's password", min_length=6, max_length=100
    )


class UserInfo(BaseModel):
    """User information model"""

    user_id: str = Field(
        ..., description="Unique user ID", example="user_1234567890abcdef"
    )
    username: str = Field(
        ..., description="User's username", example="user@example.com"
    )
    email: str = Field(
        ..., description="User's email", example="user@example.com"
    )
    client_id: Optional[str] = Field(
        None,
        description="Customer UUID",
        example="550e8400-e29b-41d4-a716-446655440000",
    )
    domain_id: str = Field(..., description="Domain ID", example="domain_123")
    domain: str = Field(..., description="Domain name", example="example.com")
    role: str = Field(..., description="User's role", example="admin")


class LoginResponse(BaseModel):
    """Login response model"""

    access_token: str = Field(
        ...,
        description="JWT token for authentication",
        example="eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
    )
    token_type: str = Field(..., description="Token type", example="bearer")
    expires_in: int = Field(
        ..., description="Expiration time in seconds", example=1800
    )
    user: UserInfo = Field(..., description="User information")


class CreateUserRequest(BaseModel):
    """User creation model"""

    username: str = Field(
        ..., description="User's username/email", example="new@example.com"
    )
    password: str = Field(..., description="User's password", example="password123")
    role: str = Field(..., description="User's role", example="user")
    name: Optional[str] = Field(
        None, description="User's full name", example="Jane Doe"
    )
    phone: Optional[str] = Field(
        None, description="User's phone number", example="+5511999999999"
    )

    @validator("role")
    def validate_role(cls, v):
        """Validates that the role is one of the valid options"""
        valid_roles = ["admin", "user", "viewer"]
        if v.lower() not in valid_roles:
            raise ValueError(
                f"Invalid role: '{v}'. Valid roles are: {', '.join(valid_roles)}"
            )
        return v.lower()


class ChangePasswordRequest(BaseModel):
    """Password change model"""

    old_password: str = Field(..., description="Current password", example="currentPassword123")
    new_password: str = Field(..., description="New password", example="newPassword123")


def create_auth_router(
    validate_credentials: Callable[[str, str], Optional[Dict[str, Any]]],
    create_user_func: Callable[[Dict[str, Any]], tuple],
    update_user_password_func: Callable[[str, str, str], tuple],
    get_user_domain_id: Optional[Callable[[Dict[str, Any]], Optional[int]]] = None,
    validate_domain_id: Optional[Callable[[int], bool]] = None,
    prefix: str = "/auth",
    tags: Optional[list] = None,
    enable_brute_force_protection: bool = True,
    max_login_attempts: int = 5,
    login_window_minutes: int = 15,
    login_block_minutes: int = 15,
) -> APIRouter:
    """
    Creates a FastAPI router for authentication

    Args:
        validate_credentials: Function that validates credentials (email, password) -> Optional[Dict]
        create_user: Function that creates a user (user_data) -> (success: bool, message: str, user_data: dict)
        update_user_password: Function that updates a password (user_id, old_password, new_password) -> (success: bool, message: str)
        get_user_domain_id: Function that gets the user's domain_id (optional)
        validate_domain_id: Function that checks whether a domain_id exists (optional)
        prefix: Route prefix (default: "/auth")
        tags: OpenAPI documentation tags (default: ["authentication"])
        enable_brute_force_protection: Enables brute force protection (default: True)
        max_login_attempts: Maximum number of login attempts (default: 5)
        login_window_minutes: Time window for counting attempts (default: 15)
        login_block_minutes: Block time after exceeding the attempts (default: 15)

    Returns:
        Configured APIRouter
    """
    router = APIRouter(prefix=prefix, tags=tags or ["authentication"])

    # Try to import the login_attempt module
    login_attempt_service = None
    if enable_brute_force_protection:
        try:
            from ..login_attempt import get_service as get_login_attempt_service

            login_attempt_service = get_login_attempt_service
            logger.info(
                f"Brute force protection enabled: {max_login_attempts} attempts "
                f"in {login_window_minutes} min, block for {login_block_minutes} min"
            )
        except ImportError:
            logger.warning(
                "login_attempt module not available. Brute force protection disabled."
            )

    @router.post(
        "/login",
        summary="🔐 User login",
        description="""
## User Authentication

Public endpoint that authenticates users.

Features:
- Email authentication: login with an email address only
- JWT token: returns a JWT token for authentication
- User information: returns the user's details
- Configurable expiration (JWT_ACCESS_TOKEN_EXPIRE_MINUTES)

Accepted credentials:
- Email: a registered email address
- Password: the user's password

Successful response:
- JWT token for authentication
- The user's details
- Token expiration time
- Data of the associated domain

Security:
- Strict credential validation
- Securely signed JWT tokens
- Brute force protection
- Rate limiting against attacks
- Login attempts are logged
        """,
        response_model=LoginResponse,
    )
    async def login(request: LoginRequest, http_request: Request):
        """Login endpoint with credentials (email and password)"""
        try:
            email = request.email.lower().strip()
            password = request.password
            client_ip = _get_client_ip(http_request)

            logger.info(f"Login attempt: email={email}, ip={client_ip}")

            # Check whether the IP or the email is blocked because of too many attempts
            if login_attempt_service and login_attempt_service():
                service = login_attempt_service()

                # Check the block by IP
                ip_blocked = await service.is_blocked(client_ip, "ip", "login")
                if ip_blocked.blocked:
                    logger.warning(
                        f"Login blocked - too many failed attempts from IP: {client_ip}"
                    )
                    raise HTTPException(
                        status_code=429,
                        detail={
                            "error": "too_many_attempts",
                            "message": ip_blocked.message,
                            "retry_after_minutes": ip_blocked.retry_after_minutes,
                        },
                    )

                # Check the block by email
                email_blocked = await service.is_blocked(email, "email", "login")
                if email_blocked.blocked:
                    logger.warning(
                        f"Login blocked - too many failed attempts for email: {email}"
                    )
                    raise HTTPException(
                        status_code=429,
                        detail={
                            "error": "too_many_attempts",
                            "message": email_blocked.message,
                            "retry_after_minutes": email_blocked.retry_after_minutes,
                        },
                    )

            # Validate the credentials
            user = validate_credentials(email, password)

            if not user:
                # Record the failed attempt
                if login_attempt_service and login_attempt_service():
                    service = login_attempt_service()
                    await service.record_attempt(
                        client_ip, "ip", "login", success=False
                    )
                    await service.record_attempt(email, "email", "login", success=False)

                logger.warning(
                    f"Login failed: invalid credentials for {email} from {client_ip}"
                )
                raise HTTPException(status_code=401, detail="Invalid credentials")

            # Validate that the domain_id exists
            domain_id = user.get("domain_id")
            if not domain_id:
                # Record the failed attempt
                if login_attempt_service and login_attempt_service():
                    service = login_attempt_service()
                    await service.record_attempt(
                        client_ip, "ip", "login", success=False
                    )
                    await service.record_attempt(email, "email", "login", success=False)

                logger.error(
                    f"User {user['user_id']} has no associated domain_id"
                )
                raise HTTPException(
                    status_code=403, detail="User is not associated with a domain"
                )

            if validate_domain_id and not validate_domain_id(domain_id):
                # Record the failed attempt
                if login_attempt_service and login_attempt_service():
                    service = login_attempt_service()
                    await service.record_attempt(
                        client_ip, "ip", "login", success=False
                    )
                    await service.record_attempt(email, "email", "login", success=False)

                logger.error(
                    f"Domain ID {domain_id} of user {user['user_id']} not found"
                )
                raise HTTPException(
                    status_code=500,
                    detail="The user's domain is misconfigured. Contact support.",
                )

            # Successful login - record it and clear the failed attempts
            if login_attempt_service and login_attempt_service():
                service = login_attempt_service()
                await service.record_attempt(client_ip, "ip", "login", success=True)
                await service.record_attempt(email, "email", "login", success=True)
                # Clear the previous failed attempts
                await service.clear_failed_attempts(client_ip, "ip", "login")
                await service.clear_failed_attempts(email, "email", "login")

            # The user's client_id (may come from the database or from the domain)
            client_id = user.get("client_id")

            # Create the JWT token
            access_token = create_token_for_user(
                user_id=user["user_id"],
                username=user["username"],
                client_id=client_id,
                domain_id=user["domain_id"],
                domain=user.get("domain", ""),
                role=user["role"],
            )

            logger.info(
                f"Login successful: user={user['user_id']}, email={email}, client_id={client_id}"
            )

            return {
                "access_token": access_token,
                "token_type": "bearer",
                "expires_in": (JWT_ACCESS_TOKEN_EXPIRE_MINUTES or 720) * 60,
                "user": {
                    "user_id": user["user_id"],
                    "username": user["username"],
                    "email": user.get("email", user["username"]),
                    "client_id": client_id,
                    "domain_id": str(domain_id),
                    "domain": user.get("domain", ""),
                    "role": user["role"],
                },
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Login error: {e}")
            raise HTTPException(status_code=500, detail="Internal server error")

    @router.get(
        "/me",
        summary="👤 User information",
        description="""
## Authenticated User Information

Protected endpoint that returns the details of the authenticated user.

### 📊 Returned information
- Unique user ID
- Username/email
- ID of the associated domain
- The user's role/permissions
- Issue and expiration timestamps of the token

### 🔐 Authentication
Requires a valid JWT token in the ``Authorization: Bearer <token>`` header
        """,
    )
    async def get_current_user_info(current_user: dict = Depends(get_current_user)):
        """Endpoint that returns the authenticated user's information"""
        return {
            "user_id": current_user.get("sub"),
            "username": current_user.get("username"),
            "client_id": current_user.get("client_id"),
            "domain_id": str(current_user.get("domain_id", "")),
            "role": current_user.get("role"),
            "iat": current_user.get("iat"),
            "exp": current_user.get("exp"),
        }

    @router.post(
        "/users",
        summary="👥 Create user",
        description="""
## Create a New User

Endpoint that creates new users (administrators only).

### 🔐 Permissions
- Administrators only: requires the admin role
- Data validation: strict input validation
- Associated domain: the user is associated with the admin's domain

### ✨ Features
- Email validation: format and uniqueness checks
- Secure password: security criteria validation
- Role management: defines the user's permissions
- Automatic activation: the user is created active
        """,
    )
    async def create_user(
        request: CreateUserRequest, current_user: dict = Depends(get_current_user)
    ):
        """Endpoint that creates a new user (admins only)"""
        try:
            # Check that the user is an admin
            if current_user.get("role") != "admin":
                raise HTTPException(
                    status_code=403,
                    detail="Only administrators can create users",
                )

            # Get the domain_id from the authenticated user's JWT (the admin creating the user)
            admin_domain_id = None
            if get_user_domain_id:
                admin_domain_id = get_user_domain_id(current_user)
            else:
                admin_domain_id = current_user.get("domain_id")

            if not admin_domain_id:
                raise HTTPException(
                    status_code=403, detail="User is not associated with a domain"
                )

            # Convert to a dict for compatibility
            request_dict = request.dict()

            # Generate a unique user_id if none was provided
            if "user_id" not in request_dict or not request_dict.get("user_id"):
                import secrets

                request_dict["user_id"] = f"user_{secrets.token_hex(16)}"

            # Use the username as the email if no email was provided
            if "email" not in request_dict or not request_dict.get("email"):
                request_dict["email"] = request_dict["username"]

            # Make sure the new user gets the same domain_id as the admin
            request_dict["domain_id"] = admin_domain_id

            # Create the user
            success, message, user_data = create_user_func(request_dict)

            if not success:
                raise HTTPException(status_code=400, detail=message)

            return {"status": "success", "message": message, "user": user_data}

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error creating user: {e}")
            raise HTTPException(status_code=500, detail="Internal server error")

    @router.put(
        "/users/{user_id}/password",
        summary="🔑 Change password",
        description="""
## Change a User's Password

Endpoint that changes the password of a specific user.

### 🔐 Permissions
- The user themselves: users can only change their own password

### ✨ Features
- Current password check: verifies the current password before changing it
- Secure new password: security criteria validation
- Change logs: password changes are logged
        """,
    )
    async def change_password(
        user_id: str,
        request: ChangePasswordRequest,
        current_user: dict = Depends(get_current_user),
    ):
        """Endpoint that changes the user's password"""
        try:
            # RevBridge Connect: users can only change their own password.
            if current_user.get("sub") != user_id:
                raise HTTPException(
                    status_code=403, detail="You can only change your own password"
                )

            old_password = request.old_password
            new_password = request.new_password

            # Update the password
            success, message = update_user_password_func(
                user_id, old_password, new_password
            )

            if not success:
                raise HTTPException(status_code=400, detail=message)

            return {"status": "success", "message": message}

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error changing password: {e}")
            raise HTTPException(status_code=500, detail="Internal server error")

    return router
