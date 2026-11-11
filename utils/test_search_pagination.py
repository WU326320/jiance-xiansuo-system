"""分页单元与真实浏览器/接口回放；外部请求均被本地 route 隔离。"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "crawler"))
from crawler.core.browser import (
    PageResult,
    PaginationState,
    ScraplingBrowser,
    StaticBrowser,
    SEARCH_CARD_SIGNATURE,
    search_pagination,
)
from crawler.core.errors import PaginationError
from crawler.core.progress import SearchProgress
from crawler.platforms import create_platform
from crawler.platforms.selectors import SEARCH_PAGINATION
from crawler.tests.conftest import fixture_text, make_config


@pytest.fixture(scope="module")
def real_browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as driver:
        browser = driver.chromium.launch(channel="msedge", headless=True)
        yield browser
        browser.close()


def setup_platform(name, make_config, browser=None):
    config = make_config()
    config.crawl.max_pages = 5
    config.crawl.taobao.max_pages = 5
    config.crawl.timeout_ms = 2500
    config.crawl.page_wait_ms = 0
    config.crawl.page_wait_ms_range = (0, 0)
    platform = create_platform(name, config, browser or StaticBrowser([]))
    platform.page_action = lambda page: None
    platform.after_search = lambda *args: None
    platform.search_progress = SearchProgress(
        config.state_root, name, [{"keyword": "葫芦兄弟", "ip_code": None}]
    )
    return platform


def document(name, number):
    if name == "1688_current":
        return "".join(
            f'<div class="gridCell--test"><a class="offerCard--test" '
            f'href="https://detail.1688.com/offer/{number}78000{index}.html?uuid=trace{number}">'
            f'<div class="offerTitle--test">葫芦兄弟玩偶{number}-{index}</div>'
            '<span class="price--test">￥19.80</span><span class="sale--test">已售20件</span>'
            '<span class="company-name">示例工厂</span></a></div>'
            for index in range(3)
        )
    filename = {
        "taobao": "taobao_search.html",
        "jd": "jd_search.html",
        "1688": "ali1688_search.html",
    }[name]
    original = fixture_text(filename)
    return re.sub(r"\b\d{4,}\b", lambda m: str(number) + m.group(), original)


def controls(name, number, *, more=False):
    disabled = ' disabled aria-disabled="true"' if number == 3 else ""
    if more:
        return (
            '<div class="no-more">没有更多了</div>'
            if number == 3
            else '<button onclick="advance()">加载更多</button>'
        )
    if name == "jd":
        return f'<div class="_pagination_center_test"><div class="_pagination_item_test _active_test">{number}</div><div class="_pagination_total_test">共3页</div><div class="_pagination_next_test"{disabled} onclick="advance()">下一页</div></div>'
    if name == "1688_current":
        arrow = "arrowDisabled--test" if number == 3 else ""
        return f'<div class="pagination--test"><div class="pagingTotal--test">共<span>3</span>页</div><div class="pageItem--test pageCurrent--test">{number}</div><div class="nextArrow--test {arrow}" onclick="advance()" style="width:24px;height:24px"></div></div>'
    return f'<div class="next-pagination"><button class="next-current">{number}</button><span>共3页</span><button class="next-next"{disabled} onclick="advance()">下一页</button></div>'


@pytest.mark.parametrize("name", ["taobao", "jd", "1688", "1688_current"])
@pytest.mark.parametrize("mode", ["xhr", "href", "more"])
def test_three_real_pages_collect_and_end(make_config, real_browser, name, mode):
    platform = setup_platform("1688" if name == "1688_current" else name, make_config)
    # 不受普通页数上限影响的持续模式，必须由末页证据自行结束。
    platform.continuous = True
    requests = []
    checkpoints = []
    with real_browser.new_context() as context:

        def serve(route):
            parts = urlsplit(route.request.url)
            if parts.hostname != "pagination.test":
                return route.abort()
            if parts.path == "/api/list":
                number = json.loads(route.request.post_data)["page"]
                requests.append(number)
                return route.fulfill(
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "html": document(name, number),
                            "controls": controls(name, number, more=mode == "more"),
                        }
                    ),
                )
            number = int(parse_qs(parts.query).get("actual", ["1"])[0])
            requests.append(number)
            ctrl = controls(name, number, more=mode == "more")
            if mode == "href" and number < 3:
                ctrl = ctrl.replace(
                    'onclick="advance()"',
                    f"onclick=\"location.href='/search?actual={number + 1}'\"",
                )
            script = """let n=NUMBER; window.clicks=0;
                async function advance() {
                    window.clicks++;
                    const data = await (await fetch('/api/list', {method:'POST',body:JSON.stringify({page:++n})})).json();
                    setTimeout(() => {
                        if (APPEND) document.querySelector('#list').insertAdjacentHTML('beforeend',data.html);
                        else document.querySelector('#list').innerHTML=data.html;
                        document.querySelector('#paging').innerHTML=data.controls;
                    }, 120);
                }
            """.replace("NUMBER", str(number)).replace(
                "APPEND", "true" if mode == "more" else "false"
            )
            body = f'<html><body><button aria-pressed="true">销量</button><div id="list">{document(name, number)}</div><div id="paging">{ctrl}</div><script>{script}</script></body></html>'
            route.fulfill(content_type="text/html; charset=utf-8", body=body)

        context.route("**/*", serve)
        browser = ScraplingBrowser(platform.config, platform.name)
        browser._session = SimpleNamespace(context=context)
        platform.browser = browser
        platform.build_search_url = lambda keyword, page: (
            f"https://pagination.test/search?ignored={page}"
        )
        platform.checkpoint_items = lambda items: checkpoints.append(
            sorted({item.page for item in items})
        )
        items = platform.search("葫芦兄弟", 1)
        assert requests == [1, 2, 3]
        assert {item.page for item in items} == {1, 2, 3}
        assert len({(item.platform, item.product_id) for item in items}) == len(items)
        per_page = len(
            platform.parse_search(
                PageResult(url="", html=document(name, 1)), "葫芦兄弟", 1, None
            )
        )
        assert len(items) == per_page * 3
        state = platform.search_progress.position("葫芦兄弟", None)
        assert state["last_completed_page"] == 3 and state["done"]
        assert state["stop_reason"] == platform.search_stop_reason == "last_page"
        assert [1, 2] in checkpoints
        print(
            json.dumps(
                {
                    "platform": name,
                    "mode": mode,
                    "pages": 3,
                    "items": len(items),
                    "stop": "last_page",
                    "uncompleted": 0,
                },
                ensure_ascii=False,
            )
        )


@pytest.mark.parametrize(
    "failure",
    ["unchanged", "http_503", "missing_control", "cycle", "disabled_while_loading"],
)
def test_browser_failure_keeps_uncompleted_page(make_config, real_browser, failure):
    platform = setup_platform("jd", make_config)
    requests = []
    with real_browser.new_context() as context:

        def serve(route):
            if urlsplit(route.request.url).path == "/api/list":
                number = json.loads(route.request.post_data)["page"]
                requests.append(number)
                if failure == "http_503":
                    return route.fulfill(status=503, body="unavailable")
                product_page = 1 if failure == "unchanged" or number == 3 else number
                return route.fulfill(
                    content_type="application/json",
                    body=json.dumps(
                        {"html": document("jd", product_page), "number": number}
                    ),
                )
            ctrl = controls("jd", 1)
            if failure == "missing_control":
                ctrl = ""
            if failure == "disabled_while_loading":
                ctrl = ctrl.replace(
                    'onclick="advance()"', 'disabled onclick="advance()"'
                )
            body = (
                '<html><body><button aria-pressed="true">销量</button><main id="list">'
                + document("jd", 1)
                + "</main>"
                + ctrl
                + """<script>
                let n=1; async function advance() {
                    const response = await fetch('/api/list', {method:'POST',body:JSON.stringify({page:++n})});
                    if (!response.ok) return;
                    const data=await response.json(); document.querySelector('#list').innerHTML=data.html;
                    document.querySelector('._pagination_item_test').textContent=n;
                }
                </script></body></html>"""
            )
            route.fulfill(content_type="text/html; charset=utf-8", body=body)

        context.route("**/*", serve)
        browser = ScraplingBrowser(platform.config, "jd")
        browser._session = SimpleNamespace(context=context)
        platform.browser = browser
        platform.build_search_url = lambda keyword, page: (
            f"https://pagination.test/search?page={page}"
        )
        with pytest.raises(PaginationError):
            platform.search("葫芦兄弟", 5)
        state = platform.search_progress.position("葫芦兄弟", None)
        completed = 2 if failure == "cycle" else 1
        assert state["last_completed_page"] == completed
        assert state["next_page"] == completed + 1
        assert not state["done"]
        assert state["stop_reason"] == "pagination_blocked"
        assert len(platform.search_progress.rows) > 0
        assert len(requests) <= 2


@pytest.mark.parametrize("continuous", [False, True])
@pytest.mark.parametrize("failure", ["empty", "repeat", "cycle"])
def test_unconfirmed_static_results_are_not_completed(make_config, continuous, failure):
    platform = setup_platform("jd", make_config)
    platform.continuous = continuous
    sequence = [1, 2, 1] if failure == "cycle" else [1, 1]
    platform.browser = StaticBrowser(
        [
            PageResult(
                url="https://search.jd.com/",
                html=(
                    "<html>loading</html>"
                    if failure == "empty" and i == 1
                    else document("jd", number)
                ),
            )
            for i, number in enumerate(sequence)
        ]
    )
    with pytest.raises(PaginationError):
        platform.search("葫芦兄弟", 5)
    state = platform.search_progress.position("葫芦兄弟", None)
    assert state["next_page"] == len(sequence)
    assert not state["done"]
    recovered = SearchProgress(
        platform.config.state_root, "jd", [{"keyword": "葫芦兄弟", "ip_code": None}]
    )
    assert recovered.position("葫芦兄弟", None)["next_page"] == len(sequence)


def test_explicit_empty_result_is_complete(make_config):
    platform = setup_platform("jd", make_config)
    platform.browser = StaticBrowser(
        [
            PageResult(
                url="https://search.jd.com/",
                html="<div>暂无结果</div>",
                pagination=PaginationState(has_next=False, empty_confirmed=True),
            )
        ]
    )
    assert platform.search("葫芦兄弟", 5) == []
    assert (
        platform.search_progress.position("葫芦兄弟", None)["stop_reason"]
        == "no_results"
    )


def test_disabled_control_with_more_pages_is_not_terminal(real_browser):
    with real_browser.new_context() as context:
        page = context.new_page()
        page.set_content(
            controls("jd", 1).replace(
                'onclick="advance()"', 'disabled onclick="advance()"'
            )
        )
        assert search_pagination(page, SEARCH_PAGINATION).has_next is None


def test_1688_disabled_arrow_during_loading_is_not_last_page(real_browser):
    with real_browser.new_context() as context:
        page = context.new_page()
        page.set_content(
            controls("1688_current", 1).replace(
                "nextArrow--test ", "nextArrow--test arrowDisabled--test"
            )
        )
        state = search_pagination(page, SEARCH_PAGINATION)
        assert state.current_page == 1 and state.total_pages == 3
        assert state.has_next is None


@pytest.mark.parametrize(
    "card",
    [
        '<a class="card" data-spm-act-id="123456" href="https://click.simba.taobao.com/cc_im?e=TRACK">商品</a>',
        '<a class="card" href="https://detail.tmall.com/item.htm?id=123456&priceTId=TRACK">商品</a>',
        '<a class="card" href="https://detail.1688.com/offer/123456.html?uuid=TRACK">商品</a>',
    ],
)
def test_product_signatures_ignore_tracking_queries(real_browser, card):
    with real_browser.new_context() as context:
        page = context.new_page()
        page.set_content(card.replace("TRACK", "before"))
        before = page.locator(".card").evaluate_all(SEARCH_CARD_SIGNATURE)
        page.set_content(card.replace("TRACK", "after"))
        after = page.locator(".card").evaluate_all(SEARCH_CARD_SIGNATURE)
        assert before and before == after
        page.set_content(card.replace("123456", "654321"))
        assert page.locator(".card").evaluate_all(SEARCH_CARD_SIGNATURE) != before


@pytest.mark.parametrize("name", ["taobao", "jd", "1688"])
def test_search_ignores_legacy_quantity_caps(make_config, monkeypatch, name):
    monkeypatch.delenv("CRAWLER_ENDS_AT", raising=False)
    platform = setup_platform(name, make_config)
    platform.config.crawl.max_pages = 1
    platform.config.crawl.max_items_per_keyword = 1
    platform.config.crawl.taobao.max_pages = 1
    platform.browser = StaticBrowser(
        [
            PageResult(
                url="https://pagination.test/",
                html=document(name, number),
                pagination=PaginationState(has_next=number < 4, current_page=number),
            )
            for number in range(1, 5)
        ]
    )
    items = platform.search("葫芦兄弟", 1)
    assert {item.page for item in items} == {1, 2, 3, 4}
    assert len(items) > 4
    state = platform.search_progress.position("葫芦兄弟", None)
    assert state["done"] and state["last_completed_page"] == 4
    assert state["stop_reason"] == "last_page"


@pytest.mark.parametrize("name", ["taobao", "jd", "1688"])
def test_untimed_runner_collects_all_pages_keywords_and_details(
    make_config, monkeypatch, name
):
    from crawler import runner
    from crawler.models import Item, ItemPatch
    from crawler.core.jsonl import read_jsonl

    monkeypatch.delenv("CRAWLER_ENDS_AT", raising=False)
    monkeypatch.setenv("CRAWLER_AMOUNT_DETAIL", "1")
    config = make_config()
    config.crawl.max_pages = config.crawl.max_items_per_keyword = 1
    config.crawl.daily_max_pages = 1
    config.crawl.taobao.max_keywords_per_run = config.crawl.taobao.max_pages = 1
    config.crawl.taobao.batch.max_pages_per_segment = 1
    config.crawl.taobao.batch.max_items_per_segment = 1
    config.crawl.taobao.detail.max_per_run = 1
    config.crawl.taobao.detail.complete_missing_amount = False
    config.crawl.detail.platforms[name] = True
    keywords = [f"关键词{n}" for n in range(6)]
    pages, details = [], []
    original_factory = runner.create_platform

    def factory(*args, **kwargs):
        platform = original_factory(*args, **kwargs)

        def request(url, *, search_page_number, **unused):
            platform.throttle.reserve_request()
            pages.append(search_page_number)
            return PageResult(
                url=url,
                html="<html>商品列表</html>",
                pagination=PaginationState(
                    has_next=search_page_number < 4, current_page=search_page_number
                ),
            )

        def parse(result, keyword, page, ip_code):
            return [
                Item(
                    platform=name,
                    product_id=f"{keywords.index(keyword) + 1}00{page}",
                    title="葫芦兄弟玩偶",
                    url="https://example.test/item",
                    search_keyword=keyword,
                    ip_code=ip_code,
                    page=page,
                    crawled_at=platform.now(),
                )
            ]

        def detail(ids):
            patches = []
            for pid in ids:
                platform.throttle.reserve_request()
                details.append(pid)
                patch = ItemPatch(
                    platform=name,
                    product_id=pid,
                    price="12.50",
                    sales=0,
                    shop_name="完整店铺",
                    title="葫芦兄弟玩偶",
                )
                platform.checkpoint_patch(patch)
                patches.append(patch)
            return patches

        platform._request = request
        platform.parse_search = parse
        platform.fetch_detail = detail
        platform.after_search = lambda *args: None
        return platform

    monkeypatch.setattr(runner, "create_platform", factory)
    monkeypatch.setattr(runner, "check_task_login", lambda crawler: True)
    record = runner.run_platform(
        name,
        config,
        date="2026-11-11",
        keywords=keywords,
        browser_factory=lambda *args: StaticBrowser([]),
    )
    assert record.status == "ok", record.errors
    assert pages == [1, 2, 3, 4] * 6
    assert len(details) == len(set(details)) == 24
    rows = list(read_jsonl(config.output_root / "2026-11-11" / f"{name}.jsonl"))
    assert len(rows) == 24 and record.keywords_done == 6
    assert all(row["shop_name"] == "完整店铺" and row["sales"] == 0 for row in rows)


def test_standalone_taobao_details_have_no_quantity_cap(make_config):
    import logging
    from crawler.runner import _taobao_detail_candidates
    from crawler.core.stats import RunStats

    config = make_config()
    config.crawl.taobao.detail.max_per_run = 1
    ids = [str(n) for n in range(1000, 1040)]
    assert (
        _taobao_detail_candidates(
            config, "taobao", "2026-11-11", ids, logging.getLogger("test"), RunStats()
        )
        == ids
    )


def test_1688_shop_backfill_has_no_quantity_cap(make_config, monkeypatch):
    from crawler.models import Item

    platform = create_platform("1688", make_config(), StaticBrowser([]))
    platform.config.crawl.ali1688.shop_name_backfill.max_per_run = 1
    platform.complete_fields = True
    items = [
        Item(
            platform="1688",
            product_id=str(n),
            title="葫芦兄弟玩偶",
            search_keyword="葫芦兄弟",
            url=f"https://detail.1688.com/offer/{n}.html",
            crawled_at=platform.now(),
        )
        for n in range(1000, 1040)
    ]
    requested = []

    def fetch(item, **kwargs):
        requested.append(item.product_id)
        return "完整店铺"

    monkeypatch.setattr(platform, "_fetch_shop_name", fetch)
    platform._backfill_missing_shop_names("葫芦兄弟", items)
    assert len(requested) == 40
    assert all(item.shop_name == "完整店铺" for item in items)
    assert not platform.detail_failures
