"""11/6 口径 B：三档位、评论缺失标记、无 shop_id / 未在架只进明细。"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

from sqlalchemy import select, text

from pipeline.aggregate import aggregate_snapshots
from pipeline.db import session_scope
from pipeline.jd_detail import JdDetailTarget, apply_jd_detail_patches
from pipeline.models import Lead, Product, Shop
from pipeline.scoring import COMMENTS_MISSING_FLAG, compute_amount, score_snapshot
from pipeline.store import upsert_many

from tests.conftest import make_item

DAY = date(2026, 11, 5)


def _ingest_and_score(session, settings, items, run_id: str):
    snapshots, dropped = upsert_many(
        session,
        platform=items[0]["platform"] if items else "taobao",
        items=items,
        settings=settings,
        run_id=run_id,
        snapshot_date=DAY,
    )
    for snapshot in snapshots:
        product = session.get(Product, snapshot.product_id)
        shop = (
            session.scalar(
                select(Shop).where(
                    Shop.platform == product.platform,
                    Shop.shop_id == product.shop_id,
                )
            )
            if product and product.shop_id
            else None
        )
        score_snapshot(
            session,
            settings=settings,
            snapshot=snapshot,
            product=product,
            shop=shop,
        )
    return snapshots, dropped


def _lead_and_tier(session, shop_id: str):
    lead = session.scalar(select(Lead).where(Lead.shop_id == shop_id))
    row = (
        session.execute(
            text("SELECT * FROM v_lead_summary WHERE shop_id = :shop_id"),
            {"shop_id": shop_id},
        )
        .mappings()
        .one()
    )
    return lead, row


def test_comments_missing_does_not_block_amount(settings):
    result = compute_amount(
        price=Decimal("20"),
        sales=100,
        comments=None,
        sales_raw="100",
        comments_raw=None,
        existing_flags=["评论缺失", "待核验", "评论数缺失-未校正"],
        settings=settings,
    )
    assert result.amount == Decimal("2000.00")
    assert result.formula == "20.00×100=2000.00"
    assert result.estimated_sales == 100
    assert COMMENTS_MISSING_FLAG not in result.flags
    # 旧标记一律收敛，不再出现「待核实」。
    assert "评论缺失" not in result.flags
    assert "待核验" not in result.flags
    assert "评论数缺失-未校正" not in result.flags


def test_all_three_tiers_and_missing_shop_only_in_links(settings, session_factory):
    with session_scope(session_factory) as session:
        items = [
            # 达标：剔除缺评论链接后仍有 600000 > 500000。
            make_item(
                product_id="T-DB-1",
                shop_id="T-DB",
                price=600,
                sales=1000,
                comments=None,
            ),
            make_item(
                product_id="T-DB-2", shop_id="T-DB", price=600, sales=1000, comments=100
            ),
            # 待核验：缺评论 400000 + 完整 200000 = 600000，剔除后只有 200000。
            make_item(
                product_id="T-VF-1",
                shop_id="T-VF",
                price=400,
                sales=1000,
                comments=None,
            ),
            make_item(
                product_id="T-VF-2", shop_id="T-VF", price=200, sales=1000, comments=100
            ),
            # 临界：完整评论 400000（60%–100%）。
            make_item(
                product_id="T-NR-1", shop_id="T-NR", price=400, sales=1000, comments=100
            ),
            # 店名为空：只进链接明细，不参与任何店铺合计。
            make_item(
                product_id="T-NS-1",
                shop_id=None,
                shop_name=None,
                price=600,
                sales=1000,
                comments=100,
            ),
        ]
        snapshots, _ = _ingest_and_score(session, settings, items, "tiers")
        aggregate_snapshots(
            session,
            settings=settings,
            snapshots=snapshots,
            snapshot_date=DAY,
            run_id="tiers",
        )

        dabao, dabao_row = _lead_and_tier(session, "T-DB")
        assert dabao.status == "待复核"
        assert dabao.total_amount == Decimal("1200000.00")
        assert dabao_row["tier"] == "达标"
        assert COMMENTS_MISSING_FLAG not in json.loads(dabao.missing_flags)

        verified, verified_row = _lead_and_tier(session, "T-VF")
        assert verified.total_amount == Decimal("600000.00")
        assert verified_row["tier"] == "达标"
        pending = (
            session.execute(
                text(
                    "SELECT amount, amount_formula FROM v_lead_links WHERE product_id='T-VF-1'"
                )
            )
            .mappings()
            .one()
        )
        assert pending["amount"] == 400000
        assert "待核验" not in pending["amount_formula"]

        near, near_row = _lead_and_tier(session, "T-NR")
        assert near.status == "待复核"
        assert near.total_amount == Decimal("400000.00")
        assert near_row["tier"] == "临界"

        # 店名为空不生成线索、不进汇总（无 shop_id 但有店名的新口径另测）。
        assert session.scalar(select(Lead).where(Lead.shop_name == "无ID店")) is None
        assert (
            session.execute(
                text("SELECT COUNT(*) FROM v_lead_summary WHERE shop_name = '无ID店'")
            ).scalar()
            == 0
        )

        # 但作为独立链接出现在 v_lead_links，lead_id 为空。
        standalone = (
            session.execute(
                text("SELECT * FROM v_lead_links WHERE product_id = 'T-NS-1'")
            )
            .mappings()
            .one()
        )
        assert standalone["lead_id"] is None
        assert "店铺名缺失" in standalone["missing_flags"]


def test_comments_complete_link_recovers_tier_to_dabiao(settings, session_factory):
    with session_scope(session_factory) as session:
        snapshots, _ = _ingest_and_score(
            session,
            settings,
            [
                make_item(
                    product_id="T-RC-1",
                    shop_id="T-RC",
                    price=600,
                    sales=1000,
                    comments=None,
                )
            ],
            "recover",
        )
        aggregate_snapshots(
            session,
            settings=settings,
            snapshots=snapshots,
            snapshot_date=DAY,
            run_id="recover",
        )
        initial, initial_row = _lead_and_tier(session, "T-RC")
        assert initial.total_amount == Decimal("600000.00")
        assert initial_row["tier"] == "达标"
        assert (
            session.execute(
                text("SELECT amount FROM v_lead_links WHERE product_id='T-RC-1'")
            ).scalar()
            == 600000
        )
        assert COMMENTS_MISSING_FLAG not in json.loads(snapshots[0].missing_flags)

        # 详情补抓补齐评论数后重算：标记消失，档位回到达标。
        snapshot = snapshots[0]
        snapshot.comments = 100
        snapshot.comments_raw = "100"
        snapshot.missing_flags = "[]"
        product = session.get(Product, snapshot.product_id)
        shop = session.scalar(
            select(Shop).where(
                Shop.platform == product.platform, Shop.shop_id == product.shop_id
            )
        )
        score_snapshot(
            session, settings=settings, snapshot=snapshot, product=product, shop=shop
        )
        aggregate_snapshots(
            session,
            settings=settings,
            snapshots=[snapshot],
            snapshot_date=DAY,
            run_id="recover",
        )
        lead, row = _lead_and_tier(session, "T-RC")
        assert row["tier"] == "达标"
        assert COMMENTS_MISSING_FLAG not in json.loads(lead.missing_flags)


def test_not_listed_link_only_in_links(settings, session_factory):
    with session_scope(session_factory) as session:
        snapshots, _ = _ingest_and_score(
            session,
            settings,
            [
                make_item(
                    product_id="T-OFF-1",
                    shop_id="T-OFF",
                    price=600,
                    sales=1000,
                    comments=100,
                    listed=False,
                )
            ],
            "offline",
        )
        aggregate_snapshots(
            session,
            settings=settings,
            snapshots=snapshots,
            snapshot_date=DAY,
            run_id="offline",
        )
        assert (
            session.execute(text("SELECT COUNT(*) FROM v_lead_summary")).scalar() == 0
        )
        row = (
            session.execute(
                text("SELECT * FROM v_lead_links WHERE product_id = 'T-OFF-1'")
            )
            .mappings()
            .one()
        )
        assert row["lead_id"] is None
        assert any("下架" in reason for reason in json.loads(snapshots[0].hit_reasons))


def test_jd_detail_never_fills_taobao_comments(settings, session_factory):
    with session_scope(session_factory) as session:
        snapshots, _ = _ingest_and_score(
            session,
            settings,
            [
                make_item(
                    product_id="T-JD-1",
                    shop_id="T-JD",
                    price=600,
                    sales=1000,
                    comments=None,
                )
            ],
            "no-cross-platform",
        )
        snapshot = snapshots[0]
        assert snapshot.comments is None
        assert COMMENTS_MISSING_FLAG not in json.loads(snapshot.missing_flags)

        changed = apply_jd_detail_patches(
            session,
            settings=settings,
            patches=[
                {"product_id": "T-JD-1", "comments": 9999, "comments_raw": "9999+"}
            ],
            target=JdDetailTarget(
                shop_id="T-JD", total_amount=Decimal("600000"), product_ids=("T-JD-1",)
            ),
            snapshot_date=DAY,
        )
        assert changed == 0
        assert snapshot.comments is None
        assert COMMENTS_MISSING_FLAG not in json.loads(snapshot.missing_flags)


def test_score_snapshot_backfills_missing_shop_name_flag(settings, session_factory):
    """即使采集端直接写表漏了 flag，判定阶段也要补「店铺名缺失」。"""

    with session_scope(session_factory) as session:
        snapshots, _ = _ingest_and_score(
            session,
            settings,
            [
                make_item(
                    product_id="T-NS-2",
                    shop_id=None,
                    shop_name=None,
                    price=600,
                    sales=1000,
                    comments=100,
                )
            ],
            "backfill-shop-name",
        )
        snapshot = snapshots[0]
        snapshot.missing_flags = "[]"
        product = session.get(Product, snapshot.product_id)
        score_snapshot(
            session, settings=settings, snapshot=snapshot, product=product, shop=None
        )
        assert "店铺名缺失" in json.loads(snapshot.missing_flags)
        assert "店铺ID缺失" not in json.loads(snapshot.missing_flags)
