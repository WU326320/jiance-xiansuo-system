"""B7 京东临界带返工：默认 CLI 入口必须补抓详情并重新聚合判门槛。"""

from __future__ import annotations

import json
from argparse import Namespace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from sqlalchemy import select

from pipeline.cli import cmd_run
from pipeline.db import make_session_factory, session_scope
from pipeline.models import Lead, ProductSnapshot, RunLog
from pipeline.runner import PipelineRunner

from tests.conftest import FakeAdapter, make_item, platform_data

DAY = date(2026, 11, 5)


@pytest.mark.parametrize("platform", ["jd", "taobao"])
@pytest.mark.parametrize("has_failure", [False, True])
def test_deadline_drains_details_and_commits_results_before_notification(
    settings, engine, monkeypatch, platform, has_failure
):
    from datetime import datetime, timedelta
    from types import SimpleNamespace

    from pipeline.jd_detail import DetailFetchError, JdDetailTarget
    from pipeline.models import Product

    settings.raw["run"]["ends_at"] = (
        datetime.now() + timedelta(minutes=10)
    ).isoformat()
    regular_calls, detail_calls, exports, notifications = [], [], [], []
    items = [
        make_item(
            platform=platform,
            product_id=pid,
            shop_id="A" if pid != "333" else "B",
            price=20,
            sales=None,
            comments=None,
        )
        for pid in ("111", "222", "333")
    ]

    class Adapter:
        def run(self, platform, run_date):
            assert not settings.deadline_reached
            regular_calls.append(platform)
            settings.raw["run"]["ends_at"] = "2000-01-01 00:00:00"
            return platform_data(platform, items, status="deadline")

    def fetch(target, day):
        assert settings.deadline_reached
        assert exports == notifications == []
        ids = (
            target.product_ids
            if isinstance(target, JdDetailTarget)
            else tuple(item.product_id for item in target)
        )
        detail_calls.extend(ids)
        patches = [
            {"platform": platform, "product_id": pid, "sales": 100}
            for pid in ids
            if not (has_failure and pid == "222")
        ]
        if has_failure and "222" in ids:
            raise DetailFetchError("222 补采重试耗尽", patches)
        return patches

    def assert_saved(session):
        rows = session.execute(
            select(Product.product_id, ProductSnapshot.sales, ProductSnapshot.amount)
            .join(ProductSnapshot, ProductSnapshot.product_id == Product.id)
            .where(ProductSnapshot.run_id == "deadline-drain")
        ).all()
        assert len(rows) == 3
        for pid, sales, amount in rows:
            missing = has_failure and pid == "222"
            assert sales == (None if missing else 100)
            assert amount == (None if missing else Decimal("2000.00"))

    def export(session, _settings, day):
        assert notifications == []
        assert sorted(detail_calls) == ["111", "222", "333"]
        assert_saved(session)
        exports.append(day)
        return None

    def notify(_settings, **kwargs):
        assert exports == [DAY]
        assert kwargs["status"] == ("partial" if has_failure else "ok")
        # 使用新连接检查：通知时补采成功结果和整体终态均已提交。
        with session_scope(make_session_factory(engine)) as session:
            assert_saved(session)
            overall = session.scalar(select(RunLog).where(RunLog.stage == "overall"))
            assert overall is not None
            assert overall.status == ("partial" if has_failure else "ok")
            details = session.scalars(
                select(RunLog).where(RunLog.stage == f"{platform}_detail")
            ).all()
            assert len(details) == int(has_failure)
            if has_failure:
                assert "222 补采重试耗尽" in (details[0].error_summary or "")
                assert "222 补采重试耗尽" in (overall.error_summary or "")
        notifications.append(kwargs["status"])
        return SimpleNamespace(sent=True)

    monkeypatch.setattr("pipeline.runner.send_completion_email", notify)
    summary = PipelineRunner(
        settings,
        adapter=Adapter(),
        jd_detail_fetcher=fetch,
        taobao_detail_fetcher=fetch,
        exporter=export,
    ).run(platforms=[platform], run_date=DAY, run_id="deadline-drain")
    assert regular_calls == [platform]
    assert sorted(detail_calls) == ["111", "222", "333"]
    assert summary.deadline_reached and summary.alerted
    assert summary.failed == has_failure
    assert summary.platforms[platform].amount_complete == (2 if has_failure else 3)
    assert summary.platforms[platform].status == (
        "partial" if has_failure else "deadline"
    )
    assert notifications == ["partial" if has_failure else "ok"]


def test_jd_missing_name_without_shop_id_is_fetched(settings, engine):
    from pipeline.models import Product

    calls = []

    def fetch(target, day):
        calls.extend(target.product_ids)
        return [{"platform": "jd", "product_id": "123456", "shop_name": "详情卖家店"}]

    adapter = FakeAdapter(
        {
            "jd": platform_data(
                "jd",
                [
                    make_item(
                        platform="jd",
                        product_id="123456",
                        shop_id=None,
                        shop_name=None,
                        price=20,
                        sales=100,
                        comments=None,
                    )
                ],
            )
        }
    )
    summary = PipelineRunner(settings, adapter=adapter, jd_detail_fetcher=fetch).run(
        platforms=["jd"], run_date=DAY, allow_alert=False
    )
    assert calls == ["123456"]
    assert summary.platforms["jd"].missing_fields["shop_name"] == 0
    with session_scope(make_session_factory(engine)) as session:
        assert session.scalar(select(Product)).shop_name == "详情卖家店"


def _prepare_input(tmp_path: Path, item: dict) -> Path:
    directory = tmp_path / "crawler-out" / DAY.isoformat()
    directory.mkdir(parents=True)
    (directory / "jd.jsonl").write_text(
        json.dumps(item, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (directory / "jd.run.json").write_text(
        json.dumps(
            {"platform": "jd", "login_ok": True, "status": "ok", "errors": []},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return directory


def _run_default_cli(
    tmp_path: Path,
    settings,
    monkeypatch,
    *,
    item: dict,
    fake_fetcher,
) -> list:
    config_path = tmp_path / "rules.yaml"
    config_path.write_text(
        yaml.safe_dump(settings.raw, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    input_dir = _prepare_input(tmp_path, item)
    calls: list = []

    def fetcher(target, run_date):
        calls.append((target, run_date))
        return fake_fetcher(target, run_date)

    # 模拟默认入口：cmd_run 构造 PipelineRunner 时注入的采集模块补抓器。
    monkeypatch.setattr(
        "pipeline.cli.make_jd_detail_fetcher",
        lambda settings, logger=None: fetcher,
    )
    args = Namespace(
        config=str(config_path),
        platform=["jd"],
        input_dir=str(input_dir),
        date=DAY.isoformat(),
        dry_run=False,
        no_alert=True,
    )
    assert cmd_run(args) == 0
    return calls


def test_default_cli_fetches_jd_near_band_400k_and_reaggregates(
    tmp_path: Path, settings, monkeypatch
):
    item = make_item(
        platform="jd",
        product_id="JD400001",
        url="https://item.jd.com/JD400001.html",
        shop_id="JD-S400",
        price=200,
        sales=2000,
        comments=1000,
        sales_raw="2000",
        comments_raw="1000",
    )

    def fake_fetcher(target, run_date):
        assert target.shop_id == "JD-S400"
        assert target.total_amount == Decimal("400000.00")
        assert target.product_ids == ("JD400001",)
        # 详情页补到更准确的销量后，店铺合计 200×3000 = 600000。
        return [
            {
                "platform": "jd",
                "product_id": "JD400001",
                "sales": 3000,
                "sales_raw": "3000",
                "comments": 1000,
                "comments_raw": "1000",
                "listed": True,
            }
        ]

    calls = _run_default_cli(
        tmp_path, settings, monkeypatch, item=item, fake_fetcher=fake_fetcher
    )

    assert len(calls) == 1
    assert calls[0][1] == DAY
    factory = make_session_factory(settings_engine(settings, tmp_path))
    try:
        with session_scope(factory) as session:
            snapshot = session.scalar(select(ProductSnapshot))
            assert snapshot is not None
            assert snapshot.sales == 3000
            assert snapshot.sales_raw == "3000"
            assert snapshot.amount == Decimal("600000.00")
            lead = session.scalar(select(Lead))
            assert lead is not None
            assert lead.total_amount == Decimal("600000.00")
            assert lead.link_count == 1
    finally:
        factory.kw["bind"].dispose()


def test_default_cli_does_not_fetch_jd_200k(tmp_path: Path, settings, monkeypatch):
    item = make_item(
        platform="jd",
        product_id="JD200001",
        url="https://item.jd.com/JD200001.html",
        shop_id="JD-S200",
        price=200,
        sales=1000,
        comments=1000,
        sales_raw="1000",
        comments_raw="1000",
    )

    def fake_fetcher(target, run_date):  # pragma: no cover - 不应被调用
        raise AssertionError("200000 不在 0.6–1.0 临界带，不应补抓")

    calls = _run_default_cli(
        tmp_path, settings, monkeypatch, item=item, fake_fetcher=fake_fetcher
    )

    assert calls == []
    factory = make_session_factory(settings_engine(settings, tmp_path))
    try:
        with session_scope(factory) as session:
            snapshot = session.scalar(select(ProductSnapshot))
            assert snapshot is not None
            assert snapshot.sales == 1000
            assert snapshot.amount == Decimal("200000.00")
            assert session.scalar(select(Lead)).total_amount == Decimal("200000.00")
    finally:
        factory.kw["bind"].dispose()


def test_jd_detail_failure_logs_run_logs_and_alerts(
    settings, engine, fake_smtp, monkeypatch
):
    """详情补抓异常时：写 run_logs(jd_detail) 并进入本次报警邮件。"""

    settings.raw["alert"]["enabled"] = True
    settings.raw["alert"]["min_items_per_platform"] = 1
    monkeypatch.setattr(
        "keyring.get_password", lambda service, user: "auth-code-for-jd-detail-test"
    )

    def broken_fetcher(target, run_date):
        assert target.shop_id == "JD-FAIL-S"
        raise RuntimeError("详情页超时")

    item = make_item(
        platform="jd",
        product_id="JD-FAIL-001",
        url="https://item.jd.com/JD-FAIL-001.html",
        shop_id="JD-FAIL-S",
        price=200,
        sales=2000,
        comments=1000,
        sales_raw="2000",
        comments_raw="1000",
    )
    adapter = FakeAdapter({"jd": platform_data("jd", [item])})
    summary = PipelineRunner(
        settings, adapter=adapter, jd_detail_fetcher=broken_fetcher
    ).run(platforms=["jd"], run_date=DAY)

    assert summary.failed is True
    assert summary.alerted is True

    factory = make_session_factory(engine)
    try:
        with session_scope(factory) as session:
            rows = session.scalars(select(RunLog).order_by(RunLog.id)).all()
            detail_rows = [row for row in rows if row.stage == "jd_detail"]
            assert len(detail_rows) == 1
            assert detail_rows[0].status == "error"
            assert "京东详情补抓失败" in (detail_rows[0].error_summary or "")
            assert "JD-FAIL-S" in (detail_rows[0].error_summary or "")
            assert "详情页超时" in (detail_rows[0].error_summary or "")
            overall = [row for row in rows if row.stage == "overall"]
            assert len(overall) == 1
            assert overall[0].status == "partial"
            assert "京东详情补抓失败" in (overall[0].error_summary or "")
    finally:
        factory.kw["bind"].dispose()

    assert len(fake_smtp.instances) == 1
    message, _ = fake_smtp.instances[0].messages[0]
    body = message.get_content()
    assert "京东详情补抓失败" in body
    assert "详情页超时" in body
    assert "auth-code-for-jd-detail-test" not in body


def settings_engine(settings, tmp_path: Path):
    """CLI 从临时 YAML 读取设置；测试按同一 database_path 打开数据库。"""

    from pipeline.db import create_engine_for

    return create_engine_for(settings)


def test_jd_missing_sales_triggers_detail_without_amount(settings, engine):
    calls = []

    def fetch(target, day):
        calls.append(target)
        return [{"platform": "jd", "product_id": "123456", "sales": 100}]

    adapter = FakeAdapter(
        {
            "jd": platform_data(
                "jd",
                [
                    make_item(
                        platform="jd",
                        product_id="123456",
                        sales=None,
                        comments=7,
                        price=20,
                    )
                ],
            )
        }
    )
    summary = PipelineRunner(settings, adapter=adapter, jd_detail_fetcher=fetch).run(
        platforms=["jd"], run_date=DAY, allow_alert=False
    )
    assert len(calls) == 1
    with session_scope(make_session_factory(engine)) as session:
        snapshot = session.scalar(select(ProductSnapshot))
        assert snapshot.amount == Decimal("2000.00")
        assert "销量缺失" not in snapshot.missing_flags


def test_amount_incomplete_is_reported_as_partial(settings):
    adapter = FakeAdapter(
        {
            "1688": platform_data(
                "1688", [make_item(platform="1688", sales=None, comments=None)]
            )
        }
    )
    summary = PipelineRunner(settings, adapter=adapter).run(
        platforms=["1688"], run_date=DAY, allow_alert=False
    )
    assert summary.failed
    assert summary.platforms["1688"].amount_missing == 1


@pytest.mark.parametrize("field", ["shop_name", "title"])
def test_shop_name_missing_is_partial_even_with_amount(settings, field):
    adapter = FakeAdapter(
        {
            "1688": platform_data(
                "1688",
                [
                    make_item(
                        platform="1688",
                        **{field: ""},
                        sales=100,
                        comments=None,
                        price=20,
                    )
                ],
            )
        }
    )
    summary = PipelineRunner(settings, adapter=adapter).run(
        platforms=["1688"], run_date=DAY, allow_alert=False
    )
    assert summary.failed
    assert summary.platforms["1688"].amount_missing == 0
    assert summary.platforms["1688"].missing_fields[field] == 1


def test_taobao_missing_fields_bypass_detail_score_gate(settings, engine):
    settings.raw.setdefault("fetch", {}).setdefault("taobao", {})[
        "detail_min_text_score"
    ] = 100
    calls = []

    def fetch(targets, day):
        calls.extend(targets)
        return [{"platform": "taobao", "product_id": "123456", "sales": 100}]

    data = platform_data(
        "taobao", [make_item(product_id="123456", price=20, sales=None, comments=None)]
    )
    summary = PipelineRunner(
        settings,
        adapter=FakeAdapter({"taobao": data}),
        taobao_detail_fetcher=fetch,
    ).run(platforms=["taobao"], run_date=DAY, allow_alert=False)
    assert len(calls) == 1 and not summary.failed
    assert summary.platforms["taobao"].amount_complete == 1


def test_inline_details_do_not_start_second_backend_budget(settings):
    def forbidden(*args):
        raise AssertionError("逐页补采后不能再次打开详情预算")

    data = platform_data("taobao", [make_item(comments=None)], detail_status="inline")
    summary = PipelineRunner(
        settings,
        adapter=FakeAdapter({"taobao": data}),
        taobao_detail_fetcher=forbidden,
    ).run(platforms=["taobao"], run_date=DAY, allow_alert=False)
    assert not summary.failed
    assert summary.platforms["taobao"].amount_missing == 0
    assert "详情补抓失败" not in summary.platforms["taobao"].error
