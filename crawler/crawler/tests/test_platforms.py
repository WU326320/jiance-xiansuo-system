from __future__ import annotations

from decimal import Decimal
from datetime import datetime
import re

import pytest

from crawler.core.browser import CapturedResponse, PageResult, PaginationState
from crawler.core.stats import RunStats
from crawler.core.errors import PaginationError
from crawler.platforms import PLATFORM_NAMES, create_platform
from crawler.tests.conftest import fixture_text


def _captured(name: str, url: str = "https://h5api.example/api") -> CapturedResponse:
    return CapturedResponse(
        url=url, text=fixture_text(name), content_type="application/json", status=200
    )


def _platform(name, config, browser=None):
    from crawler.core.browser import StaticBrowser

    return create_platform(name, config, browser or StaticBrowser([]), stats=RunStats())


def test_registry_interfaces(make_config):
    config = make_config()
    for name in PLATFORM_NAMES:
        platform = _platform(name, config)
        assert callable(platform.check_login)
        assert callable(platform.search)
        assert callable(platform.fetch_detail)


@pytest.mark.parametrize("name", ["taobao", "jd", "1688"])
def test_required_title_missing_keeps_real_json_product(make_config, monkeypatch, name):
    import json

    monkeypatch.setenv("CRAWLER_AMOUNT_DETAIL", "1")
    platform = _platform(name, make_config())
    key, id_key = {
        "taobao": ("auctions", "nid"),
        "jd": ("wareList", "skuId"),
        "1688": ("offerList", "offerId"),
    }[name]
    body = {
        "data": {
            key: [
                {
                    id_key: "300001",
                    "url": platform.detail_url("300001"),
                    "price": "20",
                    "shopName": "测试店铺",
                    "sales": 0,
                }
            ]
        }
    }
    result = PageResult(
        url=platform.build_search_url("挂件", 1),
        html="",
        captured=[
            CapturedResponse(url="https://api.example/search", text=json.dumps(body))
        ],
    )
    items = platform.parse_search(result, "挂件", 1, None)
    assert len(items) == 1
    assert items[0].product_id == "300001" and items[0].title == ""
    assert "商品标题缺失" in items[0].flags
    assert items[0].sales == 0


def test_required_fields_does_not_turn_jd_price_response_into_product(
    make_config, monkeypatch
):
    monkeypatch.setenv("CRAWLER_AMOUNT_DETAIL", "1")
    platform = _platform("jd", make_config())
    result = PageResult(
        url="https://search.jd.com/",
        html="",
        captured=[
            CapturedResponse(
                url="https://p.3.cn/prices/mgets", text='[{"id":"J_300001","p":"20"}]'
            )
        ],
    )
    assert platform.parse_search(result, "挂件", 1, None) == []


@pytest.mark.parametrize("name", ["taobao", "jd", "1688"])
@pytest.mark.parametrize("wait_at", ["keyword", "page", "rpm", "retry"])
def test_timed_search_never_dispatches_after_wait_crosses_deadline(
    make_config, monkeypatch, name, wait_at
):
    from crawler.core.browser import StaticBrowser
    from crawler.core.progress import SearchProgress
    from crawler.models import Item

    monkeypatch.setenv("CRAWLER_ENDS_AT", "2099-01-01T00:00:00+08:00")
    config = make_config()
    config.crawl.retry.attempts = 3
    platform = _platform(name, config)
    platform.continuous = True
    platform.defer_after_search = True
    matrix = [{"keyword": "葫芦兄弟", "ip_code": "huluxiongdi"}]
    platform.search_progress = SearchProgress(
        config.state_root, name, matrix, continuous=True
    )
    url = platform.build_search_url("葫芦兄弟", 1)
    browser = StaticBrowser(
        [PageResult(url=url, html="列表", status=503 if wait_at == "retry" else 200)]
    )
    platform.browser = browser

    def expire(*args):
        monkeypatch.setenv("CRAWLER_ENDS_AT", "2000-01-01T00:00:00+08:00")

    if wait_at in ("keyword", "page"):
        monkeypatch.setattr(platform.throttle, f"wait_{wait_at}_interval", expire)
    else:
        monkeypatch.setattr(platform.throttle, "sleep", expire)
        if wait_at == "rpm":
            platform.throttle.requests_per_minute = 1
            platform.throttle._recent.append(platform.throttle.clock())
    item = Item(
        platform=name,
        product_id="123456",
        title="葫芦兄弟商品",
        url=url,
        search_keyword="葫芦兄弟",
        ip_code="huluxiongdi",
        crawled_at=datetime.now(),
    )
    monkeypatch.setattr(platform, "parse_search", lambda *a: [item])
    monkeypatch.setattr(platform, "after_search", lambda *a: None)
    items = platform.search("葫芦兄弟", 1, ip_code="huluxiongdi")
    assert browser.calls == ([url] if wait_at in ("page", "retry") else [])
    assert len(items) == (1 if wait_at == "page" else 0)
    assert platform.search_deadline_reached
    saved = platform.search_progress.position("葫芦兄弟", "huluxiongdi")
    assert not saved.get("done")


def test_jd_detail_does_not_use_buyer_name_and_reads_price_object(make_config):
    import json

    platform = _platform("jd", make_config())
    result = PageResult(
        url=platform.detail_url("1001"),
        html='<div class="sku-name">商品真实标题</div>',
        captured=[
            CapturedResponse(
                url="https://api.example/detail",
                text=json.dumps(
                    {
                        "user": {"name": "jd_buyer"},
                        "price": {
                            "price": "29.9",
                            "priceContent": "首单到手价",
                            "up": "18",
                        },
                    }
                ),
            )
        ],
    )
    patch = platform.parse_detail(result, "1001")
    assert patch.title == "商品真实标题"
    assert patch.price == Decimal("29.90")


@pytest.mark.parametrize("name", ["taobao", "jd", "1688"])
@pytest.mark.parametrize("coupon", ["79.90", "0"])
def test_detail_price_prioritizes_coupon_before_original(make_config, name, coupon):
    import json

    platform = _platform(name, make_config())
    result = PageResult(
        url=platform.detail_url("1001"),
        html="",
        captured=[
            CapturedResponse(
                url="https://api.example/detail",
                text=json.dumps({"price": "99", "couponPrice": coupon}),
            )
        ],
    )
    patch = platform.parse_detail(result, "1001")
    assert patch is not None
    assert patch.price == Decimal(coupon)
    assert patch.price_raw == coupon


@pytest.mark.parametrize("name", ["taobao", "jd", "1688"])
@pytest.mark.parametrize("parse_fails", [False, True])
def test_progress_resumes_high_page_without_skipping_failure(
    make_config, monkeypatch, name, parse_fails
):
    from crawler.core.browser import StaticBrowser
    from crawler.core.progress import SearchProgress
    from crawler.models import Item

    monkeypatch.delenv("CRAWLER_ENDS_AT", raising=False)
    config = make_config()
    matrix = [{"keyword": "葫芦兄弟", "ip_code": "huluxiongdi"}]
    progress = SearchProgress(config.state_root, name, matrix)
    progress.commit("葫芦兄弟", "huluxiongdi", 19, [])
    platform = _platform(name, config)
    url = platform.build_search_url("葫芦兄弟", 20)
    platform.browser = StaticBrowser(
        [
            PageResult(
                url=url,
                html="<html>商品列表</html>",
                pagination=PaginationState(current_page=20, has_next=False),
            )
        ]
    )
    platform.search_progress = progress
    monkeypatch.setattr(
        platform, "_prepare_search_result", lambda result, *args: result
    )
    monkeypatch.setattr(platform, "after_search", lambda *args: None)

    def parse(result, keyword, page, ip_code):
        if parse_fails:
            raise ValueError("模拟解析失败")
        return [
            Item(
                platform=name,
                product_id="123456789",
                title=keyword,
                url=url,
                search_keyword=keyword,
                crawled_at=datetime.fromisoformat("2026-11-09T10:00:00+08:00"),
                ip_code=ip_code,
                page=page,
            )
        ]

    monkeypatch.setattr(platform, "parse_search", parse)
    if parse_fails:
        with pytest.raises(ValueError, match="模拟解析失败"):
            platform.search("葫芦兄弟", 1, ip_code="huluxiongdi")
    else:
        assert platform.search("葫芦兄弟", 1, ip_code="huluxiongdi")[0].page == 20
    assert platform.browser.calls == [url]
    recovered = SearchProgress(config.state_root, name, matrix)
    assert recovered.position("葫芦兄弟", "huluxiongdi")["next_page"] == (
        20 if parse_fails else 21
    )
    assert len(recovered.rows) == (0 if parse_fails else 1)
    position = recovered.position("葫芦兄弟", "huluxiongdi")
    assert bool(position.get("done")) == (not parse_fails)
    assert position.get("stop_reason") == (None if parse_fails else "last_page")


@pytest.mark.parametrize(
    "name,key,value",
    [
        ("taobao", "sort", "sale-desc"),
        ("jd", "psort", "3"),
        ("1688", "sortType", "booked"),
    ],
)
def test_search_urls_keep_sales_sort_on_every_page(make_config, name, key, value):
    from urllib.parse import parse_qs, urlsplit

    platform = _platform(name, make_config())
    for page in (1, 2, 3):
        query = parse_qs(urlsplit(platform.build_search_url("葫芦兄弟", page)).query)
        assert query[key] == [value]


class SalesControl:
    def __init__(self, page):
        self.page = page

    def count(self):
        return int(self.page.has_control)

    def nth(self, index):
        return self

    @property
    def first(self):
        return self

    def wait_for(self, **kwargs):
        if not self.page.has_control:
            raise TimeoutError("no sales control")

    def is_visible(self):
        return self.page.has_control

    def evaluate(self, script):
        return self.page.active

    def click(self, **kwargs):
        self.page.clicks += 1
        if self.page.click_error:
            raise TimeoutError("click blocked")
        self.page.active = True
        self.page.refreshed = self.page.refresh


class SalesCards:
    def __init__(self, page):
        self.page = page

    def evaluate_all(self, script):
        return "new-list" if self.page.refreshed else "old-list"


class SalesPage:
    def __init__(
        self, *, has_control=True, active=False, refresh=True, click_error=False
    ):
        self.has_control = has_control
        self.active = active
        self.refresh = refresh
        self.click_error = click_error
        self.refreshed = False
        self.clicks = 0

    def locator(self, selector):
        from crawler.platforms.selectors import SALES_SORT

        if selector in [spec["control"] for spec in SALES_SORT.values()]:
            return SalesControl(self)
        return SalesCards(self)

    def wait_for_function(self, script, **kwargs):
        if not self.refreshed:
            raise TimeoutError("list did not change")


@pytest.mark.parametrize("name", ["taobao", "jd", "1688"])
@pytest.mark.parametrize(
    "scenario,confirmed",
    [
        ({}, True),
        ({"refresh": False}, False),
        ({"has_control": False}, False),
        ({"click_error": True}, False),
        ({"active": True}, False),
    ],
)
def test_sort_never_stops_collection_when_unconfirmed(
    make_config, name, scenario, confirmed
):
    from crawler.core.browser import StaticBrowser

    fixture = {
        "taobao": "taobao_search.html",
        "jd": "jd_search.html",
        "1688": "ali1688_search.html",
    }[name]
    platform = _platform(name, make_config())
    platform.page_action = lambda page: None
    action_pages = []

    class ActionBrowser(StaticBrowser):
        def fetch_page(self, url, *, action=None, **kwargs):
            self.calls.append(url)
            self._last = self._next_page(url)
            page = SalesPage(**scenario)
            action_pages.append(page)
            assert action is not None
            action(page)
            return self._last

    platform.browser = ActionBrowser(
        [
            PageResult(
                url=platform.build_search_url("葫芦兄弟", page),
                html=re.sub(
                    r"\b\d{4,}\b",
                    lambda m: str(page) + m.group(),
                    fixture_text(fixture),
                ),
                pagination=PaginationState(current_page=page, has_next=page < 2),
            )
            for page in (1, 2)
        ]
    )
    items = platform.search("葫芦兄弟", 2)
    assert items
    assert len(platform.browser.calls) == 2
    assert {item.page for item in items} == {1, 2}
    assert all(("销量排序未确认" not in item.flags) == confirmed for item in items)
    assert bool(platform.stats.warnings) != confirmed
    assert action_pages[0].clicks == (
        0 if scenario.get("active") or not action_pages[0].has_control else 1
    )
    assert action_pages[1].clicks == 0  # 后续页不重新排序，防止重置回第一页。


@pytest.mark.parametrize("name", ["taobao", "jd", "1688"])
@pytest.mark.parametrize("unrelated_response", [False, True])
def test_sort_without_refresh_keeps_original_api_items(
    make_config, name, unrelated_response
):
    import time

    from crawler.core.browser import ResponseRecorder, StaticBrowser

    fixture = {
        "taobao": "taobao_search.json",
        "jd": "jd_prices.json",
        "1688": "ali1688_search.json",
    }[name]
    # 京东价格接口没有商品标题，使用已有的商品列表接口结构。
    body = (
        '{"data":{"wareList":[{"skuId":"100012044001",'
        '"wareName":"奥特曼 手办","price":"66.00"}]}}'
        if name == "jd"
        else fixture_text(fixture)
    )
    platform = _platform(name, make_config())
    platform.page_action = lambda page: None

    class UnchangedBrowser(StaticBrowser):
        def discard_responses_before(self, boundary):
            self.recorder.discard_before(boundary)

        def fetch_page(self, url, *, action=None, **kwargs):
            self.calls.append(url)
            self.recorder = ResponseRecorder()
            self.recorder.captured = [
                CapturedResponse(
                    url="https://api.example/search",
                    text=re.sub(
                        r"\b\d{4,}\b", lambda m: str(len(self.calls)) + m.group(), body
                    ),
                    requested_at=0,
                )
            ]
            assert action is not None
            action(SalesPage(refresh=False))
            if unrelated_response:
                self.recorder.captured.append(
                    CapturedResponse(
                        url="https://api.example/telemetry",
                        text='{"ok":true}',
                        requested_at=time.monotonic(),
                    )
                )
            return PageResult(
                url=url,
                html="",
                captured=self.recorder.collect(),
                pagination=PaginationState(
                    current_page=len(self.calls), has_next=len(self.calls) < 2
                ),
            )

    platform.browser = UnchangedBrowser([])
    items = platform.search("奥特曼", 2)
    assert items
    assert {item.page for item in items} == {1, 2}
    assert all("销量排序未确认" in item.flags for item in items)
    assert len(platform.browser.calls) == 2


def test_sort_refresh_excludes_old_responses_and_embedded_state(make_config):
    from crawler.core.browser import StaticBrowser
    import time

    platform = _platform("taobao", make_config())
    platform.page_action = lambda page: None

    class RefreshBrowser(StaticBrowser):
        def fetch_page(self, url, *, action=None, **kwargs):
            assert action is not None
            action(SalesPage())
            return PageResult(
                url=url,
                html='<script type="application/json">{"data":{"auctions":[{"nid":"9999999","raw_title":"旧商品"}]}}</script>',
                pagination=PaginationState(has_next=False),
                captured=[
                    CapturedResponse(
                        url="https://api.example/search",
                        text='{"data":{"auctions":[{"nid":"1111111","raw_title":"旧响应"}]}}',
                        requested_at=0,
                    ),
                    CapturedResponse(
                        url="https://api.example/search",
                        text='{"data":{"auctions":[{"nid":"2222222","raw_title":"新响应"}]}}',
                        requested_at=time.monotonic(),
                    ),
                ],
            )

    platform.browser = RefreshBrowser([])
    assert [item.product_id for item in platform.search("葫芦兄弟", 1)] == ["2222222"]


def test_card_nodes_merge_in_document_order():
    from crawler.core.dom import card_nodes_all, parse_html

    root = parse_html(
        '<main><div class="modern" id="first"></div><div class="legacy" id="second"></div></main>'
    )
    cards = card_nodes_all(root, [".legacy", ".modern"])
    assert [node.get("id") for node in cards] == ["first", "second"]


@pytest.fixture(scope="module")
def real_sales_browser():
    """本地真实 Chrome；所有页面及接口由 route 回放，不访问电商站点。"""
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as driver:
        try:
            browser = driver.chromium.launch(channel="chrome", headless=True)
        except playwright.Error as exc:
            pytest.skip(f"本地 Chrome 不可用：{exc}")
        try:
            yield browser
        finally:
            browser.close()


@pytest.mark.parametrize("name", ["taobao", "jd", "1688"])
@pytest.mark.parametrize("scenario", ["delayed_refresh", "unchanged", "selected"])
def test_real_browser_sales_sort_fallback(
    make_config, real_sales_browser, name, scenario
):
    import json
    from urllib.parse import urlsplit

    from crawler.core.browser import ResponseRecorder, StaticBrowser
    from crawler.platforms.selectors import SALES_SORT

    platform = _platform(name, make_config())
    hooks = []
    platform.page_action = lambda page: hooks.append(page)
    fixture = {
        "taobao": "taobao_search.html",
        "jd": "jd_search.html",
        "1688": "ali1688_search.html",
    }[name]
    original = fixture_text(fixture)
    initial_body = (
        {
            "taobao": '{"data":{"auctions":[{"nid":"1111111","raw_title":"奥特曼 手办"}]}}',
            "jd": '{"data":{"wareList":[{"skuId":"1111111","wareName":"奥特曼 手办"}]}}',
            "1688": '{"data":{"offerList":[{"offerId":"1111111","subject":"奥特曼 手办"}]}}',
        }[name]
        if scenario == "unchanged"
        else "{}"
    )
    expected = (
        ["1111111"]
        if scenario == "unchanged"
        else [
            item.product_id
            for item in platform.parse_search(
                PageResult(url="", html=original), "奥特曼", 1, None
            )
        ]
    )
    if scenario == "delayed_refresh":
        expected.reverse()
    document = "<html><body></body></html>" if scenario == "unchanged" else original
    active = "true" if scenario == "selected" else "false"
    document = document.replace(
        "<body>", f'<body><button id="sales" aria-pressed="{active}">销量</button>'
    )
    script = """
        window.sortClicks = 0;
        document.querySelector('#sales').addEventListener('click', event => {
            window.sortClicks++;
            event.currentTarget.setAttribute('aria-pressed', 'true');
            fetch('/telemetry');
    """
    if scenario == "delayed_refresh":
        script += (
            "setTimeout(() => { const cards = [...document.querySelectorAll("
            + json.dumps(SALES_SORT[name]["cards"])
            + ")]; cards.reverse().forEach(card => card.parentElement.appendChild(card)); }, 150);"
        )
    document = document.replace("</body>", f"<script>{script}}});</script></body>")

    with real_sales_browser.new_context() as context:

        def serve(route):
            url = urlsplit(route.request.url)
            if url.hostname != "sales-sort.test":
                route.abort()
            elif url.path in ("/initial", "/telemetry"):
                route.fulfill(
                    content_type="application/json",
                    body=initial_body if url.path == "/initial" else '{"ok":true}',
                )
            else:
                route.fulfill(content_type="text/html; charset=utf-8", body=document)

        context.route("**/*", serve)

        class LocalBrowser(StaticBrowser):
            def discard_responses_before(self, boundary):
                self.recorder.discard_before(boundary)

            def fetch_page(self, url, *, action=None, **kwargs):
                self.page = context.new_page()
                self.recorder = ResponseRecorder()
                self.recorder.attach(self.page)
                self.page.goto("https://sales-sort.test/search")
                self.page.evaluate("async () => await (await fetch('/initial')).text()")
                assert action is not None
                action(self.page)
                return PageResult(
                    url=url,
                    html=self.page.content(),
                    captured=self.recorder.collect(),
                    pagination=PaginationState(has_next=False),
                )

        browser = LocalBrowser([])
        platform.browser = browser
        items = platform.search("奥特曼", 1)
        assert [item.product_id for item in items] == expected
        assert [item.rank for item in items] == list(range(1, len(items) + 1))
        assert len(hooks) == 1
        assert hooks[0].evaluate("window.sortClicks") == (scenario != "selected")
        assert platform._sales_sort.confirmed == (scenario == "delayed_refresh")
        assert all(
            ("销量排序未确认" in item.flags) == (scenario != "delayed_refresh")
            for item in items
        )


def test_taobao_json_search(make_config):
    platform = _platform("taobao", make_config())
    result = PageResult(
        url="https://s.taobao.com/search?q=x",
        html="",
        captured=[_captured("taobao_search.json")],
    )
    items = platform.parse_search(result, "葫芦兄弟", 1, "huluxiongdi")
    assert len(items) == 3
    first = items[0]
    assert first.platform == "taobao"
    assert first.product_id == "1111111111"
    assert first.price == Decimal("59.90")
    assert first.sales == 10000
    assert first.sales_raw == "1万+人付款"
    assert first.comments == 1200
    assert first.shop_id == "2001"
    assert first.page == 1 and first.rank == 1
    assert "按下限估算" in first.flags
    second = items[1]
    assert second.platform == "tmall"
    assert second.price == Decimal("39.90")
    assert second.sales == 25000
    assert second.comments == 100
    assert second.shop_label_raw and "天猫" in second.shop_label_raw
    third = items[2]
    assert third.shop_id is None
    assert "店铺ID缺失" not in third.flags
    assert "评论缺失" in third.flags


@pytest.mark.parametrize("tmall_source", ["dom", "json"])
def test_taobao_dom_and_api_shared_product_identity(make_config, tmall_source):
    import json

    platform = _platform("taobao", make_config())
    host = "detail.tmall.com" if tmall_source == "dom" else "item.taobao.com"
    html = (
        '<div id="mainsrp-itemlist"><div class="item" data-nid="123456">'
        f'<div class="title"><a href="https://{host}/item.htm?id=123456">葫芦兄弟玩具</a></div>'
        '<div class="shop"><a>示例店铺</a></div>'
        '<div class="price"><strong>19.90</strong></div></div></div>'
    )
    payload = {
        "data": {
            "auctions": [
                {
                    "nid": "123456",
                    "raw_title": "葫芦兄弟玩具",
                    "view_sales": 0,
                    "isTmall": tmall_source == "json",
                },
                {"nid": "654321", "raw_title": "另一件淘宝商品", "view_sales": 5},
            ]
        }
    }
    result = PageResult(
        url="https://s.taobao.com/search",
        html=html,
        search_refreshed=True,
        captured=[
            CapturedResponse(url="https://api.example/search", text=json.dumps(payload))
        ],
    )
    items = platform.parse_search(result, "葫芦兄弟", 2, None)
    assert len(items) == 2
    merged = next(item for item in items if item.product_id == "123456")
    assert merged.platform == "tmall" and "detail.tmall.com" in merged.url
    assert merged.shop_type_raw_hint == "天猫"
    assert merged.price == Decimal("19.90") and merged.sales == 0
    assert merged.shop_name == "示例店铺"
    assert "销量缺失" not in merged.flags
    assert (
        next(item for item in items if item.product_id == "654321").platform == "taobao"
    )


def test_taobao_dom_search(make_config):
    platform = _platform("taobao", make_config())
    result = PageResult(
        url="https://s.taobao.com/search?q=x", html=fixture_text("taobao_search.html")
    )
    items = platform.parse_search(result, "葫芦兄弟", 1, None)
    assert len(items) == 3
    assert items[0].product_id == "4444444444"
    assert items[0].shop_id == "4001"
    assert items[0].sales == 1000
    assert items[0].price == Decimal("29.90")
    assert items[1].platform == "tmall"
    assert items[1].price == Decimal("88.00")
    assert items[2].comments is None and "评论缺失" in items[2].flags


def test_taobao_detail_json_and_html(make_config):
    platform = _platform("taobao", make_config())
    result = PageResult(
        url="https://item.taobao.com/item.htm?id=1111111111",
        html="",
        captured=[_captured("taobao_detail.json")],
    )
    patch = platform.parse_detail(result, "1111111111")
    assert patch.product_id == "1111111111"
    assert patch.comments == 1200
    assert patch.sales == 10000
    assert patch.shop_id == "2001"
    html_result = PageResult(
        url="https://item.taobao.com/item.htm?id=1111111111",
        html=fixture_text("taobao_detail.html"),
    )
    patch2 = platform.parse_detail(html_result, "1111111111")
    assert patch2.comments == 1200
    assert patch2.sales == 10000
    assert patch2.price == Decimal("59.90")


def test_taobao_detail_offline(make_config):
    platform = _platform("taobao", make_config())
    result = PageResult(
        url="https://item.taobao.com/item.htm?id=1",
        html=fixture_text("offline_detail.html"),
    )
    patch = platform.parse_detail(result, "1")
    assert patch.listed is False
    assert "商品下架" in patch.flags


def test_jd_json_and_dom_search(make_config):
    platform = _platform("jd", make_config())
    result = PageResult(
        url="https://search.jd.com/Search?keyword=x",
        html=fixture_text("jd_search.html"),
        captured=[
            _captured("jd_prices.json", "https://p.3.cn/prices/mgets?skuIds=J_1001")
        ],
    )
    items = platform.parse_search(result, "葫芦兄弟", 1, None)
    assert len(items) == 4
    first = items[0]
    assert first.product_id == "1001"
    assert first.price == Decimal("59.90")
    assert first.comments == 20000
    assert first.shop_id == "3001"
    assert first.sales is None
    assert "销量缺失" in first.flags
    assert "京东自营" in (first.shop_label_raw or "")
    third = items[2]
    assert third.price == Decimal("12.50")  # DOM 无价，价格 JSON 补
    assert third.price_raw in {"12.5", "12.50"}
    fourth = items[3]
    assert fourth.comments is None and "评论缺失" in fourth.flags
    assert fourth.shop_id is None and "店铺ID缺失" not in fourth.flags


def test_jd_dom_zero_price_is_not_replaced_by_api(make_config):
    platform = _platform("jd", make_config())
    result = PageResult(
        url="https://search.jd.com/Search?keyword=x",
        html='<li class="gl-item" data-sku="1001"><div class="p-name"><a>零价商品</a></div><div class="p-price"><i>0</i></div></li>',
        captured=[
            CapturedResponse(
                url="https://p.3.cn/prices", text='[{"id":"J_1001","p":"99"}]'
            )
        ],
    )
    item = platform.parse_search(result, "商品", 1, None)[0]
    assert item.price == Decimal("0.00")
    assert item.price_raw == "0"


def test_jd_price_map_preserves_numeric_zero_and_coupon(make_config):
    platform = _platform("jd", make_config())
    prices = platform._price_map(
        [
            {"id": "J_1001", "p": 0, "price": 99},
            {"1002": {"p": 0, "price": 99}},
            {"id": "J_1003", "p": 99, "couponPrice": "79.90"},
        ]
    )
    assert prices == {
        "1001": Decimal("0"),
        "1002": Decimal("0"),
        "1003": Decimal("79.90"),
    }


def test_jd_detail_and_offline(make_config):
    platform = _platform("jd", make_config())
    result = PageResult(
        url="https://item.jd.com/1001.html", html=fixture_text("jd_detail.html")
    )
    patch = platform.parse_detail(result, "1001")
    assert patch.comments == 20000
    assert patch.sales == 5000
    assert patch.shop_id == "3001"
    assert patch.price == Decimal("59.90")
    offline = platform.parse_detail(
        PageResult(
            url="https://item.jd.com/1002.html",
            html=fixture_text("offline_detail.html"),
        ),
        "1002",
    )
    assert offline.listed is False


def test_1688_gbk_url_and_json(make_config):
    platform = _platform("1688", make_config())
    url = platform.build_search_url("葫芦娃", 2)
    assert "%BA%F9%C2%AB%CD%DE" in url
    assert "beginPage=2" in url
    result = PageResult(
        url="https://s.1688.com/selloffer/offer_search.htm",
        html="",
        captured=[_captured("ali1688_search.json")],
    )
    items = platform.parse_search(result, "葫芦兄弟", 1, "huluxiongdi")
    assert len(items) == 3
    first = items[0]
    assert first.platform == "1688"
    assert first.price == Decimal("3.50")
    assert first.sales == 1000
    assert first.comments == 100
    assert first.shop_id == "6001"
    assert "实力商家" in (first.shop_label_raw or "")
    assert "诚信通5年" in (first.shop_label_raw or "")
    assert first.shop_type_raw_hint == "1688实力商家"
    assert items[2].shop_id is None and "店铺ID缺失" not in items[2].flags


def test_1688_dom_and_keyword_warning(make_config):
    platform = _platform("1688", make_config())
    result = PageResult(
        url="https://s.1688.com/x", html=fixture_text("ali1688_search.html")
    )
    items = platform.parse_search(result, "葫芦兄弟", 1, None)
    assert len(items) == 3
    assert items[0].shop_id == "7001"
    assert items[0].price == Decimal("4.50")
    assert items[0].sales == 2000
    assert "实力商家" in (items[0].shop_label_raw or "")
    platform.after_search("不存在的关键词", items)
    assert platform.stats.warnings


def test_1688_detail(make_config):
    platform = _platform("1688", make_config())
    result = PageResult(
        url="https://detail.1688.com/offer/6001.html",
        html=fixture_text("ali1688_detail.html"),
    )
    patch = platform.parse_detail(result, "6001")
    assert patch.shop_id == "6001"
    assert patch.comments == 100
    assert patch.sales == 1000
    assert patch.price == Decimal("3.50")


def test_platform_search_uses_browser_and_pages(make_config):
    from crawler.core.browser import StaticBrowser

    config = make_config()
    platform = _platform("taobao", config)
    url1 = platform.build_search_url("葫芦兄弟", 1)
    url2 = platform.build_search_url("葫芦兄弟", 2)
    browser = StaticBrowser(
        [
            PageResult(url=url1, html="", captured=[_captured("taobao_search.json")]),
            PageResult(
                url=url2,
                html="",
                pagination=PaginationState(current_page=2, has_next=False),
                captured=[
                    CapturedResponse(
                        url="https://api.example/search",
                        text=re.sub(
                            r"\b\d{4,}\b",
                            lambda m: "2" + m.group(),
                            fixture_text("taobao_search.json"),
                        ),
                    )
                ],
            ),
        ]
    )
    platform.browser = browser
    items = platform.search("葫芦兄弟", 2, ip_code="huluxiongdi")
    # 两页各 3 条且商品不同，防止把重复第一页误当作翻页成功。
    assert len(items) == 6
    assert len(browser.calls) == 2
    assert platform.stats.pages_fetched == 2


def test_jd_2026_dom_cards_and_embedded_json(make_config):
    platform = _platform("jd", make_config())
    result = PageResult(
        url="https://search.jd.com/Search?keyword=x",
        html=fixture_text("jd_search_2026.html"),
    )
    items = platform.parse_search(result, "奥特曼", 1, None)
    by_id = {item.product_id: item for item in items}
    assert len(items) == 4
    first = by_id["100012043978"]
    assert first.title == "奥特曼 玩具 男孩 正版 发光 手办"
    assert first.price == Decimal("89.00")
    assert first.shop_id == "9001"
    assert first.comments == 20000
    assert "自营" in (first.shop_label_raw or "")
    # 第二个卡片没有 p-name/p-price，但 title/price 仍在 hash class 里。
    second = by_id["100012043979"]
    assert second.price == Decimal("39.90")
    assert second.comments == 100
    # 第三个卡片的 title/price 只有普通 div，靠文本兜底。
    third = by_id["100012043980"]
    assert third.title == "黑猫警长 贴纸 卡通 儿童"
    assert third.price == Decimal("19.90")
    # 第四个只存在于 window.__INIT_DATA__。
    fourth = by_id["100012043981"]
    assert fourth.title == "奥特曼 卡牌 收藏册"
    assert fourth.price == Decimal("12.50")
    assert fourth.comments == 500


def test_1688_2026_dom_card_variants(make_config):
    platform = _platform("1688", make_config())
    result = PageResult(
        url="https://s.1688.com/x", html=fixture_text("ali1688_search_2026.html")
    )
    items = platform.parse_search(result, "奥特曼", 1, None)
    by_id = {item.product_id: item for item in items}
    assert len(items) == 3
    first = by_id["710101"]
    assert first.title == "奥特曼 玩具 批发 发光 手办"
    assert first.price == Decimal("5.20")
    assert first.sales == 3000
    assert first.shop_name == "义乌市奥特曼玩具厂"
    assert "实力商家" in (first.shop_label_raw or "")
    assert by_id["710102"].price == Decimal("3.50")
    assert by_id["710102"].shop_id == "710102"
    assert by_id["710103"].price == Decimal("1.80")


def test_jd_jsonp_xhr_records(make_config):
    platform = _platform("jd", make_config())
    body = (
        'jsonpCBK({"data":{"wareList":['
        '{"skuId":"100012044001","wareName":"奥特曼 手办 模型","price":"66.00",'
        '"shopName":"手办旗舰店","shopId":"9101","commentCount":"800+条评价"},'
        '{"skuId":"100012044002","wareName":"奥特曼 拼图","price":"25.50"}'
        "]}})"
    )
    result = PageResult(
        url="https://search.jd.com/Search?keyword=x",
        html="",
        captured=[
            CapturedResponse(
                url="https://api.m.jd.com/client.action",
                text=body,
                content_type="text/plain",
            )
        ],
    )
    items = platform.parse_search(result, "奥特曼", 1, None)
    assert len(items) == 2
    assert items[0].product_id == "100012044001"
    assert items[0].price == Decimal("66.00")
    assert items[0].shop_id == "9101"
    assert items[1].price == Decimal("25.50")


def test_1688_jsonp_xhr_records(make_config):
    platform = _platform("1688", make_config())
    body = (
        'mtopjsonp1({"data":{"offerList":['
        '{"offerId":"720101","subject":"奥特曼 玩具 批发","price":"8.80",'
        '"companyName":"义乌玩具厂","memberId":"8201","saleQuantity":"1000+件"},'
        '{"offerId":"720102","subject":"奥特曼 卡牌","price":"2.50-3.00","companyName":"卡牌商行"}'
        "]}})"
    )
    result = PageResult(
        url="https://s.1688.com/x",
        html="",
        captured=[
            CapturedResponse(
                url="https://h5api.m.1688.com/h5/mtop.1688/x",
                text=body,
                content_type="text/plain",
            )
        ],
    )
    items = platform.parse_search(result, "奥特曼", 1, None)
    assert len(items) == 2
    assert items[0].product_id == "720101"
    assert items[0].price == Decimal("8.80")
    assert items[0].sales == 1000
    assert items[1].price == Decimal("2.50")


def test_zero_search_page_writes_debug_html_and_screenshot(make_config):
    from crawler.core.browser import StaticBrowser

    config = make_config()
    platform = _platform("jd", config)
    url = platform.build_search_url("奥特曼", 1)
    html = "<html><body><div id='J_goodsList'></div><p>未识别到商品</p></body></html>"
    platform.browser = StaticBrowser([PageResult(url=url, html=html)])
    with pytest.raises(PaginationError, match="解析为空"):
        platform.search("奥特曼", 1)
    debug_dir = config.log_root / "debug"
    html_files = sorted(debug_dir.glob("jd_*.html"))
    png_files = sorted(debug_dir.glob("jd_*.png"))
    assert len(html_files) == 1
    assert len(png_files) == 1
    assert html_files[0].read_text(encoding="utf-8") == html
    assert png_files[0].read_bytes().startswith(b"\x89PNG")
    assert any(
        "列表解析 0 条" in warning and "debug_html=" in warning
        for warning in platform.stats.warnings
    )


def test_taobao_missing_product_id_writes_debug_snapshot(make_config):
    config = make_config()
    platform = _platform("taobao", config)
    html = (
        "<html><body><div id='mainsrp-itemlist'>"
        "<div class='item'><div class='row row-2 title'><a>无链接商品</a></div>"
        "<div class='price'><strong>9.90</strong></div></div>"
        "</div></body></html>"
    )
    result = PageResult(url="https://s.taobao.com/search?q=x", html=html)
    items = platform.parse_search(result, "奥特曼", 1, None)
    assert items == []
    assert platform.stats.drop_reasons["product_id解析失败"] == 1
    assert any("product_id 缺失" in warning for warning in platform.stats.warnings)
    assert list((config.log_root / "debug").glob("taobao_*.html"))
    assert list((config.log_root / "debug").glob("taobao_*.png"))


@pytest.mark.parametrize(
    "product_id,price",
    [("15081034", "59.85"), ("10229590680286", "3.34"), ("1001", "0")],
)
def test_jd_detail_price_matches_product_not_recommendation(
    make_config, product_id, price
):
    import json

    platform = _platform("jd", make_config())
    result = PageResult(
        url=platform.detail_url(product_id),
        html="<h1>最小单价计算器</h1>",
        captured=[
            CapturedResponse(
                url="https://api.m.jd.com/detail",
                text=json.dumps(
                    {
                        "recommend": {
                            "skuId": "999999",
                            "couponPrice": "0.01",
                            "skuName": "推荐商品",
                        },
                        "price": {
                            "id": product_id,
                            "m": "119.70",
                            "op": "90.20",
                            "p": price,
                        },
                        "product": {"skuId": product_id, "skuName": "罗小黑商品标题"},
                    }
                ),
            )
        ],
    )
    patch = platform.parse_detail(result, product_id)
    assert patch.price == Decimal(price)
    assert Decimal(patch.price_raw) == Decimal(price)
    assert patch.title == "罗小黑商品标题"


def test_jd_detail_unknown_title_and_other_sku_price_stay_empty(make_config):
    platform = _platform("jd", make_config())
    patch = platform.parse_detail(
        PageResult(
            url=platform.detail_url("15081034"),
            html="<h1>最小单价计算器</h1>",
            captured=[
                CapturedResponse(
                    url="https://p.3.cn/prices/mgets",
                    text='[{"id":"J_999999","p":"0.01","title":"推荐商品"}]',
                )
            ],
        ),
        "15081034",
    )
    assert patch.price is None
    assert patch.title is None


@pytest.mark.parametrize(
    "text,price,sales",
    [
        ("¥2运费2.4元10万+件", "2.00", 100000),
        ("¥2运费2.4元10件起批", "2.00", None),
        ("运费¥2.4 10件起批", None, None),
        ("¥3.5-5.0 运费¥2 20件起订 已售300件", "3.50", 300),
    ],
)
def test_1688_mixed_price_shipping_and_quantity(make_config, text, price, sales):
    platform = _platform("1688", make_config())
    html = (
        '<div class="offer-list-row"><a class="title" '
        'href="https://detail.1688.com/offer/123456.html">葫芦兄弟玩具</a>'
        f'<div class="price">{text}</div></div>'
    )
    item = platform.parse_search(
        PageResult(url="https://s.1688.com/", html=html), "葫芦兄弟", 1, None
    )[0]
    assert item.price == (Decimal(price) if price is not None else None)
    assert item.sales == sales
    assert item.comments is None


def test_taobao_missing_product_id_json_page_also_writes_debug(make_config):
    from crawler.core.browser import StaticBrowser

    config = make_config()
    platform = _platform("taobao", config)
    url = platform.build_search_url("奥特曼", 1)
    body = '{"data":{"auctions":[{"raw_title":"无 id 商品","view_price":"1.00"}]}}'
    browser = StaticBrowser(
        [
            PageResult(
                url=url,
                html="<html><body>列表由接口返回</body></html>",
                captured=[
                    CapturedResponse(
                        url="https://h5api.m.taobao.com/h5/mtop/x",
                        text=body,
                        content_type="application/json",
                    )
                ],
            )
        ]
    )
    platform.browser = browser
    with pytest.raises(PaginationError, match="解析为空"):
        platform.search("奥特曼", 1)
    assert platform.stats.drop_reasons["product_id解析失败"] == 1
    assert any("product_id 缺失" in warning for warning in platform.stats.warnings)
    assert list((config.log_root / "debug").glob("taobao_*.html"))
    assert list((config.log_root / "debug").glob("taobao_*.png"))


def test_jd_flags_follow_final_fields(make_config):
    """shop_id/销量/主图都有值时不得再输出缺失 flag（11/5 bug）。"""
    platform = _platform("jd", make_config())
    body = (
        '{"data":{"wareList":[{'
        '"skuId":"1000999","wareName":"奥特曼 发光 手办","price":"99.00",'
        '"shopName":"奥特曼旗舰店","shopId":"3009",'
        '"commentCount":"500+条评价","sales":"1000+",'
        '"imageUrl":"//img14.360buyimg.com/n7/a.jpg"}]}}'
    )
    result = PageResult(
        url="https://search.jd.com/Search?keyword=x",
        html="",
        captured=[
            CapturedResponse(
                url="https://api.m.jd.com/client.action",
                text=body,
                content_type="application/json",
            )
        ],
    )
    items = platform.parse_search(result, "奥特曼", 1, None)
    assert len(items) == 1
    item = items[0]
    assert item.shop_id == "3009"
    assert item.sales == 1000
    assert item.main_image_url and item.comments == 500
    assert "店铺ID缺失" not in item.flags
    assert "销量缺失" not in item.flags
    assert "主图缺失" not in item.flags
    assert "评论缺失" not in item.flags
    assert "按下限估算" in item.flags


def test_taobao_detail_new_comment_selector_and_shop_subdomain(make_config):
    platform = _platform("taobao", make_config())
    html = """
    <html><body>
      <div id="J_Title"><h3 class="tb-main-title">葫芦兄弟 卫衣</h3></div>
      <div id="J_StrPrice"><em class="tb-rmb-num">59.90</em></div>
      <a href="//shop2001.taobao.com">葫芦童装旗舰店</a>
      <div class="Comment--count--abc">2.3万+</div>
      <img id="J_ImgBooth" src="//img.alicdn.com/a.jpg">
    </body></html>
    """
    patch = platform.parse_detail(
        PageResult(url="https://item.taobao.com/item.htm?id=1111111111", html=html),
        "1111111111",
    )
    assert patch.comments == 23000
    assert patch.comments_raw == "2.3万+"
    assert patch.shop_id == "2001"
    assert "店铺ID缺失" not in patch.flags


def _ice_detail_html(count="46", pid="1085008955004", extra=""):
    import json

    payload = {
        "loaderData": {
            "home": {
                "data": {
                    "res": {
                        "item": {"itemId": pid},
                        "componentsVO": {"rateVO": {"totalCount": count}},
                        "recommend": {"itemId": "other", "commentCount": "9999"},
                        "ask": {"totalCount": "888"},
                    }
                }
            }
        }
    }
    return (
        "<script>!(function () {var a = window.__ICE_APP_CONTEXT__ || {};var b = "
        + json.dumps(payload)
        + ";"
        + extra
        + "for (var k in a) {b[k] = a[k]}window.__ICE_APP_CONTEXT__=b;})();</script>"
    )


@pytest.mark.parametrize("count", ["46", "0"])
def test_taobao_ice_alias_comment_total(make_config, count):
    from crawler.core.jsonutil import extract_embedded_json

    html = _ice_detail_html(count)
    assert len(extract_embedded_json(html)) == 1
    patch = _platform("taobao", make_config()).parse_detail(
        PageResult(url="https://item.taobao.com/item.htm?id=1085008955004", html=html),
        "1085008955004",
    )
    assert patch.comments == int(count)
    assert patch.comments_raw == count


def test_taobao_ice_alias_reassignment_is_not_guessed():
    from crawler.core.jsonutil import extract_embedded_json

    assert extract_embedded_json(_ice_detail_html(extra='b = {"unknown": 1};')) == []


@pytest.mark.parametrize(
    "html,expected",
    [
        (
            '<div class="Comments--x"><div class="tabDetailItemTitle--x">用户评价<span>·</span>46</div>'
            "<span>近3个月好评率100%</span><span>有图3</span></div>",
            46,
        ),
        (
            '<div class="Comments--x"><div class="tabDetailItemTitle--x">用户评价 · 0</div></div>',
            0,
        ),
        (
            '<div class="Comments--x"><img src="count.png"><span>好评率100%</span></div>',
            None,
        ),
        (
            '<div class="Comments--x"><div class="tabDetailItemTitle--x">用户评价 · 4O</div></div>',
            None,
        ),
        ("<div>商品详情暂无评价数量</div>", None),
    ],
)
def test_taobao_semantic_comment_heading(make_config, html, expected):
    patch = _platform("taobao", make_config()).parse_detail(
        PageResult(url="https://item.taobao.com/item.htm?id=1085008955004", html=html),
        "1085008955004",
    )
    assert patch.comments == expected
    if expected is None:
        assert any("待核验" in flag for flag in patch.flags)


@pytest.mark.parametrize(
    "payload",
    [
        {"ask": {"totalCount": 888}, "recommend": {"commentCount": 9999}},
        {"item": {"itemId": "other"}, "componentsVO": {"rateVO": {"totalCount": 46}}},
    ],
)
def test_taobao_comment_total_ignores_unrelated_counts(make_config, payload):
    import json

    patch = _platform("taobao", make_config()).parse_detail(
        PageResult(
            url="https://item.taobao.com/item.htm?id=1085008955004",
            html="",
            captured=[
                CapturedResponse(
                    url="https://api.example/detail", text=json.dumps(payload)
                )
            ],
        ),
        "1085008955004",
    )
    assert patch.comments is None


def test_taobao_conflicting_comment_sources_are_pending(make_config):
    html = (
        _ice_detail_html()
        + '<div class="Comments--x"><div class="tabDetailItemTitle--x">用户评价 · 99</div></div>'
    )
    patch = _platform("taobao", make_config()).parse_detail(
        PageResult(url="https://item.taobao.com/item.htm?id=1085008955004", html=html),
        "1085008955004",
    )
    assert patch.comments is None
    assert any("冲突" in flag for flag in patch.flags)


def test_taobao_saved_failure_snapshot_replay(make_config):
    """本地真实证据回放；完整页面留在原debug目录，不复制会话信息到夹具。"""
    from pathlib import Path
    from crawler.core.jsonutil import extract_embedded_json
    from crawler.platforms.taobao import _comment_total

    path = (
        Path(__file__).resolve().parents[2]
        / "logs/debug/detail_taobao_1085008955004_20261109_151034_735886.html"
    )
    if not path.exists():
        pytest.skip("本机私有失败快照不存在；脱敏结构回归由其他用例覆盖")
    html = path.read_text(encoding="utf-8")
    payloads = extract_embedded_json(html)
    assert _comment_total(payloads, "1085008955004") == "46"
    patch = _platform("taobao", make_config()).parse_detail(
        PageResult(url="https://item.taobao.com/item.htm?id=1085008955004", html=html),
        "1085008955004",
    )
    assert patch is not None
    assert patch.comments == 46
    assert not any("评论" in flag for flag in patch.flags)
    detail = next(
        p["loaderData"]["home"]["data"]["res"]
        for p in payloads
        if isinstance(p, dict) and "loaderData" in p
    )
    assert patch.title == detail["item"]["title"]
    assert patch.price == Decimal("49.00")
    assert patch.sales == 1000
    assert patch.sales_raw == "已售 1000+"
    print(
        "真实快照回放：商品1085008955004，评论46，结构化与DOM证据一致；未改写历史记录"
    )


def test_jd_detail_comment_api_and_new_selector(make_config):
    platform = _platform("jd", make_config())
    body = '{"productCommentSummaries":[{"CommentCount":321,"CommentCountStr":"321"}]}'
    result = PageResult(
        url="https://item.jd.com/1001.html",
        html="",
        captured=[
            CapturedResponse(
                url="https://club.jd.com/comment/productCommentSummaries.action",
                text=body,
                content_type="application/json",
            )
        ],
    )
    patch = platform.parse_detail(result, "1001")
    assert patch.comments == 321
    assert patch.comments_raw == "321"

    html = """
    <html><body>
      <div class="sku-name">葫芦兄弟 卫衣</div>
      <div class="product-price-panel--options-comment-count">5万+</div>
      <a href="//mall.jd.com/index-3001.html">葫芦旗舰店</a>
    </body></html>
    """
    patch2 = platform.parse_detail(
        PageResult(url="https://item.jd.com/1002.html", html=html), "1002"
    )
    assert patch2.comments == 50000
    assert patch2.shop_id == "3001"
    assert "店铺ID缺失" not in patch2.flags


def test_1688_detail_dsr_comment_count_and_member_id(make_config):
    platform = _platform("1688", make_config())
    body = (
        '{"data":{"model":{"commonTagNodeList":['
        '{"name":"有图","count":6},{"name":"全部","count":2497}]},'
        '"sellerMemberId":"b2b-2221270032164774cc"}}'
    )
    result = PageResult(
        url="https://detail.1688.com/offer/6001.html",
        html="",
        captured=[
            CapturedResponse(
                url="https://h5api.m.1688.com/h5/mtoprateservice.querydsrratedatav2/1.0/",
                text=body,
                content_type="application/json",
            )
        ],
    )
    patch = platform.parse_detail(result, "6001")
    assert patch.comments == 2497
    assert patch.comments_raw == "2497"
    assert patch.shop_id == "b2b-2221270032164774cc"


def test_1688_dom_shop_id_from_subdomain(make_config):
    platform = _platform("1688", make_config())
    html = """
    <html><body>
      <div class="offer-list-row">
        <a class="title" href="//detail.1688.com/offer/123456789.html">奥特曼 玩具 批发</a>
        <span class="price">¥ 3.50</span>
        <a href="//shop0c1039px78200.1688.com">义乌市奥特曼玩具厂</a>
        <span class="sale-quantity">已售1000+件</span>
      </div>
    </body></html>
    """
    items = platform.parse_search(
        PageResult(url="https://s.1688.com/x", html=html), "奥特曼", 1, None
    )
    assert len(items) == 1
    assert items[0].shop_id == "0c1039px78200"
    assert "店铺ID缺失" not in items[0].flags


def test_taobao_20261106_real_shell_parses_mtop_records(make_config):
    """11/6 WU 实采空壳页 + mtop 响应：列表必须能解析出商品且字段齐全。

    fixtures/taobao_search_20261106_shell.html 是 WU 上真实保存的
    s.taobao.com 搜索页（34 KB，只有骨架屏，无任何商品节点）；商品数据来自
    页面加载后的 mtop JSONP 响应。回归目标是“空壳 DOM + mtop 记录”这条真实
    代码路径，而不是再造一份静态 HTML。
    """
    platform = _platform("taobao", make_config())
    result = PageResult(
        url="https://s.taobao.com/search?q=%E5%A5%A5%E7%89%B9%E6%9B%BC&page=1",
        html=fixture_text("taobao_search_20261106_shell.html"),
        captured=[
            CapturedResponse(
                url="https://h5api.m.taobao.com/h5/mtop.taobao.wsearch.pcsearch/1.0/",
                text=fixture_text("taobao_search_20261106_mtop.txt"),
                content_type="text/plain",
                status=200,
            )
        ],
    )
    items = platform.parse_search(result, "奥特曼", 1, "ultraman")
    by_id = {item.product_id: item for item in items}
    assert len(items) == 3
    assert set(by_id) == {"820123456789", "820123456790", "820123456791"}

    first = by_id["820123456789"]
    assert first.platform == "taobao"
    assert first.title == "奥特曼 玩具 男孩 正版 发光 变身器 手办"
    assert first.price == Decimal("89.00")
    assert first.price_raw == "¥ 89.00"
    assert first.shop_id == "2200123456"
    assert first.shop_name == "奥特曼玩具旗舰店"
    assert first.sales == 20000
    assert first.sales_raw == "2万+人付款"
    assert first.main_image_url == "https://img.alicdn.com/imgextra/i1/820123456789.jpg"
    assert first.url == "https://item.taobao.com/item.htm?id=820123456789"
    assert first.rank == 1
    assert first.search_keyword == "奥特曼"
    assert "评论缺失" in first.flags  # 列表接口无评论数，按口径标记而不是补 0/京东值

    second = by_id["820123456790"]
    assert second.platform == "tmall"
    assert second.price == Decimal("39.90")  # 区间取最低
    assert second.shop_id == "2200123457"  # 嵌套 seller 对象
    assert second.comments == 320
    assert second.comments_raw == "320+条评价"
    assert "天猫" in (second.shop_label_raw or "")

    third = by_id["820123456791"]
    assert third.comments is None
    assert "评论缺失" in third.flags


def test_taobao_20261106_real_shell_without_payload_logs_reason(make_config):
    """真实空壳页本身没有商品数据：必须 0 条并给出可操作的明确原因。"""
    config = make_config()
    platform = _platform("taobao", config)
    result = PageResult(
        url="https://s.taobao.com/search?q=%E5%A5%A5%E7%89%B9%E6%9B%BC&page=1",
        html=fixture_text("taobao_search_20261106_shell.html"),
    )
    items = platform.parse_search(result, "奥特曼", 1, None)
    assert items == []
    warning = next(w for w in platform.stats.warnings if "列表解析 0 条" in w)
    assert "CSR 空壳页" in warning
    assert "mtop" in warning
    # 不能把壳页里的 loadingBox 误当成商品卡片记 product_id 缺失。
    assert platform.stats.drop_reasons.get("product_id解析失败", 0) == 0
    assert list((config.log_root / "debug").glob("taobao_*.html"))
    assert list((config.log_root / "debug").glob("taobao_*.png"))
