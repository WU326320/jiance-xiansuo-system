"""单表导出浏览验收；只操作隔离的临时数据库。

运行：python utils/export_browser_acceptance.py --keep-open
样表与截图保存到 doc；--keep-open 保留演示服务并打开浏览器。
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen
import webbrowser

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
HEADERS = [
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


def serve(port: int) -> None:
    from datetime import date, datetime
    from decimal import Decimal
    from wsgiref.simple_server import make_server

    from workbench.app import WorkbenchApp
    from workbench.config import WorkbenchConfig
    from workbench import backend_bridge as bridge

    bridge.ensure_backend_paths()
    from pipeline.models import Lead, LeadLink, Product, ProductSnapshot

    with tempfile.TemporaryDirectory(prefix="single-sheet-preview-") as temporary:
        data = Path(temporary)
        os.environ["IP_LEAD_DB_PATH"] = str(data / "leads.db")
        os.environ.pop("IP_LEAD_DB_URL", None)
        config = WorkbenchConfig(
            data_dir=data,
            db_path=data / "workbench.db",
            leads_db_path=data / "leads.db",
            host="127.0.0.1",
            port=port,
            allowed_networks=["127.0.0.1/32"],
            smtp={"enabled": False},
        )
        app = WorkbenchApp(config)
        actor = app.auth.create_supervisor("preview", "Preview1234")
        app.leads.ensure_ready()
        samples = [
            ("演示星河动漫店", "DEMO-A", 780000),
            ("演示星河动漫店", "DEMO-A", 532000),
            ("演示星河动漫店", "DEMO-A", 420000),
            ("演示青禾玩具店", "DEMO-B", 916000),
            ("演示青禾玩具店", "DEMO-B", 656000),
            ("演示小熊文创店", "DEMO-C", 280000),
        ]
        settings = bridge.load_backend_settings(config, db_path=config.leads_db_path)
        # 使用现有本地图片验证嵌图；所有店铺、商品及金额均为明确标注的演示数据。
        images = list((ROOT / "crawler" / "out").glob("*/images/*/*.jpg"))[:6]
        with bridge.leads_session(settings) as session:
            leads = {}
            for index, (shop, shop_id, amount) in enumerate(samples):
                ip = "aoteman" if index == 2 else "huluxiongdi"
                lead = leads.get(shop_id)
                if lead is None:
                    lead = Lead(
                        lead_no=f"EXPORT-DEMO-{shop_id}",
                        run_id="EXPORT-DEMO",
                        snapshot_date=date.today(),
                        platform="taobao",
                        platform_group="taobao",
                        shop_key=f"taobao|{shop}",
                        shop_id=shop_id,
                        shop_name=shop,
                        shop_type="企业",
                        total_amount=Decimal("0"),
                        link_count=0,
                        ip_codes="",
                        confidence=89,
                        hit_summary="演示数据，仅用于格式验收",
                        caliber_version="v1.0",
                        config_version="demo",
                    )
                    session.add(lead)
                    session.flush()
                    leads[shop_id] = lead
                lead.total_amount += Decimal(amount)
                lead.link_count += 1
                lead.ip_codes = "、".join(
                    dict.fromkeys(filter(None, [*lead.ip_codes.split("、"), ip]))
                )
                product = Product(
                    platform="taobao",
                    product_id=f"DEMO-{index + 1:02}",
                    url=f"https://item.example/export-demo-{index + 1:02}",
                    title=f"演示商品 {index + 1:02}（仅用于导出格式验收）",
                    shop_id=shop_id,
                    shop_name=shop,
                    shop_type="企业",
                    image_path=str(images[index % len(images)].resolve())
                    if images
                    else None,
                )
                session.add(product)
                session.flush()
                snapshot = ProductSnapshot(
                    product_id=product.id,
                    snapshot_date=date.today(),
                    crawled_at=datetime.now(),
                    run_id="EXPORT-DEMO",
                    ip_code=ip,
                    price=50,
                    sales=amount // 50,
                    sales_raw=str(amount // 50),
                    comments=4000,
                    comments_raw="4000",
                    estimated_sales=amount // 50,
                    amount=amount,
                    final_score=89,
                    hit_reasons='["演示命中"]',
                    caliber_version="v1.0",
                    config_version="demo",
                )
                session.add(snapshot)
                session.flush()
                session.add(
                    LeadLink(
                        lead_id=lead.id,
                        snapshot_id=snapshot.id,
                        ip_code=ip,
                        amount=amount,
                    )
                )
        export_path = Path(app.leads.export()["path"])
        app.tasks.pipeline_executor = lambda *_: {
            "status": "success",
            "candidates": 3,
            "run_id": "EXPORT-DEMO",
            "platforms": {
                "taobao": {"label": "淘宝/天猫", "items": 6, "leads": 3, "status": "ok"}
            },
            "risks": {"达标": 2, "待核验": 0, "临界": 1},
            "excel_path": str(export_path),
        }
        task = app.tasks.create_task(
            {"ip_name": "葫芦兄弟", "platforms": ["taobao"], "mode": "dry_run"},
            actor=actor,
            run_now=False,
        )
        app.tasks.execute(task["task_no"])
        with make_server(config.host, config.port, app) as server:
            print(f"Preview ready: http://127.0.0.1:{port}", flush=True)
            server.serve_forever()


def validate(path: Path) -> dict:
    import openpyxl

    wb = openpyxl.load_workbook(path)
    try:
        assert wb.sheetnames == ["链接明细"], wb.sheetnames
        ws = wb.active
        assert ws.max_column == 10
        assert [cell.value for cell in ws[1]] == HEADERS
        products = [row for row in ws.iter_rows(min_row=2) if row[5].value]
        subtotals = [
            row
            for row in ws.iter_rows(min_row=2)
            if str(row[3].value).startswith("同IP小计")
        ]
        assert len(products) == 6 and len(subtotals) == 2
        assert sum(row[9].value for row in products) == 3584000
        assert ws.cell(ws.max_row, 4).value == "总计（6条）"
        assert ws.cell(ws.max_row, 10).value == 3584000
        colors = []
        for shop, expected in (
            ("演示星河动漫店", 1312000),
            ("演示青禾玩具店", 1572000),
        ):
            group = [
                row
                for row in products
                if row[2].value == shop and row[1].value == "huluxiongdi"
            ]
            assert len(group) == 2
            color = group[0][0].fill.fgColor.rgb
            assert all(
                cell.fill.fill_type == "solid" and cell.fill.fgColor.rgb == color
                for row in group
                for cell in row
            )
            subtotal = next(row for row in subtotals if row[2].value == shop)
            assert subtotal[9].value == expected
            colors.append(color)
        assert colors[0] != colors[1]
        assert all(
            row[5].hyperlink and row[5].hyperlink.target == row[5].value
            for row in products
        )
        assert ws.freeze_panes == "A2"
        assert ws.auto_filter.ref == f"A1:J{ws.max_row - 1}"
        local_images = list((ROOT / "crawler" / "out").glob("*/images/*/*.jpg"))
        if local_images:
            assert len(ws._images) == 6
            assert all(image.anchor._from.col == 4 for image in ws._images)
        return {
            "sheet": ws.title,
            "headers": HEADERS,
            "products": 6,
            "subtotals": 2,
            "total": 3584000,
            "images": len(ws._images),
        }
    finally:
        wb.close()


def check(keep_open: bool) -> None:
    from playwright.sync_api import sync_playwright, expect

    for folder in (
        "doc",
        "prototype",
        "project/frontend",
        "project/backend",
        "database",
        "utils",
    ):
        assert (ROOT / folder).is_dir(), folder
    port = None
    for candidate in range(2877, 2891):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", candidate))
                port = candidate
                break
            except OSError:
                continue
    if port is None:
        raise RuntimeError("演示端口 2877–2890 均被占用")
    base = f"http://127.0.0.1:{port}"
    with tempfile.TemporaryFile(mode="w+b") as log:
        process = subprocess.Popen(
            [
                str(ROOT / "backend/.venv/Scripts/python.exe"),
                str(Path(__file__).resolve()),
                "--serve",
                "--port",
                str(port),
            ],
            cwd=ROOT,
            stdout=log,
            stderr=log,
        )
        success = False
        try:
            for _ in range(100):
                if process.poll() is not None:
                    log.seek(0)
                    raise RuntimeError(log.read().decode("utf-8", errors="replace"))
                try:
                    with urlopen(base + "/login", timeout=1) as response:
                        if response.status == 200:
                            break
                except OSError:
                    time.sleep(0.2)
            else:
                raise RuntimeError("临时工作台未在限定时间内启动")
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel="msedge", headless=True)
                try:
                    page = browser.new_page(
                        viewport={"width": 1440, "height": 1000}, accept_downloads=True
                    )
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(base + "/login")
                    form = page.locator("#login-form")
                    form.get_by_label("账号", exact=True).fill("preview")
                    form.get_by_label("密码", exact=True).fill("Preview1234")
                    form.locator('button[type="submit"]').click()
                    expect(page.locator("#lead-body [data-open]")).to_have_count(3)
                    with page.expect_response(
                        lambda response: (
                            response.url.endswith("/api/export")
                            and response.request.method == "POST"
                        )
                    ) as generated:
                        page.get_by_role("button", name="导出今日 Excel").click()
                    assert generated.value.status == 200
                    manual_path = Path(generated.value.json()["path"])
                    manual = validate(manual_path)
                    page.locator('[data-nav="tasks"]').click()
                    page.locator("#task-body [data-task]").first.click()
                    with page.expect_download() as downloaded:
                        page.get_by_role(
                            "link", name="下载本次 Excel", exact=False
                        ).click()
                    download = downloaded.value
                    assert download.failure() is None
                    sample = ROOT / "doc" / "单表导出样表.xlsx"
                    download.save_as(sample)
                    task_file = validate(sample)
                    assert manual == task_file
                    page.screenshot(
                        path=str(ROOT / "doc" / "单表导出浏览验收.png"), full_page=True
                    )
                    assert not errors, errors
                    print(
                        json.dumps(
                            {
                                "status": "passed",
                                "manual_export": True,
                                "browser_download": True,
                                "workbook": task_file,
                                "sample": str(sample),
                                "preview_url": base,
                                "preview_pid": process.pid,
                            },
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                    success = True
                finally:
                    browser.close()
            if keep_open:
                webbrowser.open(base + "/#tasks")
        finally:
            if not (success and keep_open):
                process.terminate()
                process.wait(timeout=10)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--port", type=int, default=2877)
    parser.add_argument("--keep-open", action="store_true")
    args = parser.parse_args()
    if args.serve:
        serve(args.port)
    else:
        check(args.keep_open)
