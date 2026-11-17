"""
Shared authentication settings
"""

import os
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

# JWT Configuration
# RevBridge Connect: no default secret. `make env` generates JWT_SECRET_KEY; without it (or
# with one that is too short) JWTManager refuses to issue and validate sessions instead of
# falling back to a known value.
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")

# Validate and cap the JWT expiration time (default: 12 h; maximum: 1 week).
# RevBridge Connect: 0 or a negative value no longer means "never expires".
_raw_expire_minutes = os.getenv("JWT_ACCESS_TOKEN_EXPIRE_MINUTES", "720")
try:
    _raw_expire_minutes = int(_raw_expire_minutes)
except ValueError:
    _raw_expire_minutes = 720

if _raw_expire_minutes <= 0:
    _raw_expire_minutes = 720
JWT_ACCESS_TOKEN_EXPIRE_MINUTES: Optional[int] = min(_raw_expire_minutes, 10080)

# API Key prefix
API_KEY_PREFIX = os.getenv("API_KEY_PREFIX", "rbak_")
