"""11/6 R1 补字段 c：用 WU 11/6b 小样本派生夹具锁定真实解析路径。"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from urllib.parse import quote_plus

from crawler.core.browser import (
    CapturedResponse,
    PageResult,
    PaginationState,
    StaticBrowser,
)
from crawler.core.jsonl import read_jsonl
from crawler.core.schema import apply_patch, enrich_flags
from crawler.models import Item, ItemPatch
from crawler.platforms import create_platform
from crawler.runner import run_detail, run_platform
from crawler.core.stats import RunStats
from crawler.tests.conftest import fixture_text


def _platform(name, config):
    return create_platform(name, config, StaticBrowser([]), stats=RunStats())


def _captured(
    name, url="https://h5api.m.taobao.com/h5/mtop.taobao.pcdetail.data.get/1.0/"
):
    return CapturedResponse(
        url=url, text=fixture_text(name), content_type="text/plain", status=200
    )


def _item(**overrides) -> Item:
    data = dict(
        platform="tmall",
        product_id="759606896896",
        url="https://detail.tmall.com/item.htm?id=759606896896",
        title="儿童特利迦奥特曼书包",
        search_keyword="奥特曼",
        crawled_at=datetime(2026, 11, 6, 10, 39, tzinfo=timezone.utc),
        price=163,
        main_image_url="https://img.alicdn.com/a.jpg",
        shop_id="916148540",
        shop_name="知叶玩具专营店",
    )
    data.update(overrides)
    return Item(**data)


# ---------------------------------------------------------------------------
# 1/2：天猫 "0"/null 视为缺失；从主图路径取卖家数字 ID；加密串进 shop_id_raw
# ---------------------------------------------------------------------------
def test_tmall_shop_id_zero_null_and_image_fallback(make_config):
    platform = _platform("taobao", make_config())
    result = PageResult(
        url="https://s.taobao.com/search?q=%E5%A5%A5%E7%89%B9%E6%9B%BC&page=1",
        html="",
        captured=[_captured("val_b_20261106/taobao_list_20261106_mtop.txt")],
    )
    items = platform.parse_search(result, "奥特曼", 1, "ultraman")
    by_id = {item.product_id: item for item in items}
    assert set(by_id) == {
        "973302041506",
        "759606896896",
        "722048339605",
        "999000000001",
    }

    # 淘宝加密串 -> shop_id 用主图路径里的数字卖家 ID，加密串留 raw。
    taobao = by_id["973302041506"]
    assert taobao.platform == "taobao"
    assert taobao.shop_id == "3878627212"
    assert taobao.shop_id_raw == "RAzN8HWZ4PPsXKMTeWML9SvnM9W1z2uNCvcEFRxArWaHUruoiMt"
    assert "店铺ID缺失" not in taobao.flags

    # 天猫列表 shop_id=null：从 .../i4/i1/916148540/ 取数字 ID。
    tmall = by_id["759606896896"]
    assert tmall.platform == "tmall"
    assert tmall.shop_id == "916148540"
    assert "店铺ID缺失" not in tmall.flags

    # 天猫 gw.alicdn ..._!!2455999666- 形态同样取数字 ID。
    gw = by_id["722048339605"]
    assert gw.shop_id == "2455999666"
    assert "店铺ID缺失" not in gw.flags

    # 明确 "0" 且主图无卖家 ID：归一为缺失，但不再打「店铺ID缺失」。
    zero = by_id["999000000001"]
    assert zero.shop_id is None
    assert "店铺ID缺失" not in zero.flags


def test_enrich_flags_treats_zero_and_blank_shop_id_as_missing():
    for value in ("0", "", "   ", None, "null"):
        enriched = enrich_flags(_item(shop_id=value))
        assert enriched.shop_id is None
        assert "店铺ID缺失" not in enriched.flags
        assert "店铺名缺失" not in enriched.flags


# ---------------------------------------------------------------------------
# 3：详情页评价数优先接口/页面数据，DOM 兜底；取不到写日志原因
# ---------------------------------------------------------------------------
def test_taobao_detail_comment_total_from_rate_info_json(make_config):
    platform = _platform("taobao", make_config())
    result = PageResult(
        url="https://item.taobao.com/item.htm?id=973302041506",
        html="",
        captured=[_captured("val_b_20261106/taobao_detail_20261106_mtop.txt")],
    )
    patch = platform.parse_detail(result, "973302041506")
    assert patch.comments_raw == "2.3万+"
    assert patch.comments == 23000
    # 详情页 shopId="0" 也走主图路径；detail 夹具来自真实 WU 行。
    assert patch.shop_id == "3878627212"
    assert patch.shop_id_raw == "RAzN8HWZ4PPsXKMTeWML9SvnM9W1z2uNCvcEFRxArWaHUruoiMt"


def test_taobao_detail_comment_total_from_nested_rate_vo(make_config):
    platform = _platform("taobao", make_config())
    result = PageResult(
        url="https://detail.tmall.com/item.htm?id=759606896896",
        html="",
        captured=[_captured("val_b_20261106/taobao_detail_rate_mtop.txt")],
    )
    patch = platform.parse_detail(result, "tmall:759606896896")
    assert patch.comments is not None and patch.comments > 0
    assert patch.shop_id == "916148540"


def test_taobao_detail_ignores_buyer_nick_from_login_payload(make_config):
    """详情只从商品详情/卖家节点取店名；登录态买家 nick 绝不允许进来。"""
    platform = _platform("taobao", make_config())
    detail = {
        "data": {
            "seller": {"sellerId": "702188581", "shopName": "帕龙母婴旗舰店"},
            "item": {
                "itemId": "736204892112",
                "title": "奥特曼儿童棉拖鞋",
                "shopId": "0",
                "nick": "tb4983940864",
                "picUrl": "https://img.alicdn.com/imgextra/i4/702188581/a.jpg",
            },
            "loginUser": {"nick": "tb4983940864", "userId": "0"},
        }
    }
    login = {"user": {"nick": "tb4983940864", "userId": "123456789"}}
    result = PageResult(
        url="https://detail.tmall.com/item.htm?id=736204892112",
        html="",
        captured=[
            CapturedResponse(
                url="https://h5api.m.taobao.com/login",
                text=json.dumps(login),
                status=200,
            ),
            CapturedResponse(
                url="https://h5api.m.taobao.com/detail",
                text=json.dumps(detail),
                status=200,
            ),
        ],
    )
    patch = platform.parse_detail(result, "tmall:736204892112")
    assert patch.shop_id == "702188581"
    assert patch.shop_name == "帕龙母婴旗舰店"
    assert patch.shop_name != "tb4983940864"


def test_detail_zero_and_placeholder_do_not_override_list_shop_values(make_config):
    """详情 "0"/空/占位不得覆盖列表页已有的合法 shop_id / shop_name。"""
    item = _item(shop_id="916148540", shop_name="知叶玩具专营店")
    for zero in ("0", "", "null", "undefined", "-"):
        merged = apply_patch(
            item,
            ItemPatch(
                platform="tmall",
                product_id="759606896896",
                shop_id=zero,
                shop_id_raw=zero,
                shop_name=zero,
            ),
        )
        assert merged.shop_id == "916148540"
        assert merged.shop_name == "知叶玩具专营店"

    # 详情主接口只给 "0"、没有卖家节点/主图卖家 ID 时，patch 也必须为空。
    platform = _platform("taobao", make_config())
    payload = {"item": {"itemId": "759606896896", "title": "奥特曼书包", "shopId": "0"}}
    result = PageResult(
        url="https://detail.tmall.com/item.htm?id=759606896896",
        html="",
        captured=[
            CapturedResponse(
                url="https://h5api.m.taobao.com/detail",
                text=json.dumps(payload),
                status=200,
            )
        ],
    )
    patch = platform.parse_detail(result, "tmall:759606896896")
    assert patch.shop_id is None
    assert patch.shop_name is None
    merged = apply_patch(item, patch)
    assert merged.shop_id == "916148540"
    assert merged.shop_name == "知叶玩具专营店"


def test_taobao_detail_comments_dom_fallback_and_missing_log(make_config, caplog):
    platform = _platform("taobao", make_config())
    html = """
    <html><body>
      <div id="J_Title"><h3 class="tb-main-title">奥特曼 书包</h3></div>
      <div id="J_RateCounter">5</div>
      <a href="//shop916148540.tmall.com">知叶玩具专营店</a>
      <img id="J_ImgBooth" src="//img.alicdn.com/imgextra/i4/916148540/a.jpg">
    </body></html>
    """
    patch = platform.parse_detail(
        PageResult(url="https://detail.tmall.com/item.htm?id=759606896896", html=html),
        "tmall:759606896896",
    )
    assert patch.comments == 5
    assert patch.shop_id == "916148540"

    caplog.set_level(logging.INFO, logger="crawler.taobao")
    missing = platform.parse_detail(
        PageResult(
            url="https://item.taobao.com/item.htm?id=973302041506",
            html="<html><body>无评价字段</body></html>",
        ),
        "973302041506",
    )
    assert missing.comments is None
    assert "detail comments missing" in caplog.text
    assert "973302041506" in caplog.text


# ---------------------------------------------------------------------------
# 6：淘宝主图懒加载 data-src / data-ks-lazyload / srcset / JSON pic_path
# ---------------------------------------------------------------------------
def test_taobao_lazy_image_attributes_and_json_pic_path(make_config):
    platform = _platform("taobao", make_config())
    dom = platform.parse_search(
        PageResult(
            url="https://s.taobao.com/search?q=x",
            html=fixture_text("val_b_20261106/taobao_lazy.html"),
        ),
        "奥特曼",
        1,
        None,
    )
    by_id = {item.product_id: item for item in dom}
    assert len(by_id) == 3
    assert by_id["973302041506"].main_image_url.endswith("/i3/3878627212/a.jpg")
    assert by_id["759606896896"].main_image_url.endswith("/i4/i1/916148540/b.jpg")
    assert by_id["722048339605"].main_image_url.endswith("/i2/2455999666/c.jpg")
    assert all("主图缺失" not in item.flags for item in dom)

    # JSON pic_path 也走同一条真实解析路径。
    json_items = platform.parse_search(
        PageResult(
            url="https://s.taobao.com/search?q=x",
            html="",
            captured=[_captured("val_b_20261106/taobao_list_20261106_mtop.txt")],
        ),
        "奥特曼",
        1,
        None,
    )
    assert next(
        item for item in json_items if item.product_id == "973302041506"
    ).main_image_url


# ---------------------------------------------------------------------------
# 5/7：1688 从列表卡片取公司名/店铺名，实力商家/诚信通/超级工厂写 raw_hint
# ---------------------------------------------------------------------------
def test_1688_shop_name_and_type_hint_from_list_card(make_config):
    platform = _platform("1688", make_config())
    items = platform.parse_search(
        PageResult(
            url="https://s.1688.com/x",
            html=fixture_text("val_b_20261106/ali1688_list_20261106.html"),
        ),
        "奥特曼",
        1,
        None,
    )
    by_id = {item.product_id: item for item in items}
    assert set(by_id) == {"681272751030", "680718534845"}

    first = by_id["681272751030"]
    assert first.shop_name == "义乌市灵动玩具厂"
    assert first.shop_type_raw_hint == "1688超级工厂"
    assert "实力商家" in (first.shop_label_raw or "")
    assert "诚信通5年" in (first.shop_label_raw or "")
    assert "超级工厂" in (first.shop_label_raw or "")

    second = by_id["680718534845"]
    assert second.shop_name == "义乌市卡牌供应链有限公司"
    assert second.shop_type_raw_hint == "1688店铺"
    assert second.main_image_url


# ---------------------------------------------------------------------------
# 4：每个平台每次运行写一行 run_logs，含 run_id / 登录 / 条数 / 详情状态
# ---------------------------------------------------------------------------
def _login_page(platform: str) -> PageResult:
    url = {
        "taobao": "https://i.taobao.com/my_taobao.htm",
        "jd": "https://home.jd.com/",
        "1688": "https://work.1688.com/",
    }[platform]
    return PageResult(url=url, html="<html><body>账号正常</body></html>")


def _search_page(platform: str, keyword: str = "奥特曼") -> PageResult:
    if platform == "taobao":
        return PageResult(
            url=f"https://s.taobao.com/search?q={quote_plus(keyword)}&page=1",
            html="",
            captured=[_captured("taobao_search.json")],
            pagination=PaginationState(current_page=1, has_next=False),
        )
    if platform == "jd":
        return PageResult(
            url="https://search.jd.com/Search?keyword=x",
            html=fixture_text("jd_search.html"),
            pagination=PaginationState(current_page=1, has_next=False),
        )
    return PageResult(
        url="https://s.1688.com/selloffer/offer_search.htm",
        html="",
        captured=[
            _captured("ali1688_search.json", "https://h5api.m.1688.com/h5/mtop.1688/x")
        ],
        pagination=PaginationState(current_page=1, has_next=False),
    )


def test_run_logs_one_line_per_platform_with_run_id_and_detail_status(
    make_config, tmp_path
):
    config = make_config()
    for platform in ("taobao", "jd", "1688"):
        browser = StaticBrowser([_login_page(platform), _search_page(platform)])
        record = run_platform(
            platform,
            config,
            date="2026-11-06",
            keywords=["奥特曼"],
            max_pages=1,
            browser_factory=lambda cfg, name, headless, b=browser: b,
        )
        assert record.status == "ok"
        assert record.run_id

    rows = read_jsonl(tmp_path / "out" / "2026-11-06" / "run_logs.jsonl")
    assert len(rows) == 3
    assert {row["platform"] for row in rows} == {"taobao", "jd", "1688"}
    for row in rows:
        assert row["run_id"]
        assert row["detail_status"] == "skipped"
        assert "login_ok" in row and "items_ok" in row and "risk_blocked" in row
        assert row["started_at"] and row["ended_at"]

    # 详情运行同样落一行，detail_status 为本次详情状态而不是 skipped。
    detail_browser = StaticBrowser(
        [
            _login_page("taobao"),
            PageResult(
                url="https://item.taobao.com/item.htm?id=1111111111",
                html="",
                captured=[_captured("taobao_detail.json")],
            ),
        ]
    )
    detail = run_detail(
        "taobao",
        config,
        ids=["1111111111"],
        date="2026-11-06",
        browser_factory=lambda cfg, name, headless: detail_browser,
    )
    assert detail.run_id
    rows = read_jsonl(tmp_path / "out" / "2026-11-06" / "run_logs.jsonl")
    assert len(rows) == 4
    assert rows[-1]["detail_status"] == "ok"
    assert rows[-1]["run_id"] != rows[-2]["run_id"]


def test_taobao_detail_comment_total_from_nested_rate_counts_object(make_config):
    platform = _platform("taobao", make_config())
    payload = {
        "data": {
            "rateCounts": {"rateInfo": {"commentCount": "1.2万+"}},
        }
    }
    result = PageResult(
        url="https://detail.tmall.com/item.htm?id=759606896896",
        html="",
        captured=[
            CapturedResponse(
                url="https://h5api.m.taobao.com/h5/mtop.rate/1.0/",
                text=json.dumps(payload),
                status=200,
            )
        ],
    )
    patch = platform.parse_detail(result, "tmall:759606896896")
    assert patch.comments_raw == "1.2万+"
    assert patch.comments == 12000
