"""单表导出仍尊重待复核视图过滤，保留无店铺的独立商品。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import openpyxl

from lead_excel import export_leads_excel

SCHEMA = """
CREATE TABLE leads (
    lead_no TEXT PRIMARY KEY,
    snapshot_date TEXT NOT NULL,
    platform TEXT NOT NULL,
    shop_id TEXT NOT NULL,
    shop_name TEXT NOT NULL,
    shop_type TEXT NOT NULL,
    shop_label_raw TEXT,
    ip_codes TEXT,
    link_count INTEGER,
    total_amount REAL,
    tier TEXT,
    payment_tags TEXT,
    confidence REAL,
    hit_summary TEXT,
    missing_flags TEXT,
    status TEXT NOT NULL,
    review_result TEXT,
    reviewer TEXT,
    caliber_version TEXT,
    config_version TEXT
);

CREATE TABLE lead_links (
    lead_no TEXT,
    snapshot_date TEXT NOT NULL,
    ip_code TEXT NOT NULL,
    product_id TEXT NOT NULL,
    image_path TEXT,
    main_image_url TEXT,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    price REAL,
    sales_raw TEXT,
    comments_raw TEXT,
    estimated_sales INTEGER,
    amount REAL,
    formula TEXT,
    text_score REAL,
    visual_score REAL,
    final_score REAL,
    hit_reasons TEXT,
    missing_flags TEXT
);

CREATE VIEW v_lead_summary AS
SELECT lead_no AS lead_id, snapshot_date, platform, shop_id, shop_name,
       shop_type, shop_label_raw, ip_codes, link_count,
       total_amount AS shop_amount, tier, payment_tags AS payment_ability,
       confidence, hit_summary AS hit_reasons_summary, missing_flags,
       review_result, reviewer,
       CASE shop_type WHEN '企业' THEN 1 WHEN '个体户' THEN 2
            WHEN '未知' THEN 3 WHEN '个人' THEN 4 ELSE 5 END AS shop_type_rank,
       caliber_version, config_version
FROM leads WHERE status = '待复核'
ORDER BY shop_type_rank, confidence DESC, total_amount DESC;

CREATE VIEW v_lead_links AS
SELECT ll.lead_no AS lead_id, ll.snapshot_date, ll.ip_code, ll.product_id,
       COALESCE(l.shop_name, '') AS shop_name,
       ll.image_path, ll.main_image_url, ll.title, ll.url, ll.price,
       ll.sales_raw, ll.comments_raw, ll.estimated_sales, ll.amount,
       ll.formula AS amount_formula, ll.text_score, ll.visual_score,
       ll.final_score, ll.hit_reasons, ll.missing_flags
FROM lead_links ll
LEFT JOIN leads l ON l.lead_no = ll.lead_no
WHERE (l.lead_no IS NOT NULL AND l.status = '待复核') OR ll.lead_no IS NULL
ORDER BY ll.snapshot_date DESC, ll.amount DESC;

-- 旧库可能残留的 verify 视图：导出必须完全不读、也不生成对应 Sheet。
CREATE VIEW v_lead_verify_summary AS
SELECT lead_no AS lead_id, snapshot_date, platform, shop_id, shop_name,
       shop_type, shop_label_raw, ip_codes, link_count,
       total_amount AS shop_amount, tier, payment_tags AS payment_ability,
       confidence, hit_summary AS hit_reasons_summary, missing_flags,
       review_result, reviewer, 1 AS shop_type_rank,
       caliber_version, config_version
FROM leads WHERE status = '待核实';

CREATE VIEW v_lead_verify_links AS
SELECT ll.lead_no AS lead_id, ll.snapshot_date, ll.ip_code, ll.product_id,
       COALESCE(l.shop_name, '') AS shop_name, ll.image_path,
       ll.main_image_url, ll.title, ll.url, ll.price, ll.sales_raw, ll.comments_raw,
       estimated_sales, amount, formula AS amount_formula, text_score,
       visual_score, final_score, hit_reasons, missing_flags
FROM lead_links ll LEFT JOIN leads l ON l.lead_no = ll.lead_no
WHERE ll.lead_no IN (SELECT lead_no FROM leads WHERE status = '待核实');
"""


def _make_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "data" / "leads.db"
    db_path.parent.mkdir(parents=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.executemany(
            "INSERT INTO leads VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    "taobao-20261105-0001",
                    "2026-11-05",
                    "taobao",
                    "S-NORMAL",
                    "正常店",
                    "企业",
                    "淘宝",
                    "huluxiongdi",
                    1,
                    600000.0,
                    "达标",
                    "企业",
                    90.0,
                    "文字:葫芦娃:80",
                    "[]",
                    "待复核",
                    "",
                    "",
                    "v1.0",
                    "2026-11-05.1",
                ),
                (
                    "taobao-20261105-0002",
                    "2026-11-05",
                    "taobao",
                    "S-NEAR",
                    "临界店",
                    "个体户",
                    "淘宝",
                    "huluxiongdi",
                    1,
                    400000.0,
                    "临界",
                    "个体户",
                    80.0,
                    "文字:葫芦娃:80",
                    "[]",
                    "待复核",
                    "",
                    "",
                    "v1.0",
                    "2026-11-05.1",
                ),
                (
                    "taobao-20261105-0003",
                    "2026-11-05",
                    "taobao",
                    "S-VERIFY",
                    "旧待核实店",
                    "企业",
                    "淘宝",
                    "huluxiongdi",
                    1,
                    700000.0,
                    "待核验",
                    "企业",
                    80.0,
                    "文字:葫芦娃:80",
                    '["评论缺失,待核验"]',
                    "待核实",
                    "",
                    "",
                    "v1.0",
                    "2026-11-05.1",
                ),
            ],
        )
        conn.executemany(
            "INSERT INTO lead_links VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    "taobao-20261105-0001",
                    "2026-11-05",
                    "huluxiongdi",
                    "P-NORMAL",
                    "",
                    "https://img.example/n.jpg",
                    "正常链接",
                    "https://item.example/n",
                    300.0,
                    "1000",
                    "100",
                    1000,
                    600000.0,
                    "300.00×min(1000,100×10)=600000.00",
                    80.0,
                    80.0,
                    80.0,
                    "文字:葫芦娃:80",
                    "[]",
                ),
                (
                    "taobao-20261105-0002",
                    "2026-11-05",
                    "huluxiongdi",
                    "P-NEAR",
                    "",
                    "https://img.example/r.jpg",
                    "临界链接",
                    "https://item.example/r",
                    200.0,
                    "2000",
                    "1000",
                    2000,
                    400000.0,
                    "200.00×min(2000,1000×10)=400000.00",
                    80.0,
                    80.0,
                    80.0,
                    "文字:葫芦娃:80",
                    '["评论缺失,待核验"]',
                ),
                (
                    "taobao-20261105-0003",
                    "2026-11-05",
                    "huluxiongdi",
                    "P-VERIFY",
                    "",
                    "https://img.example/v.jpg",
                    "旧待核实链接",
                    "https://item.example/v",
                    700.0,
                    "1000",
                    "",
                    1000,
                    700000.0,
                    "700.00×1000=700000.00",
                    80.0,
                    80.0,
                    80.0,
                    "文字:葫芦娃:80",
                    '["评论缺失,待核验"]',
                ),
                # 无 shop_id：lead_id 为空，只能出现在链接明细。
                (
                    None,
                    "2026-11-05",
                    "huluxiongdi",
                    "P-NOSHOP",
                    "",
                    "https://img.example/s.jpg",
                    "无店铺ID链接",
                    "https://item.example/s",
                    100.0,
                    "1000",
                    "100",
                    1000,
                    100000.0,
                    "100.00×min(1000,100×10)=100000.00",
                    80.0,
                    80.0,
                    80.0,
                    "文字:葫芦娃:80",
                    '["店铺ID缺失"]',
                ),
            ],
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


def test_export_only_pending_review_and_standalone_products(tmp_path):
    db_path = _make_db(tmp_path)
    config = {
        "storage": {"db_path": str(db_path)},
        "export": {"dir": str(tmp_path / "out")},
        "aggregate": {"shop_threshold": 500000},
        "amount": {"comment_multiplier": 10},
    }
    path = Path(export_leads_excel(db_path, "2026-11-05", config))
    wb = openpyxl.load_workbook(path)

    assert wb.sheetnames == ["链接明细"]
    links = wb["链接明细"]
    assert [links.cell(row=row, column=4).value for row in range(2, links.max_row)] == [
        "正常链接",
        "临界链接",
        "无店铺ID链接",
    ]
    assert links.cell(links.max_row, 10).value == 1100000


def test_single_sheet_filter_covers_ten_columns_except_grand_total(tmp_path):
    db_path = _make_db(tmp_path)
    config = {
        "storage": {"db_path": str(db_path)},
        "export": {"dir": str(tmp_path / "out")},
        "aggregate": {"shop_threshold": 500000},
        "amount": {"comment_multiplier": 10},
    }
    path = Path(export_leads_excel(db_path, "2026-11-05", config))
    wb = openpyxl.load_workbook(path)
    links = wb["链接明细"]
    assert links.auto_filter.ref == f"A1:J{links.max_row - 1}"
    assert links.freeze_panes == "A2"
    assert links.max_column == 10
