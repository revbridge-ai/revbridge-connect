import logging
import re
import threading
import time
from contextlib import contextmanager

import mysql.connector

from mail_milter.common.config import DB_DSN

logger = logging.getLogger(__name__)


# MySQL DSN parsing
def parse_mysql_dsn(dsn):
    """Convert a MySQL DSN into connection parameters"""
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
            "use_unicode": True,
            "collation": "utf8mb4_unicode_ci",
        }
    raise ValueError("DB_DSN must have the form mysql://user:password@host:port/database")


# MySQL connection pool, created on FIRST USE rather than at import time.
#
# If the pool cannot be created (for example, the database has no free
# connections), the caller falls back to a direct connection and the next call
# tries to create the pool again. Once the database recovers, the first call
# restores the pool without a process restart.
_mysql_pool: "mysql.connector.pooling.MySQLConnectionPool | None" = None
_pool_lock = threading.Lock()


def _get_mysql_pool():
    """Per-process pool, created on demand. Returns None if it cannot be created."""
    global _mysql_pool
    if _mysql_pool is None:
        with _pool_lock:
            if _mysql_pool is None:
                try:
                    _mysql_pool = mysql.connector.pooling.MySQLConnectionPool(
                        pool_name="mypool",
                        pool_size=25,
                        pool_reset_session=False,
                        autocommit=True,
                        **parse_mysql_dsn(DB_DSN),
                    )
                    logger.info("MySQL connection pool created")
                except Exception as e:
                    # No pool on this attempt: the caller uses a direct connection and
                    # the NEXT call tries to create it again.
                    logger.warning(f"Failed to create the MySQL pool: {e}")
                    _mysql_pool = None
    return _mysql_pool


@contextmanager
def get_conn():
    """
    Context manager that yields a MySQL connection from the pool.

    Retries with exponential backoff and always closes the connection.
    """
    conn = None
    max_retries = 3
    retry_delay = 1

    # Phase 1: get a connection, with retries
    for attempt in range(max_retries):
        try:
            pool = _get_mysql_pool()
            if pool:
                conn = pool.get_connection()
            else:
                # Fallback: direct connection
                db_config = parse_mysql_dsn(DB_DSN)
                conn = mysql.connector.connect(**db_config)

            # Check that the connection is alive
            if conn and conn.is_connected():
                # The collation is already set by parse_mysql_dsn
                # (collation=utf8mb4_unicode_ci), so no per-acquisition
                # `SET collation_*` round-trip is needed.
                break  # Connection obtained
            else:
                logger.warning(
                    f"Connection attempt {attempt + 1} failed: connection not active"
                )
                if conn:
                    try:
                        conn.close()
                    except:
                        pass
                    conn = None

        except mysql.connector.Error as e:
            logger.warning(f"MySQL connection attempt {attempt + 1} failed: {e}")
            if conn:
                try:
                    conn.close()
                except:
                    pass
                conn = None

            if attempt < max_retries - 1:
                logger.debug(f"Reconnecting in {retry_delay} seconds...")
                time.sleep(retry_delay)
                retry_delay *= 2  # Exponential backoff
            else:
                logger.error(
                    f"All {max_retries} connection attempts failed"
                )
                raise mysql.connector.Error(
                    f"Could not establish MySQL connection after {max_retries} attempts"
                ) from e

    # Check that a connection was obtained
    if not conn or not conn.is_connected():
        raise mysql.connector.Error("Could not establish MySQL connection")

    # Phase 2: yield, with guaranteed cleanup
    try:
        yield conn
    finally:
        # Always close the connection
        if conn:
            try:
                conn.close()
            except:
                pass


def exec_sql(sql, params=None):
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params or ())
            conn.commit()
    except mysql.connector.Error as e:
        logger.error(f"Error executing SQL: {e}")
        logger.error(f"SQL: {sql}")
        logger.error(f"Params: {params}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error in exec_sql: {e}")
        raise


def fetch_one(sql, params=None):
    """
    Fetch a single row from the database.

    Returns:
        dict or None: The row, or None when not found or on error
    """
    conn = None
    try:
        # Get the connection directly (no context manager) to avoid a generator issue
        pool = _get_mysql_pool()
        if pool:
            conn = pool.get_connection()
        else:
            db_config = parse_mysql_dsn(DB_DSN)
            conn = mysql.connector.connect(**db_config)
        
        if conn and conn.is_connected():
            try:
                with conn.cursor(dictionary=True) as cur:
                    cur.execute(sql, params or ())
                    result = cur.fetchone()
                return result
            except mysql.connector.Error as e:
                # Re-raise MySQL errors so the outer except handles them
                raise
            except Exception as e:
                # Re-raise any other error
                raise
            finally:
                # Always close the connection, even on error
                if conn:
                    try:
                        conn.close()
                    except:
                        pass
        else:
            logger.warning("Connection is not active")
            if conn:
                try:
                    conn.close()
                except:
                    pass
            return None
    except mysql.connector.Error as e:
        # Permission or other MySQL errors: log without raising and
        # return None so the caller can fall back
        error_code = e.errno if hasattr(e, 'errno') else None
        if error_code == 1142:  # SELECT command denied
            logger.warning(f"Permission denied while fetching a row: {e}")
        else:
            logger.error(f"MySQL error while fetching a row: {e}")
        logger.debug(f"SQL: {sql}")
        logger.debug(f"Params: {params}")
        # Make sure the connection is closed, even on error
        if conn:
            try:
                conn.close()
            except:
                pass
        return None
    except Exception as e:
        logger.error(f"Unexpected error in fetch_one: {e}", exc_info=True)
        # Make sure the connection is closed, even on error
        if conn:
            try:
                conn.close()
            except:
                pass
        return None


def fetch_all(sql, params=None):
    try:
        with get_conn() as conn:
            with conn.cursor(dictionary=True) as cur:
                cur.execute(sql, params or ())
                return cur.fetchall()
    except mysql.connector.Error as e:
        logger.error(f"Error fetching rows: {e}")
        logger.error(f"SQL: {sql}")
        logger.error(f"Params: {params}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error in fetch_all: {e}")
        raise


def check_pool_health():
    """Report whether the pool has been initialized.

    Health probes call this often, so it never acquires a connection from the
    pool (that would compete with real queries under load).
    """
    # Reads the state WITHOUT forcing creation (does not call _get_mysql_pool): a
    # probe must not create the pool or open a connection. Not created yet -> False.
    if _mysql_pool is None:
        return False
    try:
        return len(_mysql_pool._cnx_queue._queue) > 0
    except Exception:
        return True
