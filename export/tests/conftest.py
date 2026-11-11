"""pytest 夹具：构造一个只含视图 + 底层表的 SQLite，模拟后端输出。"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

SUMMARY_ROWS = [
    # lead_no, snapshot_date, platform, shop_id, shop_name, shop_type, label,
    # ip_codes, link_count, total_amount, tier, payment, confidence, hit_summary,
    # missing_flags, status, review_result, reviewer
    (
        "tmall-20261105-0001", "2026-11-05", "tmall", "S1", "企业旗舰店", "企业",
        "天猫", "huluxiongdi、heimaojingzhang", 2, 800000.0, "达标", "企业;天猫店", 92.5,
        "文字:奥特曼:40；视觉:葫芦娃:90", "", "待复核", "已确认", "张三",
    ),
    (
        "jd-20261105-0001", "2026-11-05", "jd", "S2", "个人小店", "个人",
        "京东POP", "huluxiongdi", 1, 600000.0, "待核验", "个人", 99.0,
        "文字:黑猫警长:88", "销量缺失", "待复核", "", "",
    ),
    (
        "1688-20261105-0001", "2026-11-05", "1688", "S3", "个体户商行", "个体户",
        "1688实力商家", "yeluoli", 1, 300000.0, "临界", "个体户;实力商家", 70.0,
        "文字:叶罗丽:75", "", "待复核", "", "",
    ),
    (
        "taobao-20261105-0002", "2026-11-05", "taobao", "S4", "未知类型店", "未知",
        "淘宝", "wuhuang", 1, 900000.0, "达标", "未知", 80.0,
        "文字:吾皇:70；视觉:吾皇:80", "", "待复核", "", "",
    ),
    # 非当日，不应导出
    (
        "taobao-20261104-0001", "2026-11-04", "taobao", "S9", "昨日店", "企业",
        "淘宝", "huluxiongdi", 1, 1000000.0, "达标", "企业", 99.0,
        "文字:葫芦娃:99", "", "待复核", "", "",
    ),
]

LINK_ROWS = [
    # lead_no, snapshot_date, ip, product, image_path, main_image_url, title, url,
    # price, sales_raw, comments_raw, est_sales, amount, formula, text, visual,
    # final, hit, missing
    (
        "tmall-20261105-0001", "2026-11-05", "huluxiongdi", "P1", "{image_a}",
        "https://img.example.com/p1.jpg", "葫芦娃手办 企业旗舰店", "https://item.example.com/p1",
        50.0, "10000", "1200", 10000, 500000.0, "50.00×min(10000,1200×10)=500000.00",
        88.0, 90.0, 89.0, "文字:葫芦娃:88；视觉:葫芦娃:90", "",
    ),
    (
        "tmall-20261105-0001", "2026-11-05", "heimaojingzhang", "P2", "{missing_image}",
        "https://img.example.com/p2.jpg", "黑猫警长拼图", "https://item.example.com/p2",
        30.0, "10000", "1000", 10000, 300000.0, "30.00×10000=300000.00",
        80.0, 85.0, 82.0, "文字:黑猫警长:80", "视觉分缺失",
    ),
    (
        "jd-20261105-0001", "2026-11-05", "huluxiongdi", "P3", "{image_b}",
        "https://img.example.com/p3.jpg", "黑猫警长摆件", "https://item.example.com/p3",
        99.0, "", "800", 8000, 600000.0, "99.00×(800×10)=792000.00",
        90.0, 88.0, 89.0, "文字:黑猫警长:90；视觉:黑猫警长:88", "销量缺失",
    ),
    (
        "1688-20261105-0001", "2026-11-05", "yeluoli", "P4", "",
        "https://img.example.com/p4.jpg", "叶罗丽卡片", "https://detail.1688.com/offer/P4.html",
        20.0, "27500", "2000", 20000, 550000.0, "20.00×27500=550000.00",
        75.0, 80.0, 78.0, "文字:叶罗丽:75", "",
    ),
    (
        "taobao-20261105-0002", "2026-11-05", "wuhuang", "P5", "",
        "https://img.example.com/p5.jpg", "吾皇万睡抱枕", "https://item.taobao.com/item.htm?id=P5",
        45.0, "20000", "3000", 20000, 900000.0, "45.00×min(20000,3000×10)=900000.00",
        70.0, 80.0, 76.0, "文字:吾皇:70；视觉:吾皇:80", "",
    ),
    # 非当日，不应导出
    (
        "taobao-20261104-0001", "2026-11-04", "huluxiongdi", "P9", "",
        "https://img.example.com/p9.jpg", "昨日链接", "https://item.example.com/p9",
        10.0, "100", "10", 100, 1000.0, "10.00×100=1000.00",
        50.0, 50.0, 50.0, "文字:葫芦娃:50", "",
    ),
]

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
SELECT
    lead_no AS lead_id,
    snapshot_date,
    platform,
    shop_id,
    shop_name,
    shop_type,
    shop_label_raw,
    ip_codes,
    link_count,
    total_amount AS shop_amount,
    tier,
    payment_tags AS payment_ability,
    confidence,
    hit_summary AS hit_reasons_summary,
    missing_flags,
    review_result,
    reviewer,
    CASE shop_type
        WHEN '企业' THEN 1
        WHEN '个体户' THEN 2
        WHEN '未知' THEN 3
        WHEN '个人' THEN 4
        ELSE 5
    END AS shop_type_rank,
    caliber_version,
    config_version
FROM leads
WHERE status = '待复核'
ORDER BY shop_type_rank, confidence DESC, total_amount DESC;

CREATE VIEW v_lead_links AS
SELECT
    ll.lead_no AS lead_id,
    ll.snapshot_date,
    ll.ip_code,
    ll.product_id,
    COALESCE(l.shop_name, '') AS shop_name,
    ll.image_path,
    ll.main_image_url,
    ll.title,
    ll.url,
    ll.price,
    ll.sales_raw,
    ll.comments_raw,
    ll.estimated_sales,
    ll.amount,
    ll.formula AS amount_formula,
    ll.text_score,
    ll.visual_score,
    ll.final_score,
    ll.hit_reasons,
    ll.missing_flags
FROM lead_links ll
LEFT JOIN leads l ON l.lead_no = ll.lead_no
WHERE (l.lead_no IS NOT NULL AND l.status = '待复核')
   OR ll.lead_no IS NULL
ORDER BY ll.snapshot_date DESC, ll.amount DESC;
"""


def _create_images(root: Path) -> tuple[Path, Path, Path]:
    image_dir = root / "images"
    image_dir.mkdir()
    image_a = image_dir / "a.png"
    image_b = image_dir / "b.jpg"
    Image.new("RGB", (200, 150), (220, 30, 30)).save(image_a, format="PNG")
    Image.new("RGB", (160, 240), (30, 120, 220)).save(image_b, format="JPEG")
    missing = image_dir / "missing.png"
    return image_a, image_b, missing


def create_fixture_db(root: Path) -> tuple[Path, dict[str, Any]]:
    root.mkdir(parents=True, exist_ok=True)
    image_a, image_b, missing = _create_images(root)
    db_path = root / "data" / "leads.db"
    db_path.parent.mkdir()
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA)
        for row in SUMMARY_ROWS:
            conn.execute(
                """
                INSERT INTO leads VALUES (
                    ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                )
                """,
                (*row, "v1.0", "2026-11-05.1"),
            )
        for row in LINK_ROWS:
            values = list(row)
            values[4] = (
                str(values[4])
                .replace("{image_a}", str(image_a))
                .replace("{image_b}", str(image_b))
                .replace("{missing_image}", str(missing))
            )
            conn.execute(
                """
                INSERT INTO lead_links VALUES (
                    ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                )
                """,
                tuple(values),
            )
        conn.commit()
    finally:
        conn.close()

    config: dict[str, Any] = {
        "config_version": "2026-11-05.1",
        "caliber_version": "v1.0",
        "aggregate": {"shop_threshold": 500000},
        "amount": {"comment_multiplier": 10},
        "export": {
            "dir": str(root / "输出 目录"),
            "filename": "线索_{YYYYMMDD}.xlsx",
        },
    }
    return db_path, config


@pytest.fixture()
def fixture_db(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    return create_fixture_db(tmp_path)


@pytest.fixture()
def exported(fixture_db):
    from lead_excel import export_leads_excel

    db_path, config = fixture_db
    path = export_leads_excel(db_path, "2026-11-05", config)
    return Path(path), db_path, config
