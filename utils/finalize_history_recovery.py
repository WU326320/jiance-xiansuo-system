"""限定85E5历史任务的恢复验收；默认在数据库副本执行，--apply才更新正式库。"""

from pathlib import Path
from contextlib import closing
from datetime import date, datetime
import argparse
import json
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "backend"), str(ROOT / "export")]
RUN = "WB20261109_124522_85E5"
DAY = date(2026, 11, 9)


def backup(source, target):
    with (
        closing(sqlite3.connect(source)) as src,
        closing(sqlite3.connect(target)) as dst,
    ):
        src.backup(dst)


def main(apply=False):
    from sqlalchemy import select
    from openpyxl import load_workbook
    from pipeline.config import load_config
    from pipeline.models import Product, ProductSnapshot, Shop
    from pipeline.scoring import score_snapshot
    from pipeline.aggregate import aggregate_snapshots
    from workbench import backend_bridge as bridge
    from utils.commerce_numbers import count_field, price_value
    from utils.shop_name_recovery import inventory

    for folder in (
        "doc",
        "prototype",
        "project/frontend",
        "project/backend",
        "database",
        "utils",
    ):
        assert (ROOT / folder).is_dir(), folder
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    live = ROOT / "backend/data/leads.db"
    workbench = ROOT / "workbench/data/workbench.db"
    with sqlite3.connect(workbench) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE status IN ('running','queued')"
            ).fetchone()[0]
            == 0
        )
        original = conn.execute(
            "SELECT result_json,excel_path FROM tasks WHERE task_no=?", (RUN,)
        ).fetchone()
        assert original is not None
    saved = ROOT / "database" / f"leads-before-finalize-{stamp}.db"
    backup(live, saved)
    db = live if apply else ROOT / "database" / f"recovery-preview-{stamp}.db"
    if not apply:
        backup(saved, db)
    else:
        backup(workbench, ROOT / "database" / f"workbench-before-finalize-{stamp}.db")
    settings = load_config(
        ROOT / f"workbench/data/runs/{RUN}/run_20261109_124523_奥特曼.yaml"
    )
    settings.raw.setdefault("storage", {})["db_path"] = str(db)
    assert settings.database_path.resolve() == db.resolve()
    _, evidence = inventory(verbose=False)
    changes = []
    stats = {}
    expected = {}
    with bridge.leads_session(settings) as session:
        snapshots = list(
            session.scalars(
                select(ProductSnapshot).where(
                    ProductSnapshot.run_id == RUN, ProductSnapshot.snapshot_date == DAY
                )
            )
        )
        assert len(snapshots) == 275
        for snap in snapshots:
            product = session.get(Product, snap.product_id)
            key = (product.platform, product.product_id)
            before = {
                "shop_name": product.shop_name,
                "amount": str(snap.amount) if snap.amount is not None else None,
            }
            if not str(product.shop_name or "").strip() and key in evidence:
                product.shop_name = evidence[key][0]["name"]
            shop = (
                session.scalar(
                    select(Shop).where(
                        Shop.platform == product.platform,
                        Shop.shop_id == product.shop_id,
                    )
                )
                if product.shop_id
                else None
            )
            if (
                shop is not None
                and not str(shop.shop_name or "").strip()
                and product.shop_name
            ):
                shop.shop_name = product.shop_name
            snap.price = price_value(
                snap.price_raw if snap.price_raw not in (None, "") else snap.price
            )
            snap.sales, _ = count_field(snap.sales, snap.sales_raw, field="sales")
            snap.comments, _ = count_field(
                snap.comments, snap.comments_raw, field="comments"
            )
            score_snapshot(
                session, settings=settings, snapshot=snap, product=product, shop=shop
            )
            group = "taobao" if product.platform == "tmall" else product.platform
            entry = stats.setdefault(
                group,
                {
                    "items": 0,
                    "amount_complete": 0,
                    "amount_missing": 0,
                    "missing_fields": {
                        "price": 0,
                        "sales": 0,
                        "comments": 0,
                        "shop_name": 0,
                    },
                },
            )
            entry["items"] += 1
            entry[
                "amount_complete" if snap.amount is not None else "amount_missing"
            ] += 1
            for field in ("price", "sales", "comments"):
                entry["missing_fields"][field] += int(getattr(snap, field) is None)
            entry["missing_fields"]["shop_name"] += int(
                not str(product.shop_name or "").strip()
            )
            expected[product.url] = (
                product.shop_name or None,
                float(snap.amount) if snap.amount is not None else None,
            )
            changes.append(
                {
                    "platform": product.platform,
                    "product_id": product.product_id,
                    "before": before,
                    "shop_name": product.shop_name,
                    "amount": str(snap.amount) if snap.amount is not None else None,
                    "formula": snap.formula,
                    "evidence": evidence.get(key, []),
                }
            )
        leads = aggregate_snapshots(
            session,
            settings=settings,
            snapshots=snapshots,
            snapshot_date=DAY,
            run_id=RUN,
        )
        lead_count = len(leads)
        from workbench.leads import tier_for

        risks = {"达标": 0, "待核验": 0, "临界": 0}
        for lead in leads:
            risks[
                tier_for(lead, float(settings.get("aggregate.shop_threshold", 500000)))
            ] += 1
        for group, entry in stats.items():
            entry["leads"] = sum(lead.platform_group == group for lead in leads)
    with sqlite3.connect(db) as conn:
        visible = {
            row[0]
            for row in conn.execute(
                "SELECT url FROM v_lead_links WHERE run_id=? AND snapshot_date=?",
                (RUN, DAY.isoformat()),
            )
        }
    expected = {url: values for url, values in expected.items() if url in visible}
    filename = f"历史恢复_{RUN}_{stamp}.xlsx"
    settings.raw["export"] = {
        **settings.raw.get("export", {}),
        "run_id": RUN,
        "filename": filename,
    }
    excel = bridge.export_leads(
        settings, DAY.isoformat(), output_dir=ROOT / "workbench/data/exports"
    )
    book = load_workbook(excel, read_only=True)
    try:
        assert book.active.max_column == 10
        rows = [
            r
            for r in book.active.iter_rows(min_row=2, values_only=True)
            if r[5] in expected
        ]
        assert len(rows) == len(expected) > 0, (len(rows), len(expected))
        assert {row[5] for row in rows} == set(expected)
        for row in rows:
            assert (row[2] or None, row[9]) == expected[row[5]], (
                row[5],
                row[2],
                row[9],
                expected[row[5]],
            )
    finally:
        book.close()
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()
    result = json.loads(original[0])
    result["historical_recovery"] = {
        "at": stamp,
        "original_excel": original[1],
        "note": "使用已保存采集证据恢复店名并按当前口径重算；非新采集任务",
    }
    result["status"] = "partial"
    result["candidates"] = lead_count
    result["risks"] = risks
    result["excel_path"] = str(excel)
    for group, entry in stats.items():
        result.setdefault("platforms", {}).setdefault(group, {}).update(entry)
    result["errors"] = [
        "历史数据已恢复重算；原任务截止及采集警告保留，未补齐字段仍待核验"
    ]
    if apply:
        with sqlite3.connect(workbench) as conn:
            conn.execute(
                "UPDATE tasks SET status='partial',result_json=?,excel_path=? WHERE task_no=?",
                (json.dumps(result, ensure_ascii=False), str(excel), RUN),
            )
            conn.execute(
                "INSERT INTO audit_log(actor_name,source_ip,action,object_type,object_id,before_json,after_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    "本地恢复工具",
                    "127.0.0.1",
                    "task.history_recovery",
                    "task",
                    RUN,
                    original[0],
                    json.dumps(result, ensure_ascii=False),
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
    report = {
        "applied": apply,
        "backup": str(saved),
        "database": str(db),
        "excel": str(excel),
        "excel_rows_verified": len(rows),
        "leads": lead_count,
        "platforms": stats,
        "changes": changes,
    }
    report_path = ROOT / "doc" / f"历史恢复验收-{stamp}.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {k: v for k, v in report.items() if k != "changes"},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    main(parser.parse_args().apply)
