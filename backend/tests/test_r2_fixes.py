"""R2 终审返工验收：C4、白名单、tmall、报警、近门槛与健壮性。"""

from __future__ import annotations

import json
import subprocess
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from openpyxl import Workbook
from sqlalchemy import select, text

from pipeline.aggregate import aggregate_snapshots
from pipeline.config import load_config
from pipeline.db import create_engine_for, make_session_factory, session_scope
from pipeline.models import Lead, Product, ProductSnapshot, RunLog, Shop, Whitelist
from pipeline.notify import collect_alert_lines
from pipeline.review import import_review_excel
from pipeline.runner import PipelineRunner
from pipeline.store import upsert_many
from pipeline.whitelist import sync_whitelist

from tests.conftest import FakeAdapter, make_item, platform_data

DAY = date(2026, 11, 5)


# ---------------------------------------------------------------------------
# P1-1：真实 CrawlerJdDetailFetcher 必须解析 RunRecord 并在失败时抛异常
# ---------------------------------------------------------------------------
def _completed(returncode: int, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


def _jd_target():
    from pipeline.jd_detail import JdDetailTarget

    return JdDetailTarget(
        shop_id="JD-FAIL-S",
        total_amount=Decimal("400000.00"),
        product_ids=("JD-FAIL-1",),
    )


def test_crawler_jd_detail_fetcher_raises_on_nonzero_exit(settings, monkeypatch):
    from pipeline.jd_detail import CrawlerJdDetailFetcher

    settings.crawler_dir.mkdir(parents=True, exist_ok=True)
    output = settings.crawler_output_dir / DAY.isoformat() / "jd_detail.jsonl"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps({"platform": "jd", "product_id": "JD-FAIL-1", "comments": 1}) + "\n",
        encoding="utf-8",
    )

    def fake_run(*args, **kwargs):
        return _completed(
            1,
            stdout=json.dumps(
                {
                    "platform": "jd",
                    "status": "error",
                    "login_ok": True,
                    "errors": ["风控滑块"],
                },
                ensure_ascii=False,
            ),
            stderr="browser crashed",
        )

    monkeypatch.setattr("pipeline.jd_detail.subprocess.run", fake_run)
    with pytest.raises(RuntimeError) as excinfo:
        CrawlerJdDetailFetcher(settings)(_jd_target(), DAY)
    message = str(excinfo.value)
    assert "JD-FAIL-S" in message
    assert "status=error" in message
    assert "风控滑块" in message
    assert "browser crashed" in message


def test_crawler_jd_detail_fetcher_raises_on_login_failed(settings, monkeypatch):
    from pipeline.jd_detail import CrawlerJdDetailFetcher

    settings.crawler_dir.mkdir(parents=True, exist_ok=True)

    def fake_run(*args, **kwargs):
        return _completed(
            0,
            stdout=json.dumps(
                {
                    "platform": "jd",
                    "status": "login_failed",
                    "login_ok": False,
                    "errors": ["登录态失效"],
                },
                ensure_ascii=False,
            ),
        )

    monkeypatch.setattr("pipeline.jd_detail.subprocess.run", fake_run)
    with pytest.raises(RuntimeError) as excinfo:
        CrawlerJdDetailFetcher(settings)(_jd_target(), DAY)
    assert "login_failed" in str(excinfo.value)
    assert "login_ok=False" in str(excinfo.value)


def test_crawler_jd_detail_fetcher_ignores_stale_rows(settings, monkeypatch):
    from pipeline.jd_detail import CrawlerJdDetailFetcher

    settings.crawler_dir.mkdir(parents=True, exist_ok=True)
    output = settings.crawler_output_dir / DAY.isoformat() / "jd_detail.jsonl"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps({"platform": "jd", "product_id": "JD-FAIL-1", "comments": 1}) + "\n",
        encoding="utf-8",
    )

    def fake_run(command, **kwargs):
        assert command[:4] == [command[0], "-m", "crawler", "detail"]
        with output.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {"platform": "jd", "product_id": "JD-FAIL-1", "comments": 99}
                )
                + "\n"
            )
        return _completed(
            0,
            stdout=json.dumps(
                {"platform": "jd", "status": "ok", "login_ok": True, "errors": []}
            ),
        )

    monkeypatch.setattr("pipeline.jd_detail.subprocess.run", fake_run)
    rows = CrawlerJdDetailFetcher(settings)(_jd_target(), DAY)
    assert len(rows) == 1
    assert rows[0]["comments"] == 99


def test_real_jd_fetcher_failure_logs_run_logs_and_alerts(
    settings, engine, fake_smtp, monkeypatch
):
    settings.raw["alert"]["enabled"] = True
    settings.raw["alert"]["min_items_per_platform"] = 1
    settings.crawler_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "keyring.get_password", lambda service, user: "auth-code-for-real-jd-fail"
    )

    def fake_run(*args, **kwargs):
        return _completed(
            1,
            stdout=json.dumps(
                {
                    "platform": "jd",
                    "status": "risk_blocked",
                    "login_ok": False,
                    "errors": ["触发滑块"],
                },
                ensure_ascii=False,
            ),
            stderr="risk",
        )

    monkeypatch.setattr("pipeline.jd_detail.subprocess.run", fake_run)
    item = make_item(
        platform="jd",
        product_id="JD-REAL-FAIL",
        url="https://item.jd.com/JD-REAL-FAIL.html",
        shop_id="JD-REAL-FAIL-S",
        price=200,
        sales=2000,
        comments=1000,
        sales_raw="2000",
        comments_raw="1000",
    )
    summary = PipelineRunner(
        settings, adapter=FakeAdapter({"jd": platform_data("jd", [item])})
    ).run(platforms=["jd"], run_date=DAY)

    assert summary.failed is True
    assert summary.alerted is True
    factory = make_session_factory(engine)
    try:
        with session_scope(factory) as session:
            rows = session.scalars(
                select(RunLog).where(RunLog.stage == "jd_detail")
            ).all()
            assert len(rows) == 1
            assert rows[0].status == "error"
            assert "JD-REAL-FAIL-S" in (rows[0].error_summary or "")
            assert "触发滑块" in (rows[0].error_summary or "")
    finally:
        factory.kw["bind"].dispose()

    body = fake_smtp.instances[0].messages[0][0].get_content()
    assert "京东详情补抓" in body
    assert "触发滑块" in body
    assert "auth-code-for-real-jd-fail" not in body


# ---------------------------------------------------------------------------
# P1-2：C4 淘宝/天猫详情触发策略
# ---------------------------------------------------------------------------
def test_c4_taobao_detail_policy(settings, session_factory):
    item = make_item(
        platform="tmall",
        product_id="C4-TMALL-1",
        url="https://detail.tmall.com/item.htm?id=C4-TMALL-1",
        shop_id="C4-S",
        price=600,
        sales=None,
        sales_raw=None,
        comments=None,
        comments_raw=None,
    )
    calls: list[list] = []

    def fake_fetcher(targets, run_date):
        calls.append(list(targets))
        return [
            {
                "platform": "tmall",
                "product_id": "C4-TMALL-1",
                "comments": 100,
                "comments_raw": "100",
                "sales": 2000,
                "sales_raw": "2000",
                "listed": True,
            }
        ]

    summary = PipelineRunner(
        settings,
        adapter=FakeAdapter({"taobao": platform_data("taobao", [item])}),
        taobao_detail_fetcher=fake_fetcher,
    ).run(platforms=["taobao"], run_date=DAY, allow_alert=False)

    assert summary.failed is False
    assert len(calls) == 1
    assert calls[0][0].platform == "tmall"
    assert calls[0][0].detail_id == "tmall:C4-TMALL-1"

    with session_scope(session_factory) as session:
        snapshot = session.scalar(select(ProductSnapshot))
        assert snapshot.comments == 100
        assert snapshot.comments_raw == "100"
        assert snapshot.amount == Decimal("1200000.00")
        lead = session.scalar(select(Lead))
        assert lead is not None
        assert lead.platform == "tmall"
        assert lead.lead_no.startswith("tmall-")
        assert lead.total_amount == Decimal("1200000.00")


def test_c4_below_threshold_and_no_need_vision_skips(settings, session_factory):
    item = make_item(
        product_id="C4-LOW-1",
        title="普通商品名称较长",
        price=600,
        sales=2000,
        comments=None,
        comments_raw=None,
    )

    def fake_fetcher(targets, run_date):  # pragma: no cover - 不应调用
        raise AssertionError("文字分 <35 且非 need_vision 不应补抓详情")

    summary = PipelineRunner(
        settings,
        adapter=FakeAdapter({"taobao": platform_data("taobao", [item])}),
        taobao_detail_fetcher=fake_fetcher,
    ).run(platforms=["taobao"], run_date=DAY, allow_alert=False)

    assert summary.failed is False  # 评论不再阻断金额计算。
    assert summary.platforms["taobao"].amount_missing == 0
    with session_scope(session_factory) as session:
        snapshot = session.scalar(select(ProductSnapshot))
        assert snapshot.text_score < 35
        assert snapshot.need_vision is False
        assert snapshot.comments is None
        assert session.scalar(select(Lead)) is None
        assert (
            session.scalar(select(RunLog).where(RunLog.stage == "taobao_detail"))
            is None
        )


def test_c4_need_vision_triggers(settings, session_factory):
    item = make_item(
        product_id="C4-VISION-1",
        title="印花 童装 新款",
        price=600,
        sales=None,
        comments=None,
        comments_raw=None,
    )
    calls: list[list] = []

    def fake_fetcher(targets, run_date):
        calls.append(list(targets))
        return [
            {
                "platform": "taobao",
                "product_id": "C4-VISION-1",
                "sales": 2000,
                "comments": 100,
                "comments_raw": "100",
            }
        ]

    PipelineRunner(
        settings,
        adapter=FakeAdapter({"taobao": platform_data("taobao", [item])}),
        taobao_detail_fetcher=fake_fetcher,
    ).run(platforms=["taobao"], run_date=DAY, allow_alert=False)

    assert len(calls) == 1
    with session_scope(session_factory) as session:
        snapshot = session.scalar(select(ProductSnapshot))
        assert snapshot.need_vision is True
        assert snapshot.text_score < 35
        assert snapshot.comments == 100
        assert snapshot.amount == Decimal("1200000.00")


def test_crawler_taobao_detail_fetcher_passes_tmall_prefix(settings, monkeypatch):
    from pipeline.taobao_detail import (
        CrawlerTaobaoDetailFetcher,
        TaobaoDetailTarget,
    )

    settings.crawler_dir.mkdir(parents=True, exist_ok=True)
    output = settings.crawler_output_dir / DAY.isoformat() / "taobao_detail.jsonl"
    captured_ids: list[str] = []

    def fake_run(command, **kwargs):
        ids_path = Path(command[command.index("--ids-file") + 1])
        captured_ids.extend(
            line.strip()
            for line in ids_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "platform": "tmall",
                        "product_id": "123",
                        "comments": 88,
                    }
                )
                + "\n"
            )
        assert kwargs["env"]["CRAWLER_OUTPUT_DIR"] == str(settings.crawler_output_dir)
        return _completed(
            0,
            stdout=json.dumps(
                {
                    "platform": "taobao",
                    "status": "ok",
                    "login_ok": True,
                    "errors": [],
                }
            ),
        )

    monkeypatch.setattr("pipeline.taobao_detail.subprocess.run", fake_run)
    rows = CrawlerTaobaoDetailFetcher(settings)(
        [
            TaobaoDetailTarget("tmall", "123"),
            TaobaoDetailTarget("taobao", "456"),
        ],
        DAY,
    )
    assert captured_ids == ["tmall:123", "456"]
    assert [row["product_id"] for row in rows] == ["123"]


def test_c4_taobao_detail_failure_logs_and_alerts(
    settings, engine, fake_smtp, monkeypatch
):
    settings.raw["alert"]["enabled"] = True
    settings.raw["alert"]["min_items_per_platform"] = 1
    monkeypatch.setattr(
        "keyring.get_password", lambda service, user: "auth-code-for-c4-fail"
    )
    item = make_item(
        product_id="C4-FAIL-1",
        price=600,
        sales=None,
        comments=None,
        comments_raw=None,
    )

    def broken_fetcher(targets, run_date):
        raise RuntimeError("详情接口超时")

    summary = PipelineRunner(
        settings,
        adapter=FakeAdapter({"taobao": platform_data("taobao", [item])}),
        taobao_detail_fetcher=broken_fetcher,
    ).run(platforms=["taobao"], run_date=DAY)

    assert summary.failed is True
    assert summary.alerted is True
    factory = make_session_factory(engine)
    try:
        with session_scope(factory) as session:
            rows = session.scalars(
                select(RunLog).where(RunLog.stage == "taobao_detail")
            ).all()
            assert len(rows) == 1
            assert rows[0].status == "error"
            assert "详情接口超时" in (rows[0].error_summary or "")
    finally:
        factory.kw["bind"].dispose()
    body = fake_smtp.instances[0].messages[0][0].get_content()
    assert "淘宝/天猫详情补抓" in body
    assert "详情接口超时" in body
    assert "auth-code-for-c4-fail" not in body


# ---------------------------------------------------------------------------
# P2-1：whitelist_file CSV 加载
# ---------------------------------------------------------------------------
def test_whitelist_csv_sync_changes_take_effect(settings, session_factory, tmp_path):
    csv_path = tmp_path / "whitelist.csv"
    csv_path.write_text(
        "platform,shop_id,ip_code,reason,active\ntmall,S9,huluxiongdi,授权店铺,1\n",
        encoding="utf-8",
    )
    settings.raw["whitelist_file"] = str(csv_path)

    with session_scope(session_factory) as session:
        stats = sync_whitelist(session, settings)
        assert stats.added == 1
        row = session.scalar(select(Whitelist))
        assert (row.platform, row.shop_id, row.ip_code) == (
            "taobao",
            "S9",
            "huluxiongdi",
        )
        assert row.note == "授权店铺"

    item = make_item(
        platform="tmall",
        product_id="WL-TMALL-1",
        url="https://detail.tmall.com/item.htm?id=WL-TMALL-1",
        shop_id="S9",
        price=600,
        sales=1000,
        comments=100,
    )
    adapter = FakeAdapter({"taobao": platform_data("taobao", [item])})
    runner = PipelineRunner(settings, adapter=adapter)
    first = runner.run(platforms=["taobao"], run_date=DAY, allow_alert=False)
    assert first.leads == 0
    with session_scope(session_factory) as session:
        snapshot = session.scalar(select(ProductSnapshot))
        assert "排除:白名单" in "；".join(json.loads(snapshot.hit_reasons))

    # 只保留表头：同步清空白名单后，同一商品应立即恢复参与聚合。
    csv_path.write_text("platform,shop_id,ip_code,reason,active\n", encoding="utf-8")
    second = runner.run(platforms=["taobao"], run_date=DAY, allow_alert=False)
    assert second.leads == 1
    with session_scope(session_factory) as session:
        assert session.query(Whitelist).count() == 0
        lead = session.scalar(select(Lead))
        assert lead.platform == "tmall"
        assert lead.lead_no.startswith("tmall-")


def test_whitelist_missing_or_empty_file_does_not_raise(
    settings, session_factory, tmp_path
):
    missing = tmp_path / "does-not-exist.csv"
    settings.raw["whitelist_file"] = str(missing)
    with session_scope(session_factory) as session:
        assert sync_whitelist(session, settings).added == 0

    empty = tmp_path / "empty.csv"
    empty.write_text("", encoding="utf-8")
    settings.raw["whitelist_file"] = str(empty)
    with session_scope(session_factory) as session:
        assert sync_whitelist(session, settings).removed == 0


# ---------------------------------------------------------------------------
# P2-2：tmall 枚举保留 + 聚合纳入 tmall
# ---------------------------------------------------------------------------
def test_tmall_platform_is_preserved_and_aggregated(settings, session_factory):
    item = make_item(
        platform="tmall",
        product_id="TM-1",
        url="https://detail.tmall.com/item.htm?id=TM-1",
        shop_id="TM-S1",
        price=600,
        sales=1000,
        comments=100,
    )
    summary = PipelineRunner(
        settings, adapter=FakeAdapter({"taobao": platform_data("taobao", [item])})
    ).run(platforms=["taobao"], run_date=DAY, allow_alert=False)

    assert summary.leads == 1
    with session_scope(session_factory) as session:
        product = session.scalar(select(Product))
        shop = session.scalar(select(Shop))
        lead = session.scalar(select(Lead))
        assert product.platform == "tmall"
        assert shop.platform == "tmall"
        assert lead.platform == "tmall"
        assert lead.lead_no.startswith("tmall-")


@pytest.mark.parametrize(
    "run_id,expected_items", [("tmall-count", 1), ("different-task", 0)]
)
def test_runner_counts_existing_tmall_snapshots_for_taobao_channel(
    settings, session_factory, run_id, expected_items
):
    with session_scope(session_factory) as session:
        snapshots, _ = upsert_many(
            session,
            platform="taobao",
            items=[
                make_item(
                    platform="tmall",
                    product_id="TM-COUNT-1",
                    url="https://detail.tmall.com/item.htm?id=TM-COUNT-1",
                    shop_id="TM-COUNT-S",
                    price=1,
                    sales=1,
                    comments=1,
                )
            ],
            settings=settings,
            run_id="tmall-count",
            snapshot_date=DAY,
        )
        for snapshot in snapshots:
            product = session.get(Product, snapshot.product_id)
            shop = session.scalar(
                select(Shop).where(
                    Shop.platform == product.platform, Shop.shop_id == product.shop_id
                )
            )
            from pipeline.scoring import score_snapshot

            score_snapshot(
                session,
                settings=settings,
                snapshot=snapshot,
                product=product,
                shop=shop,
            )
        session.commit()

    summary = PipelineRunner(
        settings, adapter=FakeAdapter({"taobao": platform_data("taobao", [])})
    ).run(platforms=["taobao"], run_date=DAY, allow_alert=False, run_id=run_id)
    assert summary.platforms["taobao"].items == expected_items


# ---------------------------------------------------------------------------
# P2-3：报警只读 run_logs；配置阈值
# ---------------------------------------------------------------------------
def test_collect_alert_lines_reads_export_and_detail_stages(settings, session_factory):
    settings.raw["alert"]["min_items_per_platform"] = 50
    with session_scope(session_factory) as session:
        session.add(
            RunLog(
                run_id="alert-run",
                platform="taobao",
                stage="crawl",
                login_ok=True,
                items_count=50,
                status="ok",
            )
        )
        session.add(
            RunLog(
                run_id="alert-run",
                platform=None,
                stage="export",
                status="error",
                error_summary="Excel 导出失败: 文件被占用",
            )
        )
        session.add(
            RunLog(
                run_id="alert-run",
                platform=None,
                stage="jd_detail",
                status="error",
                error_summary="京东详情补抓失败 shop=JD1",
            )
        )
        session.add(
            RunLog(
                run_id="alert-run",
                platform=None,
                stage="taobao_detail",
                status="error",
                error_summary="淘宝详情补抓失败",
            )
        )
        session.add(
            RunLog(
                run_id="alert-run",
                platform=None,
                stage="overall",
                status="partial",
                error_summary="不应重复进入报警",
            )
        )
        session.flush()
        lines = collect_alert_lines(session, run_id="alert-run", settings=settings)
        session.commit()

    joined = "\n".join(lines)
    assert "Excel 导出: 状态 error" in joined
    assert "京东详情补抓: 状态 error" in joined
    assert "淘宝/天猫详情补抓: 状态 error" in joined
    assert "不应重复进入报警" not in joined


def test_alert_min_items_config_is_50():
    assert load_config().min_items_per_platform == 50


# ---------------------------------------------------------------------------
# P2-4：近门槛只读视图 + 运行报告
# ---------------------------------------------------------------------------
def test_near_threshold_shops_view_and_run_summary(settings, session_factory):
    item = make_item(
        product_id="NEAR-1",
        shop_id="NEAR-S",
        price=200,
        sales=2000,
        comments=1000,
        comments_raw="1000",
    )
    summary = PipelineRunner(
        settings, adapter=FakeAdapter({"taobao": platform_data("taobao", [item])})
    ).run(platforms=["taobao"], run_date=DAY, allow_alert=False)

    assert summary.leads == 1
    assert len(summary.near_threshold) == 1
    row = summary.near_threshold[0]
    assert row["shop_id"] == "NEAR-S"
    assert row["shop_amount"] == "400000.00"
    assert any("NEAR-S" in line for line in summary.lines())

    with session_scope(session_factory) as session:
        view_rows = (
            session.execute(text("SELECT * FROM v_near_threshold_shops"))
            .mappings()
            .all()
        )
        assert len(view_rows) == 1
        assert view_rows[0]["shop_id"] == "NEAR-S"
        assert view_rows[0]["shop_amount"] == Decimal("400000")
        lead_rows = (
            session.execute(text("SELECT * FROM v_lead_summary")).mappings().all()
        )
        assert len(lead_rows) == 1
        assert lead_rows[0]["tier"] == "临界"


# ---------------------------------------------------------------------------
# P3：复核人尾列为空时安全读取
# ---------------------------------------------------------------------------
def test_review_import_handles_trimmed_reviewer_cell(
    settings, session_factory, monkeypatch, tmp_path
):
    item = make_item(product_id="REV-1", price=600, sales=1000, comments=100)
    runner = PipelineRunner(
        settings, adapter=FakeAdapter({"taobao": platform_data("taobao", [item])})
    )
    runner.run(platforms=["taobao"], run_date=DAY, allow_alert=False)
    with session_scope(session_factory) as session:
        lead_no = session.scalar(select(Lead.lead_no))

    class Cell:
        def __init__(self, value):
            self.value = value

    class FakeSheet:
        def __getitem__(self, index):
            assert index == 1
            return [Cell("线索编号"), Cell("复核结论"), Cell("复核人")]

        def iter_rows(self, min_row=2, values_only=True):
            # 模拟表格工具裁掉尾随空单元格：行只有前两列。
            yield (lead_no, "已确认")

    class FakeWorkbook:
        sheetnames = ["线索汇总"]

        def __getitem__(self, name):
            assert name == "线索汇总"
            return FakeSheet()

        def close(self):
            pass

    monkeypatch.setattr("pipeline.review.load_workbook", lambda *a, **k: FakeWorkbook())
    fake_path = tmp_path / "trimmed.xlsx"
    fake_path.write_bytes(b"placeholder")
    with session_scope(session_factory) as session:
        stats = import_review_excel(session, fake_path)
        assert stats.updated == 1
        session.commit()
    with session_scope(session_factory) as session:
        lead = session.scalar(select(Lead))
        assert lead.status == "已确认"
        assert lead.reviewer == "Excel复核"
