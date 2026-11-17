import concurrent.futures
import logging
import os
import re
import threading
import time
from contextlib import contextmanager

import mysql.connector
from mysql.connector import Error as MySQLError
from mysql.connector import pooling

from mail_api.common.config import (
    DB_AUTOCOMMIT,
    DB_CONNECTION_TIMEOUT,
    DB_DSN,
)

logger = logging.getLogger(__name__)

# Connection pool (DB_POOL_ENABLED, off by default).
#
# Why it exists: without it, get_conn() opens and CLOSES one connection per call, so
# the handshake dominates the cost. Over TLS a new connection costs tens of
# milliseconds while a reused one costs about 2 ms; a queue processor that makes
# several queries per email sends far fewer emails per minute without the pool, and
# the connection churn can exhaust the database's connection limit.
_POOL_MAX = 32  # mysql-connector's CNX_POOL_MAXSIZE
_pool: "pooling.MySQLConnectionPool | None" = None
_pool_key: tuple | None = None
_pool_lock = threading.Lock()


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _pool_enabled() -> bool:
    return _truthy(os.getenv("DB_POOL_ENABLED"))


def _pool_size() -> int:
    size = int(os.getenv("DB_POOL_SIZE") or os.getenv("DB_EXECUTOR_WORKERS") or 20)
    return max(1, min(size, _POOL_MAX))


def _get_pool(db_config: dict) -> "pooling.MySQLConnectionPool":
    """One pool per process, recreated if the target changes (database cutover)."""
    global _pool, _pool_key
    key = (
        db_config.get("host"),
        db_config.get("port"),
        db_config.get("user"),
        db_config.get("database"),
        _pool_size(),
    )
    if _pool is None or _pool_key != key:
        with _pool_lock:
            if _pool is None or _pool_key != key:
                _pool = pooling.MySQLConnectionPool(
                    pool_name="mail_api_mysql",
                    pool_size=key[4],
                    pool_reset_session=True,
                    **db_config,
                )
                _pool_key = key
                logger.info(
                    "MySQL pool created: size=%s host=%s", key[4], db_config.get("host")
                )
    return _pool

# Errors where we know the connection was NEVER established: safe to retry reads and writes.
_CONNECT_ERRNOS = {2002, 2003}
_CONNECT_RETRY_ATTEMPTS = 3
_CONNECT_RETRY_BASE_BACKOFF = 0.2  # 0.2s, 0.4s, 0.8s


def _is_connect_error(e: MySQLError) -> bool:
    return getattr(e, "errno", None) in _CONNECT_ERRNOS


def _retry_on_connect_error(operation, *, op_name: str):
    """Retry with backoff for MySQL TCP/handshake failures (errno 2002/2003)."""
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

# Dedicated executor for database operations
_db_executor = None

def _get_db_executor():
    """Returns (creating it if needed) the dedicated executor for database operations"""
    global _db_executor
    if _db_executor is None:
        # Sized by DB_EXECUTOR_WORKERS. The number matters: without the pool, every
        # worker in flight is a new connection.
        _db_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=int(os.getenv("DB_EXECUTOR_WORKERS") or 20),
            thread_name_prefix="db_executor"
        )
    return _db_executor

# MySQL DSN parsing
def parse_mysql_dsn(dsn):
    """Converts a MySQL DSN into connection parameters"""
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
            'raise_on_warnings': False,  # Do not raise warnings (better performance)
            'sql_mode': '',  # Use the server's default SQL mode
        }
    raise ValueError("DB_DSN must have the form mysql://user:password@host:port/database")

@contextmanager
def get_conn():
    """
    Context manager that hands out a MySQL connection.
    The connection is closed (or returned to the pool) when the context exits.
    """
    conn = None
    db_config = parse_mysql_dsn(DB_DSN)

    try:
        if _pool_enabled():
            try:
                conn = _get_pool(db_config).get_connection()
                # The connection may have died while idle in the pool
                # (the server's wait_timeout).
                conn.ping(reconnect=True, attempts=2, delay=0)
            except pooling.PoolError as e:
                # An exhausted pool must not become an application error: fall back
                # to a direct connection.
                logger.warning("pool exhausted (%s); using a direct connection for this call", e)
                conn = mysql.connector.connect(**db_config)
        else:
            conn = mysql.connector.connect(**db_config)
        yield conn
    except MySQLError as e:
        # Connect timeouts (2002/2003) are absorbed by the retry in the layers above
        (logger.warning if _is_connect_error(e) else logger.error)(
            f"MySQL error during operation: {e}"
        )
        # Roll back the broken connection if needed
        if conn and conn.is_connected():
            try:
                conn.rollback()
            except Exception:
                pass
        raise
    finally:
        # Close the connection
        if conn:
            try:
                if conn.is_connected():
                    conn.close()
            except Exception as e:
                logger.warning(f"Error closing connection: {e}")


def exec_sql(sql, params=None):
    def _op():
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params or ())
            conn.commit()
    _retry_on_connect_error(_op, op_name="exec_sql")


def fetch_one(sql, params=None):
    """Fetches a single row - no commit (read only)"""
    def _op():
        with get_conn() as conn:
            with conn.cursor(dictionary=True) as cur:
                cur.execute(sql, params or ())
                result = cur.fetchone()
            return result
    return _retry_on_connect_error(_op, op_name="fetch_one")


def fetch_all(sql, params=None):
    """Fetches every row - no commit (read only)"""
    def _op():
        with get_conn() as conn:
            with conn.cursor(dictionary=True) as cur:
                cur.execute(sql, params or ())
                result = cur.fetchall()
            return result
    return _retry_on_connect_error(_op, op_name="fetch_all")

def _run_sync_in_executor(func, *args, **kwargs):
    """
    Runs a synchronous function on the dedicated database executor.
    """
    import asyncio
    loop = asyncio.get_event_loop()
    executor = _get_db_executor()
    return loop.run_in_executor(executor, lambda: func(*args, **kwargs))


async def exec_sql_async(sql, params=None):
    """
    Async version of exec_sql.
    Runs the SQL on an executor so the event loop is not blocked.
    """
    def _exec():
        with get_conn() as conn:
            # buffered=True so that lastrowid is returned correctly
            with conn.cursor(buffered=True) as cur:
                cur.execute(sql, params or ())
                lastrowid = cur.lastrowid
            conn.commit()
            return lastrowid

    try:
        return await _run_sync_in_executor(
            lambda: _retry_on_connect_error(_exec, op_name="exec_sql_async")
        )
    except MySQLError as e:
        logger.error(f"Error running async SQL: {e}\nSQL: {sql}\nParams: {params}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error running async SQL: {e}")
        raise


async def exec_sql_rowcount_async(sql, params=None):
    """
    Like exec_sql_async, but returns cur.rowcount (affected rows) instead of
    lastrowid. Needed for conditional UPDATEs where the number of affected rows
    is the result (e.g. the atomic claim of send_ledger).
    """
    def _exec():
        with get_conn() as conn:
            with conn.cursor(buffered=True) as cur:
                cur.execute(sql, params or ())
                rowcount = cur.rowcount
            conn.commit()
            return rowcount

    try:
        return await _run_sync_in_executor(
            lambda: _retry_on_connect_error(_exec, op_name="exec_sql_rowcount_async")
        )
    except MySQLError as e:
        logger.error(f"Error running async SQL (rowcount): {e}\nSQL: {sql}\nParams: {params}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error running async SQL (rowcount): {e}")
        raise


async def fetch_one_async(sql, params=None):
    """
    Async version of fetch_one.
    Runs the query on an executor so the event loop is not blocked.
    No commit (read only).
    """
    def _fetch():
        with get_conn() as conn:
            with conn.cursor(dictionary=True, buffered=True) as cur:
                cur.execute(sql, params or ())
                result = cur.fetchone()
            # No commit for read queries - saves a round-trip
            return result

    try:
        return await _run_sync_in_executor(
            lambda: _retry_on_connect_error(_fetch, op_name="fetch_one_async")
        )
    except MySQLError as e:
        logger.error(f"Error fetching a row (async): {e}\nSQL: {sql}\nParams: {params}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error fetching a row (async): {e}")
        raise


async def fetch_all_async(sql, params=None):
    """
    Async version of fetch_all.
    Runs the query on an executor so the event loop is not blocked.
    No commit (read only).
    """
    def _fetch():
        with get_conn() as conn:
            with conn.cursor(dictionary=True, buffered=True) as cur:
                cur.execute(sql, params or ())
                result = cur.fetchall()
            # No commit for read queries - saves a round-trip
            return result

    try:
        return await _run_sync_in_executor(
            lambda: _retry_on_connect_error(_fetch, op_name="fetch_all_async")
        )
    except MySQLError as e:
        logger.error(f"Error fetching rows (async): {e}\nSQL: {sql}\nParams: {params}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error fetching rows (async): {e}")
        raise
