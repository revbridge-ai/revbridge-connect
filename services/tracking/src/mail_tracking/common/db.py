import logging
import os
import re
import time
from contextlib import contextmanager

from mysql.connector import Error as MySQLError
from mysql.connector.pooling import MySQLConnectionPool, PoolError

from mail_tracking.common.config import (
    DB_AUTOCOMMIT,
    DB_CONNECTION_TIMEOUT,
    DB_DSN,
)

logger = logging.getLogger(__name__)

# Errors where the connection was certainly NEVER established: safe to retry for reads and writes.
_CONNECT_ERRNOS = {2002, 2003}
_CONNECT_RETRY_ATTEMPTS = 3
_CONNECT_RETRY_BASE_BACKOFF = 0.2  # 0.2s, 0.4s, 0.8s


def _is_connect_error(e: MySQLError) -> bool:
    return getattr(e, "errno", None) in _CONNECT_ERRNOS


def _retry_on_connect_error(operation, *, op_name: str):
    """Retry with backoff on MySQL TCP/handshake failures (errno 2002/2003)."""
    last = None
    for attempt in range(_CONNECT_RETRY_ATTEMPTS):
        try:
            return operation()
        except MySQLError as e:
            if not _is_connect_error(e) or attempt == _CONNECT_RETRY_ATTEMPTS - 1:
                raise
            wait = _CONNECT_RETRY_BASE_BACKOFF * (2 ** attempt)
            logger.warning(
                "MySQL connection failed in %s (attempt %d/%d, errno=%s). Retrying in %.2fs",
                op_name, attempt + 1, _CONNECT_RETRY_ATTEMPTS, getattr(e, "errno", None), wait,
            )
            time.sleep(wait)
            last = e
    raise last

# Pool size, configurable through the environment (default: 10)
DB_POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "10"))

# MySQL DSN parsing
def parse_mysql_dsn(dsn):
    """Convert a MySQL DSN into connection parameters"""
    # mysql://user:pass@host:port/database
    match = re.match(r'mysql://([^:]+):([^@]+)@([^:]+):(\d+)/(.+)', dsn)
    if match:
        user, password, host, port, database = match.groups()
        return {
            'host': host,
            'port': int(port),
            'user': user,
            'password': password,
            'database': database,
            'charset': 'utf8mb4',
            'autocommit': DB_AUTOCOMMIT,
            'use_unicode': True,
            'collation': 'utf8mb4_unicode_ci',
            'connection_timeout': DB_CONNECTION_TIMEOUT,
            'raise_on_warnings': False,
            'sql_mode': '',
        }
    raise ValueError("DB_DSN must have the form mysql://user:password@host:port/database")

# Global connection pool
_pool: MySQLConnectionPool = None

def _get_pool() -> MySQLConnectionPool:
    """Get or create the MySQL connection pool"""
    global _pool
    if _pool is None:
        db_config = parse_mysql_dsn(DB_DSN)
        _pool = MySQLConnectionPool(
            pool_name="tracking_pool",
            pool_size=DB_POOL_SIZE,
            pool_reset_session=True,
            **db_config,
        )
        logger.debug(f"MySQL pool created with {DB_POOL_SIZE} connections")
    return _pool

@contextmanager
def get_conn():
    """
    Context manager that yields a connection from the MySQL pool.
    The connection goes back to the pool when the context exits.
    """
    conn = None
    try:
        conn = _get_pool().get_connection()
        yield conn
    except PoolError as e:
        logger.error(f"MySQL pool exhausted: {e}")
        raise
    except MySQLError as e:
        # Connect timeouts (2002/2003) are absorbed by the retry in the callers
        (logger.warning if _is_connect_error(e) else logger.error)(
            f"MySQL error during the operation: {e}"
        )
        if conn and conn.is_connected():
            try:
                conn.rollback()
            except Exception:
                pass
        raise
    finally:
        if conn:
            try:
                if conn.is_connected():
                    conn.close()  # returns it to the pool (does not really close it)
            except Exception as e:
                logger.warning(f"Error returning the connection to the pool: {e}")


def exec_sql(sql, params=None):
    def _op():
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params or ())
            conn.commit()

    _retry_on_connect_error(_op, op_name="exec_sql")


def fetch_one(sql, params=None):
    """Fetch a single row - no commit (read only)"""
    def _op():
        with get_conn() as conn:
            with conn.cursor(dictionary=True) as cur:
                cur.execute(sql, params or ())
                result = cur.fetchone()
            # No commit for read queries: saves a round-trip
            return result

    return _retry_on_connect_error(_op, op_name="fetch_one")


def fetch_all(sql, params=None):
    """Fetch all rows - no commit (read only)"""
    def _op():
        with get_conn() as conn:
            with conn.cursor(dictionary=True) as cur:
                cur.execute(sql, params or ())
                result = cur.fetchall()
            # No commit for read queries: saves a round-trip
            return result

    return _retry_on_connect_error(_op, op_name="fetch_all")
