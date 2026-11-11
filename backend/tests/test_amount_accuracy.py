from decimal import Decimal

import pytest

from pipeline.scoring import compute_amount
from pipeline.store import parse_decimal, parse_int


@pytest.mark.parametrize(
    "sales,comments,quantity,amount",
    [
        (100, 20, 100, "1999.00"),
        (1000, 20, 1000, "19990.00"),
        (200, 20, 200, "3998.00"),
        (0, 20, 0, "0.00"),
        (100, 0, 100, "1999.00"),
        (100, None, 100, "1999.00"),
        (100, -1, 100, "1999.00"),
        (None, 20, 20, "399.80"),
        (None, 0, 0, "0.00"),
        (-1, 20, 20, "399.80"),
    ],
)
def test_complete_amount(settings, sales, comments, quantity, amount):
    result = compute_amount(
        price=Decimal("19.99"),
        sales=sales,
        comments=comments,
        sales_raw=None,
        comments_raw=None,
        existing_flags=[],
        settings=settings,
    )
    assert result.estimated_sales == quantity
    assert result.amount == Decimal(amount)


@pytest.mark.parametrize(
    "price,sales,comments",
    [
        (None, 100, 10),
        (Decimal("1"), None, None),
        (Decimal("-1"), 100, 10),
        (Decimal("1"), -1, -1),
        (Decimal("NaN"), 100, 10),
    ],
)
def test_missing_invalid_does_not_produce_amount(settings, price, sales, comments):
    result = compute_amount(
        price=price,
        sales=sales,
        comments=comments,
        sales_raw=None,
        comments_raw=None,
        existing_flags=[],
        settings=settings,
    )
    assert result.amount is None
    assert result.estimated_sales is None
    assert "待核验" in result.formula


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("１万＋", 10000),
        ("1.2k+", 1200),
        ("1.2万", 12000),
        ("1,234", 1234),
        ("-5", None),
        ("约200", None),
        ("100-200", None),
        ("1,2", None),
        (True, None),
        (12.9, None),
        ("好评率99%", None),
    ],
)
def test_count_accuracy(raw, expected):
    assert parse_int(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("29.99-19.99", Decimal("19.99")),
        ("原价100 券后价79.90", Decimal("79.90")),
        ("满199减20", None),
        ("-5", None),
        ("NaN", None),
        ("10美元", None),
    ],
)
def test_price_accuracy(raw, expected):
    assert parse_decimal(raw) == expected


def test_fixed_multiplier_and_fullwidth_lower_bound(settings):
    settings.raw.setdefault("amount", {})["comment_multiplier"] = 99
    result = compute_amount(
        price=Decimal("0.29"),
        sales=10000,
        comments=3,
        sales_raw="１万＋",
        comments_raw="3",
        existing_flags=[],
        settings=settings,
    )
    assert result.estimated_sales == 10000
    assert result.amount == Decimal("2900.00")
    assert "按下限估算" in result.flags


@pytest.mark.parametrize(
    "sales_raw,comments_raw", [("评价1000", "10"), ("1000", "已售10"), ("约1000", "10")]
)
def test_field_semantics(settings, sales_raw, comments_raw):
    result = compute_amount(
        price=Decimal("5"),
        sales=1000,
        comments=10,
        sales_raw=sales_raw,
        comments_raw=comments_raw,
        existing_flags=[],
        settings=settings,
    )
    assert result.amount == (
        Decimal("5000.00") if sales_raw == "1000" else Decimal("50.00")
    )


def test_comment_fallback_records_basis_without_multiplier(settings):
    settings.raw.setdefault("amount", {})["comment_multiplier"] = 99
    result = compute_amount(
        price=Decimal("383"),
        sales=None,
        comments=None,
        sales_raw=None,
        comments_raw="383500+条评价",
        existing_flags=[],
        settings=settings,
    )
    assert result.amount == Decimal("146880500.00")
    assert result.estimated_sales == 383500
    assert "按评论数估算" in result.formula
    assert "按评论数估算" in result.flags
    assert "按下限估算" in result.flags
    assert "金额待核验" not in result.flags


def test_sales_replaces_comment_basis_and_stale_shop_flag(settings):
    result = compute_amount(
        price=Decimal("5"),
        sales=0,
        comments=1000,
        sales_raw="0",
        comments_raw="1000+",
        settings=settings,
        existing_flags=["按评论数估算", "按下限估算", "金额待核验"],
    )
    assert result.amount == Decimal("0.00")
    assert not {"按评论数估算", "按下限估算", "金额待核验"}.intersection(result.flags)


def test_amount_overflow(settings):
    result = compute_amount(
        price=Decimal("9999999999"),
        sales=9223372036854775807,
        comments=9223372036854775807,
        sales_raw=None,
        comments_raw=None,
        existing_flags=[],
        settings=settings,
    )
    assert result.amount is None
    assert "超出" in result.formula


@pytest.mark.parametrize("existing_price_raw", [False, True])
def test_history_migration_preserves_rows_foreign_keys_and_backup(
    settings, monkeypatch, existing_price_raw
):
    import sqlite3
    from alembic import command
    from alembic.config import Config
    from pipeline.db import ALEMBIC_INI, ALEMBIC_DIR, build_url, ensure_schema

    monkeypatch.delenv("IP_LEAD_DB_URL", raising=False)
    monkeypatch.setenv("IP_LEAD_DB_PATH", str(settings.database_path))
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", build_url(settings.database_path))
    cfg.attributes["settings"] = settings
    command.upgrade(cfg, "0010_export_shop_fields")
    with sqlite3.connect(settings.database_path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("""INSERT INTO products
            (id,platform,product_id,url,title,shop_name,shop_type,shop_id_missing,first_seen_at,last_seen_at)
            VALUES (1,'jd','1001','https://item.jd.com/1001.html','历史商品','历史店','企业',0,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)""")
        conn.execute("""INSERT INTO product_snapshots
            (id,product_id,snapshot_date,crawled_at,run_id,listed,need_vision,eligible,
             hit_reasons,amount,formula,missing_flags,config_version,caliber_version)
            VALUES (1,1,'2026-11-05',CURRENT_TIMESTAMP,'old-run',1,0,1,
            '[]',123.45,'旧公式','[]','old','old')""")
        conn.execute("""INSERT INTO leads
            (id,lead_no,run_id,snapshot_date,platform,platform_group,shop_key,shop_id,shop_name,shop_type,
             total_amount,link_count,ip_codes,payment_tags,confidence,hit_summary,missing_flags,status,
             caliber_version,config_version,created_at,updated_at)
            VALUES (1,'OLD-1','old-run','2026-11-05','jd','jd','jd|历史店','','历史店','企业',
            123.45,1,'ip','',80,'历史命中','[]','待复核','old','old',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)""")
        conn.execute(
            "INSERT INTO lead_links (id,lead_id,snapshot_id,amount,formula) VALUES (1,1,1,123.45,'旧公式')"
        )
        if existing_price_raw:
            conn.execute("ALTER TABLE product_snapshots ADD COLUMN price_raw TEXT")
            conn.execute("UPDATE product_snapshots SET price_raw='历史价格原文'")
    ensure_schema(settings)
    backups = list(settings.database_path.parent.glob("*.before_amount_*.db"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as conn:
        assert (
            conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            == "0010_export_shop_fields"
        )
        assert conn.execute("SELECT amount,formula FROM lead_links").fetchone() == (
            123.45,
            "旧公式",
        )
    with sqlite3.connect(settings.database_path) as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert (
            conn.execute("SELECT COUNT(*) FROM v_near_threshold_shops").fetchone()[0]
            == 0
        )
        for table in ("products", "product_snapshots", "leads", "lead_links"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1
        assert conn.execute(
            "SELECT amount,formula FROM product_snapshots"
        ).fetchone() == (123.45, "旧公式")
        assert conn.execute("SELECT price_raw FROM product_snapshots").fetchone()[
            0
        ] == ("历史价格原文" if existing_price_raw else None)
        assert conn.execute("SELECT amount FROM v_lead_links").fetchone()[0] == 123.45
        conn.execute("UPDATE product_snapshots SET amount=NULL")
        conn.execute("UPDATE lead_links SET amount=NULL")
        assert conn.execute("SELECT amount FROM v_lead_links").fetchone()[0] is None
    ensure_schema(settings)
    assert len(list(settings.database_path.parent.glob("*.before_amount_*.db"))) == 1


def test_already_stamped_amount_database_restores_missing_view(settings, monkeypatch):
    import sqlite3
    from pipeline.db import ensure_schema

    monkeypatch.delenv("IP_LEAD_DB_URL", raising=False)
    monkeypatch.setenv("IP_LEAD_DB_PATH", str(settings.database_path))
    ensure_schema(settings)
    with sqlite3.connect(settings.database_path) as conn:
        conn.execute("DROP VIEW v_near_threshold_shops")
        conn.execute("UPDATE alembic_version SET version_num='0011_amount_accuracy'")
    ensure_schema(settings)
    with sqlite3.connect(settings.database_path) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM v_near_threshold_shops").fetchone()[0]
            == 0
        )
        assert (
            conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            == "0013_sales_amount_view"
        )
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
