"""用后端真实视图定义建库的集成自测（不复制视图 SQL）。

要求（UPDATES 15:31）：若后端已有真实视图，用它建的库做一次集成自测。
本测试直接加载 backend/pipeline/views.py 的 CREATE_VIEWS_SQL，建一个与后端
表结构同名的临时库，再调用导出模块；后端视图以后无论用英文列名还是中文
别名、是否带版本列，本测试都按真实定义执行。
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import openpyxl
import pytest

from lead_excel import export_leads_excel

BACKEND_VIEWS = (
    Path(__file__).resolve().parents[2] / "backend" / "pipeline" / "views.py"
)


def _load_backend_views():
    if not BACKEND_VIEWS.is_file():
        pytest.skip(f"后端视图文件不存在：{BACKEND_VIEWS}")
    spec = importlib.util.spec_from_file_location("_backend_views", BACKEND_VIEWS)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


SCHEMA = """
CREATE TABLE shops (
    id INTEGER PRIMARY KEY,
    platform TEXT, shop_id TEXT, shop_name TEXT,
    shop_type TEXT, shop_label_raw TEXT
);
CREATE TABLE leads (
    id INTEGER PRIMARY KEY,
    lead_no TEXT, snapshot_date TEXT, platform TEXT, platform_group TEXT,
    shop_key TEXT, shop_id TEXT, shop_name TEXT, shop_type TEXT,
    shop_label_raw TEXT,
    total_amount REAL, link_count INTEGER, ip_codes TEXT, payment_tags TEXT,
    confidence REAL, hit_summary TEXT, missing_flags TEXT, status TEXT,
    review_result TEXT, reviewer TEXT,
    caliber_version TEXT, config_version TEXT
);
CREATE TABLE products (
    id INTEGER PRIMARY KEY,
    platform TEXT, product_id TEXT, url TEXT, title TEXT, shop_id TEXT,
    shop_name TEXT, shop_type TEXT, shop_label_raw TEXT,
    main_image_url TEXT, image_path TEXT, image_phash TEXT
);
CREATE TABLE product_snapshots (
    id INTEGER PRIMARY KEY,
    product_id INTEGER, snapshot_date TEXT, price REAL,
    sales_raw TEXT, comments_raw TEXT, estimated_sales INTEGER,
    text_score REAL, visual_score REAL, final_score REAL,
    hit_reasons TEXT, missing_flags TEXT, amount REAL, formula TEXT,
    caliber_version TEXT, config_version TEXT, listed INTEGER, ip_code TEXT,
    eligible INTEGER
);
CREATE TABLE lead_links (
    id INTEGER PRIMARY KEY,
    lead_id INTEGER, snapshot_id INTEGER, ip_code TEXT,
    amount REAL, formula TEXT
);
"""

DATA = """
INSERT INTO shops VALUES (1, 'tmall', 'S1', '后端视图旗舰店', '企业', '天猫');
INSERT INTO leads VALUES (
    1, 'tmall-20261105-0001', '2026-11-05', 'tmall', 'taobao',
    'taobao|后端视图旗舰店', 'S1', '后端视图旗舰店', '企业', '天猫',
    600000.0, 1, 'huluxiongdi', '企业;天猫店', 92.5,
    '文字:葫芦娃:88', '[]', '待复核', '', '',
    'v-view-1', 'cfg-view-1'
);
INSERT INTO products VALUES (
    1, 'tmall', 'P1', 'https://item.example.com/p1', '葫芦娃手办',
    'S1', '后端视图旗舰店', '企业', '天猫',
    'https://img.example.com/p1.jpg', '', ''
);
INSERT INTO product_snapshots VALUES (
    1, 1, '2026-11-05', 50.0, '12000', '1000', 10000,
    88.0, 90.0, 89.0, '["文字:葫芦娃:88"]', '[]',
    600000.0, '50.00×12000=600000.00', 'v-view-1', 'cfg-view-1', 1, 'huluxiongdi', 1
);
INSERT INTO lead_links VALUES (
    1, 1, 1, 'huluxiongdi', 600000.0, '50.00×12000=600000.00'
);
"""


def test_export_against_database_built_from_backend_views(tmp_path):
    views = _load_backend_views()
    db_path = tmp_path / "data" / "leads.db"
    db_path.parent.mkdir(parents=True)

    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.executescript(DATA)
        conn.execute("ALTER TABLE leads ADD COLUMN run_id TEXT DEFAULT 'integration'")
        conn.execute(
            "ALTER TABLE product_snapshots ADD COLUMN run_id TEXT DEFAULT 'integration'"
        )
        for statement in views.CREATE_VIEWS_SQL:
            conn.execute(statement)
        conn.commit()
        view_columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(v_lead_summary)").fetchall()
        }
    finally:
        conn.close()

    config = {
        "storage": {"db_path": str(db_path)},
        "export": {"dir": str(tmp_path / "输出")},
        "aggregate": {"shop_threshold": 500000},
        "amount": {"comment_multiplier": 10},
        "caliber_version": "v-config-fallback",
        "config_version": "cfg-config-fallback",
    }
    path = Path(export_leads_excel(None, "2026-11-05", config))
    assert path.is_file()

    wb = openpyxl.load_workbook(path)
    assert wb.sheetnames == ["链接明细"]
    links = wb["链接明细"]
    assert [cell.value for cell in links[1]] == [
        "平台",
        "IP",
        "店铺名",
        "标题",
        "缩略图",
        "商品链接",
        "单价",
        "销量",
        "评论",
        "总金额",
    ]
    assert links["A2"].value == "tmall"
    assert links["C2"].value == "后端视图旗舰店"
    assert links["F2"].value == "https://item.example.com/p1"
    assert links["J2"].value == links["J3"].value == 600000
