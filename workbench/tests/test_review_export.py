from __future__ import annotations

from pathlib import Path

import pytest

from openpyxl import Workbook, load_workbook

from workbench import backend_bridge as bridge
from workbench.leads import LeadsService
from workbench.tests.conftest import make_lead


def _review_via_api(client, lead_nos, result, reviewer, confirm_text=""):
    status, body, _ = client.request(
        "POST",
        "/api/leads/review",
        json_body={
            "lead_nos": lead_nos,
            "result": result,
            "reviewer": reviewer,
            "confirm_text": confirm_text,
        },
    )
    return status, body


def _lead_row(config, lead_no):
    from sqlalchemy import select
    from pipeline.models import Lead

    settings = bridge.load_backend_settings(config, db_path=config.leads_db_path)
    with bridge.leads_session(settings) as session:
        lead = session.scalar(select(Lead).where(Lead.lead_no == lead_no))
        return {
            "status": lead.status,
            "review_result": lead.review_result,
            "reviewer": lead.reviewer,
            "reviewed_at": lead.reviewed_at,
            "saas": lead.saas_entered_at,
        }


def _write_review_excel(path: Path, lead_no: str, result: str, reviewer: str):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "线索汇总"
    sheet.append(["线索编号", "复核结论", "复核人"])
    sheet.append([lead_no, result, reviewer])
    workbook.save(path)


def test_page_review_and_excel_import_write_same_fields(
    client, tmp_config, normal_user
):
    make_lead(tmp_config, lead_no="taobao-20261106-0001")
    assert client.login("user1", "User1234")[0] == 200
    status, body = _review_via_api(client, ["taobao-20261106-0001"], "已确认", "张三")
    assert status == 200 and body["updated"] == ["taobao-20261106-0001"]
    row = _lead_row(tmp_config, "taobao-20261106-0001")
    assert (row["status"], row["review_result"], row["reviewer"]) == (
        "已确认",
        "已确认",
        "张三",
    )

    # 同结论 Excel 回写：不变
    excel = tmp_config.data_dir / "review.xlsx"
    _write_review_excel(excel, "taobao-20261106-0001", "已确认", "张三")
    settings = bridge.load_backend_settings(
        tmp_config, db_path=tmp_config.leads_db_path
    )
    with bridge.leads_session(settings) as session:
        stats = bridge.import_review_excel(session, excel)
    assert stats.unchanged == 1 and stats.updated == 0

    # 页面改成驳回后，Excel 再回写已确认（时间更晚）-> 以 Excel 为准
    assert _review_via_api(client, ["taobao-20261106-0001"], "驳回", "李四")[0] == 200
    _write_review_excel(excel, "taobao-20261106-0001", "已确认", "张三")
    with bridge.leads_session(settings) as session:
        stats = bridge.import_review_excel(session, excel)
    assert stats.updated == 1
    row = _lead_row(tmp_config, "taobao-20261106-0001")
    assert (row["status"], row["reviewer"]) == ("已确认", "张三")
    rows = client.app.db.fetchall("SELECT * FROM audit_log WHERE action='lead.review'")
    assert len(rows) == 2


def test_batch_reject_requires_manual_confirm(client, tmp_config, normal_user):
    make_lead(tmp_config, lead_no="L-1")
    make_lead(tmp_config, lead_no="L-2", shop_name="第二家店")
    assert client.login("user1", "User1234")[0] == 200
    status, body = _review_via_api(client, ["L-1", "L-2"], "驳回", "李四")
    assert status == 400
    status, body = _review_via_api(client, ["L-1", "L-2"], "驳回", "李四", "确认删除")
    assert status == 200 and len(body["updated"]) == 2
    assert _lead_row(tmp_config, "L-1")["status"] == "驳回"


def test_saas_review_is_skipped(client, tmp_config, normal_user):
    make_lead(tmp_config, lead_no="SAAS-1", status="已录入SaaS")
    assert client.login("user1", "User1234")[0] == 200
    status, body = _review_via_api(client, ["SAAS-1"], "已确认", "张三")
    assert status == 200
    assert body["updated"] == [] and body["skipped"]
    assert _lead_row(tmp_config, "SAAS-1")["status"] == "已录入SaaS"


def test_export_single_sheet_ten_columns_and_total(client, tmp_config, normal_user):
    make_lead(tmp_config, lead_no="EXPORT-1")
    assert client.login("user1", "User1234")[0] == 200
    status, body, _ = client.request(
        "POST", "/api/export", json_body={"snapshot_date": "2026-11-06"}
    )
    assert status == 200
    path = Path(body["path"])
    assert path.exists()
    workbook = load_workbook(path, read_only=True)
    try:
        assert workbook.sheetnames == ["链接明细"]
        sheet = workbook["链接明细"]
        headers = [cell.value for cell in sheet[1]]
        assert headers == [
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
        values = [row for row in sheet.iter_rows(min_row=2, values_only=True)]
        assert values
        assert values[0][0] == "taobao"
        assert values[0][2] == "测试旗舰店"
        assert values[0][9] == values[-1][9] == 600000
        assert values[-1][3] == "总计（1条）"
    finally:
        workbook.close()


def test_leads_list_filters_and_detail(client, tmp_config, normal_user):
    make_lead(tmp_config, lead_no="F-1", amount=700000, shop_name="达标店")
    make_lead(tmp_config, lead_no="F-2", amount=400000, shop_name="临界店")
    assert client.login("user1", "User1234")[0] == 200
    status, body, _ = client.request("GET", "/api/leads?status=all&tier=达标")
    assert status == 200
    assert {item["lead_no"] for item in body["items"]} == {"F-1"}
    status, body, _ = client.request("GET", "/api/leads?status=all&tier=临界")
    assert {item["lead_no"] for item in body["items"]} == {"F-2"}
    status, body, _ = client.request("GET", "/api/leads/F-1")
    assert status == 200
    assert body["lead_no"] == "F-1" and body["links"]
    assert "50.00" in body["links"][0]["formula"]


@pytest.mark.parametrize("amount", [None, 0])
def test_detail_api_preserves_unknown_amount_and_real_zero(
    client, tmp_config, normal_user, amount
):
    from sqlalchemy import update
    from pipeline.models import LeadLink, ProductSnapshot

    make_lead(tmp_config, lead_no="AMOUNT-1")
    settings = bridge.load_backend_settings(
        tmp_config, db_path=tmp_config.leads_db_path
    )
    with bridge.leads_session(settings) as session:
        session.execute(update(LeadLink).values(amount=amount))
        session.execute(
            update(ProductSnapshot).values(
                amount=amount, estimated_sales=None if amount is None else 0
            )
        )
    assert client.login("user1", "User1234")[0] == 200
    status, body, _ = client.request("GET", "/api/leads/AMOUNT-1")
    assert status == 200
    if amount is None:
        assert body["links"][0]["amount"] is None
        assert body["links"][0]["estimated_sales"] is None
    else:
        assert body["links"][0]["amount"] == 0
        assert body["links"][0]["estimated_sales"] == 0


def test_amount_filters_boundaries_stats_and_pagination(
    client, tmp_config, normal_user
):
    amounts = [
        0,
        49999.99,
        50000,
        99999.99,
        100000,
        199999.99,
        200000,
        399999.99,
        400000,
        499999.99,
        500000,
        500000.01,
    ]
    for index, amount in enumerate(amounts):
        make_lead(tmp_config, lead_no=f"BOUND-{index}", amount=amount)
    assert client.login("user1", "User1234")[0] == 200
    for threshold in (0, 50000, 100000, 200000, 400000, 500000):
        expected = sorted(
            (value for value in amounts if value >= threshold), reverse=True
        )
        status, body, _ = client.request(
            "GET", f"/api/leads?min_amount={threshold}&sort=amount_desc&page_size=2"
        )
        assert status == 200
        assert body["total"] == body["stats"]["total"] == len(expected)
        assert [row["total_amount"] for row in body["items"]] == expected[:2]
        status, full, _ = client.request(
            "GET", f"/api/leads?min_amount={threshold}&sort=amount_desc&page_size=100"
        )
        assert status == 200
        assert [row["total_amount"] for row in full["items"]] == expected
    for invalid in ("-1", "NaN", "Infinity", "50001", "1e5", "abc"):
        assert client.request("GET", f"/api/leads?min_amount={invalid}")[0] == 400
    status, body, _ = client.request("GET", "/api/leads?min_amount=500000&q=missing")
    assert status == 200 and body["total"] == 0


@pytest.mark.parametrize("snapshot_stamp", ["151034_735886", "163538_689418"])
def test_real_taobao_snapshot_to_jsonl_database_api_excel_and_browser(
    client, tmp_config, normal_user, tmp_path, monkeypatch, snapshot_stamp
):
    """真实HTML在隔离库回放；测试门槛降低以便核对导出，不修改业务库。"""
    import json
    from datetime import date, datetime
    from decimal import Decimal
    from threading import Thread
    from wsgiref.simple_server import make_server

    root = Path(__file__).resolve().parents[2]
    evidence = (
        root
        / f"crawler/logs/debug/detail_taobao_1085008955004_20261109_{snapshot_stamp}.html"
    )
    if not evidence.is_file():
        pytest.skip("私有真实快照未分发；常规回归由平台解析测试覆盖")
    monkeypatch.syspath_prepend(str(root / "crawler"))
    from crawler.config import AppConfig
    from crawler.core.browser import PageResult, StaticBrowser
    from crawler.core.jsonl import JsonlWriter, read_jsonl
    from crawler.core.schema import apply_patch, enrich_flags
    from crawler.models import Item
    from crawler.platforms import create_platform
    from pipeline.aggregate import aggregate_snapshots
    from pipeline.models import Product, Shop
    from pipeline.scoring import score_snapshot
    from pipeline.store import upsert_many
    from sqlalchemy import select, text
    from playwright.sync_api import sync_playwright, expect

    config = AppConfig()
    config.crawl.log_dir = str(tmp_path / "crawler-logs")
    config.crawl.state_dir = str(tmp_path / "crawler-state")
    platform = create_platform("taobao", config, StaticBrowser([]))
    pid = "1085008955004"
    url = f"https://item.taobao.com/item.htm?id={pid}"
    response_url = (
        url
        if snapshot_stamp == "151034_735886"
        else f"https://detail.tmall.com/item.htm?id={pid}"
    )
    patch = platform.parse_detail(
        PageResult(url=response_url, html=evidence.read_text(encoding="utf-8")), pid
    )
    assert patch is not None
    assert (patch.price, patch.sales, patch.comments) == (Decimal("49.00"), 1000, 46)
    assert patch.shop_name
    # 同一快照输入的消融对照，不冒充原始历史记录：只移除未修复时缺失的评论。
    before = enrich_flags(
        Item(
            platform="taobao",
            product_id=pid,
            url=url,
            title=patch.title,
            shop_id=patch.shop_id,
            shop_name=patch.shop_name,
            price=patch.price,
            price_raw=patch.price_raw,
            sales=patch.sales,
            sales_raw=patch.sales_raw,
            comments=None,
            comments_raw=None,
            search_keyword="罗小黑",
            ip_code="luoxiaohei",
            crawled_at=datetime.now().astimezone(),
            listed=True,
        )
    )
    if snapshot_stamp == "163538_689418":
        source = (
            root
            / "crawler/out/runs/f55df686d190416ca89e1525172fb51c/2026-11-09/taobao.jsonl"
        )
        before = Item.model_validate(
            next(row for row in read_jsonl(source) if row["product_id"] == pid)
        )
        assert before.sales is None and before.comments is None
    after = apply_patch(before, patch)
    assert (after.sales, after.comments) == (1000, 46)
    settings = bridge.load_backend_settings(
        tmp_config, db_path=tmp_config.leads_db_path
    )
    settings.raw.setdefault("aggregate", {})["shop_threshold"] = 10000
    settings.raw.setdefault("combined", {})["min_final_score"] = 0
    assert client.login("user1", "User1234")[0] == 200
    comparisons = []
    for label, item, expected in (
        ("before", before, Decimal("49000.00") if before.sales is not None else None),
        ("after", after, Decimal("49000.00")),
    ):
        jsonl = tmp_path / f"{label}.jsonl"
        writer = JsonlWriter(jsonl)
        try:
            assert writer.write(item)
        finally:
            writer.close()
        rows = read_jsonl(jsonl)
        with bridge.leads_session(settings) as session:
            snapshots, dropped = upsert_many(
                session,
                platform="taobao",
                items=rows,
                settings=settings,
                run_id=label,
                snapshot_date=date.today(),
            )
            assert not dropped and len(snapshots) == 1
            snapshot = snapshots[0]
            product = session.get(Product, snapshot.product_id)
            if label == "after":
                from pipeline.taobao_detail import apply_taobao_detail_patches

                product.shop_name = None
                session.flush()
                assert (
                    apply_taobao_detail_patches(
                        session,
                        settings=settings,
                        patches=[
                            {
                                "platform": "taobao",
                                "product_id": pid,
                                "shop_name": patch.shop_name,
                            }
                        ],
                        snapshot_date=date.today(),
                        run_id=label,
                    )
                    == 1
                )
                session.refresh(product)
                assert product.shop_name == patch.shop_name
            shop = session.scalar(
                select(Shop).where(
                    Shop.platform == product.platform, Shop.shop_id == product.shop_id
                )
            )
            score_snapshot(
                session,
                settings=settings,
                snapshot=snapshot,
                product=product,
                shop=shop,
            )
            assert snapshot.amount == expected
            assert snapshot.eligible
            aggregate_snapshots(
                session,
                settings=settings,
                snapshots=snapshots,
                snapshot_date=date.today(),
                run_id=label,
            )
            row = (
                session.execute(
                    text(
                        "SELECT amount, amount_formula, missing_flags FROM v_lead_links WHERE product_id=:pid AND run_id=:run"
                    ),
                    {"pid": pid, "run": label},
                )
                .mappings()
                .one()
            )
            assert row["amount"] == (None if expected is None else float(expected))
            if expected is None:
                assert "待核验" in row["amount_formula"]
            else:
                assert "按下限估算" in row["missing_flags"]
            comparisons.append(
                {
                    "phase": label,
                    "amount": row["amount"],
                    "formula": row["amount_formula"],
                }
            )
        status, response, _ = client.request(
            "POST",
            "/api/export",
            json_body={"snapshot_date": date.today().isoformat()},
        )
        assert status == 200
        workbook = load_workbook(response["path"])
        try:
            matches = [
                r for r in workbook.active.iter_rows(min_row=2) if r[5].value == url
            ]
            # 日导出保留全部批次；用评论原文区分消融组与修复组，不依赖行顺序。
            matches = [
                r
                for r in matches
                if str(r[8].value or "") == str(item.comments_raw or "")
            ]
            assert len(matches) == 1
            cell = matches[0][9]
            assert matches[0][2].value == patch.shop_name
            assert cell.value == (None if expected is None else float(expected))
            if expected is None:
                assert cell.comment and "待核验" in cell.comment.text
        finally:
            workbook.close()

    # 仅任务调度使用桩；报表来自上面实际计算与导出的隔离库。
    monkeypatch.setattr(
        client.app.tasks,
        "pipeline_executor",
        lambda *_: {
            "status": "success",
            "candidates": 1,
            "run_id": "after",
            "excel_path": response["path"],
            "platforms": {
                "taobao": {"label": "淘宝/天猫", "items": 1, "leads": 1, "status": "ok"}
            },
            "risks": {"达标": 0, "待核验": 0, "临界": 1},
        },
    )
    task = client.app.tasks.create_task(
        {"ip_name": "罗小黑", "platforms": ["taobao"], "mode": "dry_run"},
        actor=normal_user,
        run_now=False,
    )
    client.app.tasks.execute(task["task_no"])
    # 浏览器只连接本机隔离工作台，不访问真实平台。
    server = make_server("127.0.0.1", 0, client.app)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            try:
                page = browser.new_page(accept_downloads=True)
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{server.server_port}/login")
                form = page.locator("#login-form")
                form.get_by_label("账号", exact=True).fill("user1")
                form.get_by_label("密码", exact=True).fill("User1234")
                form.locator('button[type="submit"]').click()
                export_button = page.get_by_role("button", name="导出今日 Excel")
                expect(export_button).to_be_visible()
                with page.expect_response(
                    lambda res: (
                        res.url.endswith("/api/export") and res.request.method == "POST"
                    )
                ) as exported:
                    export_button.click()
                assert exported.value.status == 200
                page.locator('[data-nav="leads"]').click()
                expect(page.locator("#lead-body")).to_contain_text(patch.shop_name)
                page.locator('[data-nav="tasks"]').click()
                page.locator("#task-body [data-task]").first.click()
                with page.expect_download() as downloaded:
                    page.get_by_role("link", name="下载本次 Excel", exact=False).click()
                download = downloaded.value
                assert download.failure() is None
                target = tmp_path / "browser-amount.xlsx"
                download.save_as(target)
                workbook = load_workbook(target, read_only=True)
                try:
                    amounts = [
                        r[9]
                        for r in workbook.active.iter_rows(min_row=2, values_only=True)
                        if r[5] == url
                    ]
                    assert 49000 in amounts
                    named_rows = [
                        r
                        for r in workbook.active.iter_rows(min_row=2, values_only=True)
                        if r[5] == url
                    ]
                    assert all(r[2] == patch.shop_name for r in named_rows)
                finally:
                    workbook.close()
                page.screenshot(
                    path=str(tmp_path / "amount-browser.png"), full_page=True
                )
                assert not errors, errors
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    print(
        json.dumps(
            {
                "product_id": pid,
                "comparison": comparisons,
                "browser_download": "passed",
                "artifacts": str(tmp_path),
            },
            ensure_ascii=False,
        )
    )
