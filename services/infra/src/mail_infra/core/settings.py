"""Minimal settings for the deliverability jobs, read from the environment (and from .env, if present)."""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("DB_PORT", "3306"))
# No default credentials: `make env` generates them. Without them the connection is refused.
DB_USER = os.getenv("DB_USER", "")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_NAME = os.getenv("DB_NAME", "infra")

# Redis that holds the job metrics (mail_infra.metrics_sink)
REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
