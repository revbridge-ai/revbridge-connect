"""Connection pool of mail_shared.db.mysql_sync.

Why these tests exist: opening one connection per CALL makes the handshake dominate the cost
(for a remote database over TLS, tens of milliseconds per call against a couple of
milliseconds when the connection is reused), which caps the queue processor's throughput.
The pool is opt-in through DB_POOL_ENABLED, so the tests must prove both sides: with the flag
it reuses connections, without the flag it keeps one connection per call.
"""

import sys
import types
from unittest import mock

import pytest


@pytest.fixture
def mysql_sync(monkeypatch):
    """Imports the module with mysql.connector stubbed (no real database)."""
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
    connector.connect = mock.MagicMock(name="connect_direto")
    mysql_mod = types.ModuleType("mysql")
    mysql_mod.connector = connector

    monkeypatch.setitem(sys.modules, "mysql", mysql_mod)
    monkeypatch.setitem(sys.modules, "mysql.connector", connector)
    monkeypatch.setitem(sys.modules, "mysql.connector.pooling", pooling)
    # Clear the whole PACKAGE, not just the submodule: a cached `mail_shared.db` keeps its
    # `mysql_sync` attribute pointing to the old module, and `from ... import` would return
    # that one, imported with the REAL mysql.connector and without the stubs.
    for nome in [m for m in sys.modules if m.startswith("mail_shared")]:
        monkeypatch.delitem(sys.modules, nome, raising=False)

    from mail_shared.db import mysql_sync as mod

    monkeypatch.setenv("DB_DSN", "mysql://u:p@h:3306/d")
    mod._pool = None
    mod._pool_key = None
    return mod, connector, fake_pool_cls, PoolError


def test_without_flag_opens_a_connection_per_call(mysql_sync, monkeypatch):
    mod, connector, fake_pool_cls, _ = mysql_sync
    monkeypatch.delenv("DB_POOL_ENABLED", raising=False)
    for _ in range(3):
        with mod.get_conn():
            pass
    assert connector.connect.call_count == 3, "one connection per call without the flag"
    fake_pool_cls.assert_not_called(), "must not create a pool without the flag"


def test_with_flag_uses_the_pool_and_returns_the_connection(mysql_sync, monkeypatch):
    mod, connector, fake_pool_cls, _ = mysql_sync
    monkeypatch.setenv("DB_POOL_ENABLED", "true")
    monkeypatch.setenv("DB_POOL_SIZE", "7")
    for _ in range(3):
        with mod.get_conn():
            pass
    # A single pool for the 3 calls, and no direct connection.
    assert fake_pool_cls.call_count == 1
    assert connector.connect.call_count == 0
    assert fake_pool_cls.call_args.kwargs["pool_size"] == 7
    # ping(reconnect=True) is what protects against a connection closed by wait_timeout.
    conn = fake_pool_cls.return_value.get_connection.return_value
    assert conn.ping.called


def test_pool_size_respects_the_connector_maximum(mysql_sync, monkeypatch):
    mod, _, fake_pool_cls, _ = mysql_sync
    monkeypatch.setenv("DB_POOL_ENABLED", "1")
    monkeypatch.setenv("DB_POOL_SIZE", "500")
    with mod.get_conn():
        pass
    assert fake_pool_cls.call_args.kwargs["pool_size"] == mod._POOL_MAX == 32


def test_exhausted_pool_falls_back_to_a_direct_connection(mysql_sync, monkeypatch):
    mod, connector, fake_pool_cls, PoolError = mysql_sync
    monkeypatch.setenv("DB_POOL_ENABLED", "1")
    fake_pool_cls.return_value.get_connection.side_effect = PoolError("exhausted")
    with mod.get_conn() as c:
        assert c is connector.connect.return_value
    assert connector.connect.call_count == 1, "must not become an application error"


def test_dsn_change_recreates_the_pool(mysql_sync, monkeypatch):
    """Database cutover: the old pool points to the old host."""
    mod, _, fake_pool_cls, _ = mysql_sync
    monkeypatch.setenv("DB_POOL_ENABLED", "1")
    with mod.get_conn():
        pass
    monkeypatch.setenv("DB_DSN", "mysql://u:p@other-host:21232/d")
    with mod.get_conn():
        pass
    assert fake_pool_cls.call_count == 2
