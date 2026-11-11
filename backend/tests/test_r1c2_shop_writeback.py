"""11/6 b 轮复审返工：店铺 ID 缺失归一与详情补抓回写。"""

from __future__ import annotations

import json
import pytest
from datetime import date

from sqlalchemy import select

from pipeline.db import session_scope
from pipeline.models import Product, ProductSnapshot, Shop
from pipeline.store import normalize_shop_id, upsert_many
from pipeline.taobao_detail import (
    TaobaoDetailTarget,
    apply_taobao_detail_patches,
    select_taobao_detail_targets,
)

from tests.conftest import make_item

DAY = date(2026, 11, 6)


@pytest.mark.parametrize("platform", ["taobao", "jd"])
@pytest.mark.parametrize("shop_id", [None, "702188581"])
def test_detail_name_written_without_new_shop_id(
    settings, session_factory, platform, shop_id
):
    from pipeline.scoring import score_snapshot
    from pipeline.jd_detail import JdDetailTarget, apply_jd_detail_patches
    from decimal import Decimal

    with session_scope(session_factory) as session:
        snapshot = _ingest(
            session,
            settings,
            make_item(
                platform=platform,
                product_id="970000000009",
                shop_id=shop_id,
                shop_name=None,
                price=20,
                sales=100,
                comments=None,
            ),
        )
        product = session.get(Product, snapshot.product_id)
        score_snapshot(
            session, settings=settings, snapshot=snapshot, product=product, shop=None
        )
        if platform == "taobao":
            plan = select_taobao_detail_targets(
                session, settings=settings, snapshots=[snapshot]
            )
            assert len(plan.targets) == 1
        patch = {
            "platform": platform,
            "product_id": product.product_id,
            "shop_name": "详情证据店",
        }
        if platform == "taobao":
            apply_taobao_detail_patches(
                session, settings=settings, patches=[patch], snapshot_date=DAY
            )
        else:
            apply_jd_detail_patches(
                session,
                settings=settings,
                patches=[patch],
                snapshot_date=DAY,
                target=JdDetailTarget(
                    shop_id=shop_id or "",
                    total_amount=Decimal(0),
                    product_ids=(product.product_id,),
                ),
            )
        session.flush()
        session.expire(product)
        assert product.shop_name == "详情证据店"
        if shop_id:
            shop = session.scalar(
                select(Shop).where(Shop.platform == platform, Shop.shop_id == shop_id)
            )
            assert shop.shop_name == "详情证据店"


def _ingest(session, settings, item):
    snapshots, _ = upsert_many(
        session,
        platform=item["platform"],
        items=[item],
        settings=settings,
        run_id="r1c2-writeback",
        snapshot_date=DAY,
    )
    assert len(snapshots) == 1
    return snapshots[0]


def test_normalize_shop_id_treats_zero_and_placeholders_as_missing():
    for value in (None, "", "   ", "0", "00", "null", "None", "undefined", "-", "--"):
        assert normalize_shop_id(value) is None
    assert normalize_shop_id("702188581") == "702188581"


def test_upsert_zero_shop_id_is_missing(settings, session_factory):
    with session_scope(session_factory) as session:
        snapshot = _ingest(
            session,
            settings,
            make_item(product_id="970000000001", shop_id="0"),
        )
        product = session.get(Product, snapshot.product_id)
        assert product.shop_id is None
        assert product.shop_id_missing is True
        assert "店铺ID缺失" not in json.loads(snapshot.missing_flags)
        assert "店铺名缺失" not in json.loads(snapshot.missing_flags)
        assert session.scalar(select(Shop).where(Shop.shop_id == "0")) is None


def test_taobao_detail_backfills_product_shop_id(settings, session_factory):
    with session_scope(session_factory) as session:
        snapshot = _ingest(
            session,
            settings,
            make_item(product_id="970000000002", shop_id=None, comments=None),
        )
        product = session.get(Product, snapshot.product_id)
        assert product.shop_id is None
        assert "店铺ID缺失" not in json.loads(snapshot.missing_flags)

        changed = apply_taobao_detail_patches(
            session,
            settings=settings,
            patches=[
                {
                    "platform": "taobao",
                    "product_id": "970000000002",
                    "shop_id": "702188581",
                    "shop_name": "帕龙母婴旗舰店",
                    "comments": 12,
                    "comments_raw": "12",
                    "flags": ["店铺ID缺失"],
                }
            ],
            snapshot_date=DAY,
            targets=[TaobaoDetailTarget(platform="taobao", product_id="970000000002")],
        )
        assert changed == 1
        assert product.shop_id == "702188581"
        assert product.shop_id_missing is False
        shop = session.scalar(
            select(Shop).where(Shop.platform == "taobao", Shop.shop_id == "702188581")
        )
        assert shop is not None
        assert shop.shop_name == "帕龙母婴旗舰店"
        assert snapshot.comments == 12
        assert "店铺ID缺失" not in json.loads(snapshot.missing_flags)


def test_taobao_detail_zero_shop_id_does_not_override_product(
    settings, session_factory
):
    with session_scope(session_factory) as session:
        snapshot = _ingest(
            session,
            settings,
            make_item(product_id="970000000003", shop_id="916148540"),
        )
        product = session.get(Product, snapshot.product_id)
        apply_taobao_detail_patches(
            session,
            settings=settings,
            patches=[
                {
                    "platform": "taobao",
                    "product_id": "970000000003",
                    "shop_id": "0",
                    "shop_name": "0",
                }
            ],
            snapshot_date=DAY,
            targets=[TaobaoDetailTarget(platform="taobao", product_id="970000000003")],
        )
        assert product.shop_id == "916148540"
        assert product.shop_id_missing is False
