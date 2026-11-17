"""The mail_milter.common.db pool is created on demand, not at import time.

If creating the pool fails (e.g. the database has no free connections), the
module falls back to a direct connection for that call and tries to create the
pool again on the next one; a failure must never leave the process without a
pool for good (one direct connection per call would keep the database exhausted).

The milter is the hottest path: Postfix calls it for every message.
"""

import importlib.util
import pathlib
import sys
import types
from unittest import mock

import pytest


@pytest.fixture
def db(monkeypatch):
    fake_pool_cls = mock.MagicMock(name="MySQLConnectionPool")
    connector = types.ModuleType("mysql.connector")
    pooling = types.ModuleType("mysql.connector.pooling")
    pooling.MySQLConnectionPool = fake_pool_cls

    class MySQLError(Exception):
        errno = None

    connector.pooling = pooling
    connector.Error = MySQLError
    connector.connect = mock.MagicMock(name="connect_direct")
    mysql_mod = types.ModuleType("mysql")
    mysql_mod.connector = connector

    monkeypatch.setitem(sys.modules, "mysql", mysql_mod)
    monkeypatch.setitem(sys.modules, "mysql.connector", connector)
    monkeypatch.setitem(sys.modules, "mysql.connector.pooling", pooling)

    cfg = types.ModuleType("mail_milter.common.config")
    cfg.DB_DSN = "mysql://u:p@h:3306/revbridge_connect"
    monkeypatch.setitem(sys.modules, "mail_milter.common.config", cfg)

    # Load by PATH, under a throwaway name: touching the real package in
    # sys.modules would break the service's other test files.
    caminho = (
        pathlib.Path(__file__).resolve().parents[1]
        / "src" / "mail_milter" / "common" / "db.py"
    )
    spec = importlib.util.spec_from_file_location("db_milter_under_test", caminho)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, connector, fake_pool_cls


def test_import_does_not_create_the_pool(db):
    """Importing must not touch the database; only the first use does."""
    mod, _, fake_pool_cls = db
    assert mod._mysql_pool is None
    fake_pool_cls.assert_not_called()


def test_first_use_creates_the_pool_and_reuses_it(db):
    mod, connector, fake_pool_cls = db
    for _ in range(3):
        with mod.get_conn():
            pass
    assert fake_pool_cls.call_count == 1, "one pool for the 3 calls"
    assert connector.connect.call_count == 0, "no direct connection"


def test_pool_creation_failure_is_not_permanent(db):
    """A failure to create the pool must not leave the process WITHOUT a pool for good."""
    mod, connector, fake_pool_cls = db
    fake_pool_cls.side_effect = Exception("database has no free connection")

    with mod.get_conn() as c:
        assert c is connector.connect.return_value, "falls back to a direct connection"
    assert mod._mysql_pool is None

    # The database recovered: the NEXT call must try to create the pool again.
    fake_pool_cls.side_effect = None
    with mod.get_conn():
        pass
    assert mod._mysql_pool is not None, "pool restored without a restart"


def test_probe_does_not_create_the_pool(db):
    """Health probes call check_pool_health: it must not create the pool nor connect."""
    mod, connector, fake_pool_cls = db
    assert mod.check_pool_health() is False
    fake_pool_cls.assert_not_called()
    assert connector.connect.call_count == 0


def test_no_set_collation_per_acquisition(db):
    """No per-acquisition SET collation_* round-trips (the DSN already sets the collation)."""
    mod, _, fake_pool_cls = db
    conn = fake_pool_cls.return_value.get_connection.return_value
    with mod.get_conn():
        pass
    executados = [
        str(c) for c in conn.cursor.return_value.__enter__.return_value.execute.call_args_list
    ]
    assert not any("collation" in e.lower() for e in executados), executados
