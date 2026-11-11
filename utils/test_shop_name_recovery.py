import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest


@pytest.fixture
def recovery(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "shop_recovery_test", Path(__file__).with_name("shop_name_recovery.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for folder in ("backend/data", "database", "doc"):
        (tmp_path / folder).mkdir(parents=True)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    db = tmp_path / "backend/data/leads.db"
    with sqlite3.connect(db) as conn:
        conn.executescript("""
            CREATE TABLE products(id INTEGER PRIMARY KEY, platform TEXT, product_id TEXT, shop_id TEXT, shop_name TEXT);
            CREATE TABLE shops(platform TEXT, shop_id TEXT, shop_name TEXT);
            CREATE TABLE product_snapshots(id INTEGER PRIMARY KEY, product_id INTEGER, missing_flags TEXT);
            INSERT INTO products VALUES(1,'jd','one','s1',NULL),(2,'jd','two','s2',NULL);
            INSERT INTO shops VALUES('jd','s1',NULL),('jd','s2',NULL);
            INSERT INTO product_snapshots VALUES(1,1,'["店铺名缺失","价格缺失"]'),(2,2,'["店铺名缺失"]');
        """)
    monkeypatch.setattr(
        module,
        "inventory",
        lambda **kwargs: (
            [],
            {
                ("jd", "one"): [{"name": "证据店一", "source": "fixture"}],
                ("jd", "two"): [{"name": "证据店二", "source": "fixture"}],
            },
        ),
    )
    return module, db


def test_single_product_writeback_and_backup(recovery):
    module, db = recovery
    report = module.writeback("jd", "one")
    assert report["updated"] == 1
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT shop_name FROM products ORDER BY id"
        ).fetchall() == [("证据店一",), (None,)]
        assert (
            conn.execute("SELECT shop_name FROM shops WHERE shop_id='s1'").fetchone()[0]
            == "证据店一"
        )
        assert json.loads(
            conn.execute(
                "SELECT missing_flags FROM product_snapshots WHERE id=1"
            ).fetchone()[0]
        ) == ["价格缺失"]
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    with sqlite3.connect(report["backup"]) as conn:
        assert (
            conn.execute("SELECT shop_name FROM products WHERE id=1").fetchone()[0]
            is None
        )


@pytest.mark.parametrize(
    "platform,pid", [("jd", None), (None, "one"), ("jd", "unknown")]
)
def test_invalid_single_target_does_not_write(recovery, platform, pid):
    module, db = recovery
    with pytest.raises(ValueError):
        module.writeback(platform, pid)
    with sqlite3.connect(db) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM products WHERE shop_name IS NOT NULL"
            ).fetchone()[0]
            == 0
        )


def test_schema_check_closes_source_and_backup_connections(recovery, monkeypatch):
    from types import SimpleNamespace
    from pipeline import db as database

    _, path = recovery
    connections = []
    original = sqlite3.connect

    def tracked(*args, **kwargs):
        connection = original(*args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.delenv("IP_LEAD_DB_URL", raising=False)
    monkeypatch.setattr(sqlite3, "connect", tracked)
    monkeypatch.setattr(database.command, "upgrade", lambda *_: None)
    database.ensure_schema(SimpleNamespace(database_path=path))
    assert len(connections) == 2
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")
