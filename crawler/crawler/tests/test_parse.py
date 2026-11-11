from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from crawler.core.parse import (
    clean_text,
    clean_title,
    dedupe_items,
    extract_product_id,
    extract_shop_id,
    gbk_quote,
    is_lower_bound,
    keyword_hit_ratio,
    normalize_image_url,
    normalize_product_url,
    parse_fuzzy_int,
    parse_price,
    safe_filename,
)
from crawler.models import Item


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1万+", 10000),
        ("2.5万+", 25000),
        ("300+", 300),
        ("10万+人付款", 100000),
        ("已售1000+件", 1000),
        ("100+条评价", 100),
        ("1,234", 1234),
        ("暂无", None),
        ("", None),
        (None, None),
        (1234, 1234),
    ],
)
def test_parse_fuzzy_int(raw, expected):
    assert parse_fuzzy_int(raw) == expected


def test_parse_fuzzy_int_float_and_decimal():
    assert parse_fuzzy_int(12.9) is None
    assert parse_fuzzy_int(float("nan")) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("29.90", Decimal("29.90")),
        ("¥ 29.90", Decimal("29.90")),
        ("￥1,299.00", Decimal("1299.00")),
        ("29.90-39.90", Decimal("29.90")),
        ("39.90~29.90", Decimal("29.90")),
        ("面议", None),
        ("询价", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_price(raw, expected):
    value = parse_price(raw)
    if expected is None:
        assert value is None
    else:
        assert value == expected


def test_lower_bound_hint():
    assert is_lower_bound("1万+")
    assert is_lower_bound("2.5万")
    assert not is_lower_bound("300")
    assert not is_lower_bound(None)


def test_product_id_and_url_normalization():
    assert (
        extract_product_id(
            "taobao", "https://item.taobao.com/item.htm?id=123456789&spm=x"
        )
        == "123456789"
    )
    assert (
        extract_product_id("tmall", "https://detail.tmall.com/item.htm?id=123456789")
        == "123456789"
    )
    assert (
        extract_product_id("jd", "https://item.jd.com/100012043978.html")
        == "100012043978"
    )
    assert (
        extract_product_id("1688", "https://detail.1688.com/offer/612345678901.html")
        == "612345678901"
    )
    assert extract_product_id("jd", "https://example.com/not-a-product") is None
    assert normalize_product_url(
        "taobao", "123456789", "//item.taobao.com/item.htm?id=123456789&spm=a"
    ) == ("https://item.taobao.com/item.htm?id=123456789")
    assert normalize_product_url(
        "tmall", "123456789", "//item.taobao.com/item.htm?id=123456789"
    ) == ("https://detail.tmall.com/item.htm?id=123456789")
    assert (
        normalize_product_url("jd", "100012043978")
        == "https://item.jd.com/100012043978.html"
    )
    assert (
        normalize_product_url("1688", "612345678901")
        == "https://detail.1688.com/offer/612345678901.html"
    )


def test_shop_id_extraction():
    assert extract_shop_id("jd", "https://mall.jd.com/index-3001.html") == "3001"
    assert extract_shop_id("jd", "https://shop.jd.com?shopId=3002") == "3002"
    assert extract_shop_id("1688", "https://shop6001.1688.com") == "6001"
    assert (
        extract_shop_id("1688", "https://shop0c1039px78200.1688.com") == "0c1039px78200"
    )
    assert extract_shop_id("1688", "https://x.1688.com?memberId=6002") == "6002"
    assert (
        extract_shop_id("1688", "member_id@b2b-2221270032164774cc")
        == "b2b-2221270032164774cc"
    )
    assert extract_shop_id("taobao", "https://shop.taobao.com?sellerId=2001") == "2001"
    assert extract_shop_id("taobao", "https://shop2001.taobao.com") == "2001"
    assert extract_shop_id("taobao", "https://shop2002.tmall.com") == "2002"
    assert extract_shop_id("taobao", "https://shop.taobao.com") is None


def test_gbk_quote_and_invalid_char():
    assert gbk_quote("葫芦娃") == "%BA%F9%C2%AB%CD%DE"
    with pytest.raises(Exception):
        gbk_quote("🦄")
    with pytest.raises(Exception):
        gbk_quote("  ")


def test_keyword_hit_ratio_and_safe_filename():
    assert keyword_hit_ratio("葫芦", ["葫芦兄弟", "葫芦娃", None]) == 1.0
    assert keyword_hit_ratio("葫芦", ["叶罗丽", "黑猫警长"]) == 0.0
    assert keyword_hit_ratio("", []) == 0.0
    assert safe_filename("../../商品 A") == ".._.._A" or safe_filename("../../商品 A")


def test_clean_text_removes_highlight_tags_without_merging_spaces():
    assert clean_title("<span>葫芦</span><span>兄弟</span> 印花") == "葫芦兄弟 印花"
    assert clean_title("葫芦&nbsp;兄弟") == "葫芦 兄弟"
    assert clean_title("尺寸 1 < 2 > 0") == "尺寸 1 < 2 > 0"
    assert (
        clean_text("https://example.test/?x=1&not=2")
        == "https://example.test/?x=1&not=2"
    )


def _item(pid: str, **overrides) -> Item:
    data = dict(
        platform="taobao",
        product_id=pid,
        url=f"https://item.taobao.com/item.htm?id={pid}",
        title="葫芦兄弟",
        search_keyword="葫芦兄弟",
        crawled_at=datetime(2026, 11, 5, tzinfo=timezone.utc),
    )
    data.update(overrides)
    return Item.model_validate(data)


def test_dedupe_items_merges_non_null_fields_and_flags():
    first = _item("123456789", shop_id=None, sales=None, flags=["销量缺失"])
    second = _item(
        "123456789",
        shop_id="2001",
        sales=1000,
        price=Decimal("9.90"),
        flags=["按下限估算"],
    )
    merged = dedupe_items([first, second])
    assert len(merged) == 1
    assert merged[0].shop_id == "2001"
    assert merged[0].sales == 1000
    assert merged[0].price == Decimal("9.90")
    assert set(merged[0].flags) == {"销量缺失", "按下限估算"}


def test_dedupe_items_keeps_platform_scope():
    assert (
        len(dedupe_items([_item("123456789"), _item("123456789", platform="tmall")]))
        == 2
    )


@pytest.mark.parametrize("valid_first", [False, True])
@pytest.mark.parametrize(
    ("field", "invalid_raw", "valid_raw", "value"),
    [
        ("sales", "本月行业热销", "800+人付款", 800),
        ("sales", "本月行业热销", None, 800),
        ("sales", "本月行业热销", "已售0件", 0),
        ("sales", "本月行业热销", None, 0),
        ("price", "满100减20", "¥49.00", Decimal("49.00")),
        ("price", "面议", None, Decimal("49.00")),
        ("comments", "好评率99%", "60条评价", 60),
        ("comments", "好评率99%", None, 60),
    ],
)
def test_dedupe_items_keeps_commerce_value_with_its_source(
    field, invalid_raw, valid_raw, value, valid_first
):
    raw_field = field + "_raw"
    invalid = _item("1085008955004", **{raw_field: invalid_raw})
    valid = _item("1085008955004", **{field: value, raw_field: valid_raw})
    original = [invalid.model_dump(), valid.model_dump()]

    merged = dedupe_items([valid, invalid] if valid_first else [invalid, valid])[0]

    assert getattr(merged, field) == value
    assert getattr(merged, raw_field) == valid_raw
    saved = Item.model_validate(merged.model_dump())
    assert getattr(saved, field) == value
    assert [invalid.model_dump(), valid.model_dump()] == original


@pytest.mark.parametrize(
    ("field", "first_value", "later_value", "later_raw"),
    [
        ("sales", 0, 800, "800+人付款"),
        ("comments", 0, 60, "60条评价"),
        ("price", Decimal("20.00"), Decimal("49.00"), "¥49.00"),
    ],
)
def test_dedupe_items_keeps_first_valid_commerce_pair(
    field, first_value, later_value, later_raw
):
    first = _item("1085008955004", **{field: first_value})
    second = _item("1085008955004", **{field: later_value, field + "_raw": later_raw})

    merged = dedupe_items([first, second])[0]

    assert getattr(merged, field) == first_value
    assert getattr(merged, field + "_raw") is None
    assert getattr(Item.model_validate(merged.model_dump()), field) == first_value


@pytest.mark.parametrize(
    ("field", "raw"),
    [("sales", "本月行业热销"), ("comments", "好评率99%"), ("price", "面议")],
)
def test_dedupe_items_preserves_unparseable_raw_without_inventing_value(field, raw):
    empty = _item("1085008955004")
    invalid = _item("1085008955004", **{field + "_raw": raw})

    merged = dedupe_items([empty, invalid])[0]

    assert getattr(merged, field) is None
    assert getattr(merged, field + "_raw") == raw
    assert getattr(Item.model_validate(merged.model_dump()), field) is None


def test_normalize_image_url():
    assert (
        normalize_image_url("//img.alicdn.com/a.jpg") == "https://img.alicdn.com/a.jpg"
    )
    assert normalize_image_url("https://img.jd.com/a.jpg") == "https://img.jd.com/a.jpg"
    assert normalize_image_url("data:image/png;base64,x") is None
    assert normalize_image_url(None) is None


def test_parse_json_text_supports_jsonp_and_xssi_prefix():
    from crawler.core.jsonutil import parse_json_text

    assert parse_json_text('mtopjsonp1({"a": 1})') == {"a": 1}
    assert parse_json_text(')]}\'\n[{"a": 1}]') == [{"a": 1}]
    assert parse_json_text("<html>{broken</html>") is None


def test_extract_embedded_json_from_script_assignments():
    from crawler.core.jsonutil import extract_embedded_json

    html = (
        "<html><body>"
        '<script>window.__INIT_DATA__ = {"data":{"offerList":[{"offerId":"1"}]}};</script>'
        '<script type="application/json" id="__NEXT_DATA__">{"props":{"x":1}}</script>'
        "<script>window._offerList = JSON.parse('{\"z\": 2}');</script>"
        "</body></html>"
    )
    payloads = extract_embedded_json(html)
    assert {"data": {"offerList": [{"offerId": "1"}]}} in payloads
    assert {"props": {"x": 1}} in payloads
    assert {"z": 2} in payloads

    import json
    from urllib.parse import quote

    encoded = quote(json.dumps({"data": {"offerList": [{"offerId": "2"}]}}), safe="")
    decoded_html = f'<script>window.__INIT_DATA__ = JSON.parse(decodeURIComponent("{encoded}"));</script>'
    assert {"data": {"offerList": [{"offerId": "2"}]}} in extract_embedded_json(
        decoded_html
    )
