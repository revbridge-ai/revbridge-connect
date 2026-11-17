"""Sync MySQL helpers (mysql-connector-python) with retry + async wrappers.

Shared by the api, tracking, milter and infra services; the variants of these
helpers differed only in pool size.

Env vars (read at call time, not import time):
    DB_DSN                  — mysql://user:pass@host:port/db   (required)
    DB_CONNECTION_TIMEOUT   — int seconds, default 10
    DB_AUTOCOMMIT           — "1"/"true"/"yes" → True, default True
    DB_EXECUTOR_WORKERS     — async wrapper pool size, default 20
    DB_POOL_ENABLED         — "1"/"true" → reuse connections, default False
    DB_POOL_SIZE            — pool size, default DB_EXECUTOR_WORKERS (max 32)

About DB_POOL_ENABLED: without it, `get_conn()` opens and closes one connection
per CALL, and the handshake dominates the cost. Over TLS a new connection costs
tens of milliseconds, while a reused one costs about 2 ms, so a queue processor
that makes several queries per email sends far fewer emails per minute without
the pool.

The async wrappers run blocking calls on a dedicated ThreadPoolExecutor so that
FastAPI's event loop never blocks on MySQL.
"""

import asyncio
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

logger = logging.getLogger(__name__)

# mysql-connector's own limit (CNX_POOL_MAXSIZE).
_POOL_MAX = 32
_pool: "pooling.MySQLConnectionPool | None" = None
_pool_key: tuple | None = None
_pool_lock = threading.Lock()

# Errno values where we know the connection was NEVER established — safe to retry.
_CONNECT_ERRNOS = {2002, 2003}
_CONNECT_RETRY_ATTEMPTS = 3
_CONNECT_RETRY_BASE_BACKOFF = 0.2  # 0.2s, 0.4s, 0.8s

_db_executor: concurrent.futures.ThreadPoolExecutor | None = None


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _is_connect_error(e: MySQLError) -> bool:
    return getattr(e, "errno", None) in _CONNECT_ERRNOS


def _retry_on_connect_error(operation, *, op_name: str):
    """Retry with backoff for TCP/handshake failures (errno 2002/2003)."""
    last: MySQLError | None = None
    for attempt in range(_CONNECT_RETRY_ATTEMPTS):
        try:
            return operation()
        except MySQLError as e:
            if not _is_connect_error(e) or attempt == _CONNECT_RETRY_ATTEMPTS - 1:
                raise
            wait = _CONNECT_RETRY_BASE_BACKOFF * (2**attempt)
            logger.warning(
                "MySQL connect failed in %s (try %d/%d, errno=%s). Retry in %.2fs",
                op_name,
                attempt + 1,
                _CONNECT_RETRY_ATTEMPTS,
                getattr(e, "errno", None),
                wait,
            )
            time.sleep(wait)
            last = e
    if last is not None:
        raise last


def _get_db_executor() -> concurrent.futures.ThreadPoolExecutor:
    global _db_executor
    if _db_executor is None:
        max_workers = int(os.getenv("DB_EXECUTOR_WORKERS", "20"))
        _db_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="mail_db_executor",
        )
    return _db_executor


def parse_mysql_dsn(dsn: str) -> dict:
    """Parse mysql://user:pass@host:port/db into mysql.connector kwargs."""
    timeout = int(os.getenv("DB_CONNECTION_TIMEOUT", "10"))
    autocommit = _truthy(os.getenv("DB_AUTOCOMMIT", "true"))
    base = {
        "charset": "utf8mb4",
        "autocommit": autocommit,
        "use_unicode": True,
        "collation": "utf8mb4_unicode_ci",
        "connection_timeout": timeout,
        "raise_on_warnings": False,
        "sql_mode": "",
    }
    match = re.match(r"mysql://([^:]+):([^@]+)@([^:]+):(\d+)/(.+)", dsn)
    if match:
        user, password, host, port, database = match.groups()
        return {
            **base,
            "host": host,
            "port": int(port),
            "user": user,
            "password": password,
            "database": database,
        }
    raise ValueError("DB_DSN must have the form mysql://user:password@host:port/database")


def _dsn() -> str:
    dsn = os.getenv("DB_DSN")
    if not dsn:
        raise RuntimeError("DB_DSN env var is required for mail_shared.db.mysql_sync")
    return dsn


def _pool_enabled() -> bool:
    return _truthy(os.getenv("DB_POOL_ENABLED"))


def _get_pool(cfg: dict) -> "pooling.MySQLConnectionPool":
    """One pool per process, recreated if the DSN changes (e.g. a database cutover)."""
    global _pool, _pool_key
    size = int(os.getenv("DB_POOL_SIZE") or os.getenv("DB_EXECUTOR_WORKERS") or 20)
    size = max(1, min(size, _POOL_MAX))
    key = (cfg.get("host"), cfg.get("port"), cfg.get("user"), cfg.get("database"), size)
    if _pool is None or _pool_key != key:
        with _pool_lock:
            if _pool is None or _pool_key != key:
                _pool = pooling.MySQLConnectionPool(
                    pool_name="mail_shared_mysql",
                    pool_size=size,
                    # Return a clean session to the pool (costs one round-trip, but
                    # avoids inheriting a transaction or session variable from another use).
                    pool_reset_session=True,
                    **cfg,
                )
                _pool_key = key
                logger.info("MySQL pool created: size=%s host=%s", size, cfg.get("host"))
    return _pool


@contextmanager
def get_conn():
    """Hands a MySQL connection to the caller (sync context).

    With DB_POOL_ENABLED the connection comes from the pool, and the `.close()` in
    the finally block RETURNS it instead of closing it (that is how mysql-connector
    works). Without the flag, one connection is opened per call.
    """
    conn = None
    try:
        if _pool_enabled():
            try:
                conn = _get_pool(parse_mysql_dsn(_dsn())).get_connection()
                # The server may have dropped the connection (wait_timeout)
                # while it sat idle in the pool.
                conn.ping(reconnect=True, attempts=2, delay=0)
            except pooling.PoolError as e:
                # An exhausted pool must not become an application error: fall back
                # to a direct connection.
                logger.warning("pool exhausted (%s); using a direct connection for this call", e)
                conn = mysql.connector.connect(**parse_mysql_dsn(_dsn()))
        else:
            conn = mysql.connector.connect(**parse_mysql_dsn(_dsn()))
        yield conn
    except MySQLError as e:
        # Connect timeouts (2002/2003) are absorbed by the retry in the layers above
        (logger.warning if _is_connect_error(e) else logger.error)("MySQL error: %s", e)
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
                    conn.close()
            except Exception as e:
                logger.warning("Error closing connection: %s", e)


def exec_sql(sql: str, params=None) -> None:
    def _op():
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params or ())
            conn.commit()

    _retry_on_connect_error(_op, op_name="exec_sql")


def fetch_one(sql: str, params=None):
    def _op():
        with get_conn() as conn:
            with conn.cursor(dictionary=True) as cur:
                cur.execute(sql, params or ())
                return cur.fetchone()

    return _retry_on_connect_error(_op, op_name="fetch_one")


def fetch_all(sql: str, params=None):
    def _op():
        with get_conn() as conn:
            with conn.cursor(dictionary=True) as cur:
                cur.execute(sql, params or ())
                return cur.fetchall()

    return _retry_on_connect_error(_op, op_name="fetch_all")


def _run_sync_in_executor(func, *args, **kwargs):
    loop = asyncio.get_event_loop()
    return loop.run_in_executor(_get_db_executor(), lambda: func(*args, **kwargs))


async def exec_sql_async(sql: str, params=None):
    """Async wrapper. Returns lastrowid for INSERTs."""

    def _exec():
        with get_conn() as conn:
            with conn.cursor(buffered=True) as cur:
                cur.execute(sql, params or ())
                lastrowid = cur.lastrowid
            conn.commit()
            return lastrowid

    return await _run_sync_in_executor(
        lambda: _retry_on_connect_error(_exec, op_name="exec_sql_async")
    )


async def fetch_one_async(sql: str, params=None):
    def _fetch():
        with get_conn() as conn:
            with conn.cursor(dictionary=True, buffered=True) as cur:
                cur.execute(sql, params or ())
                return cur.fetchone()

    return await _run_sync_in_executor(
        lambda: _retry_on_connect_error(_fetch, op_name="fetch_one_async")
    )


async def fetch_all_async(sql: str, params=None):
    def _fetch():
        with get_conn() as conn:
            with conn.cursor(dictionary=True, buffered=True) as cur:
                cur.execute(sql, params or ())
                return cur.fetchall()

    return await _run_sync_in_executor(
        lambda: _retry_on_connect_error(_fetch, op_name="fetch_all_async")
    )


__all__ = [
    "exec_sql",
    "exec_sql_async",
    "fetch_all",
    "fetch_all_async",
    "fetch_one",
    "fetch_one_async",
    "get_conn",
    "parse_mysql_dsn",
]
