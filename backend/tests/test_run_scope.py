from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from pipeline.aggregate import aggregate_day
from pipeline.models import Base, Lead, LeadLink, Product, ProductSnapshot, Whitelist
from pipeline.scoring import score_snapshot
from pipeline.store import upsert_crawl_item
from tests.conftest import make_item

DAY = date(2026, 11, 6)


@pytest.mark.parametrize("failed_platform", [None, "jd"])
def test_platforms_start_together_and_isolate_failure(settings, failed_platform):
    from threading import Barrier
    from pipeline.runner import PipelineRunner
    from tests.conftest import platform_data

    barrier = Barrier(3)
    started = set()

    class ConcurrentAdapter:
        def run(self, platform, run_date):
            started.add(platform)
            barrier.wait(timeout=2)
            if platform == failed_platform:
                raise RuntimeError("isolated failure")
            return platform_data(platform, [make_item(platform=platform, price=1)])

    result = PipelineRunner(
        settings,
        adapter=ConcurrentAdapter(),
        taobao_detail_fetcher=lambda *args: [],
        jd_detail_fetcher=lambda *args: [],
    ).run(platforms=["taobao", "jd", "1688"], run_date=DAY, allow_alert=False)
    assert started == {"taobao", "jd", "1688"}
    for platform, summary in result.platforms.items():
        assert summary.status == ("error" if platform == failed_platform else "ok")
        assert summary.items == (0 if platform == failed_platform else 1)
    assert result.failed == bool(failed_platform)


def test_failed_platform_keeps_collected_rows_in_aggregation(settings):
    from pipeline.runner import PipelineRunner
    from tests.conftest import FakeAdapter, platform_data

    settings.raw["aggregate"]["shop_threshold"] = 1000
    result = PipelineRunner(
        settings,
        adapter=FakeAdapter(
            {
                "taobao": platform_data(
                    "taobao",
                    [make_item(price=5000, sales=1, comments=1)],
                    status="risk_blocked",
                    errors=["platform stopped"],
                )
            }
        ),
        taobao_detail_fetcher=lambda *args: [],
    ).run(platforms=["taobao"], run_date=DAY, allow_alert=False)
    assert result.failed and result.platforms["taobao"].items == 1
    assert result.leads == 1


@pytest.fixture
def scope_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def add(session, settings, run, pid, ip, amount):
    row = upsert_crawl_item(
        session,
        platform="jd",
        settings=settings,
        run_id=run,
        snapshot_date=DAY,
        data=make_item(
            platform="jd",
            product_id=pid,
            url=f"https://item.example/{pid}",
            ip_code=ip,
            title="奥特曼 葫芦兄弟 印花 童装 批发",
            shop_name="同一家店",
            price=amount,
            sales=1,
            comments=1,
            sales_raw="1",
            comments_raw="1",
        ),
    )
    score_snapshot(
        session,
        settings=settings,
        snapshot=row,
        product=session.get(Product, row.product_id),
        shop=None,
    )
    return row


def test_multiple_ips_merge_shop_but_not_duplicate_product(scope_session, settings):
    session = scope_session
    settings.raw["ips"] = [
        {"code": "a", "name": "奥特曼"},
        {"code": "b", "name": "葫芦兄弟"},
    ]
    settings.raw["aggregate"]["shop_threshold"] = 10000
    add(session, settings, "run1", "p1", "a", 5000)
    add(session, settings, "run1", "p2", "b", 8000)
    add(session, settings, "run1", "p1", "b", 5000)
    assert len(session.scalars(select(ProductSnapshot)).all()) == 3
    leads = aggregate_day(session, settings=settings, snapshot_date=DAY, run_id="run1")
    assert len(leads) == 1
    assert leads[0].total_amount == Decimal("13000.00")
    assert leads[0].link_count == 2
    assert set(leads[0].ip_codes.split("、")) == {"a", "b"}
    assert len(session.scalars(select(LeadLink)).all()) == 2


def test_same_day_new_run_preserves_previous_lead(scope_session, settings):
    session = scope_session
    settings.raw["aggregate"]["shop_threshold"] = 1000
    add(session, settings, "first", "p1", "huluxiongdi", 5000)
    first = aggregate_day(
        session, settings=settings, snapshot_date=DAY, run_id="first"
    )[0]
    first_no = first.lead_no
    add(session, settings, "second", "p1", "huluxiongdi", 8000)
    second = aggregate_day(
        session, settings=settings, snapshot_date=DAY, run_id="second"
    )[0]
    assert second.lead_no != first_no
    assert first.total_amount == Decimal("5000.00")
    assert second.total_amount == Decimal("8000.00")
    assert len(session.scalars(select(Lead)).all()) == 2


def test_whitelist_only_excludes_its_own_ip(scope_session, settings):
    session = scope_session
    settings.raw["ips"] = [
        {"code": "a", "name": "奥特曼"},
        {"code": "b", "name": "葫芦兄弟"},
    ]
    settings.raw["aggregate"]["shop_threshold"] = 1000
    session.add(
        Whitelist(
            platform="jd",
            shop_id="S1",
            shop_name="同一家店",
            shop_name_norm="同一家店",
            ip_code="a",
        )
    )
    a = add(session, settings, "run", "p1", "a", 5000)
    b = add(session, settings, "run", "p1", "b", 5000)
    assert not a.eligible
    assert b.eligible
    lead = aggregate_day(session, settings=settings, snapshot_date=DAY, run_id="run")[0]
    assert lead.total_amount == Decimal("5000.00")
    assert lead.ip_codes == "b"


def test_html_highlight_tags_are_removed_before_scoring(scope_session, settings):
    row = upsert_crawl_item(
        scope_session,
        platform="taobao",
        settings=settings,
        run_id="highlight-run",
        snapshot_date=DAY,
        data=make_item(title="<span>葫芦</span><span>兄弟</span> 印花童装"),
    )
    product = scope_session.get(Product, row.product_id)
    assert product.title == "葫芦兄弟 印花童装"


def test_excel_only_contains_selected_run(settings, session_factory):
    import openpyxl

    from pipeline.excel_export import export_leads_for_run

    settings.raw["aggregate"]["shop_threshold"] = 1000
    with session_factory() as session:
        for run, pid in (("current", "current-product"), ("old", "old-product")):
            add(session, settings, run, pid, "huluxiongdi", 5000)
            aggregate_day(session, settings=settings, snapshot_date=DAY, run_id=run)
            # 无店名商品也会进入明细视图，必须同样按任务隔离。
            upsert_crawl_item(
                session,
                platform="jd",
                settings=settings,
                run_id=run,
                snapshot_date=DAY,
                data=make_item(
                    platform="jd",
                    product_id=f"{run}-no-shop",
                    shop_name=None,
                    url=f"https://item.example/{run}-no-shop",
                ),
            )
        session.commit()
    settings.raw["export"]["run_id"] = "current"
    path = export_leads_for_run(None, settings, DAY)
    wb = openpyxl.load_workbook(path, read_only=True)
    try:
        assert wb.sheetnames == ["链接明细"]
        links = list(wb["链接明细"].values)
        products = {str(row[5]) for row in links[1:] if row[5]}
        assert len(products) == 2
        assert any("current-product" in url for url in products)
        assert any("current-no-shop" in url for url in products)
        assert not any("old-" in url for url in products)
        assert all(row[0] == "jd" for row in links[1:] if row[5])
    finally:
        wb.close()


def test_upgrade_repairs_stamped_link_view_without_losing_data(
    settings, engine, session_factory
):
    from sqlalchemy import text
    from pipeline.db import ensure_schema
    from pipeline.views import LEAD_LINKS_VIEW_SQL

    with session_factory() as session:
        add(session, settings, "repair-view", "retained-product", "huluxiongdi", 5000)
        session.commit()
    # 重现已标记 0008，但旧视图未包含外层 run_id 的实际部署状态。
    with engine.begin() as connection:
        connection.execute(text("DROP VIEW v_lead_links"))
        connection.execute(
            text(
                LEAD_LINKS_VIEW_SQL.replace(
                    "    CAST(run_id AS TEXT)                         AS run_id,\n",
                    "",
                    1,
                )
            )
        )
        connection.execute(
            text("UPDATE alembic_version SET version_num='0008_link_view_run_id'")
        )
    ensure_schema(settings)
    with engine.connect() as connection:
        columns = {
            row[1]
            for row in connection.execute(text("PRAGMA table_info(v_lead_links)"))
        }
        assert "run_id" in columns
        assert (
            connection.execute(
                text(
                    "SELECT COUNT(*) FROM product_snapshots WHERE run_id='repair-view'"
                )
            ).scalar()
            == 1
        )


def test_upgrade_adds_export_shop_fields_without_losing_data(
    settings, engine, session_factory
):
    from sqlalchemy import text
    from pipeline.db import ensure_schema
    from pipeline.views import LEAD_LINKS_VIEW_SQL

    settings.raw["aggregate"]["shop_threshold"] = 1000
    with session_factory() as session:
        add(session, settings, "export-upgrade", "retained", "huluxiongdi", 5000)
        aggregate_day(
            session, settings=settings, snapshot_date=DAY, run_id="export-upgrade"
        )
        session.commit()
    tables = ("products", "product_snapshots", "leads", "lead_links")
    with engine.begin() as connection:
        before = [
            connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
            for table in tables
        ]
        old_sql = LEAD_LINKS_VIEW_SQL
        for line in (
            "    CAST(platform AS TEXT)                       AS platform,\n",
            "    CAST(shop_id AS TEXT)                        AS shop_id,\n",
        ):
            old_sql = old_sql.replace(line, "", 1)
        connection.execute(text("DROP VIEW v_lead_links"))
        connection.execute(text(old_sql))
        connection.execute(
            text(
                "UPDATE alembic_version SET version_num='0009_refresh_link_task_scope'"
            )
        )
        assert "platform" not in {
            row[1]
            for row in connection.execute(text("PRAGMA table_info(v_lead_links)"))
        }
    ensure_schema(settings)
    with engine.connect() as connection:
        assert [
            connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
            for table in tables
        ] == before
        row = connection.execute(
            text("SELECT platform, shop_id, product_id, amount FROM v_lead_links")
        ).one()
        assert tuple(row) == ("jd", "S1", "retained", 5000)
        assert (
            connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
            == "0013_sales_amount_view"
        )


def test_taobao_tmall_same_business_product_counts_once(scope_session, settings):
    session = scope_session
    settings.raw["ips"] = [
        {"code": "a", "name": "葫芦娃"},
        {"code": "b", "name": "葫芦兄弟"},
    ]
    settings.raw["aggregate"]["shop_threshold"] = 1000
    for platform, ip, amount, hour in (
        ("taobao", "a", 5000, 10),
        ("tmall", "b", 8000, 11),
    ):
        row = upsert_crawl_item(
            session,
            platform="taobao",
            settings=settings,
            run_id="current",
            snapshot_date=DAY,
            data=make_item(
                platform=platform,
                product_id="same-id",
                ip_code=ip,
                title="葫芦娃 葫芦兄弟 印花童装",
                price=amount,
                sales=1,
                comments=1,
                crawled_at=f"2026-11-06T{hour}:00:00+08:00",
            ),
        )
        score_snapshot(
            session,
            settings=settings,
            snapshot=row,
            product=session.get(Product, row.product_id),
            shop=None,
        )
    lead = aggregate_day(
        session, settings=settings, snapshot_date=DAY, run_id="current"
    )[0]
    assert lead.total_amount == Decimal("8000.00")
    assert lead.link_count == 1
    links = session.scalars(select(LeadLink)).all()
    assert len(links) == 1
    assert set(links[0].ip_code.split("、")) == {"a", "b"}
    assert len(session.scalars(select(ProductSnapshot)).all()) == 2


@pytest.mark.parametrize("platform", ["taobao", "tmall", "jd"])
def test_detail_patch_updates_all_current_ips_only(scope_session, settings, platform):
    from pipeline.jd_detail import (
        JdDetailTarget,
        apply_jd_detail_patches,
        rescore_jd_targets,
    )
    from pipeline.taobao_detail import TaobaoDetailTarget, apply_taobao_detail_patches

    session = scope_session
    settings.raw["ips"] = [
        {"code": "a", "name": "葫芦娃"},
        {"code": "b", "name": "葫芦兄弟"},
    ]
    rows = []
    for run, ip in (("old", "a"), ("current", "a"), ("current", "b")):
        row = upsert_crawl_item(
            session,
            platform=platform,
            settings=settings,
            run_id=run,
            snapshot_date=DAY,
            data=make_item(
                platform=platform,
                product_id="same",
                ip_code=ip,
                title="葫芦娃 葫芦兄弟 印花童装",
                price=10,
                sales=100,
                comments=None,
                comments_raw=None,
            ),
        )
        score_snapshot(
            session,
            settings=settings,
            snapshot=row,
            product=session.get(Product, row.product_id),
            shop=None,
        )
        rows.append(row)
    old_amount = rows[0].amount
    patch = {
        "platform": platform,
        "product_id": "same",
        "comments": 3,
        "comments_raw": "3",
    }
    if platform == "jd":
        target = JdDetailTarget("S1", Decimal("1000"), ("same",))
        changed = apply_jd_detail_patches(
            session,
            settings=settings,
            patches=[patch],
            target=target,
            snapshot_date=DAY,
            run_id="current",
        )
        assert (
            rescore_jd_targets(
                session,
                settings=settings,
                target=target,
                snapshot_date=DAY,
                run_id="current",
            )
            == 2
        )
    else:
        changed = apply_taobao_detail_patches(
            session,
            settings=settings,
            patches=[patch],
            targets=[TaobaoDetailTarget(platform, "same")],
            snapshot_date=DAY,
            run_id="current",
        )
    assert changed == 2
    assert rows[0].comments is None
    assert rows[0].amount == old_amount
    assert [row.comments for row in rows[1:]] == [3, 3]
    assert rows[1].amount == rows[2].amount


@pytest.mark.parametrize("platform", ["taobao", "jd"])
def test_detail_without_run_id_rejects_ambiguous_tasks(
    scope_session, settings, platform
):
    from pipeline.jd_detail import JdDetailTarget, apply_jd_detail_patches
    from pipeline.taobao_detail import apply_taobao_detail_patches

    for run in ("first", "second"):
        upsert_crawl_item(
            scope_session,
            platform=platform,
            settings=settings,
            run_id=run,
            snapshot_date=DAY,
            data=make_item(platform=platform),
        )
    kwargs = dict(
        settings=settings,
        patches=[{"product_id": "10001", "comments": 7}],
        snapshot_date=DAY,
    )
    with pytest.raises(ValueError, match="run_id"):
        if platform == "jd":
            apply_jd_detail_patches(
                scope_session,
                **kwargs,
                target=JdDetailTarget("S1", Decimal("1"), ("10001",)),
            )
        else:
            apply_taobao_detail_patches(scope_session, **kwargs)
