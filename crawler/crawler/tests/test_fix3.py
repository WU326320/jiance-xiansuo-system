"""11/6 c 轮遗留修复：1688 店名解码、run_logs、详情原始响应留存。"""

from __future__ import annotations

import json

from crawler.core.browser import CapturedResponse, PageResult, StaticBrowser
from crawler.core.jsonl import read_jsonl
from crawler.core.stats import RunStats
from crawler.platforms import create_platform
from crawler.platforms.ali1688 import decode_shop_name
from crawler.runner import run_platform


def _platform(name, config):
    return create_platform(name, config, StaticBrowser([]), stats=RunStats())


def test_1688_shop_name_url_decoded_before_output():
    assert (
        decode_shop_name(
            "%E7%88%86%E4%B9%90%E5%88%9B%E6%84%8F%E7%8E%A9%E5%85%B7%E5%BA%97"
        )
        == "爆乐创意玩具店"
    )
    assert decode_shop_name("普通店铺") == "普通店铺"
    assert decode_shop_name("  %E5%A5%A5%E7%89%B9%E6%9B%BC  ") == "奥特曼"


def test_disabled_platform_still_writes_one_run_log_row(make_config, tmp_path):
    config = make_config(platforms={"jd": {"enabled": False}})
    record = run_platform("jd", config, date="2026-11-06")
    assert record.status == "disabled"
    path = tmp_path / "out" / "2026-11-06" / "run_logs.jsonl"
    rows = list(read_jsonl(path))
    assert len(rows) == 1
    row = rows[0]
    assert row["platform"] == "jd"
    assert row["status"] == "disabled"
    assert row["keywords"] == []
    assert row["pages_fetched"] == 0
    assert row["items_ok"] == 0
    assert row["login_ok"] is False
    assert row["risk_blocked"] is False
    assert row["started_at"] and row["ended_at"]


def test_detail_debug_payload_saves_html_and_captured_json(make_config, tmp_path):
    config = make_config()
    platform = _platform("taobao", config)
    result = PageResult(
        url="https://detail.tmall.com/item.htm?id=1",
        html="<html>detail</html>",
        captured=[
            CapturedResponse(
                url="https://h5api.m.taobao.com/h5/mtop.rate/1.0/",
                text='{"data":{"rateInfo":{"commentCount":"1.2万+"}}}',
                content_type="application/json",
                status=200,
            )
        ],
    )
    paths = platform.save_debug_payloads(result, "test", product_id="tmall:1")
    html_path = tmp_path / paths["html"]
    json_path = tmp_path / paths["json"]
    assert html_path.read_text(encoding="utf-8") == "<html>detail</html>"
    payloads = json.loads(json_path.read_text(encoding="utf-8"))
    assert payloads[0]["url"].endswith("/mtop.rate/1.0/")
    assert "commentCount" in payloads[0]["text"]


def test_page_wait_range_is_random_and_within_bounds(make_config):
    config = make_config(crawl={"page_wait_ms_range": [1500, 4000]})
    values = [config.crawl.wait_ms() for _ in range(30)]
    assert all(1500 <= value <= 4000 for value in values)
    assert len(set(values)) > 1


def test_taobao_segment_limits_default_to_unlimited(make_config):
    config = make_config()
    batch = config.crawl.taobao.batch
    assert batch.segments_per_day == 0
    assert batch.max_pages_per_segment == 0
    assert batch.max_items_per_segment == 0
