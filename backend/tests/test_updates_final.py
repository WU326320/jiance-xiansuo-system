"""UPDATES.md 补充项回归：视图最终列名、storage.db_path、Excel 导出失败报警。"""

from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path

import yaml
from sqlalchemy import select, text

from pipeline.config import BACKEND_ROOT, load_config
from pipeline.db import ensure_schema, make_session_factory, session_scope
from pipeline.logging_utils import setup_run_logger
from pipeline.models import RunLog
from pipeline.runner import PipelineRunner

from tests.conftest import FakeAdapter, make_item, platform_data


def test_storage_db_path_key_is_used_and_legacy_paths_database_is_compatible(
    tmp_path: Path,
):
    # 仓库自带 rules.yaml 使用第 6 节最终键 storage.db_path。
    settings = load_config()
    assert settings.get("storage.db_path") == "data/leads.db"
    assert settings.database_path == BACKEND_ROOT / "data" / "leads.db"

    raw = yaml.safe_load(
        (BACKEND_ROOT / "config" / "rules.yaml").read_text(encoding="utf-8")
    )
    raw["storage"] = {"db_path": str(tmp_path / "new" / "leads.db")}
    raw["paths"]["database"] = str(tmp_path / "old" / "leads.db")
    path = tmp_path / "rules.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    # storage.db_path 优先于旧 paths.database。
    assert load_config(path).database_path == (tmp_path / "new" / "leads.db").resolve()

    raw.pop("storage")
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    # 存量配置兼容：没有 storage 时仍能读旧键，不至于突然换库。
    assert load_config(path).database_path == (tmp_path / "old" / "leads.db").resolve()


def test_views_only_contain_pending_leads(settings, session_factory):
    """第 6 节：两个视图只含待复核；已处理线索不出现在 Excel 数据源里。"""

    from pipeline.aggregate import aggregate_snapshots
    from pipeline.scoring import score_snapshot
    from pipeline.store import upsert_many
    from pipeline.models import Product, Shop
    from sqlalchemy import select as sa_select

    day = date(2026, 11, 5)
    with session_scope(session_factory) as session:
        snapshots, _ = upsert_many(
            session,
            platform="taobao",
            items=[
                make_item(
                    product_id="V1001",
                    price=600,
                    sales=1000,
                    comments=100,
                )
            ],
            settings=settings,
            run_id="view-pending",
            snapshot_date=day,
        )
        for snapshot in snapshots:
            product = session.get(Product, snapshot.product_id)
            shop = session.scalar(
                sa_select(Shop).where(
                    Shop.platform == product.platform, Shop.shop_id == product.shop_id
                )
            )
            score_snapshot(
                session, settings=settings, snapshot=snapshot, product=product, shop=shop
            )
        aggregate_snapshots(
            session,
            settings=settings,
            snapshots=snapshots,
            snapshot_date=day,
            run_id="view-pending",
        )
        session.commit()

        assert session.execute(text("SELECT COUNT(*) FROM v_lead_summary")).scalar() == 1
        assert session.execute(text("SELECT COUNT(*) FROM v_lead_links")).scalar() == 1
        row = session.execute(
            text("SELECT snapshot_date, review_result, reviewer FROM v_lead_summary")
        ).mappings().one()
        assert row["snapshot_date"] == "2026-11-05"
        assert row["review_result"] == "" and row["reviewer"] == ""

        # 改成已确认后，两个视图都应立即为空（视图只含待复核）。
        session.execute(text("UPDATE leads SET status='已确认', review_result='已确认'"))
        session.commit()
        assert session.execute(text("SELECT COUNT(*) FROM v_lead_summary")).scalar() == 0
        assert session.execute(text("SELECT COUNT(*) FROM v_lead_links")).scalar() == 0


def test_runner_calls_export_module_and_writes_excel(settings, session_factory, tmp_path):
    """主流程默认调用 /workspace/ip-monitor/export 的 export_leads_excel。"""

    output = tmp_path / "excel-out"
    settings.raw["export"] = {
        "enabled": True,
        "dir": str(output),
        "filename": "线索_{YYYYMMDD}.xlsx",
    }

    day = date(2026, 11, 5)
    item = make_item(product_id="X1001", price=600, sales=1000, comments=100)
    adapter = FakeAdapter({"taobao": platform_data("taobao", [item])})
    summary = PipelineRunner(settings, adapter=adapter).run(
        platforms=["taobao"], run_date=day, allow_alert=False
    )

    assert summary.excel_error is None
    assert summary.failed is False
    assert summary.excel and Path(summary.excel).exists()
    assert Path(summary.excel).name == "线索_20261105.xlsx"
    assert "Excel：" in "\n".join(summary.lines())


def test_runner_export_failure_logs_run_logs_and_alerts(
    settings, engine, session_factory, fake_smtp, monkeypatch
):
    """导出模块抛异常时：不中断其它结果，写 run_logs(export) 并发报警邮件。"""

    settings.raw["alert"]["enabled"] = True
    settings.raw["alert"]["min_items_per_platform"] = 1
    monkeypatch.setattr(
        "keyring.get_password", lambda service, user: "auth-code-for-export-test"
    )

    def broken_exporter(session, settings, run_date):
        raise RuntimeError("xlsx 被占用")

    day = date(2026, 11, 5)
    item = make_item(product_id="X2001", price=1, sales=1, comments=1)
    adapter = FakeAdapter({"taobao": platform_data("taobao", [item])})
    summary = PipelineRunner(
        settings, adapter=adapter, exporter=broken_exporter
    ).run(platforms=["taobao"], run_date=day)

    assert summary.failed is True
    assert summary.excel is None
    assert "Excel 导出失败" in (summary.excel_error or "")
    assert "xlsx 被占用" in (summary.excel_error or "")
    assert summary.alerted is True

    factory = make_session_factory(engine)
    try:
        with session_scope(factory) as session:
            rows = session.scalars(select(RunLog).order_by(RunLog.id)).all()
            export_rows = [row for row in rows if row.stage == "export"]
            assert len(export_rows) == 1
            assert export_rows[0].status == "error"
            assert "Excel 导出失败" in (export_rows[0].error_summary or "")
            overall = [row for row in rows if row.stage == "overall"]
            assert len(overall) == 1
            assert overall[0].status == "partial"
            assert "Excel 导出失败" in (overall[0].error_summary or "")
    finally:
        factory.kw["bind"].dispose()

    assert len(fake_smtp.instances) == 1
    message, _ = fake_smtp.instances[0].messages[0]
    body = message.get_content()
    assert "Excel 导出失败" in body
    assert "xlsx 被占用" in body
    assert "auth-code-for-export-test" not in body


def test_migration_does_not_disable_pipeline_file_logger(settings):
    """alembic 的 fileConfig 不能禁用运行中已建立的 pipeline 文件日志。"""

    pipeline_logger = logging.getLogger("pipeline")
    # 避免前序用例留下的 disabled 状态掩盖本次回归。
    pipeline_logger.disabled = False
    logger = setup_run_logger(settings, "b8-migration-logging")
    try:
        ensure_schema(settings)
        logger.info("B8_MIGRATION_LOG_SENTINEL")
        for handler in logger.handlers:
            handler.flush()

        log_path = settings.logs_dir / f"run_{datetime.now():%Y%m%d}.log"
        assert log_path.exists()
        assert "B8_MIGRATION_LOG_SENTINEL" in log_path.read_text(encoding="utf-8")
    finally:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()
