"""Connection pool of mail_api.common.db.

This is the module that the mail-api and the queue processor actually use
(mail_shared.db.mysql_sync is a separate copy that these services do NOT call).

Without the pool, get_conn() opens and closes a connection per call; with
DB_POOL_ENABLED the connections are reused, which is much cheaper over TLS.
"""

import importlib.util
import pathlib
import sys
import types
from unittest import mock

import pytest


@pytest.fixture
def db(monkeypatch):
    """Imports mail_api.common.db with mysql.connector stubbed."""
    fake_pool_cls = mock.MagicMock()
    connector = types.ModuleType("mysql.connector")
    pooling = types.ModuleType("mysql.connector.pooling")

    class PoolError(Exception):
        pass

    class MySQLError(Exception):
        errno = None

    pooling.MySQLConnectionPool = fake_pool_cls
    pooling.PoolError = PoolError
    connector.pooling = pooling
    connector.Error = MySQLError
    connector.connect = mock.MagicMock(name="connect_direct")
    mysql_mod = types.ModuleType("mysql")
    mysql_mod.connector = connector

    monkeypatch.setitem(sys.modules, "mysql", mysql_mod)
    monkeypatch.setitem(sys.modules, "mysql.connector", connector)
    monkeypatch.setitem(sys.modules, "mysql.connector.pooling", pooling)

    cfg = types.ModuleType("mail_api.common.config")
    cfg.DB_AUTOCOMMIT = True
    cfg.DB_CONNECTION_TIMEOUT = 10
    cfg.DB_DSN = "mysql://u:p@old-host:3306/revbridge_connect"
    monkeypatch.setitem(sys.modules, "mail_api.common.config", cfg)

    # Loads db.py BY PATH, under a throwaway name, instead of importing
    # `mail_api.common.db`. Touching the real package's sys.modules would break
    # the other test files (they would re-import a fresh mail_api and lose what
    # the conftest set up). This way the real package is never touched.
    caminho = (
        pathlib.Path(__file__).resolve().parents[1]
        / "src" / "mail_api" / "common" / "db.py"
    )
    spec = importlib.util.spec_from_file_location("db_under_test", caminho)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    mod._pool = None
    mod._pool_key = None
    yield mod, connector, fake_pool_cls, PoolError, cfg


def test_without_flag_opens_a_connection_per_call(db, monkeypatch):
    mod, connector, fake_pool_cls, _, _ = db
    monkeypatch.delenv("DB_POOL_ENABLED", raising=False)
    for _ in range(3):
        with mod.get_conn():
            pass
    assert connector.connect.call_count == 3
    fake_pool_cls.assert_not_called()


def test_with_flag_reuses_and_pings(db, monkeypatch):
    mod, connector, fake_pool_cls, _, _ = db
    monkeypatch.setenv("DB_POOL_ENABLED", "true")
    monkeypatch.setenv("DB_POOL_SIZE", "9")
    for _ in range(4):
        with mod.get_conn():
            pass
    assert fake_pool_cls.call_count == 1, "one pool for the 4 calls"
    assert connector.connect.call_count == 0, "no direct connection"
    assert fake_pool_cls.call_args.kwargs["pool_size"] == 9
    assert fake_pool_cls.return_value.get_connection.return_value.ping.called


def test_pool_size_falls_back_to_DB_EXECUTOR_WORKERS(db, monkeypatch):
    """Without DB_POOL_SIZE, the pool size falls back to DB_EXECUTOR_WORKERS."""
    mod, _, fake_pool_cls, _, _ = db
    monkeypatch.setenv("DB_POOL_ENABLED", "1")
    monkeypatch.delenv("DB_POOL_SIZE", raising=False)
    monkeypatch.setenv("DB_EXECUTOR_WORKERS", "16")
    with mod.get_conn():
        pass
    assert fake_pool_cls.call_args.kwargs["pool_size"] == 16


def test_pool_respects_the_connector_limit(db, monkeypatch):
    mod, _, fake_pool_cls, _, _ = db
    monkeypatch.setenv("DB_POOL_ENABLED", "1")
    monkeypatch.setenv("DB_POOL_SIZE", "999")
    with mod.get_conn():
        pass
    assert fake_pool_cls.call_args.kwargs["pool_size"] == mod._POOL_MAX == 32


def test_exhausted_pool_falls_back_to_a_direct_connection(db, monkeypatch):
    mod, connector, fake_pool_cls, PoolError, _ = db
    monkeypatch.setenv("DB_POOL_ENABLED", "1")
    fake_pool_cls.return_value.get_connection.side_effect = PoolError("exhausted")
    with mod.get_conn() as c:
        assert c is connector.connect.return_value
    assert connector.connect.call_count == 1


def test_executor_respects_DB_EXECUTOR_WORKERS(db, monkeypatch):
    """The DB executor size comes from DB_EXECUTOR_WORKERS."""
    mod, _, _, _, _ = db
    monkeypatch.setenv("DB_EXECUTOR_WORKERS", "7")
    mod._db_executor = None
    ex = mod._get_db_executor()
    try:
        assert ex._max_workers == 7
    finally:
        ex.shutdown(wait=False)
        mod._db_executor = None
