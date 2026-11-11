from __future__ import annotations

from crawler.core.dom import (
    all_texts,
    attr_from_node,
    card_nodes,
    first_attr,
    first_text,
    has_text,
    href_of,
    parse_html,
)
from crawler.core.jsonutil import deep_find, extract_records, parse_json_text, payload_has_risk
from crawler.core.stats import RunStats
from crawler.platforms.helpers import join_labels, pick, pick_int, pick_price, pick_text, text_list

HTML = """
<html><body>
<div class="list">
  <div class="card" data-id="1"><a class="t" href="/a">标题A</a><span class="p">¥ 9.90</span></div>
  <div class="card" data-id="2"><a class="t" href="/b">标题B</a><span class="p">19.90</span></div>
</div>
</body></html>
"""


def test_dom_helpers():
    assert parse_html(None) is None
    assert parse_html("") is None
    root = parse_html(HTML)
    assert first_text(root, [".missing", ".t"]) == "标题A"
    assert first_attr(root, [".t"], ["href"]) == "/a"
    assert href_of(root, [".t"]) == "/a"
    assert all_texts(root, [".p"]) == ["¥ 9.90", "19.90"]
    cards = card_nodes(root, [".missing", ".card"])
    assert len(cards) == 2
    assert attr_from_node(cards[0], ["data-id"]) == "1"
    assert has_text(root, ["标题B"]) is True


def test_json_helpers():
    assert parse_json_text("not-json") is None
    assert parse_json_text('{"a":1}') == {"a": 1}
    payload = {"data": {"list": [{"id": "1", "title": "x"}, {"id": "1", "title": "dup"}, {"id": "2", "title": "y"}]}}
    rows = extract_records([payload], list_keys=("list",), id_keys=("id",))
    assert len(rows) == 2
    assert deep_find(payload, ("title",)) == "x"
    assert deep_find({"a": [{"b": 2}]}, ("b",)) == 2
    assert payload_has_risk({"x": {"captcha": True}}) == (True, "captcha=True")


def test_platform_helpers():
    record = {"Title": "  hello  ", "PriceRange": {"min": "3.5", "max": "5.2"}, "tags": ["a", {"text": "b"}], "n": "2.5万+"}
    assert pick(record, ["title"]) == "  hello  "
    assert pick_text(record, ["title"]) == "hello"
    assert pick_price(record, ["pricerange"]) is not None
    assert pick_int(record, ["n"]) == 25000
    assert text_list(record["tags"]) == ["a", "b"]
    assert join_labels(["a"], record["tags"]) == "a / b"


def test_stats_merge_and_helpers():
    first = RunStats()
    first.page_fetched()
    first.drop("product_id解析失败", "x")
    first.warning("w")
    second = RunStats(items_duplicate=2)
    first.merge(second)
    assert first.pages_fetched == 1
    assert first.items_dropped == 1
    assert first.items_duplicate == 2
    assert first.drop_reasons["product_id解析失败"] == 1
    assert first.warnings == ["w"]
    first.error("e")
    assert first.errors[-1] == "e"
