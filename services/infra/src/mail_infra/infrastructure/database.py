"""Database utilities."""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager

import mysql.connector
from mysql.connector import DatabaseError, MySQLConnection, pooling

from mail_infra.core.exceptions import DatabaseConnectionError
from mail_infra.core.settings import DB_HOST, DB_NAME, DB_PASSWORD, DB_PORT, DB_USER

logger = logging.getLogger(__name__)

# Optional connection pool (DB_POOL_ENABLED). Without it every call opens and closes a
# connection, and for a remote database over TLS the handshake costs far more than the query
# itself. Jobs that run many queries in sequence benefit the most.
_POOL_MAX = 32  # CNX_POOL_MAXSIZE of mysql-connector
_pool: pooling.MySQLConnectionPool | None = None
_pool_lock = threading.Lock()

_CONEXAO = {
    "host": DB_HOST,
    "port": DB_PORT,
    "user": DB_USER,
    "password": DB_PASSWORD,
    "database": DB_NAME,
    # Use utf8mb4 charset to properly handle all Unicode characters
    "use_unicode": True,
    "charset": "utf8mb4",
    "collation": "utf8mb4_unicode_ci",
}


def _pool_enabled() -> bool:
    return (os.getenv("DB_POOL_ENABLED") or "").strip().lower() in {"1", "true", "yes", "on"}


def _get_pool() -> pooling.MySQLConnectionPool | None:
    """Per-process pool, created ON DEMAND.

    Never at import time: if the database had no free connection at that moment, the process
    would stay without a pool for good and fall back to one connection per call, which keeps
    an already saturated database saturated.
    """
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                tamanho = int(os.getenv("DB_POOL_SIZE") or 8)
                try:
                    _pool = pooling.MySQLConnectionPool(
                        pool_name="mail_infra_mysql",
                        pool_size=max(1, min(tamanho, _POOL_MAX)),
                        pool_reset_session=True,
                        **_CONEXAO,
                    )
                    logger.info("MySQL pool created: size=%s host=%s", tamanho, DB_HOST)
                except Exception as exc:
                    logger.warning("Could not create the MySQL pool: %s", exc)
                    _pool = None
    return _pool


@contextmanager
def get_connection() -> Iterator[MySQLConnection]:
    """Yield a MySQL connection configured from application settings."""
    connection = None
    try:
        pool = _get_pool() if _pool_enabled() else None
        if pool is not None:
            try:
                connection = pool.get_connection()
                # The server's wait_timeout may have closed a connection that sat idle in
                # the pool.
                connection.ping(reconnect=True, attempts=2, delay=0)
            except pooling.PoolError as exc:
                # An exhausted pool must never become an application error.
                logger.warning("pool exhausted (%s); using a direct connection for this call", exc)
                connection = mysql.connector.connect(**_CONEXAO)
        else:
            connection = mysql.connector.connect(**_CONEXAO)
    except DatabaseError as exc:
        raise DatabaseConnectionError(
            f"Failed to connect to MySQL server at {DB_HOST}:{DB_PORT}: {exc}"
        ) from exc

    try:
        yield connection
    finally:
        # With a pool, close() RETURNS the connection to the pool instead of closing it.
        connection.close()

