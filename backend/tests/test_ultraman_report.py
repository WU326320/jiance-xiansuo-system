"""11/6 第二轮小修：奥特曼报告拆“本轮采集/含历史数据”双口径。"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_REPORT_PATH = ROOT / "wu_tools" / "ultraman_report.py"


def _load_report():
    spec = importlib.util.spec_from_file_location("wu_ultraman_report", _REPORT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE products (
                id INTEGER PRIMARY KEY,
                platform TEXT,
                product_id TEXT,
                title TEXT
            );
            CREATE TABLE product_snapshots (
                id INTEGER PRIMARY KEY,
                product_id INTEGER,
                snapshot_date TEXT,
                text_score REAL,
                final_score REAL,
                need_vision INTEGER,
                eligible INTEGER,
                exclusion_reason TEXT,
                hit_reasons TEXT
            );
            """
        )
        products = [
            (1, "1688", "1", "奥特曼 玩具 1"),
            (2, "1688", "2", "奥特曼 玩具 2"),
            (3, "jd", "3", "奥特曼 玩具 3"),
            (4, "taobao", "4", "奥特曼 玩具 4"),
            (5, "1688", "5", "叶罗丽 玩具 5"),
        ]
        connection.executemany(
            "INSERT INTO products (id, platform, product_id, title) VALUES (?, ?, ?, ?)",
            products,
        )
        snapshots = [
            # 本轮采集：3 条，2 条候选。
            (1, 1, "2026-11-06", 61, 61, 0, 1, None, "ip_hit:奥特曼"),
            (2, 2, "2026-11-06", 45, 45, 0, 1, None, "ip_hit:奥特曼"),
            (3, 3, "2026-11-06", 10, 10, 0, 0, "低分", "ip_hit:奥特曼"),
            # 历史数据：2 条，1 条候选。
            (4, 4, "2026-11-05", 70, 70, 0, 1, None, "ip_hit:奥特曼"),
            (5, 5, "2026-11-05", 20, 20, 0, 0, "低分", "ip_hit:叶罗丽"),
        ]
        connection.executemany(
            """
            INSERT INTO product_snapshots
                (id, product_id, snapshot_date, text_score, final_score,
                 need_vision, eligible, exclusion_reason, hit_reasons)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            snapshots,
        )
        connection.commit()
    finally:
        connection.close()


def test_report_splits_current_and_history_calibers(tmp_path):
    report_module = _load_report()
    db = tmp_path / "leads.db"
    _make_db(db)

    rows = report_module.load_rows(db)
    report = report_module.build_report(db, rows, sample_limit=10, run_date="2026-11-06")

    assert report["current"]["total"] == 3
    assert report["current"]["candidates"] == 2
    assert report["all"]["total"] == 5
    assert report["all"]["candidates"] == 3
    assert report["history_dates"] == ["2026-11-05"]
    assert report["history_label"] == "含历史数据（2026-11-05）"
    # 顶层字段保持旧消费者兼容，默认取含历史口径。
    assert report["total"] == 5
    assert report["candidates"] == 3

    text = report_module.render_markdown(report)
    assert "本轮采集（2026-11-06）" in text
    assert "含历史数据（2026-11-05）" in text
    assert "| 本轮采集（2026-11-06） | 3 | 2 |" in text
    assert "| 含历史数据（2026-11-05） | 5 | 3 |" in text


def test_report_without_run_date_uses_latest_snapshot_date(tmp_path):
    report_module = _load_report()
    db = tmp_path / "leads.db"
    _make_db(db)

    report = report_module.build_report(db, report_module.load_rows(db), sample_limit=5)
    assert report["run_date"] == "2026-11-06"
    assert report["current"]["total"] == 3
    assert report["current"]["candidates"] == 2
