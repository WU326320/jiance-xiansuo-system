"""11/6 第二轮小修：1688 店名前缀、候选补抓、候选店名非空率。"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from crawler.core.browser import PageResult, StaticBrowser
from crawler.core.parse import now_local
from crawler.core.stats import RunStats
from crawler.models import Item
from crawler.platforms import create_platform
from crawler.platforms.ali1688 import decode_shop_name

_MODULE_PATH = Path(__file__).resolve().parents[2] / "tools" / "trial_metrics.py"
_SPEC = importlib.util.spec_from_file_location("trial_metrics_r2a", _MODULE_PATH)
assert _SPEC and _SPEC.loader
trial_metrics = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(trial_metrics)

_HUOXINGREN = (
    "f%E7%81%AB%E6%98%9F%E4%BA%BA%E7%BD%91%E7%BB%9C%E7%A7%91%E6%8A%80"
    "%E6%9C%89%E9%99%90%E5%85%AC%E5%8F%B8"
)


def test_decode_shop_name_strips_source_ascii_prefix_but_keeps_english_names():
    assert decode_shop_name(_HUOXINGREN) == "火星人网络科技有限公司"
    # 图标与百分号串之间有空格时同样处理。
    assert decode_shop_name("f " + _HUOXINGREN[1:]) == "火星人网络科技有限公司"
    # 双层编码同样处理。
    assert decode_shop_name("%66" + _HUOXINGREN[1:]) == "火星人网络科技有限公司"
    # 正常英文开头店名不能被误伤。
    assert decode_shop_name("forescoco旗舰店") == "forescoco旗舰店"
    assert decode_shop_name("f火星人网络科技有限公司") == "f火星人网络科技有限公司"
    # 普通百分号编码不受影响。
    assert (
        decode_shop_name(
            "%E7%88%86%E4%B9%90%E5%88%9B%E6%84%8F%E7%8E%A9%E5%85%B7%E5%BA%97"
        )
        == "爆乐创意玩具店"
    )


def _item(
    pid: str, title: str, shop_name: str | None = None, shop_id: str = "2y9x5s4346283"
) -> Item:
    return Item(
        platform="1688",
        product_id=pid,
        url=f"https://detail.1688.com/offer/{pid}.html",
        title=title,
        shop_id=shop_id,
        shop_name=shop_name,
        search_keyword="奥特曼",
        crawled_at=now_local(),
    )


def _detail_page(pid: str) -> PageResult:
    return PageResult(
        url=f"https://detail.1688.com/offer/{pid}.html",
        html=(
            "<html><body>"
            "<div class='title-text'>奥特曼 玩具</div>"
            "<div class='price-text'>1.00</div>"
            f"<div class='company-name'>{_HUOXINGREN}</div>"
            "<div class='sale-quantity'>已售 10 件</div>"
            "<img class='main-image' src='https://img.1688.com/a.jpg'>"
            "</body></html>"
        ),
    )


def test_1688_backfills_missing_shop_name_for_keyword_hit(make_config):
    config = make_config(
        crawl={
            "ali1688": {
                "shop_name_backfill": {
                    "enabled": True,
                    "max_per_run": 2,
                    "source": "detail",
                }
            }
        }
    )
    hit = _item("681272751030", "正版奥特曼声光武器")
    miss = _item("681272751031", "叶罗丽 贴纸")
    browser = StaticBrowser([_detail_page(hit.product_id)])
    platform = create_platform("1688", config, browser, stats=RunStats())
    items = [hit, miss]

    platform.after_search("奥特曼", items)

    assert items[0].shop_name == "火星人网络科技有限公司"
    assert items[1].shop_name is None  # 未命中关键词，不补抓
    assert browser.calls == [hit.url]
    assert any("1688 店名补抓" in warning for warning in platform.stats.warnings)


def test_1688_backfill_ignores_old_per_run_cap(make_config):
    config = make_config(
        crawl={
            "ali1688": {
                "shop_name_backfill": {
                    "enabled": True,
                    "max_per_run": 1,
                    "source": "detail",
                }
            }
        }
    )
    first = _item("681272751030", "正版奥特曼声光武器")
    second = _item("681272751031", "奥特曼 卡牌")
    browser = StaticBrowser(
        [_detail_page(first.product_id), _detail_page(second.product_id)]
    )
    platform = create_platform("1688", config, browser, stats=RunStats())

    first_items = [first]
    second_items = [second]
    platform.after_search("奥特曼", first_items)
    platform.after_search("奥特曼", second_items)

    assert first_items[0].shop_name == "火星人网络科技有限公司"
    assert second_items[0].shop_name == "火星人网络科技有限公司"
    assert browser.calls == [first.url, second.url]


def test_1688_backfill_falls_back_to_shop_page(make_config):
    config = make_config(
        crawl={
            "ali1688": {
                "shop_name_backfill": {
                    "enabled": True,
                    "max_per_run": 1,
                    "source": "shop",
                }
            }
        }
    )
    item = _item("681272751030", "正版奥特曼声光武器", shop_id="2y9x5s4346283")
    browser = StaticBrowser(
        [
            PageResult(
                url="https://2y9x5s4346283.1688.com/",
                html=(
                    "<html><head><title>火星人网络科技有限公司 - 1688</title></head>"
                    "<body><h1>火星人网络科技有限公司</h1></body></html>"
                ),
            )
        ]
    )
    platform = create_platform("1688", config, browser, stats=RunStats())
    items = [item]

    platform.after_search("奥特曼", items)

    assert items[0].shop_name == "火星人网络科技有限公司"
    assert browser.calls == ["https://2y9x5s4346283.1688.com/"]


def _write_jsonl(path, rows):
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )


def test_trial_metrics_reports_candidate_shop_name_rate(tmp_path):
    _write_jsonl(
        tmp_path / "1688.jsonl",
        [
            {
                "platform": "1688",
                "product_id": "1",
                "title": "奥特曼 玩具",
                "search_keyword": "奥特曼",
                "shop_name": _HUOXINGREN,
                "shop_id": "S1",
                "price": 9.9,
                "main_image_url": "https://img.1688.com/a.jpg",
            },
            {
                "platform": "1688",
                "product_id": "2",
                "title": "奥特曼 玩具 2",
                "search_keyword": "奥特曼",
                "shop_name": None,
                "shop_id": "S2",
                "price": 9.9,
                "main_image_url": "https://img.1688.com/b.jpg",
            },
            {
                "platform": "1688",
                "product_id": "3",
                "title": "叶罗丽 玩具",
                "search_keyword": "奥特曼",
                "shop_name": None,
                "shop_id": "S3",
                "price": 9.9,
                "main_image_url": "https://img.1688.com/c.jpg",
            },
            {
                "platform": "1688",
                "product_id": "4",
                "title": "奥特曼 玩具 4",
                "search_keyword": "奥特曼",
                "shop_name": "普通店",
                "shop_id": "S4",
                "price": 9.9,
                "main_image_url": "https://img.1688.com/d.jpg",
            },
        ],
    )

    metrics = trial_metrics.collect_metrics(tmp_path)
    platform = metrics["platforms"]["1688"]
    assert platform["shop_name_actual"] == 2
    assert platform["candidate_count"] == 3
    assert platform["candidate_shop_name_actual"] == 2
    assert platform["candidate_shop_name_rate"] == 2 / 3

    text = trial_metrics.render_markdown(metrics)
    assert "候选店名非空率" in text
    assert "候选条数（标题命中关键词）" in text
