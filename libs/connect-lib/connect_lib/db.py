"""
Database access module for connect-lib

Provides MySQL database access functions.
Each project can have its own database implementation.
"""

from mysql.connector import Error as MySQLError
from mysql.connector import pooling
from contextlib import contextmanager
import re
import logging
import concurrent.futures
import os
import threading
from typing import Optional, Dict, Any, List
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Dedicated executor for database operations
_db_executor = None

# MySQL connection pool (one per process): reusing connections avoids a new TCP handshake
# (and its connect timeout) on every call.
_pool: Optional[pooling.MySQLConnectionPool] = None
_pool_lock = threading.Lock()
_pool_dsn: Optional[str] = None


def _get_db_executor():
    """Gets or creates the dedicated executor for database operations"""
    global _db_executor
    if _db_executor is None:
        _db_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=10, thread_name_prefix="connect_lib_db_executor"
        )
    return _db_executor


# MySQL DSN parsing
def parse_mysql_dsn(dsn: str) -> Dict[str, Any]:
    """Converts a MySQL DSN into connection parameters"""
    # mysql://user:pass@host:port/database
    match = re.match(r"mysql://([^:]+):([^@]+)@([^:]+):(\d+)/(.+)", dsn)
    if match:
        user, password, host, port, database = match.groups()
        return {
            "host": host,
            "port": int(port),
            "user": user,
            "password": password,
            "database": database,
            "charset": "utf8mb4",
            "autocommit": os.getenv("DB_AUTOCOMMIT", "false").lower() == "true",
            "use_unicode": True,
            "collation": "utf8mb4_unicode_ci",
            "connection_timeout": int(os.getenv("DB_CONNECTION_TIMEOUT", "5")),
            "raise_on_warnings": False,
            "sql_mode": "",
        }
    else:
        # Fallback to the individual DB_* variables
        return {
            "host": "localhost",
            "port": 3306,
            "user": os.getenv("DB_USER", ""),
            "password": os.getenv("DB_PASSWORD", ""),
            "database": os.getenv("DB_NAME", "revbridge"),
            "charset": "utf8mb4",
            "autocommit": os.getenv("DB_AUTOCOMMIT", "false").lower() == "true",
            "use_unicode": True,
            "collation": "utf8mb4_unicode_ci",
            "connection_timeout": int(os.getenv("DB_CONNECTION_TIMEOUT", "5")),
            "raise_on_warnings": False,
            "sql_mode": "",
        }


def _get_pool() -> pooling.MySQLConnectionPool:
    """Lazy-init MySQLConnectionPool. It is reset if the DSN changes (tests/reload)."""
    global _pool, _pool_dsn
    db_dsn = os.getenv("DB_DSN", "")
    if _pool is not None and _pool_dsn == db_dsn:
        return _pool

    with _pool_lock:
        if _pool is not None and _pool_dsn == db_dsn:
            return _pool

        db_config = parse_mysql_dsn(db_dsn)
        # The pool size has to cover the async executor's ThreadPoolExecutor
        # (max_workers=10). mysql.connector accepts up to 32 per pool.
        pool_size = max(1, min(32, int(os.getenv("DB_POOL_SIZE", "10"))))
        _pool = pooling.MySQLConnectionPool(
            pool_name="connect_lib_pool",
            pool_size=pool_size,
            pool_reset_session=True,
            **db_config,
        )
        _pool_dsn = db_dsn
        logger.info(
            "connect_lib MySQL pool initialized (size=%s, host=%s)",
            pool_size,
            db_config.get("host"),
        )
        return _pool


@contextmanager
def get_conn():
    """
    Context manager that gets a MySQL connection (from the pool).
    On exit, the connection goes back to the pool (the socket is not closed).
    """
    conn = None
    try:
        conn = _get_pool().get_connection()
        # ping revives idle connections the server has closed (wait_timeout); without it a
        # long-lived process would get a dead connection and fail.
        try:
            conn.ping(reconnect=True, attempts=2, delay=0)
        except MySQLError as e:
            logger.warning(f"Pooled connection ping/reconnect failed: {e}")
            try:
                conn.close()
            except Exception:
                pass
            conn = None
            raise
        yield conn
    except MySQLError as e:
        logger.error(f"MySQL error during the operation: {e}")
        if conn is not None:
            try:
                if conn.is_connected():
                    conn.rollback()
            except Exception:
                pass
        raise
    finally:
        if conn is not None:
            try:
                # close() on a PooledMySQLConnection returns it to the pool
                conn.close()
            except Exception as e:
                logger.warning(f"Could not return the connection to the pool: {e}")


def exec_sql(sql: str, params: Optional[tuple] = None) -> None:
    """Executes SQL (INSERT/UPDATE/DELETE) and commits"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
        conn.commit()


def fetch_one(sql: str, params: Optional[tuple] = None) -> Optional[Dict[str, Any]]:
    """Fetches a single row - no commit (read only)"""
    with get_conn() as conn:
        with conn.cursor(dictionary=True) as cur:
            cur.execute(sql, params or ())
            result = cur.fetchone()
        return result


def _run_sync_in_executor(func, *args, **kwargs):
    """
    Runs a synchronous function in a dedicated async executor.
    Uses an executor reserved for database operations.
    """
    import asyncio

    loop = asyncio.get_event_loop()
    executor = _get_db_executor()
    return loop.run_in_executor(executor, lambda: func(*args, **kwargs))


async def exec_sql_async(sql: str, params: Optional[tuple] = None) -> Any:
    """
    Async version of exec_sql.
    Runs the SQL in an executor so the event loop is not blocked.
    """

    def _exec():
        with get_conn() as conn:
            with conn.cursor(buffered=True) as cur:
                cur.execute(sql, params or ())
                lastrowid = cur.lastrowid
            conn.commit()
            return lastrowid

    try:
        return await _run_sync_in_executor(_exec)
    except MySQLError as e:
        logger.error(
            f"Error executing async SQL: {e}\nSQL: {sql}\nParams: {params}"
        )
        raise
    except Exception as e:
        logger.error(f"Unexpected error executing async SQL: {e}")
        raise


async def fetch_one_async(
    sql: str, params: Optional[List[Any]] = None
) -> Optional[Dict[str, Any]]:
    """
    Async version of fetch_one.
    Runs the query in an executor so the event loop is not blocked.
    No commit (read only).
    """

    def _fetch():
        with get_conn() as conn:
            with conn.cursor(dictionary=True, buffered=True) as cur:
                cur.execute(sql, params or ())
                result = cur.fetchone()
            return result

    try:
        return await _run_sync_in_executor(_fetch)
    except MySQLError as e:
        logger.error(
            f"Error fetching a row (async): {e}\nSQL: {sql}\nParams: {params}"
        )
        raise
    except Exception as e:
        logger.error(f"Unexpected error fetching a row (async): {e}")
        raise


async def fetch_all_async(
    sql: str, params: Optional[List[Any]] = None
) -> List[Dict[str, Any]]:
    """
    Async version of fetch_all.
    Runs the query in an executor so the event loop is not blocked.
    No commit (read only).
    """

    def _fetch():
        with get_conn() as conn:
            with conn.cursor(dictionary=True, buffered=True) as cur:
                cur.execute(sql, params or ())
                result = cur.fetchall()
            return result

    try:
        return await _run_sync_in_executor(_fetch)
    except MySQLError as e:
        logger.error(
            f"Error fetching rows (async): {e}\nSQL: {sql}\nParams: {params}"
        )
        raise
    except Exception as e:
        logger.error(f"Unexpected error fetching rows (async): {e}")
        raise
