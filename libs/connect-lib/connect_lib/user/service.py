"""
User management service

Provides an abstract interface and a base implementation of the user operations.
Uses connect-lib's own database functions by default.
"""

import logging
from typing import Optional, Dict, Any, Tuple, Callable
from datetime import datetime

from .utils import verify_password, get_password_hash
from ..db import fetch_one, exec_sql

logger = logging.getLogger(__name__)


class UserServiceBase:
    """
    Abstract interface for user services

    Projects can extend this class to implement their own database logic.
    """

    def authenticate_user(self, email: str, password: str) -> Optional[Dict[str, Any]]:
        """
        Authenticates a user with email and password

        Args:
            email: User's email
            password: User's password

        Returns:
            dict: The user's data if authenticated, None otherwise
        """
        raise NotImplementedError

    def get_user_by_id(self, user_id: str) -> Optional[Dict[str, Any]]:
        """
        Gets a user by ID

        Args:
            user_id: Unique user ID

        Returns:
            dict: The user's data if found, None otherwise
        """
        raise NotImplementedError

    def create_user(
        self, user_data: Dict[str, Any]
    ) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """
        Creates a new user

        Args:
            user_data: User data (user_id, username, email, password, domain_id, role)

        Returns:
            tuple: (success, message, user_data)
        """
        raise NotImplementedError

    def update_user_password(
        self, user_id: str, old_password: str, new_password: str
    ) -> Tuple[bool, str]:
        """
        Updates the user's password

        Args:
            user_id: User ID
            old_password: Current password
            new_password: New password

        Returns:
            tuple: (success, message)
        """
        raise NotImplementedError


class UserService(UserServiceBase):
    """
    Base implementation of the user service

    Uses connect-lib's own database functions by default.
    They can be overridden with custom functions if needed.
    """

    def __init__(
        self,
        fetch_user_by_email: Optional[
            Callable[[str, tuple], Optional[Dict[str, Any]]]
        ] = None,
        fetch_user_by_id: Optional[
            Callable[[str, tuple], Optional[Dict[str, Any]]]
        ] = None,
        fetch_domain: Optional[Callable[[str, tuple], Optional[Dict[str, Any]]]] = None,
        execute_sql: Optional[Callable[[str, tuple], None]] = None,
        update_last_login: Optional[Callable[[str], None]] = None,
    ):
        """
        Initializes the user service

        Args:
            fetch_user_by_email: Optional function that fetches a user by email
                If None, uses connect-lib's fetch_one
                Signature: (query: str, params: tuple) -> Optional[Dict]
            fetch_user_by_id: Optional function that fetches a user by ID
                If None, uses connect-lib's fetch_one
                Signature: (query: str, params: tuple) -> Optional[Dict]
            fetch_domain: Optional function that fetches the domain's information
                If None, uses connect-lib's fetch_one
                Signature: (query: str, params: tuple) -> Optional[Dict]
            execute_sql: Optional function that executes SQL (INSERT/UPDATE)
                If None, uses connect-lib's exec_sql
                Signature: (query: str, params: tuple) -> None
            update_last_login: Optional function that updates the last login
                If None, uses connect-lib's exec_sql
                Signature: (user_id: str) -> None
        """
        # connect-lib's functions by default
        self.fetch_user_by_email = fetch_user_by_email or fetch_one
        self.fetch_user_by_id = fetch_user_by_id or fetch_one
        self.fetch_domain = fetch_domain or fetch_one
        self.execute_sql = execute_sql or exec_sql

        # Default update_last_login function
        if update_last_login:
            self.update_last_login = update_last_login
        else:

            def _default_update_last_login(user_id: str) -> None:
                exec_sql(
                    "UPDATE users SET last_login = NOW() WHERE user_id = %s", (user_id,)
                )

            self.update_last_login = _default_update_last_login

    def authenticate_user(self, email: str, password: str) -> Optional[Dict[str, Any]]:
        """
        Authenticates a user with email and password

        Args:
            email: User's email
            password: User's password

        Returns:
            dict: The user's data if authenticated, None otherwise
        """
        try:
            # Look up the user by email (including customer_id and client_id)
            user = self.fetch_user_by_email(
                """
                SELECT
                    u.id, u.user_id, u.username, u.email, u.password_hash,
                    u.domain_id, u.customer_id, u.role, u.is_active, u.last_login, u.created_at,
                    c.client_id
                FROM users u
                LEFT JOIN customers c ON u.customer_id = c.id
                WHERE u.email = %s AND u.is_active = TRUE
                """,
                (email,),
            )

            if not user:
                return None

            # Domain information, if available
            domain_info = None
            if user.get("domain_id") and self.fetch_domain:
                try:
                    domain_info = self.fetch_domain(
                        "SELECT * FROM virtual_domains WHERE id = %s",
                        (user["domain_id"],),
                    )
                except Exception as e:
                    logger.warning(f"Error fetching domain {user['domain_id']}: {e}")

            # Add the domain information to the user
            if domain_info:
                domain_value = (
                    domain_info.get("domain") or domain_info.get("name") or None
                )
                user["domain"] = domain_value
                user["domain_name"] = domain_info.get("name") or domain_info.get(
                    "domain"
                )
            else:
                user["domain"] = None
                user["domain_name"] = None

            # Check the password
            if not verify_password(password, user["password_hash"]):
                return None

            # Update the last login if the function is available
            if self.update_last_login:
                try:
                    self.update_last_login(user["user_id"])
                    user["last_login"] = datetime.now()
                except Exception as e:
                    logger.warning(f"Error updating last_login: {e}")

            return {
                "id": user["id"],
                "user_id": user["user_id"],
                "username": user["username"],
                "email": user["email"],
                "domain_id": user["domain_id"],
                "domain": user.get("domain"),
                "domain_name": user.get("domain_name"),
                "customer_id": user.get("customer_id"),
                "client_id": user.get("client_id"),
                "role": user["role"],
                "is_active": user["is_active"],
                "last_login": user.get("last_login"),
                "created_at": user.get("created_at"),
            }

        except Exception as e:
            logger.error(f"Error authenticating user {email}: {e}")
            return None

    def get_user_by_id(self, user_id: str) -> Optional[Dict[str, Any]]:
        """
        Gets a user by ID

        Args:
            user_id: Unique user ID

        Returns:
            dict: The user's data if found, None otherwise
        """
        try:
            user = self.fetch_user_by_id(
                """
                SELECT u.*, c.client_id
                FROM users u
                LEFT JOIN customers c ON u.customer_id = c.id
                WHERE u.user_id = %s AND u.is_active = TRUE
                """,
                (user_id,),
            )

            if not user:
                return None

            # Domain information, if available
            domain_info = None
            if user.get("domain_id") and self.fetch_domain:
                try:
                    domain_info = self.fetch_domain(
                        "SELECT * FROM virtual_domains WHERE id = %s",
                        (user["domain_id"],),
                    )
                except Exception as e:
                    logger.warning(f"Error fetching domain {user['domain_id']}: {e}")

            # Determine domain and domain_name
            if domain_info:
                domain_value = (
                    domain_info.get("domain") or domain_info.get("name") or None
                )
                domain_name = domain_info.get("name") or domain_info.get("domain")
            else:
                domain_value = None
                domain_name = None

            return {
                "id": user["id"],
                "user_id": user["user_id"],
                "username": user["username"],
                "email": user["email"],
                "domain_id": user["domain_id"],
                "domain": domain_value,
                "domain_name": domain_name,
                "customer_id": user.get("customer_id"),
                "client_id": user.get("client_id"),
                "role": user["role"],
                "is_active": user["is_active"],
                "last_login": user.get("last_login"),
                "created_at": user.get("created_at"),
            }

        except Exception as e:
            logger.error(f"Error fetching user {user_id}: {e}")
            return None

    def create_user(
        self, user_data: Dict[str, Any]
    ) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """
        Creates a new user

        Args:
            user_data: User data (user_id, username, email, password, domain_id, role)

        Returns:
            tuple: (success, message, user_data)
        """
        if not self.execute_sql:
            return False, "execute_sql function not provided", None

        try:
            # Validate the required fields
            required_fields = ["user_id", "username", "email", "password"]
            for field in required_fields:
                if not user_data.get(field):
                    return False, f"Required field: {field}", None

            # Check whether the user already exists
            existing_user = self.fetch_user_by_id(
                "SELECT id FROM users WHERE user_id = %s OR username = %s OR email = %s",
                (user_data["user_id"], user_data["username"], user_data["email"]),
            )

            if existing_user:
                return False, "A user with this ID, username or email already exists", None

            # Hash the password
            password_hash = get_password_hash(user_data["password"])

            # Insert the user
            self.execute_sql(
                """
                INSERT INTO users (user_id, username, email, password_hash, domain_id, customer_id, role)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    user_data["user_id"],
                    user_data["username"],
                    user_data["email"],
                    password_hash,
                    user_data.get("domain_id"),
                    user_data.get("customer_id"),
                    user_data.get("role", "user"),
                ),
            )

            # Read the created user
            created_user = self.get_user_by_id(user_data["user_id"])

            return True, "User created successfully", created_user

        except Exception as e:
            return False, f"Error creating user: {str(e)}", None

    def update_user_password(
        self, user_id: str, old_password: str, new_password: str
    ) -> Tuple[bool, str]:
        """
        Updates the user's password

        Args:
            user_id: User ID
            old_password: Current password
            new_password: New password

        Returns:
            tuple: (success, message)
        """
        if not self.execute_sql:
            return False, "execute_sql function not provided"

        try:
            # Look up the user
            user = self.fetch_user_by_id(
                "SELECT password_hash FROM users WHERE user_id = %s AND is_active = TRUE",
                (user_id,),
            )

            if not user:
                return False, "User not found"

            # Check the current password
            if not verify_password(old_password, user["password_hash"]):
                return False, "Current password is incorrect"

            # Hash the new password
            new_password_hash = get_password_hash(new_password)

            # Update the password
            self.execute_sql(
                "UPDATE users SET password_hash = %s, updated_at = CURRENT_TIMESTAMP WHERE user_id = %s",
                (new_password_hash, user_id),
            )

            return True, "Password updated successfully"

        except Exception as e:
            return False, f"Error updating password: {str(e)}"
