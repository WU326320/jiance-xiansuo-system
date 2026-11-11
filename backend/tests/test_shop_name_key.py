from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

from sqlalchemy import select, text

from pipeline.aggregate import SAME_NAME_FLAG, aggregate_snapshots
from pipeline.models import Lead, Product, Whitelist
from pipeline.scoring import is_whitelisted, score_snapshot
from pipeline.shopkey import decode_shop_name, normalize_shop_name, platform_group, shop_key
from pipeline.store import upsert_many
from pipeline.whitelist import parse_whitelist_text

from .conftest import make_item
from pipeline.db import session_scope

DAY = date(2026, 11, 6)


def _ingest_score(session, settings, items, run_id="t"):
    snapshots, _ = upsert_many(
        session,
        platform="taobao",
        items=items,
        settings=settings,
        run_id=run_id,
        snapshot_date=DAY,
    )
    for snapshot in snapshots:
        product = session.get(Product, snapshot.product_id)
        score_snapshot(session, settings=settings, snapshot=snapshot, product=product, shop=None)
    return snapshots


def _view_rows(session, view):
    result = session.execute(text(f"SELECT * FROM {view}"))
    return [dict(zip(result.keys(), row)) for row in result.all()]


def test_normalize_and_platform_group_rules():
    assert platform_group("tmall") == "taobao"
    assert platform_group("jd") == "jd"
    assert normalize_shop_name(" 活石(LIVING  STONES)玩具旗舰店 ") == "活石(livingstones)玩具旗舰店"
    assert normalize_shop_name("ＡＢＣ旗舰店") == "abc旗舰店"
    # 不删除旗舰店/括号内容。
    assert shop_key("tmall", "ABC旗舰店") == "taobao|abc旗舰店"


def test_b4_taobao_tmall_same_name_without_shop_id_merges(settings, session_factory):
    with session_scope(session_factory) as session:
        items = [
            make_item(platform="taobao", product_id="B4-1", shop_id=None, shop_name="同名店", price=300, sales=1000, comments=100),
            make_item(platform="tmall", product_id="B4-2", shop_id=None, shop_name="同名店", price=300, sales=1000, comments=100),
        ]
        snapshots = _ingest_score(session, settings, items, "b4-merge")
        aggregate_snapshots(session, settings=settings, snapshots=snapshots, snapshot_date=DAY, run_id="b4-merge")
        rows = _view_rows(session, "v_lead_summary")
        assert len(rows) == 1
        assert rows[0]["platform_group"] == "taobao"
        assert rows[0]["shop_name"] == "同名店"
        assert rows[0]["shop_amount"] == 600000.0
        assert rows[0]["link_count"] == 2


def test_b4_same_name_different_shop_id_splits_and_marks(settings, session_factory):
    with session_scope(session_factory) as session:
        items = [
            make_item(product_id="B4S-1", shop_id="S-A", shop_name="同名店", price=600, sales=1000, comments=100),
            make_item(product_id="B4S-2", shop_id="S-B", shop_name="同名店", price=600, sales=1000, comments=100),
        ]
        snapshots = _ingest_score(session, settings, items, "b4-split")
        aggregate_snapshots(session, settings=settings, snapshots=snapshots, snapshot_date=DAY, run_id="b4-split")
        leads = list(session.scalars(select(Lead).where(Lead.shop_name == "同名店")).all())
        assert {lead.shop_id for lead in leads} == {"S-A", "S-B"}
        assert all(SAME_NAME_FLAG in json.loads(lead.missing_flags) for lead in leads)
        assert all(lead.shop_key == "taobao|同名店" for lead in leads)


def test_b5_missing_shop_id_aggregates_by_name_but_empty_name_does_not(settings, session_factory):
    with session_scope(session_factory) as session:
        items = [
            make_item(product_id="B5-ID", shop_id=None, shop_name="无ID店", price=600, sales=1000, comments=100),
            make_item(product_id="B5-NAME", shop_id="S-NAME", shop_name=None, price=600, sales=1000, comments=100),
        ]
        snapshots = _ingest_score(session, settings, items, "b5")
        aggregate_snapshots(session, settings=settings, snapshots=snapshots, snapshot_date=DAY, run_id="b5")
        leads = list(session.scalars(select(Lead)).all())
        assert [lead.shop_name for lead in leads] == ["无ID店"]
        assert leads[0].shop_id == ""

        summary = _view_rows(session, "v_lead_summary")
        assert {row["shop_name"] for row in summary} == {"无ID店"}
        links = _view_rows(session, "v_lead_links")
        missing = next(row for row in links if row["product_id"] == "B5-NAME")
        assert missing["lead_id"] is None
        assert "店铺名缺失" in missing["missing_flags"]
        assert "店铺ID缺失" not in missing["missing_flags"]


def test_whitelist_name_key_group_and_shop_id_rule(settings, session_factory):
    with session_scope(session_factory) as session:
        session.add_all(
            [
                Whitelist(
                    platform="taobao",
                    shop_id="name:同名店",
                    shop_name="同名店",
                    shop_name_norm="同名店",
                    ip_code="奥特曼",
                ),
                Whitelist(
                    platform="jd",
                    shop_id="1001",
                    shop_name="有ID店",
                    shop_name_norm="有id店",
                    ip_code="奥特曼",
                ),
            ]
        )
        session.flush()
        assert is_whitelisted(session, platform="tmall", shop_name=" 同名店 ", shop_id=None, ip_code="奥特曼") is True
        assert is_whitelisted(session, platform="taobao", shop_name="同名店", shop_id="ANY", ip_code="奥特曼") is True
        assert is_whitelisted(session, platform="jd", shop_name="有ID店", shop_id="1001", ip_code="奥特曼") is True
        assert is_whitelisted(session, platform="jd", shop_name="有ID店", shop_id="2002", ip_code="奥特曼") is False
        assert is_whitelisted(session, platform="jd", shop_name="有ID店", shop_id="1001", ip_code="迪迦") is False


def test_parse_whitelist_text_decodes_1688_and_ignores_comments():
    entries = parse_whitelist_text(
        "# 注释\n1688,%E7%88%86%E4%B9%90%E5%88%9B%E6%84%8F%E7%8E%A9%E5%85%B7%E5%BA%97\n"
    )
    assert len(entries) == 1
    assert entries[0].platform_group == "1688"
    assert entries[0].shop_name == "爆乐创意玩具店"
    assert entries[0].shop_id == "name:爆乐创意玩具店"


def test_backend_1688_decode_strips_source_prefix_without_hurting_english_names():
    raw = (
        "f%E7%81%AB%E6%98%9F%E4%BA%BA%E7%BD%91%E7%BB%9C%E7%A7%91%E6%8A%80"
        "%E6%9C%89%E9%99%90%E5%85%AC%E5%8F%B8"
    )
    assert decode_shop_name(raw) == "火星人网络科技有限公司"
    assert decode_shop_name("f " + raw[1:]) == "火星人网络科技有限公司"
    assert decode_shop_name("%66" + raw[1:]) == "火星人网络科技有限公司"
    assert decode_shop_name("forescoco旗舰店") == "forescoco旗舰店"
    assert decode_shop_name("%E7%88%86%E4%B9%90%E5%88%9B%E6%84%8F%E7%8E%A9%E5%85%B7%E5%BA%97") == "爆乐创意玩具店"
