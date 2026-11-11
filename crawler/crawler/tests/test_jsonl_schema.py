from __future__ import annotations

import json
import os
import pytest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from crawler.core.jsonl import JsonlWriter, read_jsonl, write_run_record
from crawler.core.schema import apply_patch, enrich_flags, validate_item
from crawler.models import (
    Item,
    ItemPatch,
    RunRecord,
    item_json_dict,
    run_record_json_dict,
)


def _item(**overrides) -> Item:
    data = dict(
        platform="taobao",
        product_id="123456789",
        url="https://item.taobao.com/item.htm?id=123456789",
        title="葫芦兄弟 印花卫衣",
        search_keyword="葫芦兄弟",
        ip_code="huluxiongdi",
        crawled_at=datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc),
        price=Decimal("59.90"),
        main_image_url="https://img.alicdn.com/a.jpg",
        shop_id="2001",
        shop_name="葫芦旗舰店",
        sales_raw="1万+",
        sales=10000,
        comments_raw="1200",
        comments=1200,
    )
    data.update(overrides)
    for field in ("sales", "comments"):
        if field in overrides and field + "_raw" not in overrides:
            value = overrides[field]
            data[field + "_raw"] = None if value is None else str(value)
    return Item(**data)


def test_item_json_fields_order_and_nulls():
    item = _item(shop_id=None, price=None, sales=None, comments=None)
    data = item_json_dict(item)
    assert list(data)[:6] == [
        "platform",
        "product_id",
        "url",
        "title",
        "shop_id",
        "shop_name",
    ]
    assert data["shop_id"] is None
    assert data["price"] is None  # 缺失为 null，不是 0
    assert data["sales"] is None
    assert data["crawled_at"].startswith("2026-11-05T12:00:00")
    assert data["flags"] == []


@pytest.mark.parametrize("price", [-1, "NaN", "未知价格", True])
def test_invalid_price_preserves_original_and_pending_reason(price):
    for item in (
        _item(price=price),
        ItemPatch(platform="jd", product_id="123456789", price=price),
    ):
        assert item.price is None
        assert item.price_raw == str(price)
        assert "价格无法识别,待核验" in item.flags


def test_enrich_flags_missing_and_lower_bound():
    item = _item(
        shop_id=None, price=None, main_image_url=None, sales_raw="1万+", comments=None
    )
    enriched = enrich_flags(item)
    assert "店铺ID缺失" not in enriched.flags
    assert "价格缺失" in enriched.flags
    assert "主图缺失" in enriched.flags
    assert "销量缺失" not in enriched.flags  # sales 有值
    assert "评论缺失" in enriched.flags
    assert "按下限估算" in enriched.flags


def test_enrich_flags_clears_stale_missing_flags_when_fields_present():
    """11/5 京东 bug：字段有值却被上游旧 flag 标成缺失。"""
    item = _item(
        shop_id="3001",
        sales_raw="10万+",
        sales=100000,
        main_image_url="https://img14.360buyimg.com/n7/a.jpg",
        comments_raw="2万+条评价",
        comments=20000,
        flags=["店铺ID缺失", "销量缺失", "主图缺失", "评论缺失", "按下限估算"],
    )
    enriched = enrich_flags(item)
    assert "店铺ID缺失" not in enriched.flags
    assert "销量缺失" not in enriched.flags
    assert "主图缺失" not in enriched.flags
    assert "评论缺失" not in enriched.flags
    assert "按下限估算" in enriched.flags


def test_enrich_flags_recomputes_after_patch_fills_fields():
    item = _item(
        platform="jd",
        shop_id=None,
        sales=None,
        sales_raw=None,
        comments=None,
        comments_raw=None,
        main_image_url=None,
        price=None,
        flags=["店铺ID缺失", "销量缺失", "评论缺失", "主图缺失", "价格缺失"],
    )
    patch = ItemPatch(
        platform="jd",
        product_id=item.product_id,
        shop_id="3001",
        sales_raw="5000+",
        sales=5000,
        comments_raw="800+条评价",
        comments=800,
        main_image_url="https://img14.360buyimg.com/n7/a.jpg",
        price=Decimal("99.00"),
        listed=True,
    )
    merged = apply_patch(item, patch)
    assert merged.shop_id == "3001"
    assert merged.sales == 5000
    assert merged.comments == 800
    assert merged.main_image_url
    assert merged.price == Decimal("99.00")
    assert merged.flags == ["按下限估算"]


def test_validate_drops_bad_product_id_but_keeps_missing_price():
    bad = _item(product_id="abc")
    valid, reason = validate_item(bad)
    assert valid is None and reason == "product_id解析失败"
    ok, reason = validate_item(_item(price=None))
    assert ok is not None and reason is None
    assert "价格缺失" in ok.flags


def test_apply_patch_merges_non_null_fields():
    item = _item(comments=None, shop_id=None)
    patch = ItemPatch(
        platform="taobao",
        product_id=item.product_id,
        comments_raw="100+条评价",
        comments=100,
        shop_id="2001",
        shop_name=None,
        listed=True,
    )
    merged = apply_patch(item, patch)
    assert merged.comments == 100
    assert merged.comments_raw == "100+条评价"
    assert merged.shop_id == "2001"
    assert "按下限估算" in merged.flags


def test_jsonl_write_dedupe_and_append(tmp_path):
    path = tmp_path / "taobao.jsonl"
    writer = JsonlWriter(path)
    assert writer.write(_item())
    assert not writer.write(_item(title="重复"))
    writer.close()
    rows = read_jsonl(path)
    assert len(rows) == 1
    assert rows[0]["title"] == "葫芦兄弟 印花卫衣"
    assert (
        json.loads(path.read_text(encoding="utf-8").splitlines()[0])["platform"]
        == "taobao"
    )

    writer2 = JsonlWriter(path)  # 重新运行也不重复
    assert not writer2.write(_item())
    writer2.write(_item(product_id="987654321"))
    writer2.close()
    assert len(read_jsonl(path)) == 2


def test_write_run_record_and_run_logs(tmp_path):
    started = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
    record = RunRecord(
        platform="taobao",
        run_id="20261105_120000_abcdef",
        started_at=started,
        ended_at=started,
        login_ok=True,
        risk_blocked=False,
        detail_status="skipped",
        keywords_done=1,
        pages_fetched=2,
        items_ok=3,
        drop_reasons={"product_id解析失败": 1},
        errors=["x"],
        config_version="v1",
        caliber_version="v1.0",
    )
    path = write_run_record(tmp_path, record)
    assert path.exists()
    assert (tmp_path / "run_logs.jsonl").exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["platform"] == "taobao"
    assert data["run_id"] == "20261105_120000_abcdef"
    assert data["detail_status"] == "skipped"
    assert data["drop_reasons"] == {"product_id解析失败": 1}
    log_row = json.loads(
        (tmp_path / "run_logs.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )
    assert log_row["run_id"] == data["run_id"]
    assert log_row["detail_status"] == "skipped"
    assert run_record_json_dict(record)["started_at"].startswith("2026-11-05T12:00:00")


@pytest.mark.parametrize("winerror", [5, 32, 33])
def test_sync_rows_recovers_temporary_windows_replace_failure(
    tmp_path, monkeypatch, winerror
):
    from crawler.core import jsonl

    path = tmp_path / "items.jsonl"
    replace = jsonl.os.replace
    calls = []

    def temporarily_busy(source, target):
        calls.append(source)
        if len(calls) < 3:
            error = PermissionError("temporary Windows access conflict")
            error.winerror = winerror
            raise error
        return replace(source, target)

    with JsonlWriter(path) as writer:
        writer.write(_item())
        monkeypatch.setattr(jsonl.os, "replace", temporarily_busy)
        writer.sync_rows([item_json_dict(_item(title="补齐标题"))])
        assert list(read_jsonl(path))[0]["title"] == "补齐标题"
        assert not writer.write(_item())
        assert writer.write(_item(product_id="987654321"))
    assert len(calls) == 3
    assert len(list(read_jsonl(path))) == 2


@pytest.mark.parametrize(
    "winerror, expected_calls", [(5, 5), (32, 5), (33, 5), (None, 1)]
)
def test_sync_rows_failed_replace_preserves_old_data(
    tmp_path, monkeypatch, winerror, expected_calls
):
    from crawler.core import jsonl

    path = tmp_path / "items.jsonl"
    calls = []
    error = PermissionError("persistent access failure")
    if winerror is not None:
        error.winerror = winerror

    def denied(source, target):
        calls.append(source)
        raise error

    with JsonlWriter(path) as writer:
        writer.write(_item())
        before = path.read_bytes()
        monkeypatch.setattr(jsonl.os, "replace", denied)
        with pytest.raises(PermissionError) as caught:
            writer.sync_rows([item_json_dict(_item(product_id="987654321"))])
        assert caught.value is error
        assert path.read_bytes() == before
        assert writer.write(_item(product_id="987654321"))
    assert len(calls) == expected_calls
    assert len(list(read_jsonl(path))) == 2
    assert not list(tmp_path.glob(".items.jsonl.*"))


@pytest.mark.skipif(os.name != "nt", reason="验证 Windows 文件句柄共享语义")
def test_sync_rows_recovers_after_real_windows_reader_releases(tmp_path, monkeypatch):
    from crawler.core import jsonl

    path = tmp_path / "items.jsonl"
    replace = jsonl.os.replace
    conflicts = []
    with JsonlWriter(path) as writer:
        writer.write(_item())
        with path.open("rb") as reader:

            def release_after_conflict(source, target):
                try:
                    return replace(source, target)
                except OSError as exc:
                    conflicts.append(exc.winerror)
                    reader.close()
                    raise

            monkeypatch.setattr(jsonl.os, "replace", release_after_conflict)
            writer.sync_rows([item_json_dict(_item(title="真实句柄释放后补齐"))])
        assert list(read_jsonl(path))[0]["title"] == "真实句柄释放后补齐"
    assert len(conflicts) == 1
    assert conflicts[0] in (5, 32, 33)
