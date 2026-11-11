"""金额口径核对：历史样本只读报告、隔离工作台及 Excel 浏览验收。"""

from __future__ import annotations

import argparse
import csv
from datetime import date
from decimal import Decimal
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
from wsgiref.simple_server import make_server, WSGIRequestHandler

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "backend"), str(ROOT / "export")]
DOC = ROOT / "doc"


def audit_samples():
    from pipeline.config import load_config
    from pipeline.scoring import compute_amount
    from utils.commerce_numbers import count_field, count_value, normalized, price_value

    settings = load_config(ROOT / "backend/config/rules.yaml")
    sources = [
        "6150da34d46a496794474cee6918ba25/2026-11-09/taobao.jsonl",
        "912ef0247bc8482993f31eda8371d5d7/2026-11-09/jd.jsonl",
        "f539dd90f5994778934a30690956f8f3/2026-11-08/1688.jsonl",
    ]
    rows, summary = [], {}
    for source in sources:
        path = ROOT / "crawler/out/runs" / source
        stats = {"rows": 0, "known": 0, "pending": 0, "lower_bound": 0}
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            sales, sales_issue = count_field(
                item.get("sales"), item.get("sales_raw"), field="sales"
            )
            comments, comments_issue = count_field(
                item.get("comments"), item.get("comments_raw"), field="comments"
            )
            raw_price = item.get("price_raw")
            price = price_value(
                raw_price if raw_price not in (None, "") else item.get("price")
            )
            result = compute_amount(
                settings=settings,
                existing_flags=item.get("flags", []),
                price=price,
                sales=sales,
                comments=comments,
                sales_raw=item.get("sales_raw"),
                comments_raw=item.get("comments_raw"),
            )
            lower = any(
                count_value(item.get(field)) is not None
                and any(
                    marker in normalized(item.get(field)) for marker in ("+", "以上")
                )
                for field in ("sales_raw", "comments_raw")
            )
            rows.append(
                {
                    "平台": item["platform"],
                    "商品ID": item["product_id"],
                    "价格原文": raw_price,
                    "原数值价格": item.get("price"),
                    "采用价格P": price,
                    "销量原文": item.get("sales_raw"),
                    "原数值销量": item.get("sales"),
                    "销量S": sales,
                    "评论原文": item.get("comments_raw"),
                    "原数值评论": item.get("comments"),
                    "评论C": comments,
                    "采用数量": result.estimated_sales,
                    "金额": result.amount,
                    "计算说明": result.formula,
                    "待核验原因": "；".join(
                        filter(None, [sales_issue, comments_issue, *result.flags])
                    ),
                    "按下限估算": lower,
                }
            )
            stats["rows"] += 1
            stats["known" if result.amount is not None else "pending"] += 1
            stats["lower_bound"] += int(lower)
        summary[path.stem] = stats
    with (DOC / "金额规则_历史样本核对.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return summary


def live_inventory():
    result = {}
    for name, path, queries in (
        (
            "leads",
            ROOT / "backend/data/leads.db",
            {
                "revision": "SELECT version_num FROM alembic_version",
                "products": "SELECT COUNT(*) FROM products",
                "snapshots": "SELECT COUNT(*) FROM product_snapshots",
                "leads": "SELECT COUNT(*) FROM leads",
                "links": "SELECT COUNT(*) FROM lead_links",
                "snapshot_amount_total": "SELECT SUM(amount) FROM product_snapshots",
                "link_amount_total": "SELECT SUM(amount) FROM lead_links",
                "integrity": "PRAGMA integrity_check",
                "foreign_keys": "PRAGMA foreign_key_check",
                "columns": "PRAGMA table_info(product_snapshots)",
            },
        ),
        (
            "workbench",
            ROOT / "workbench/data/workbench.db",
            {
                "active_tasks": "SELECT COUNT(*) FROM tasks WHERE status IN ('running','queued')",
            },
        ),
    ):
        if not path.exists():
            result[name] = {"exists": False}
            continue
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as conn:
            result[name] = {}
            for key, query in queries.items():
                try:
                    result[name][key] = conn.execute(query).fetchall()
                except sqlite3.Error as error:
                    result[name][key] = str(error)
    return result


def browser_check():
    import importlib.util
    import site

    if importlib.util.find_spec("playwright") is None:
        site.addsitedir(str(ROOT / "crawler/.venv/Lib/site-packages"))
    from playwright.sync_api import sync_playwright, expect
    from openpyxl import load_workbook
    from workbench.app import WorkbenchApp
    from workbench.config import WorkbenchConfig
    from workbench import backend_bridge as bridge
    from workbench.tests.conftest import make_lead
    from pipeline.models import Lead, LeadLink, Product, ProductSnapshot
    from sqlalchemy import select

    class QuietHandler(WSGIRequestHandler):
        def log_message(self, format, *args):
            pass

    with tempfile.TemporaryDirectory(prefix="amount-acceptance-") as temporary:
        data = Path(temporary)
        os.environ["IP_LEAD_DB_PATH"] = str(data / "leads.db")
        os.environ.pop("IP_LEAD_DB_URL", None)
        config = WorkbenchConfig(
            data_dir=data,
            db_path=data / "workbench.db",
            leads_db_path=data / "leads.db",
            host="127.0.0.1",
            allowed_networks=["127.0.0.1/32"],
            smtp={"enabled": False},
        )
        app = WorkbenchApp(config)
        app.auth.create_supervisor("amount-preview", "AmountPreview1234")
        app.leads.ensure_ready()
        make_lead(
            config, lead_no="AMOUNT-DEMO", shop_name="金额口径验收店", amount=600000
        )
        settings = bridge.load_backend_settings(config, db_path=config.leads_db_path)
        with bridge.leads_session(settings) as session:
            lead = session.scalar(select(Lead))
            assert lead is not None
            lead.snapshot_date = date.today()
            lead.link_count = 3
            first_snapshot = session.scalar(select(ProductSnapshot))
            first_link = session.scalar(select(LeadLink))
            assert first_snapshot is not None and first_link is not None
            first_snapshot.snapshot_date = date.today()
            first_snapshot.price = 60
            first_snapshot.price_raw = "60.00"
            first_snapshot.formula = "60.00×min(10000,1200×10)=600000.00"
            first_link.formula = first_snapshot.formula
            for label, amount, formula in (
                ("评论缺失商品", None, "待核验：评论缺失"),
                ("真实零价商品", Decimal("0.00"), "0.00×min(10000,1000×10)=0.00"),
            ):
                product = Product(
                    platform="taobao",
                    product_id=label,
                    url="https://example.com/amount-check",
                    title=label,
                    shop_name=lead.shop_name,
                    shop_type="企业",
                )
                session.add(product)
                session.flush()
                snapshot = ProductSnapshot(
                    product_id=product.id,
                    snapshot_date=lead.snapshot_date,
                    crawled_at=lead.created_at,
                    run_id=lead.run_id,
                    ip_code="huluxiongdi",
                    price=0 if amount is not None else 10,
                    sales=10000,
                    sales_raw="1万+",
                    comments=1000 if amount is not None else None,
                    comments_raw="1000" if amount is not None else None,
                    amount=amount,
                    estimated_sales=10000 if amount is not None else None,
                    formula=formula,
                    missing_flags='["评论缺失"]' if amount is None else "[]",
                    caliber_version="v1.0",
                    config_version="test",
                )
                session.add(snapshot)
                session.flush()
                session.add(
                    LeadLink(
                        lead_id=lead.id,
                        snapshot_id=snapshot.id,
                        ip_code="huluxiongdi",
                        amount=amount,
                        formula=formula,
                    )
                )
        server = make_server("127.0.0.1", 0, app, handler_class=QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel="msedge", headless=True)
                try:
                    page = browser.new_page(viewport={"width": 1440, "height": 1000})
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(f"http://127.0.0.1:{server.server_port}/login")
                    page.locator('#login-form input[name="username"]').fill(
                        "amount-preview"
                    )
                    page.locator('#login-form input[name="password"]').fill(
                        "AmountPreview1234"
                    )
                    page.locator('#login-form button[type="submit"]').click()
                    page.locator('[data-nav="leads"]').click()
                    page.locator('[data-open="AMOUNT-DEMO"]').click()
                    expect(page.locator(".link-item")).to_have_count(3)
                    expect(
                        page.locator(".link-item").filter(has_text="评论缺失商品")
                    ).to_contain_text("待核验")
                    expect(
                        page.locator(".link-item").filter(has_text="真实零价商品")
                    ).to_contain_text("¥0.00")
                    response = page.request.get(
                        f"http://127.0.0.1:{server.server_port}/api/leads/AMOUNT-DEMO"
                    )
                    assert response.status == 200
                    links = {item["title"]: item for item in response.json()["links"]}
                    assert links["评论缺失商品"]["amount"] is None
                    assert links["真实零价商品"]["amount"] == 0
                    page.screenshot(
                        path=str(DOC / "金额规则_浏览验收.png"), full_page=True
                    )
                    page.get_by_role("button", name="关闭", exact=True).click()
                    with page.expect_response(
                        lambda response: response.url.endswith("/api/export")
                    ) as generated:
                        page.get_by_role("button", name="导出今日 Excel").click()
                    assert generated.value.status == 200
                    workbook = load_workbook(generated.value.json()["path"])
                    try:
                        sheet = workbook.active
                        assert sheet is not None
                        assert sheet.max_column == 10
                        cells = {
                            sheet.cell(row, 4).value: sheet.cell(row, 10)
                            for row in range(2, sheet.max_row + 1)
                        }
                        assert cells["评论缺失商品"].value is None
                        assert cells["评论缺失商品"].comment is not None
                        assert "评论缺失" in cells["评论缺失商品"].comment.text
                        assert cells["真实零价商品"].value == 0
                        assert sheet.cell(sheet.max_row, 10).value == 600000
                        workbook.save(DOC / "金额规则_验收样表.xlsx")
                    finally:
                        workbook.close()
                    assert not errors, errors
                    return {
                        "page": "passed",
                        "api_null_zero": "passed",
                        "excel_ten_columns": "passed",
                        "console_errors": errors,
                    }
                finally:
                    browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-inventory", action="store_true")
    args = parser.parse_args()
    for folder in (
        "doc",
        "prototype",
        "project/frontend",
        "project/backend",
        "database",
        "utils",
    ):
        assert (ROOT / folder).is_dir(), folder
    if args.live_inventory:
        print(json.dumps(live_inventory(), ensure_ascii=False, indent=2))
        return
    report = {
        "historical_samples_read_only": audit_samples(),
        "browser": browser_check(),
    }
    (DOC / "金额规则_验收结果.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
