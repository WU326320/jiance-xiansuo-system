from __future__ import annotations

import json
import pytest
from urllib.parse import quote_plus, urlsplit, parse_qs

from crawler.core.browser import (
    CapturedResponse,
    PageResult,
    PaginationState,
    StaticBrowser,
)
from crawler.core.jsonl import read_jsonl
from crawler.runner import build_keyword_matrix, run_detail, run_platform, run_platforms
from crawler.tests.conftest import assert_private_mode, fixture_text


def _captured(name: str, url: str = "https://h5api.example/api") -> CapturedResponse:
    return CapturedResponse(
        url=url, text=fixture_text(name), content_type="application/json", status=200
    )


def _login_page() -> PageResult:
    return PageResult(
        url="https://i.taobao.com/my_taobao.htm",
        html="<html><body><h1>我的淘宝</h1><p>账号正常</p></body></html>",
    )


def _search_url(keyword: str, page: int = 1) -> str:
    return f"https://s.taobao.com/search?q={quote_plus(keyword)}&page={page}"


def _search_page(keyword: str, page: int = 1, *, has_next: bool = False) -> PageResult:
    return PageResult(
        url=_search_url(keyword, page),
        html="",
        captured=[_captured("taobao_search.json")],
        pagination=PaginationState(has_next=has_next, current_page=page),
    )


def _factory(browser):
    return lambda config, platform, headless: browser


def _search_requests(browser):
    return [
        (query["q"][0], int(query["page"][0]))
        for url in browser.calls
        if "q" in (query := parse_qs(urlsplit(url).query)) and "page" in query
    ]


def test_resume_failed_second_page_across_output_directories(
    make_config, tmp_path, monkeypatch
):
    from crawler.core.progress import SearchProgress
    from crawler.platforms.base import PlatformBase

    monkeypatch.delenv("CRAWLER_ENDS_AT", raising=False)
    monkeypatch.setattr(PlatformBase, "after_search", lambda *args: None)
    config = make_config()
    config.crawl.max_pages = 2
    config.crawl.taobao.max_pages = 2
    failed = PageResult(
        url=_search_url("葫芦兄弟", 2), html=fixture_text("login_taobao.html")
    )
    first_browser = StaticBrowser(
        [_login_page(), _search_page("葫芦兄弟", has_next=True), failed]
    )
    first = run_platform(
        "taobao",
        config,
        date="2026-11-05",
        max_pages=2,
        browser_factory=_factory(first_browser),
    )
    assert first.status == "login_failed"
    assert first.items_ok == 3
    progress = SearchProgress(config.state_root, "taobao", build_keyword_matrix(config))
    assert progress.resumed
    assert progress.position("葫芦兄弟", "huluxiongdi")["next_page"] == 2
    assert progress.state["current"]["page"] == 2
    assert len(progress.rows) == 3
    assert any("第 2 页继续" in error for error in first.errors)

    config.crawl.output_dir = str(tmp_path / "retry-output")
    empty = PageResult(
        url=_search_url("葫芦兄弟", 2),
        html="<html>没有结果</html>",
        pagination=PaginationState(has_next=False, empty_confirmed=True),
    )
    monkeypatch.setattr(
        "crawler.platforms.taobao.TaobaoPlatform.on_empty_result", lambda *args: None
    )
    second_browser = StaticBrowser([_login_page(), empty])
    second = run_platform(
        "taobao",
        config,
        date="2026-11-06",
        max_pages=2,
        browser_factory=_factory(second_browser),
    )
    assert second.status == "ok"
    assert second.items_ok == 3
    assert _search_requests(second_browser) == [("葫芦兄弟", 2)]
    assert (
        len(list(read_jsonl(config.output_root / "2026-11-06" / "taobao.jsonl"))) == 3
    )
    assert not SearchProgress(
        config.state_root, "taobao", build_keyword_matrix(config)
    ).resumed


def test_progress_scope_and_interrupted_page(make_config):
    from crawler.core.progress import SearchProgress

    config = make_config()
    matrix = build_keyword_matrix(config)
    progress = SearchProgress(config.state_root, "taobao", matrix)
    progress.begin("葫芦兄弟", "huluxiongdi", 1)
    progress.commit("葫芦兄弟", "huluxiongdi", 1, [])
    progress.begin("葫芦兄弟", "huluxiongdi", 2)
    recovered = SearchProgress(config.state_root, "taobao", matrix)
    assert recovered.position("葫芦兄弟", "huluxiongdi")["next_page"] == 2
    assert not SearchProgress(config.state_root, "jd", matrix).resumed
    assert not SearchProgress(
        config.state_root, "taobao", [{"keyword": "不同词", "ip_code": "other"}]
    ).resumed
    recovered.finish_keyword("葫芦兄弟", "huluxiongdi")
    assert SearchProgress(config.state_root, "taobao", matrix).position(
        "葫芦兄弟", "huluxiongdi"
    )["done"]


def test_resume_skips_finished_keyword_after_interruption(
    make_config, tmp_path, monkeypatch
):
    from crawler.platforms.base import PlatformBase
    from crawler.core.progress import SearchProgress

    monkeypatch.delenv("CRAWLER_ENDS_AT", raising=False)
    config = make_config()
    first_entry = config.crawl.keywords[0]
    config.crawl.keywords.append(first_entry.model_copy(update={"keyword": "葫芦娃"}))
    original = PlatformBase.search

    def interrupt(self, keyword, *args, **kwargs):
        if keyword == "葫芦娃":
            self.search_progress.begin(keyword, "huluxiongdi", 1)
            raise KeyboardInterrupt
        return original(self, keyword, *args, **kwargs)

    monkeypatch.setattr(PlatformBase, "search", interrupt)
    browser = StaticBrowser([_login_page(), _search_page("葫芦兄弟")])
    with pytest.raises(KeyboardInterrupt):
        run_platform(
            "taobao",
            config,
            date="2026-11-05",
            max_pages=1,
            browser_factory=_factory(browser),
        )
    progress = SearchProgress(config.state_root, "taobao", build_keyword_matrix(config))
    assert progress.resumed and progress.state["status"] == "interrupted"
    assert progress.position("葫芦兄弟", "huluxiongdi")["done"]
    monkeypatch.setattr(PlatformBase, "search", original)
    retry = StaticBrowser([_login_page(), _search_page("葫芦娃")])
    record = run_platform(
        "taobao",
        config,
        date="2026-11-06",
        max_pages=1,
        browser_factory=_factory(retry),
    )
    assert record.status == "ok" and record.items_ok == 3
    assert _search_requests(retry) == [("葫芦娃", 1)]


def test_corrupt_progress_is_reported_without_overwrite(make_config):
    from crawler.core.progress import SearchProgress

    config = make_config()
    matrix = build_keyword_matrix(config)
    progress = SearchProgress(config.state_root, "taobao", matrix)
    progress.begin("葫芦兄弟", "huluxiongdi", 2)
    progress.path.write_text("{broken", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        SearchProgress(config.state_root, "taobao", matrix)
    assert progress.path.read_text(encoding="utf-8") == "{broken"


@pytest.mark.parametrize("detail", [False, True])
def test_first_login_waits_for_authorization_then_continues(
    make_config, monkeypatch, detail
):
    config = make_config()
    login = PageResult(
        url="https://login.taobao.com/member/login.jhtml",
        html=fixture_text("login_taobao.html"),
    )
    detail_page = PageResult(
        url="https://item.taobao.com/item.htm?id=1111111111",
        html="",
        captured=[_captured("taobao_detail.json")],
    )

    class FirstLoginBrowser(StaticBrowser):
        headless = False

        def _next_page(self, url):
            self._last = self.pages.pop(0)
            return self._last

    browser = FirstLoginBrowser(
        [login, _login_page(), detail_page if detail else _search_page("葫芦兄弟")]
    )
    monkeypatch.delenv("CRAWLER_ENDS_AT", raising=False)
    clock = [0.0]
    waits = []
    monkeypatch.setattr("crawler.login.time.monotonic", lambda: clock[0])

    def wait(seconds):
        assert not browser.closed
        assert len(browser.calls) == 1  # 等待扫码时不得刷新页面
        waits.append(seconds)
        clock[0] += seconds
        if clock[0] >= 6:
            browser._last = _login_page()
            browser.pages = [detail_page if detail else _search_page("葫芦兄弟")]

    monkeypatch.setattr("crawler.login.time.sleep", wait)
    browser.cookie_values = lambda: {"_nk_": "test-user"} if clock[0] >= 6 else {}
    if detail:
        record = run_detail(
            "taobao",
            config,
            ids=["1111111111"],
            date="2026-11-05",
            browser_factory=_factory(browser),
        )
    else:
        record = run_platform(
            "taobao",
            config,
            date="2026-11-05",
            max_pages=1,
            browser_factory=_factory(browser),
        )
    assert waits and clock[0] >= 6
    assert record.status == "ok" and record.login_ok
    assert record.items_ok > 0 and browser.closed


def test_deadline_checkpoint_exists_before_keyword_backfill(
    make_config, tmp_path, monkeypatch
):
    import pytest
    from crawler.platforms.base import PlatformBase

    monkeypatch.setenv("CRAWLER_ENDS_AT", "2099-01-01 10:00:00")
    config = make_config()

    class DeadlineBrowser(StaticBrowser):
        def fetch_page(self, url, **kwargs):
            result = super().fetch_page(url, **kwargs)
            if urlsplit(url).path == "/search":
                monkeypatch.setenv("CRAWLER_ENDS_AT", "2000-01-01 00:00:00")
            return result

    browser = DeadlineBrowser([_login_page(), _search_page("葫芦兄弟")])

    def interrupted_backfill(self, keyword, items):
        path = tmp_path / "out" / "2026-11-05" / "taobao.partial.json"
        rows = json.loads(path.read_text(encoding="utf-8"))
        assert len(rows) == len(items) == 3
        assert {row["product_id"] for row in rows} == {
            item.product_id for item in items
        }
        raise KeyboardInterrupt

    monkeypatch.setattr(PlatformBase, "after_search", interrupted_backfill)
    with pytest.raises(KeyboardInterrupt):
        run_platform(
            "taobao",
            config,
            date="2026-11-05",
            max_pages=1,
            browser_factory=_factory(browser),
        )
    assert browser.closed
    assert (
        len(
            json.loads(
                (tmp_path / "out" / "2026-11-05" / "taobao.partial.json").read_text(
                    encoding="utf-8"
                )
            )
        )
        == 3
    )


def test_deadline_detail_flushes_each_patch(make_config, tmp_path, monkeypatch):
    from crawler.platforms.base import PlatformBase
    from crawler.models import ItemPatch

    monkeypatch.setenv("CRAWLER_ENDS_AT", "2099-01-01 10:00:00")
    config = make_config()
    browser = StaticBrowser([_login_page()])

    def fetch_detail(self, ids):
        patch = ItemPatch(platform="taobao", product_id="1111111111", comments=17)
        self.checkpoint_patch(patch)
        rows = list(read_jsonl(tmp_path / "out" / "2026-11-05" / "taobao_detail.jsonl"))
        assert len(rows) == 1 and rows[0]["comments"] == 17
        return [patch]

    monkeypatch.setattr(PlatformBase, "fetch_detail", fetch_detail)
    record = run_detail(
        "taobao",
        config,
        ids=["1111111111"],
        date="2026-11-05",
        browser_factory=_factory(browser),
    )
    assert record.items_ok == 1
    assert record.items_duplicate == 0


def test_run_platform_end_to_end(make_config, tmp_path):
    config = make_config()
    browser = StaticBrowser([_login_page(), _search_page("葫芦兄弟")])
    record = run_platform(
        "taobao",
        config,
        date="2026-11-05",
        max_pages=1,
        browser_factory=_factory(browser),
    )
    assert record.status == "ok"
    assert record.login_ok is True
    assert record.keywords_done == 1
    assert record.pages_fetched == 2  # 登录检查 + 搜索
    assert record.items_ok == 3
    assert record.output_path
    rows = list(read_jsonl(tmp_path / "out" / "2026-11-05" / "taobao.jsonl"))
    assert len(rows) == 3
    assert rows[0]["platform"] == "taobao"
    assert rows[1]["platform"] == "tmall"
    assert all("销量排序未确认" in row["flags"] for row in rows)
    assert any("销量排序未确认" in warning for warning in record.warnings)
    saved_record = json.loads(
        (tmp_path / "out" / "2026-11-05" / "taobao.run.json").read_text(
            encoding="utf-8"
        )
    )
    assert saved_record["status"] == "ok"
    assert any("销量排序未确认" in warning for warning in saved_record["warnings"])
    assert (tmp_path / "out" / "2026-11-05" / "taobao.run.json").exists()
    assert (tmp_path / "out" / "2026-11-05" / "run_logs.jsonl").exists()
    assert (tmp_path / "logs" / "2026-11-05_taobao.log").exists()
    assert browser.closed is True


def test_run_platform_dedupes_across_keywords(make_config, tmp_path):
    config = make_config()
    config.crawl.keywords = [
        type(config.crawl.keywords[0])(keyword="葫芦兄弟", ip_code="huluxiongdi"),
        type(config.crawl.keywords[0])(keyword="葫芦娃", ip_code="huluxiongdi"),
    ]
    browser = StaticBrowser(
        [
            _login_page(),
            _search_page("葫芦兄弟"),
            _search_page("葫芦娃"),
        ]
    )
    record = run_platform(
        "taobao",
        config,
        date="2026-11-05",
        max_pages=1,
        browser_factory=_factory(browser),
    )
    assert record.items_ok == 3
    assert record.items_duplicate == 3
    rows = read_jsonl(tmp_path / "out" / "2026-11-05" / "taobao.jsonl")
    assert len(rows) == 3


def test_run_platform_login_failure_stops(make_config, tmp_path):
    config = make_config()
    browser = StaticBrowser(
        [
            PageResult(
                url="https://login.taobao.com/member/login.jhtml",
                html=fixture_text("login_taobao.html"),
            )
        ]
    )
    record = run_platform(
        "taobao",
        config,
        date="2026-11-05",
        max_pages=1,
        browser_factory=_factory(browser),
    )
    assert record.login_ok is False
    assert record.status == "login_failed"
    assert record.pages_fetched == 1
    path = tmp_path / "out" / "2026-11-05" / "taobao.jsonl"
    assert path.exists() and path.read_text(encoding="utf-8") == ""


def test_run_platform_risk_stops_with_screenshot(make_config, tmp_path):
    config = make_config()
    browser = StaticBrowser(
        [
            PageResult(
                url="https://s.taobao.com/search?q=x",
                html=fixture_text("risk_taobao.html"),
                dom_hints=["#nc_1_wrapper"],
            )
        ]
    )
    record = run_platform(
        "taobao",
        config,
        date="2026-11-05",
        max_pages=1,
        browser_factory=_factory(browser),
    )
    assert record.risk_blocked is True
    assert record.status == "risk_blocked"
    assert record.screenshot_path
    assert browser.screenshots


def test_run_platform_ignores_daily_quantity_limit(make_config):
    config = make_config()
    config.crawl.daily_max_pages = 1
    browser = StaticBrowser([_login_page(), _search_page("葫芦兄弟")])
    record = run_platform(
        "taobao",
        config,
        date="2026-11-05",
        max_pages=1,
        browser_factory=_factory(browser),
    )
    assert record.status == "ok"
    assert record.pages_fetched == 2 and record.items_ok == 3


def test_run_platforms_all_and_unknown(make_config):
    config = make_config()
    records = run_platforms(
        ["all"], config, date="2026-11-05", browser_factory=lambda *a: StaticBrowser([])
    )
    # 三个平台都会因登录页缺失而失败，但每个都有运行记录
    assert len(records) == 3
    assert all(record.status in {"error", "login_failed"} for record in records)
    try:
        run_platforms(["pdd"], config)
    except Exception as exc:
        assert "未知平台" in str(exc)
    else:
        raise AssertionError("pdd 应报未知平台")


def test_run_detail(make_config, tmp_path):
    config = make_config()
    detail_url = "https://item.taobao.com/item.htm?id=1111111111"
    browser = StaticBrowser(
        [
            _login_page(),
            PageResult(
                url=detail_url, html="", captured=[_captured("taobao_detail.json")]
            ),
        ]
    )
    record = run_detail(
        "taobao",
        config,
        ids=["1111111111"],
        date="2026-11-05",
        browser_factory=_factory(browser),
    )
    assert record.status == "ok"
    assert record.items_ok == 1
    rows = read_jsonl(tmp_path / "out" / "2026-11-05" / "taobao_detail.jsonl")
    assert rows[0]["product_id"] == "1111111111"
    assert rows[0]["comments"] == 1200


def test_build_keyword_matrix(make_config):
    config = make_config()
    matrix = build_keyword_matrix(config)
    assert matrix[0]["keyword"] == "葫芦兄弟"
    assert matrix[0]["ip_code"] == "huluxiongdi"
    override = build_keyword_matrix(config, ["葫芦娃", "自定义词"])
    assert override[0]["ip_code"] == "huluxiongdi"
    assert override[1]["ip_code"] == "manual"


@pytest.mark.parametrize("platform", ["taobao", "jd", "1688"])
@pytest.mark.parametrize("resumed", [False, True])
def test_timed_collection_searches_before_backfill_and_images(
    make_config, monkeypatch, platform, resumed
):
    from datetime import datetime

    from crawler.core.progress import SearchProgress
    from crawler.models import Item, ItemPatch
    from crawler.platforms import create_platform

    monkeypatch.setenv("CRAWLER_ENDS_AT", "2099-01-01T00:00:00+08:00")
    monkeypatch.setenv("CRAWLER_AMOUNT_DETAIL", "1")
    config = make_config()
    config.crawl.max_pages = config.crawl.max_items_per_keyword = 1
    config.crawl.taobao.max_pages = config.crawl.taobao.max_keywords_per_run = 1
    config.crawl.taobao.batch.max_pages_per_segment = 1
    config.crawl.taobao.batch.max_items_per_segment = 1
    config.crawl.taobao.detail.complete_missing_amount = True
    config.crawl.detail.platforms[platform] = True
    keywords = ["葫芦兄弟", "葫芦娃"]
    matrix = build_keyword_matrix(config, keywords)
    impl = type(create_platform(platform, config, StaticBrowser([])))
    path = config.output_root / f"2026-11-10/{platform}.jsonl"
    events = []

    def item_for(keyword, page, ip_code):
        pid = str(100000 + keywords.index(keyword) * 100 + page)
        return Item(
            platform=platform,
            product_id=pid,
            title=keyword + "测试商品",
            url=f"https://example.test/{pid}",
            price=20,
            main_image_url=f"https://example.test/{pid}.jpg",
            search_keyword=keyword,
            ip_code=ip_code,
            page=page,
            crawled_at=datetime.now(),
        )

    if resumed:
        progress = SearchProgress(config.state_root, platform, matrix, continuous=True)
        progress.commit(
            keywords[0],
            matrix[0]["ip_code"],
            1,
            [item_for(keywords[0], 1, matrix[0]["ip_code"])],
        )
        progress.finish("interrupted")

    def parse(self, result, keyword, page, ip_code):
        if keyword == keywords[0] and page == 3:
            return []
        return [item_for(keyword, page, ip_code)]

    class TimedBrowser(StaticBrowser):
        def fetch_page(self, url, **kwargs):
            if url == impl.login_check_url:
                return PageResult(url=url, html="账号正常")
            for keyword in keywords:
                for page in (1, 2, 3):
                    if url == probe.build_search_url(keyword, page):
                        saved_rows = list(read_jsonl(path))
                        expected_count = (
                            page - 1 if keyword == keywords[0] else page + 1
                        )
                        assert len(saved_rows) == expected_count
                        assert all(
                            row["sales"] is None and not row["shop_name"]
                            for row in saved_rows
                        )
                        events.append(("search", keyword, page))
                        if keyword == keywords[1] and page == 3:
                            monkeypatch.setenv(
                                "CRAWLER_ENDS_AT", "2000-01-01T00:00:00+08:00"
                            )
                        return PageResult(
                            url=url,
                            html="商品列表",
                            pagination=PaginationState(
                                has_next=False, empty_confirmed=True
                            )
                            if keyword == keywords[0] and page == 3
                            else None,
                        )
            pid = next(
                (
                    str(pid)
                    for pid in (100001, 100002, 100101, 100102, 100103)
                    if str(pid) in url
                ),
                None,
            )
            assert pid is not None, f"截止后不能搜索新页面: {url}"
            events.append(("detail", pid))
            return PageResult(url=url, html="商品详情")

    def detail(self, result, pid):
        return ItemPatch(
            platform=platform, product_id=pid, sales=100, shop_name="补采店名"
        )

    def image(self, url, pid):
        events.append(("image", pid))
        return f"images/{pid}.jpg", None

    browser = TimedBrowser([])
    probe = create_platform(platform, config, browser)
    monkeypatch.setattr(impl, "parse_search", parse)
    monkeypatch.setattr(impl, "parse_detail", detail)
    monkeypatch.setattr("crawler.runner.ImageDownloader.download", image)
    record = run_platform(
        platform,
        config,
        date="2026-11-10",
        keywords=keywords,
        max_pages=1,
        download_images=True,
        browser_factory=_factory(browser),
    )
    searches = [("search", keywords[0], p) for p in ((2, 3) if resumed else (1, 2, 3))]
    searches += [("search", keywords[1], p) for p in (1, 2, 3)]
    assert events[: len(searches)] == searches
    assert len([e for e in events if e[0] == "search"]) == len(searches)
    assert len([e for e in events if e[0] == "detail"]) == 5
    assert len([e for e in events if e[0] == "image"]) == 5
    assert record.status == "deadline" and record.deadline_reached
    assert record.keywords_done == 1  # 第二个关键词仅因截止暂停，尚未采完。
    assert record.items_ok == 5 and not record.detail_failures
    rows = read_jsonl(path)
    assert len(rows) == 5
    assert all(
        row["shop_name"] == "补采店名" and row["sales"] == 100 and row["image_path"]
        for row in rows
    )
    saved = SearchProgress(config.state_root, platform, matrix, continuous=True)
    assert saved.position(keywords[0], matrix[0]["ip_code"])["done"]
    assert saved.position(keywords[1], matrix[1]["ip_code"])["next_page"] == 4
    assert not saved.position(keywords[1], matrix[1]["ip_code"])["done"]
    assert saved.state["detail_pending"] == []
    assert saved.state["detail_unresolved"] == []


def test_run_platform_disabled(make_config):
    config = make_config()
    config.platforms["taobao"].enabled = False
    record = run_platform(
        "taobao",
        config,
        date="2026-11-05",
        browser_factory=lambda *a: StaticBrowser([]),
    )
    assert record.status == "disabled"


def test_run_platform_browser_unavailable(make_config):
    from crawler.core.errors import BrowserUnavailable

    config = make_config()

    class Broken:
        def start(self):
            raise BrowserUnavailable("no browser")

        def close(self):
            pass

    record = run_platform(
        "taobao", config, date="2026-11-05", browser_factory=lambda *a: Broken()
    )
    assert record.status == "error"
    assert record.errors


def test_run_platform_risk_during_search(make_config, tmp_path):
    config = make_config()
    browser = StaticBrowser(
        [
            _login_page(),
            PageResult(
                url=_search_url("葫芦兄弟"),
                html=fixture_text("risk_taobao.html"),
                dom_hints=["#nc_1_wrapper"],
            ),
        ]
    )
    record = run_platform(
        "taobao",
        config,
        date="2026-11-05",
        max_pages=1,
        browser_factory=_factory(browser),
    )
    assert record.status == "risk_blocked"
    assert record.risk_blocked and record.screenshot_path


def test_run_platform_login_during_search(make_config, tmp_path):
    config = make_config()
    browser = StaticBrowser(
        [
            _login_page(),
            PageResult(
                url=_search_url("葫芦兄弟"), html=fixture_text("login_taobao.html")
            ),
        ]
    )
    record = run_platform(
        "taobao",
        config,
        date="2026-11-05",
        max_pages=1,
        browser_factory=_factory(browser),
    )
    assert record.status == "login_failed"
    assert record.login_ok is False


def test_run_platform_image_failure_flags(make_config, tmp_path, monkeypatch):
    config = make_config()
    config.crawl.image.enabled = True
    browser = StaticBrowser([_login_page(), _search_page("葫芦兄弟")])
    monkeypatch.setattr(
        "crawler.runner.ImageDownloader.download",
        lambda self, url, product_id: (None, "network down"),
    )
    record = run_platform(
        "taobao",
        config,
        date="2026-11-05",
        max_pages=1,
        browser_factory=_factory(browser),
    )
    assert record.status == "ok"
    rows = read_jsonl(tmp_path / "out" / "2026-11-05" / "taobao.jsonl")
    assert all("主图下载失败" in row["flags"] for row in rows)


def test_run_detail_login_failure(make_config):
    config = make_config()
    browser = StaticBrowser(
        [
            PageResult(
                url="https://login.taobao.com/member/login.jhtml",
                html=fixture_text("login_taobao.html"),
            )
        ]
    )
    record = run_detail(
        "taobao",
        config,
        ids=["1111111111"],
        date="2026-11-05",
        browser_factory=_factory(browser),
    )
    assert record.status == "login_failed"


def test_run_detail_risk_and_daily(make_config, tmp_path):
    config = make_config()
    browser = StaticBrowser(
        [
            _login_page(),
            PageResult(
                url="https://item.taobao.com/item.htm?id=1111111111",
                html=fixture_text("risk_taobao.html"),
            ),
        ]
    )
    record = run_detail(
        "taobao",
        config,
        ids=["1111111111"],
        date="2026-11-05",
        browser_factory=_factory(browser),
    )
    assert record.status == "risk_blocked"

    config2 = make_config()
    config2.crawl.daily_max_pages = 1
    config2.crawl.state_dir = str(tmp_path / "state-daily")
    browser2 = StaticBrowser(
        [
            _login_page(),
            PageResult(
                url="https://item.taobao.com/item.htm?id=1111111111",
                html="",
                captured=[_captured("taobao_detail.json")],
            ),
        ]
    )
    record2 = run_detail(
        "taobao",
        config2,
        ids=["1111111111"],
        date="2026-11-05",
        browser_factory=_factory(browser2),
    )
    assert record2.status == "ok" and record2.items_ok == 1


def test_same_day_risk_circuit_breaker(make_config, tmp_path):
    config = make_config()
    browser = StaticBrowser(
        [
            _login_page(),
            PageResult(
                url=_search_url("葫芦兄弟"),
                html=fixture_text("risk_taobao.html"),
                dom_hints=["#nc_1_wrapper"],
            ),
        ]
    )
    first = run_platform(
        "taobao",
        config,
        date="2026-11-05",
        max_pages=1,
        browser_factory=_factory(browser),
    )
    assert first.status == "risk_blocked"
    marker = tmp_path / "state" / "risk" / "taobao-2026-11-05.json"
    assert_private_mode(marker, 0o600)

    second_browser = StaticBrowser([])
    second = run_platform(
        "taobao",
        config,
        date="2026-11-05",
        max_pages=1,
        browser_factory=_factory(second_browser),
    )
    assert second.status == "risk_blocked"
    assert second.risk_blocked is True
    assert second.pages_fetched == 0
    assert second_browser.calls == []


def test_run_detail_1688_disabled_by_default(make_config, tmp_path):
    """1688 详情默认停用：不启动浏览器、不写详情行，但给出 disabled 状态。"""
    config = make_config()
    calls = []

    def factory(config, platform, headless):
        calls.append(platform)
        raise AssertionError("1688 详情已停用，不应创建浏览器")

    record = run_detail(
        "1688",
        config,
        ids=["123456789"],
        date="2026-11-05",
        browser_factory=factory,
    )
    assert record.status == "disabled"
    assert calls == []
    assert any("详情补抓已按配置停用" in warning for warning in record.warnings)
    assert (tmp_path / "out" / "2026-11-05" / "1688_detail.jsonl").exists()
    assert read_jsonl(tmp_path / "out" / "2026-11-05" / "1688_detail.jsonl") == []
    run_json = tmp_path / "out" / "2026-11-05" / "1688.run.json"
    assert run_json.exists()
    import json as _json

    assert _json.loads(run_json.read_text(encoding="utf-8"))["status"] == "disabled"


def test_run_record_marks_browser_started_only_when_chrome_launched(make_config):
    from types import SimpleNamespace

    config = make_config()
    detail_url = "https://item.taobao.com/item.htm?id=1111111111"
    browser = StaticBrowser(
        [
            _login_page(),
            PageResult(
                url=detail_url, html="", captured=[_captured("taobao_detail.json")]
            ),
        ]
    )
    browser.chrome_handle = SimpleNamespace(launched=True)
    record = run_detail(
        "taobao",
        config,
        ids=["1111111111"],
        date="2026-11-05",
        browser_factory=_factory(browser),
    )
    assert record.browser_started is True
    run_json = config.output_root / "2026-11-05" / "taobao.run.json"
    assert json.loads(run_json.read_text(encoding="utf-8"))["browser_started"] is True

    plain = StaticBrowser(
        [
            _login_page(),
            PageResult(
                url=detail_url, html="", captured=[_captured("taobao_detail.json")]
            ),
        ]
    )
    record2 = run_detail(
        "taobao",
        config,
        ids=["1111111111"],
        date="2026-11-06",
        browser_factory=_factory(plain),
    )
    assert record2.browser_started is False


def test_run_platform_taobao_real_shell_with_mtop_end_to_end(make_config, tmp_path):
    """WU 空壳页 → mtop 响应 → JSONL 的完整真实路径。"""
    config = make_config()
    shell = fixture_text("taobao_search_20261106_shell.html")
    mtop = fixture_text("taobao_search_20261106_mtop.txt")
    url = _search_url("奥特曼")
    browser = StaticBrowser(
        [
            _login_page(),
            PageResult(
                url=url,
                html=shell,
                pagination=PaginationState(has_next=False),
                captured=[
                    CapturedResponse(
                        url="https://h5api.m.taobao.com/h5/mtop.taobao.wsearch.pcsearch/1.0/",
                        text=mtop,
                        content_type="text/plain",
                        status=200,
                    )
                ],
            ),
        ]
    )
    record = run_platform(
        "taobao",
        config,
        date="2026-11-06",
        keywords=["奥特曼"],
        max_pages=1,
        browser_factory=_factory(browser),
    )
    assert record.status == "ok"
    assert record.items_ok == 3
    rows = read_jsonl(tmp_path / "out" / "2026-11-06" / "taobao.jsonl")
    assert len(rows) == 3
    assert {row["product_id"] for row in rows} == {
        "820123456789",
        "820123456790",
        "820123456791",
    }
    first = next(row for row in rows if row["product_id"] == "820123456789")
    assert first["title"].startswith("奥特曼 玩具")
    assert first["shop_id"] == "2200123456"
    assert first["price"] == 89.0
    assert "评论缺失" in first["flags"]


@pytest.mark.parametrize("platform", ["taobao", "jd"])
def test_amount_detail_saved_before_next_page(make_config, monkeypatch, platform):
    from crawler.platforms.base import PlatformBase
    from crawler.models import Item, ItemPatch
    from datetime import datetime

    monkeypatch.setenv("CRAWLER_AMOUNT_DETAIL", "1")
    config = make_config()
    config.crawl.max_pages = 2
    config.crawl.taobao.detail.max_per_run = 1
    events = []
    browser = StaticBrowser([_login_page()])
    path = config.output_root / "2026-11-09" / f"{platform}.jsonl"

    def request(self, url, **kwargs):
        events.append("list")
        if len(events) > 1:
            assert read_jsonl(path)[0]["comments"] == 7
        return PageResult(
            url=url,
            html="",
            pagination=PaginationState(has_next=kwargs["search_page_number"] < 2),
        )

    def parse(self, result, keyword, page, ip_code):
        return [
            Item(
                platform=platform,
                product_id=str(123455 + page),
                url=f"https://example.test/{123455 + page}",
                title=keyword,
                search_keyword=keyword,
                ip_code=ip_code,
                price=20,
                sales=None if page == 1 else 100,
                comments=None if page == 1 else 7,
                shop_name=None if page == 1 else "测试店",
                crawled_at=datetime.now(),
            )
        ]

    def detail(self, ids):
        events.append("detail")
        assert read_jsonl(path)[0]["comments"] is None  # 请求前已有列表落盘
        patch = ItemPatch(
            platform=platform,
            product_id="123456",
            sales=100,
            comments=7,
            shop_name="测试店",
        )
        self.checkpoint_patch(patch)
        return [patch]

    monkeypatch.setattr(PlatformBase, "_request", request)
    monkeypatch.setattr(PlatformBase, "fetch_detail", detail)
    monkeypatch.setattr(
        f"crawler.platforms.{'taobao.TaobaoPlatform' if platform == 'taobao' else 'jd.JdPlatform'}.parse_search",
        parse,
    )
    record = run_platform(
        platform,
        config,
        date="2026-11-09",
        max_pages=2,
        browser_factory=_factory(browser),
    )
    assert record.status == "ok"
    assert events == ["list", "detail", "list"]
    row = read_jsonl(path)[0]
    assert row["comments"] == 7 and "评论缺失" not in row["flags"]


def test_amount_missing_bypasses_taobao_second_score_filter(make_config):
    import logging
    from crawler.runner import _taobao_detail_candidates
    from crawler.core.stats import RunStats

    config = make_config()
    path = config.output_root / "2026-11-09" / "taobao.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "platform": "taobao",
                "product_id": "123456",
                "title": "葫芦兄弟",
                "price": 20,
                "sales": None,
                "comments": None,
            }
        ),
        encoding="utf-8",
    )
    assert _taobao_detail_candidates(
        config,
        "taobao",
        "2026-11-09",
        ["123456"],
        logging.getLogger("test"),
        RunStats(),
    ) == ["123456"]


@pytest.mark.parametrize("resumed", [False, True])
@pytest.mark.parametrize("item_platform", ["taobao", "tmall"])
@pytest.mark.parametrize("outcome", ["complete", "empty", "error", "disabled"])
def test_inline_details_use_validated_sales(
    make_config, monkeypatch, resumed, item_platform, outcome
):
    """旧去重或检查点中存在数字时，按落盘后是否缺失决定补采。"""
    from datetime import datetime

    from crawler.core.progress import SearchProgress
    from crawler.models import Item, ItemPatch
    from crawler.platforms.base import PlatformBase
    from crawler.platforms.taobao import TaobaoPlatform

    monkeypatch.setenv("CRAWLER_AMOUNT_DETAIL", "1")
    monkeypatch.delenv("CRAWLER_ENDS_AT", raising=False)
    config = make_config()
    config.crawl.taobao.detail.enabled = outcome != "disabled"
    matrix = build_keyword_matrix(config)
    entry = matrix[0]
    pid = "1085008955004"
    item = Item.model_validate(
        {
            "platform": item_platform,
            "product_id": pid,
            "url": f"https://item.taobao.com/item.htm?id={pid}",
            "title": "补采回归测试挂件",
            "shop_name": "已有测试店",
            "price": 49,
            "sales": 800,
            "sales_raw": "800+人付款",
            "search_keyword": entry["keyword"],
            "ip_code": entry["ip_code"],
            "crawled_at": datetime.now(),
        }
    )
    # 重现历史去重产生的未经重新校验的数值/原文组合。
    invalid = item.model_copy(update={"sales_raw": "本月行业热销"})
    zero = Item.model_validate(
        {
            **item.model_dump(),
            "product_id": "1085008955005",
            "sales": 0,
            "sales_raw": "已售0件",
        }
    )
    delisted = invalid.model_copy(
        update={"product_id": "1085008955006", "listed": False}
    )
    items = [invalid, zero, delisted]
    if resumed:
        progress = SearchProgress(config.state_root, "taobao", matrix)
        progress.commit(entry["keyword"], entry["ip_code"], 1, items, done=True)
        progress.finish("partial")
        monkeypatch.setattr(
            PlatformBase,
            "search",
            lambda *a, **kw: pytest.fail("已完成的列表不应重新采集"),
        )
    else:
        monkeypatch.setattr(
            PlatformBase,
            "_request",
            lambda self, url, **kw: PageResult(
                url=url, html="", pagination=PaginationState(has_next=False)
            ),
        )
        monkeypatch.setattr(TaobaoPlatform, "parse_search", lambda *a, **kw: items)

    path = config.output_root / "2026-11-10/taobao.jsonl"
    calls = []

    def detail(self, ids):
        calls.extend(ids)
        before = next(row for row in read_jsonl(path) if row["product_id"] == pid)
        assert before["sales"] is None
        assert "销量缺失" in before["flags"]
        if outcome == "empty":
            return []
        if outcome == "error":
            raise RuntimeError("回归用例模拟详情失败")
        patch = ItemPatch(
            platform=item_platform,
            product_id=pid,
            sales=1200,
            sales_raw="1200+人付款",
        )
        self.checkpoint_patch(patch)
        return [patch]

    monkeypatch.setattr(PlatformBase, "fetch_detail", detail)
    record = run_platform(
        "taobao",
        config,
        date="2026-11-10",
        max_pages=1,
        browser_factory=_factory(StaticBrowser([_login_page()])),
    )
    detail_id = f"tmall:{pid}" if item_platform == "tmall" else pid
    assert calls == ([] if outcome == "disabled" else [detail_id])
    assert record.items_ok == 3
    assert record.status == ("ok" if outcome == "complete" else "partial")
    rows = {row["product_id"]: row for row in read_jsonl(path)}
    assert rows["1085008955005"]["sales"] == 0
    assert not rows["1085008955006"]["listed"]
    key = f"{item_platform}:{pid}"
    saved = json.loads(
        SearchProgress(config.state_root, "taobao", matrix).path.read_text(
            encoding="utf-8"
        )
    )
    assert saved["detail_attempted"] == ([] if outcome == "disabled" else [key])
    assert saved["detail_pending"] == []
    if outcome == "complete":
        assert (rows[pid]["sales"], rows[pid]["sales_raw"]) == (1200, "1200+人付款")
        assert "销量缺失" not in rows[pid]["flags"]
        assert not any(flag.startswith("金额补采：") for flag in rows[pid]["flags"])
        assert record.detail_failures == {}
        assert saved["detail_unresolved"] == []
    else:
        assert rows[pid]["sales"] is None
        assert "销量缺失" in rows[pid]["flags"]
        assert set(record.detail_failures) == {key}
        assert saved["detail_unresolved"] == [key]
        reason = {"empty": "解析失败", "error": "RuntimeError", "disabled": "未启用"}
        assert reason[outcome] in record.detail_failures[key]


@pytest.mark.parametrize(
    "mode", ["cap", "disabled", "global_disabled", "failure", "deadline"]
)
def test_inline_details_ignore_caps_and_preserve_rows(make_config, monkeypatch, mode):
    from datetime import datetime
    from crawler.core.progress import check_deadline
    from crawler.models import Item, ItemPatch
    from crawler.platforms.base import PlatformBase

    config = make_config()
    config.crawl.taobao.detail.max_per_run = 1
    if mode == "disabled":
        config.crawl.taobao.detail.enabled = False
    if mode == "global_disabled":
        config.crawl.detail.platforms["taobao"] = False
    monkeypatch.setenv("CRAWLER_AMOUNT_DETAIL", "1")
    # 本用例验证普通批次的逐页补采；定时分阶段行为由独立用例覆盖。
    monkeypatch.delenv("CRAWLER_ENDS_AT", raising=False)
    path = config.output_root / "2026-11-09" / "taobao.jsonl"
    calls = []

    def parse(self, result, keyword, page, ip_code):
        return [
            Item(
                platform="taobao",
                product_id=str(123450 + page),
                url=f"https://item.taobao.com/item.htm?id={123450 + page}",
                title=keyword,
                search_keyword=keyword,
                ip_code=ip_code,
                price=20,
                sales=None,
                comments=None,
                crawled_at=datetime.now(),
            )
        ]

    def detail(self, ids):
        calls.extend(ids)
        assert (
            next(row for row in read_jsonl(path) if row["product_id"] == ids[0])[
                "comments"
            ]
            is None
        )
        if mode == "failure":
            raise RuntimeError("详情超时")
        if mode == "deadline" and len(calls) == 1:
            monkeypatch.setenv("CRAWLER_ENDS_AT", "2000-01-01 00:00:00")
            check_deadline()  # 真实补采作用域内不会因常规截止而中断。
        patch = ItemPatch(
            platform="taobao",
            product_id=ids[0],
            sales=100,
            comments=7,
            shop_name="测试店",
        )
        self.checkpoint_patch(patch)
        return [patch]

    monkeypatch.setattr(
        PlatformBase,
        "_request",
        lambda self, url, **kw: PageResult(
            url=url,
            html="",
            pagination=PaginationState(has_next=kw["search_page_number"] < 2),
        ),
    )
    monkeypatch.setattr(PlatformBase, "fetch_detail", detail)
    monkeypatch.setattr("crawler.platforms.taobao.TaobaoPlatform.parse_search", parse)
    record = run_platform(
        "taobao",
        config,
        date="2026-11-09",
        max_pages=2,
        browser_factory=_factory(StaticBrowser([_login_page()])),
    )
    assert calls == (
        []
        if "disabled" in mode
        else ["123451"]
        if mode == "deadline"
        else ["123451", "123452"]
    )
    assert record.status == (
        "deadline"
        if mode == "deadline"
        else "partial"
        if mode in ("failure", "disabled", "global_disabled")
        else "ok"
    )
    rows = read_jsonl(path)
    assert len(rows) == (1 if mode == "deadline" else 2)
    assert rows[0]["comments"] == (7 if mode in ("cap", "deadline") else None)
    if mode == "deadline":
        monkeypatch.delenv("CRAWLER_ENDS_AT", raising=False)
        resumed = run_platform(
            "taobao",
            config,
            date="2026-11-09",
            max_pages=1,
            browser_factory=_factory(StaticBrowser([_login_page()])),
        )
        assert resumed.status == "ok"
        assert calls == ["123451", "123452"]  # 已补齐第一页；续采第二页，不重复补采。
        assert len(read_jsonl(path)) == 2


@pytest.mark.parametrize("platform", ["taobao", "jd", "1688"])
@pytest.mark.parametrize("mode", ["complete", "no_details", "retry_success", "failure"])
def test_deadline_drains_inflight_rows_and_detail_retries(
    make_config, monkeypatch, platform, mode
):
    from collections import Counter
    from datetime import datetime
    from crawler.core.progress import SearchProgress
    from crawler.models import Item, ItemPatch
    from crawler.platforms import create_platform

    config = make_config()
    config.crawl.detail.platforms[platform] = True
    config.crawl.taobao.detail.complete_missing_amount = True
    config.crawl.retry.attempts = 3
    monkeypatch.setenv("CRAWLER_AMOUNT_DETAIL", "1")
    monkeypatch.setenv("CRAWLER_ENDS_AT", "2099-01-01 00:00:00")
    requests = []
    attempts = Counter()
    impl = type(create_platform(platform, config, StaticBrowser([])))

    def parse(self, result, keyword, page, ip_code):
        return [
            Item(
                platform=platform,
                product_id=str(123451 + index),
                title=keyword,
                url=self.detail_url(str(123451 + index)),
                price=20,
                sales=100 if mode == "no_details" else None,
                shop_name="已有店名" if mode == "no_details" else None,
                search_keyword=keyword,
                ip_code=ip_code,
                crawled_at=datetime.now(),
            )
            for index in range(2)
        ]

    def detail(self, result, product_id):
        return ItemPatch(
            platform=platform,
            product_id=product_id,
            shop_name="补采店名",
            price=20,
            sales=100,
            comments=7,
        )

    class DeadlineBrowser(StaticBrowser):
        def fetch_page(self, url, **kwargs):
            if url == impl.login_check_url:
                return PageResult(url=url, html="<html>账号正常</html>")
            if url == impl.build_search_url(self_platform, "葫芦兄弟", 1):
                requests.append("search")
                # 截止发生在请求发出之后；返回结果必须保存并产生补采。
                monkeypatch.setenv("CRAWLER_ENDS_AT", "2000-01-01 00:00:00")
                return PageResult(url=url, html="")
            pid = next((pid for pid in ("123451", "123452") if pid in url), None)
            assert pid is not None, f"截止后不应发起新的列表请求: {url}"
            requests.append(pid)
            attempts[pid] += 1
            fails = pid == "123451" and (
                mode == "failure" or (mode == "retry_success" and attempts[pid] < 3)
            )
            return PageResult(url=url, html="", status=503 if fails else 200)

    browser = DeadlineBrowser([])
    self_platform = create_platform(platform, config, browser)
    monkeypatch.setattr(impl, "parse_search", parse)
    monkeypatch.setattr(impl, "parse_detail", detail)
    record = run_platform(
        platform,
        config,
        date="2026-11-10",
        max_pages=2,
        browser_factory=_factory(browser),
    )
    assert requests.count("search") == 1
    assert attempts == (
        {}
        if mode == "no_details"
        else {"123451": 3 if mode in ("retry_success", "failure") else 1, "123452": 1}
    )
    rows = read_jsonl(config.output_root / f"2026-11-10/{platform}.jsonl")
    assert len(rows) == 2
    assert record.status == ("partial" if mode == "failure" else "deadline")
    assert bool(record.detail_failures) == (mode == "failure")
    saved = json.loads(
        SearchProgress(
            config.state_root, platform, build_keyword_matrix(config), continuous=True
        ).path.read_text(encoding="utf-8")
    )
    assert saved["detail_pending"] == []
    assert len(saved["detail_unresolved"]) == (1 if mode == "failure" else 0)
    if mode != "failure":
        assert all(row["shop_name"] and row["sales"] == 100 for row in rows)
    else:
        assert rows[1]["shop_name"] == "补采店名"
        assert any("重试耗尽" in error for error in record.errors)


@pytest.mark.parametrize("complete", [False, True])
def test_pending_details_resume_after_search_finished(
    make_config, monkeypatch, complete
):
    """第一页已结束，旧失败不能挡住未尝试项；补齐模式可覆盖超过5条。"""
    from datetime import datetime
    from crawler.core.progress import SearchProgress
    from crawler.models import Item, ItemPatch
    from crawler.platforms.base import PlatformBase

    monkeypatch.setenv("CRAWLER_AMOUNT_DETAIL", "1")
    monkeypatch.delenv("CRAWLER_ENDS_AT", raising=False)
    config = make_config()
    config.crawl.taobao.detail.max_per_run = 1
    config.crawl.taobao.detail.complete_missing_amount = complete
    matrix = build_keyword_matrix(config)
    progress = SearchProgress(config.state_root, "taobao", matrix)
    entry = matrix[0]
    rows = [
        Item(
            platform="taobao",
            product_id=str(200000 + i),
            url=f"https://item.taobao.com/item.htm?id={200000 + i}",
            title="葫芦兄弟挂件",
            search_keyword=entry["keyword"],
            ip_code=entry["ip_code"],
            price=20,
            sales=None,
            comments=None,
            crawled_at=datetime.now(),
        )
        for i in range(7)
    ]
    progress.commit(entry["keyword"], entry["ip_code"], 1, rows, done=True)
    progress.state["detail_attempted"] = ["taobao:200000"] * 5
    progress.state["detail_pending"] = [progress.detail_key(item) for item in rows]
    progress.finish("ok")
    assert SearchProgress(config.state_root, "taobao", matrix).resumed
    calls = []

    def no_search(*args, **kwargs):
        pytest.fail("列表已完成时不能重新搜索")

    def detail(self, ids):
        calls.extend(ids)
        # 旧失败记录始终无有效补丁；其他记录补齐，检查不会饿死后续商品。
        if ids[0] == "200000":
            return []
        patch = ItemPatch(
            platform="taobao",
            product_id=ids[0],
            sales=100,
            comments=7,
            shop_name="回归测试店",
        )
        self.checkpoint_patch(patch)
        return [patch]

    monkeypatch.setattr(PlatformBase, "search", no_search)
    monkeypatch.setattr(PlatformBase, "fetch_detail", detail)
    record = run_platform(
        "taobao",
        config,
        date="2026-11-09",
        max_pages=1,
        browser_factory=_factory(StaticBrowser([_login_page()])),
    )
    assert record.status == "partial"
    assert calls[0] == "200001"
    assert len(calls) == 7
    result = read_jsonl(config.output_root / "2026-11-09" / "taobao.jsonl")
    assert sum(row["comments"] == 7 for row in result) == 6
    saved = SearchProgress(config.state_root, "taobao", matrix)
    assert saved.resumed
    assert saved.state["detail_pending"] == []
    assert len(saved.state["detail_unresolved"]) == 1
    assert any(
        "金额补采：" in flag
        for row in result
        if row["comments"] is None
        for flag in row["flags"]
    )
    if not complete:
        calls.clear()
        run_platform(
            "taobao",
            config,
            date="2026-11-09",
            max_pages=1,
            browser_factory=_factory(StaticBrowser([_login_page()])),
        )
        assert calls == ["200000"]  # 只重试真实失败项，不重复请求已补齐商品。


def test_real_task_50_rows_coverage_and_redirect_patch(make_config, monkeypatch):
    """真实50条仅离线回放调度；其中一条使用本轮实时详情，其余不虚构字段。"""
    from pathlib import Path
    from crawler.core.progress import SearchProgress
    from crawler.models import Item
    from crawler.platforms.base import PlatformBase

    root = Path(__file__).resolve().parents[2]
    source = root / "out/runs/f55df686d190416ca89e1525172fb51c/2026-11-09/taobao.jsonl"
    html_path = (
        root / "logs/debug/detail_taobao_1085008955004_20261109_163538_689418.html"
    )
    if not source.exists() or not html_path.exists():
        pytest.skip("真实任务和实时详情证据仅本机保留")
    rows = [Item.model_validate(row) for row in read_jsonl(source)]
    assert len(rows) == 50
    config = make_config()
    config.crawl.taobao.detail.complete_missing_amount = True
    config.crawl.taobao.detail.max_per_run = 5
    monkeypatch.setenv("CRAWLER_AMOUNT_DETAIL", "1")
    monkeypatch.delenv("CRAWLER_ENDS_AT", raising=False)
    matrix = build_keyword_matrix(config)
    progress = SearchProgress(config.state_root, "taobao", matrix)
    progress.update_items(rows)
    for entry in matrix:
        progress.finish_keyword(entry["keyword"], entry["ip_code"])
    progress.state["detail_attempted"] = [
        progress.detail_key(item) for item in rows[:5]
    ]
    progress.save()
    calls = []

    def fetch(self, ids):
        calls.extend(ids)
        if ids == ["1085008955004"]:
            patch = self.parse_detail(
                PageResult(
                    url="https://detail.tmall.com/item.htm?id=1085008955004",
                    html=html_path.read_text(encoding="utf-8"),
                ),
                ids[0],
            )
            assert patch.platform == "taobao"
            self.checkpoint_patch(patch)
            return [patch]
        return []

    monkeypatch.setattr(PlatformBase, "fetch_detail", fetch)
    monkeypatch.setattr(
        PlatformBase, "search", lambda *a, **kw: pytest.fail("不应重跑已完成列表")
    )
    result = run_platform(
        "taobao",
        config,
        date="2026-11-09",
        browser_factory=_factory(StaticBrowser([_login_page()])),
    )
    assert result.status == "partial"
    needed = [item for item in rows if item.price is None or item.sales is None]
    assert len(calls) == len(set(calls)) == len(needed) == 8
    output = read_jsonl(config.output_root / "2026-11-09/taobao.jsonl")
    fixed = next(row for row in output if row["product_id"] == "1085008955004")
    assert (fixed["price"], fixed["sales"], fixed["comments"]) == (49, 1000, 46)
    assert not any(flag.startswith("金额补采：") for flag in fixed["flags"])
    assert (
        len(
            SearchProgress(config.state_root, "taobao", matrix).state[
                "detail_unresolved"
            ]
        )
        == len(needed) - 1
    )


def test_missing_shop_triggers_detail_with_complete_amount(make_config, monkeypatch):
    from datetime import datetime
    from crawler.models import Item, ItemPatch
    from crawler.platforms.base import PlatformBase

    config = make_config()
    monkeypatch.setenv("CRAWLER_AMOUNT_DETAIL", "1")
    calls = []

    def parse(self, result, keyword, page, ip_code):
        return [
            Item(
                platform="taobao",
                product_id="234567",
                title=keyword,
                url="https://item.taobao.com/item.htm?id=234567",
                price=20,
                sales=100,
                search_keyword=keyword,
                ip_code=ip_code,
                crawled_at=datetime.now(),
            )
        ]

    def detail(self, ids):
        calls.extend(ids)
        patch = ItemPatch(platform="taobao", product_id=ids[0], shop_name="详情测试店")
        self.checkpoint_patch(patch)
        return [patch]

    monkeypatch.setattr(
        PlatformBase,
        "_request",
        lambda self, url, **kw: PageResult(
            url=url, html="", pagination=PaginationState(has_next=False)
        ),
    )
    monkeypatch.setattr(PlatformBase, "fetch_detail", detail)
    monkeypatch.setattr("crawler.platforms.taobao.TaobaoPlatform.parse_search", parse)
    record = run_platform(
        "taobao",
        config,
        date="2026-11-10",
        max_pages=1,
        browser_factory=_factory(StaticBrowser([_login_page()])),
    )
    assert record.status == "ok"
    assert calls == ["234567"]
    row = read_jsonl(config.output_root / "2026-11-10/taobao.jsonl")[0]
    assert row["shop_name"] == "详情测试店"
    assert "店铺名缺失" not in row["flags"]


@pytest.mark.parametrize("resumed", [False, True])
@pytest.mark.parametrize("mode", ["complete", "deadline", "failure", "disabled"])
def test_jd_shop_names_and_real_sales_are_independent(
    make_config, monkeypatch, resumed, mode
):
    """缺店名优先处理；已有评论（含0）也必须补真实销量。"""
    from datetime import datetime
    from crawler.core.progress import check_deadline, SearchProgress
    from crawler.models import Item, ItemPatch
    from crawler.platforms.base import PlatformBase
    from crawler.platforms.jd import JdPlatform

    config = make_config()
    config.crawl.detail.platforms["jd"] = mode != "disabled"
    monkeypatch.setattr(
        "crawler.runner.time.sleep",
        lambda seconds: monkeypatch.setenv("CRAWLER_ENDS_AT", "2000-01-01 00:00:00"),
    )
    monkeypatch.setenv("CRAWLER_AMOUNT_DETAIL", "1")
    monkeypatch.setenv("CRAWLER_ENDS_AT", "2099-01-01 00:00:00")
    matrix = build_keyword_matrix(config)
    entry = matrix[0]
    variants = [
        {},  # 有价格、评论和店名，仅销量缺失
        {"price": None},
        {"shop_name": None},
        {"shop_name": "  ", "sales": 5, "comments": None},
        {"comments": 0},  # 真实0评论不能当作缺失
        {"comments": None},
        {"shop_name": None, "listed": False},
    ]
    items = [
        Item.model_validate(
            {
                "platform": "jd",
                "product_id": str(100000 + index),
                "url": f"https://item.jd.com/{100000 + index}.html",
                "title": "测试挂件",
                "shop_name": "已知测试店",
                "price": 20,
                "sales": None,
                "comments": 10,
                "search_keyword": entry["keyword"],
                "ip_code": entry["ip_code"],
                "crawled_at": datetime.now(),
                **variant,
            }
        )
        for index, variant in enumerate(variants)
    ]
    if resumed:
        progress = SearchProgress(config.state_root, "jd", matrix, continuous=True)
        progress.commit(entry["keyword"], entry["ip_code"], 1, items, done=True)
        progress.state["detail_attempted"] = ["jd:100002"] * 4
        progress.state["detail_pending"] = [progress.detail_key(item) for item in items]
        progress.finish("deadline")
        monkeypatch.setattr(
            PlatformBase, "search", lambda *a, **kw: pytest.fail("不能重跑已完成列表")
        )
    else:
        monkeypatch.setattr(
            PlatformBase,
            "_request",
            lambda self, url, **kw: PageResult(
                url=url, html="", pagination=PaginationState(has_next=False)
            ),
        )
        monkeypatch.setattr(JdPlatform, "parse_search", lambda *a, **kw: items)
    calls = []

    def detail(self, ids):
        pid = ids[0]
        calls.append(pid)
        if mode == "failure" and pid == "100002":
            return []
        fields = (
            {
                "shop_name": "详情测试店",
                "title": "最小单价计算器",
                "price": 9999,
                "sales": 5,
            }
            if pid in {"100002", "100003"}
            else {"price": 20, "sales": 5}
        )
        patch = ItemPatch.model_validate(
            {"platform": "jd", "product_id": pid, **fields}
        )
        self.checkpoint_patch(patch)
        if mode == "deadline":
            monkeypatch.setenv("CRAWLER_ENDS_AT", "2000-01-01 00:00:00")
            check_deadline()
        return [patch]

    monkeypatch.setattr(PlatformBase, "fetch_detail", detail)
    login = PageResult(url="https://home.jd.com/", html="<html>我的京东</html>")
    record = run_platform(
        "jd",
        config,
        date="2026-11-10",
        max_pages=1,
        browser_factory=_factory(StaticBrowser([login])),
    )
    name_order = ["100003", "100002"] if resumed else ["100002", "100003"]
    expected = name_order + ["100000", "100001", "100004", "100005"]
    if mode == "disabled":
        expected = []
    assert calls == expected
    assert record.status == (
        "partial" if mode in ("failure", "disabled") else "deadline"
    )
    rows = list(read_jsonl(config.output_root / "2026-11-10/jd.jsonl"))
    assert len(rows) == len(items)
    by_id = {row["product_id"]: row for row in rows}
    # 可算金额的记录不再带错误的金额待补标记。
    for pid in ("100000", "100004"):
        assert any(f.startswith("金额补采：") for f in by_id[pid]["flags"]) == (
            mode == "disabled"
        )
        assert by_id[pid]["sales"] == (None if mode == "disabled" else 5)
    for pid in name_order:
        if pid in calls and not (mode == "failure" and pid == "100002"):
            assert by_id[pid]["shop_name"] == "详情测试店"
            assert "店铺名缺失" not in by_id[pid]["flags"]
            assert by_id[pid]["title"] == "测试挂件"
            assert by_id[pid]["price"] == 20
    saved = json.loads(
        SearchProgress(config.state_root, "jd", matrix, continuous=True).path.read_text(
            encoding="utf-8"
        )
    )
    pending = set(saved["detail_pending"])
    assert not pending.intersection({"jd:100000", "jd:100004", "jd:100006"})
    if mode == "complete":
        assert pending == set()
        assert saved["active"]
    elif mode == "failure":
        assert pending == set()
        assert saved["detail_unresolved"] == ["jd:100002"]
        assert saved["active"]
    elif mode == "deadline":
        assert pending == set()
        assert saved["detail_unresolved"] == []
        assert saved["active"]
    else:
        assert pending == set()
        assert any("详情补采未启用" in f for f in by_id["100002"]["flags"])


def test_new_task_scope_does_not_restore_previous_round(make_config, monkeypatch):
    from crawler.core.progress import SearchProgress

    config = make_config()
    matrix = build_keyword_matrix(config)
    monkeypatch.delenv("CRAWLER_PROGRESS_SCOPE", raising=False)
    legacy = SearchProgress(config.state_root, "1688", matrix)
    legacy.commit("葫芦兄弟", "huluxiongdi", 6, [], done=True)
    before = legacy.path.read_bytes()
    paths = []
    for task in ("new-task-one", "new-task-two"):
        monkeypatch.setenv("CRAWLER_PROGRESS_SCOPE", task)
        progress = SearchProgress(config.state_root, "1688", matrix)
        assert not progress.resumed
        assert progress.position("葫芦兄弟", "huluxiongdi") == {}
        progress.commit("葫芦兄弟", "huluxiongdi", 1, [])
        assert SearchProgress(config.state_root, "1688", matrix).resumed
        paths.append(progress.path)
    assert len(set(paths + [legacy.path])) == 3
    assert legacy.path.read_bytes() == before


@pytest.mark.parametrize("resumed", [False, True])
@pytest.mark.parametrize("cap", [1, 3])
@pytest.mark.parametrize("timed", [False, True])
def test_1688_independent_shop_backfill_ignores_cap(
    make_config, monkeypatch, resumed, cap, timed
):
    from datetime import datetime
    from crawler.core.progress import SearchProgress
    from crawler.models import Item
    from crawler.platforms.ali1688 import Ali1688Platform

    monkeypatch.setenv("CRAWLER_AMOUNT_DETAIL", "1")
    monkeypatch.setenv("CRAWLER_PROGRESS_SCOPE", "new-round-backfill")
    monkeypatch.delenv("CRAWLER_ENDS_AT", raising=False)
    if timed:
        monkeypatch.setenv("CRAWLER_ENDS_AT", "2099-01-01 00:00:00")
        monkeypatch.setattr(
            "crawler.runner.time.sleep",
            lambda seconds: monkeypatch.setenv(
                "CRAWLER_ENDS_AT", "2000-01-01 00:00:00"
            ),
        )
    config = make_config()
    config.crawl.detail.platforms["1688"] = False
    backfill = config.crawl.ali1688.shop_name_backfill
    backfill.enabled, backfill.max_per_run, backfill.source = True, cap, "detail"
    matrix = build_keyword_matrix(config)
    entry = matrix[0]
    items = [
        Item(
            platform="1688",
            product_id=str(500000 + index),
            url=f"https://detail.1688.com/offer/{500000 + index}.html",
            title="葫芦兄弟测试商品",
            search_keyword=entry["keyword"],
            ip_code=entry["ip_code"],
            price=20,
            sales=100,
            crawled_at=datetime.now(),
        )
        for index in range(3)
    ]
    pages = [PageResult(url=Ali1688Platform.login_check_url, html="账号正常")]
    if resumed:
        progress = SearchProgress(config.state_root, "1688", matrix, continuous=timed)
        progress.commit(entry["keyword"], entry["ip_code"], 1, items, done=True)
        progress.finish("partial")
        monkeypatch.setattr(
            Ali1688Platform,
            "search",
            lambda *a, **kw: pytest.fail("不能重跑本任务已完成列表"),
        )
    else:
        pages.append(
            PageResult(
                url="https://s.1688.com/",
                html="列表",
                pagination=PaginationState(has_next=False),
            )
        )

        def parse(*args, **kwargs):
            if timed:
                monkeypatch.setenv("CRAWLER_ENDS_AT", "2000-01-01 00:00:00")
            return items

        monkeypatch.setattr(Ali1688Platform, "parse_search", parse)
    expected_cap = len(items)
    pages.extend(
        PageResult(
            url=item.url, html=f'<div class="company-name">店铺{item.product_id}</div>'
        )
        for item in items[:expected_cap]
    )
    browser = StaticBrowser(pages)
    record = run_platform(
        "1688",
        config,
        date="2026-11-10",
        max_pages=1,
        browser_factory=_factory(browser),
    )
    rows = read_jsonl(config.output_root / "2026-11-10/1688.jsonl")
    assert sum(bool(row["shop_name"]) for row in rows) == expected_cap
    assert record.status == (
        "partial" if expected_cap != 3 else "deadline" if timed else "ok"
    )
    assert len(record.detail_failures) == 3 - expected_cap
    assert all("额度" in value for value in record.detail_failures.values())
    assert all(row["price"] == 20 and row["sales"] == 100 for row in rows)
    assert not any("金额补采：" in flag for row in rows[:cap] for flag in row["flags"])
    state = json.loads(
        SearchProgress(
            config.state_root, "1688", matrix, continuous=timed
        ).path.read_text(encoding="utf-8")
    )
    assert state["detail_pending"] == []
    assert len(state["detail_unresolved"]) == 3 - expected_cap
    assert len(browser.calls) == 1 + int(not resumed) + expected_cap


@pytest.mark.parametrize("platform", ["taobao", "jd", "1688"])
@pytest.mark.parametrize("phase", ["search", "supplemental"])
@pytest.mark.parametrize("stop", ["risk_blocked", "login_failed"])
def test_timed_collection_respects_safety_stops(
    make_config, monkeypatch, platform, phase, stop
):
    from datetime import datetime
    from crawler.core.progress import SearchProgress
    from crawler.models import Item
    from crawler.platforms import create_platform

    monkeypatch.setenv("CRAWLER_ENDS_AT", "2099-01-01 00:00:00")
    monkeypatch.setenv("CRAWLER_AMOUNT_DETAIL", "1")
    config = make_config()
    config.crawl.detail.platforms[platform] = True
    # 保留旧配额输入，第三次请求仍应发出，并由实际登录/风控状态停止。
    config.crawl.daily_max_pages = 2
    probe = create_platform(platform, config, StaticBrowser([]))
    calls = []

    class SafetyBrowser(StaticBrowser):
        def fetch_page(self, url, **kwargs):
            calls.append(url)
            if url == probe.login_check_url:
                return PageResult(url=url, html="账号正常")
            if url == probe.build_search_url("葫芦兄弟", 1):
                if phase == "supplemental":
                    monkeypatch.setenv("CRAWLER_ENDS_AT", "2000-01-01 00:00:00")
                return PageResult(url=url, html="商品列表")
            return PageResult(
                url=url, html="", status=403 if stop == "risk_blocked" else 401
            )

    def parse(self, result, keyword, page, ip_code):
        return [
            Item(
                platform=platform,
                product_id="123451",
                title=keyword,
                url=self.detail_url("123451"),
                search_keyword=keyword,
                ip_code=ip_code,
                crawled_at=datetime.now(),
            )
        ]

    monkeypatch.setattr(type(probe), "parse_search", parse)
    record = run_platform(
        platform, config, date="2026-11-10", browser_factory=_factory(SafetyBrowser([]))
    )
    assert record.status == stop
    assert record.items_ok == 1 and record.detail_failures
    assert len(calls) == 3
    assert calls[-1] == (
        probe.build_search_url("葫芦兄弟", 2)
        if phase == "search"
        else probe.detail_url("123451")
    )
    assert record.risk_blocked == (stop == "risk_blocked")
    # 风控立即停止请求；已通过的登录检查不等于风控解除。
    assert record.login_ok == (stop != "login_failed")
    saved = SearchProgress(
        config.state_root, platform, build_keyword_matrix(config), continuous=True
    )
    assert saved.state["detail_pending"] == []
    assert saved.state["detail_unresolved"] == [f"{platform}:123451"]
    assert saved.state["status"] == stop
    position = saved.position("葫芦兄弟", "huluxiongdi")
    assert position["next_page"] == 2
    assert not position.get("done")


def test_parallel_platforms_share_deadline_and_drain_inflight_results(
    make_config, monkeypatch
):
    from datetime import datetime
    from threading import Barrier, Event
    from crawler.models import Item, ItemPatch
    from crawler.platforms import create_platform

    config = make_config()
    config.crawl.parallel_platforms = True
    config.crawl.max_pages = 1
    config.crawl.taobao.detail.complete_missing_amount = True
    config.crawl.detail.platforms["1688"] = True
    monkeypatch.setenv("CRAWLER_AMOUNT_DETAIL", "1")
    expired, detail_started = Event(), Event()
    barrier = Barrier(3, action=expired.set, timeout=10)
    remaining = lambda: 0.0 if expired.is_set() else 60.0
    monkeypatch.setattr("crawler.core.progress.remaining_seconds", remaining)
    monkeypatch.setattr("crawler.runner.remaining_seconds", remaining)
    searches = {name: [] for name in ("taobao", "jd", "1688")}
    details = {name: [] for name in searches}
    browsers = {}

    def parse(self, result, keyword, page, ip_code):
        return [
            Item(
                platform=self.name,
                product_id=str(123450 + page),
                title=keyword,
                url=self.detail_url(str(123450 + page)),
                search_keyword=keyword,
                ip_code=ip_code,
                crawled_at=datetime.now(),
            )
        ]

    def detail(self, result, pid):
        return ItemPatch.model_validate(
            {
                "platform": self.name,
                "product_id": pid,
                "price": 20,
                "sales": 100,
                "shop_name": "并发补采店",
            }
        )

    def factory(config, name, headless):
        probe = create_platform(name, config, StaticBrowser([]))

        class ParallelBrowser(StaticBrowser):
            def fetch_page(self, url, **kwargs):
                if url == probe.login_check_url:
                    return PageResult(url=url, html="账号正常")
                for page in (1, 2, 3):
                    if url == probe.build_search_url("葫芦兄弟", page):
                        assert not expired.is_set()
                        searches[name].append(page)
                        if page == 3:
                            barrier.wait()
                            if name == "1688":
                                assert detail_started.wait(10)
                        return PageResult(url=url, html="商品列表")
                assert expired.is_set()
                assert url in [
                    probe.detail_url(str(pid)) for pid in (123451, 123452, 123453)
                ]
                details[name].append(url)
                detail_started.set()
                return PageResult(url=url, html="详情")

        browser = ParallelBrowser([])
        browsers[name] = browser
        return browser

    for name in searches:
        impl = type(create_platform(name, config, StaticBrowser([])))
        monkeypatch.setattr(impl, "parse_search", parse)
        monkeypatch.setattr(impl, "parse_detail", detail)
    records = run_platforms(
        list(searches), config, date="2026-11-10", browser_factory=factory
    )
    assert all(pages == [1, 2, 3] for pages in searches.values())
    assert all(len(calls) == 3 for calls in details.values())
    assert all(
        record.status == "deadline" and record.items_ok == 3 for record in records
    )
    assert all(
        not record.detail_failures and record.deadline_reached for record in records
    )
    assert all(browser.closed for browser in browsers.values())


@pytest.mark.parametrize("platform", ["taobao", "jd", "1688"])
@pytest.mark.parametrize("missing", ["title", "shop_name", "price", "sales"])
def test_every_required_field_reaches_detail(
    make_config, monkeypatch, platform, missing
):
    """四字段逐项补缺，已有评论不能让缺销量商品跳过详情。"""
    from datetime import datetime
    from crawler.models import Item, ItemPatch
    from crawler.platforms import create_platform

    monkeypatch.delenv("CRAWLER_ENDS_AT", raising=False)
    monkeypatch.setenv("CRAWLER_AMOUNT_DETAIL", "1")
    config = make_config()
    config.crawl.detail.platforms[platform] = True
    config.crawl.taobao.detail.complete_missing_amount = True
    browser = StaticBrowser([])
    probe = create_platform(platform, config, browser)
    values = {"title": "葫芦兄弟挂件", "shop_name": "测试店铺", "price": 20, "sales": 0}
    calls = []

    def parse(self, result, keyword, page, ip_code):
        item = Item(
            platform=platform,
            product_id="300001",
            url=self.detail_url("300001"),
            search_keyword=keyword,
            ip_code=ip_code,
            crawled_at=datetime.now(),
            comments=100,
            **{**values, missing: "" if missing == "title" else None},
        )
        valid = self.validate(item)
        return [valid] if valid is not None else []

    def detail(self, result, product_id):
        calls.append(product_id)
        return ItemPatch(platform=platform, product_id=product_id, **values)

    class Browser(StaticBrowser):
        def fetch_page(self, url, **kwargs):
            return PageResult(
                url=url, html="账号正常", pagination=PaginationState(has_next=False)
            )

    monkeypatch.setattr(type(probe), "parse_search", parse)
    monkeypatch.setattr(type(probe), "parse_detail", detail)
    record = run_platform(
        platform,
        config,
        date="2026-11-10",
        max_pages=1,
        browser_factory=_factory(Browser([])),
    )
    assert calls == ["300001"]
    assert record.status == "ok" and record.items_ok == 1
    row = read_jsonl(config.output_root / f"2026-11-10/{platform}.jsonl")[0]
    assert all(row[field] == value for field, value in values.items())
    assert row["comments"] == 100 and not record.detail_failures


@pytest.mark.parametrize("platform", ["taobao", "jd", "1688"])
def test_timed_required_fields_are_not_sampled(make_config, monkeypatch, platform):
    """33条候选不能因每日页数、淘宝5条或1688的30条限制被截断。"""
    from datetime import datetime
    from crawler.models import Item, ItemPatch
    from crawler.platforms import create_platform

    monkeypatch.setenv("CRAWLER_ENDS_AT", "2099-01-01 00:00:00")
    monkeypatch.setenv("CRAWLER_AMOUNT_DETAIL", "1")
    config = make_config()
    config.crawl.daily_max_pages = 2
    config.crawl.taobao.detail.max_per_run = 1
    config.crawl.taobao.detail.complete_missing_amount = False
    config.crawl.ali1688.shop_name_backfill.max_per_run = 1
    config.crawl.detail.platforms[platform] = True
    probe = create_platform(platform, config, StaticBrowser([]))
    calls = []

    def parse(self, result, keyword, page, ip_code):
        monkeypatch.setenv("CRAWLER_ENDS_AT", "2000-01-01 00:00:00")
        return [
            Item(
                platform=platform,
                product_id=str(300000 + i),
                url=self.detail_url(str(300000 + i)),
                title=keyword,
                search_keyword=keyword,
                ip_code=ip_code,
                crawled_at=datetime.now(),
            )
            for i in range(33)
        ]

    def detail(self, result, pid):
        calls.append(pid)
        return ItemPatch(
            platform=platform,
            product_id=pid,
            title="葫芦兄弟挂件",
            shop_name="完整测试店",
            price=20,
            sales=100,
        )

    class Browser(StaticBrowser):
        def fetch_page(self, url, **kwargs):
            return PageResult(
                url=url, html="账号正常", pagination=PaginationState(has_next=False)
            )

    monkeypatch.setattr(type(probe), "parse_search", parse)
    monkeypatch.setattr(type(probe), "parse_detail", detail)
    record = run_platform(
        platform, config, date="2026-11-10", browser_factory=_factory(Browser([]))
    )
    assert len(calls) == len(set(calls)) == 33
    assert record.status == "deadline" and record.deadline_reached
    assert not record.detail_failures and record.items_ok == 33
    rows = read_jsonl(config.output_root / f"2026-11-10/{platform}.jsonl")
    assert all(
        row["shop_name"] and row["title"] and row["price"] == 20 and row["sales"] == 100
        for row in rows
    )


@pytest.mark.parametrize("platform", ["taobao", "jd", "1688"])
def test_finished_keywords_restart_before_deadline(make_config, monkeypatch, platform):
    from datetime import datetime
    from crawler.models import Item
    from crawler.platforms import create_platform
    from crawler.core.progress import SearchProgress

    monkeypatch.setenv("CRAWLER_ENDS_AT", "2099-01-01 00:00:00")
    monkeypatch.setenv("CRAWLER_AMOUNT_DETAIL", "1")
    monkeypatch.setattr("crawler.runner.time.sleep", lambda seconds: None)
    config = make_config()
    probe = create_platform(platform, config, StaticBrowser([]))
    rounds = []

    def parse(self, result, keyword, page, ip_code):
        if page > 1:
            return []
        rounds.append(self.search_progress.state["round"])
        if len(rounds) == 2:
            monkeypatch.setenv("CRAWLER_ENDS_AT", "2000-01-01 00:00:00")
        pid = str(400000 + len(rounds))
        return [
            Item(
                platform=platform,
                product_id=pid,
                url=self.detail_url(pid),
                title=keyword,
                shop_name="测试店",
                price=20,
                sales=0,
                search_keyword=keyword,
                ip_code=ip_code,
                crawled_at=datetime.now(),
            )
        ]

    class Browser(StaticBrowser):
        def fetch_page(self, url, **kwargs):
            return PageResult(
                url=url, html="账号正常", pagination=PaginationState(has_next=False)
            )

    monkeypatch.setattr(type(probe), "parse_search", parse)
    record = run_platform(
        platform, config, date="2026-11-10", browser_factory=_factory(Browser([]))
    )
    assert rounds == [1, 2]
    assert record.status == "deadline" and record.deadline_reached
    assert record.items_ok == 2
    saved = SearchProgress(
        config.state_root, platform, build_keyword_matrix(config), continuous=True
    )
    assert saved.state["round"] == 2


def test_timed_multi_platform_ignores_serial_default(make_config, monkeypatch):
    from threading import Barrier

    config = make_config()
    config.crawl.parallel_platforms = False
    monkeypatch.setenv("CRAWLER_ENDS_AT", "2099-01-01 00:00:00")
    barrier = Barrier(3, timeout=2)

    def run(name, config, **kwargs):
        barrier.wait()
        return name

    monkeypatch.setattr("crawler.runner.run_platform", run)
    assert run_platforms(["all"], config) == ["taobao", "jd", "1688"]


@pytest.mark.parametrize("platform", ["taobao", "jd", "1688"])
def test_timed_transient_failure_retries_same_page(make_config, monkeypatch, platform):
    from datetime import datetime
    from crawler.models import Item
    from crawler.platforms import create_platform

    monkeypatch.setenv("CRAWLER_ENDS_AT", "2099-01-01 00:00:00")
    monkeypatch.setattr("crawler.runner.time.sleep", lambda seconds: None)
    config = make_config()
    probe = create_platform(platform, config, StaticBrowser([]))
    calls = []

    class Browser(StaticBrowser):
        def fetch_page(self, url, **kwargs):
            if url == probe.login_check_url:
                return PageResult(url=url, html="账号正常")
            calls.append(url)
            if len(calls) == 2:
                return PageResult(url=url, html="", status=503)
            if len(calls) == 3:
                monkeypatch.setenv("CRAWLER_ENDS_AT", "2000-01-01 00:00:00")
            return PageResult(url=url, html="商品列表")

    def parse(self, result, keyword, page, ip_code):
        return [
            Item(
                platform=platform,
                product_id=str(300000 + page),
                url=self.detail_url(str(300000 + page)),
                title=keyword,
                shop_name="测试店",
                price=20,
                sales=0,
                search_keyword=keyword,
                ip_code=ip_code,
                crawled_at=datetime.now(),
            )
        ]

    monkeypatch.setattr(type(probe), "parse_search", parse)
    record = run_platform(
        platform, config, date="2026-11-10", browser_factory=_factory(Browser([]))
    )
    assert calls == [probe.build_search_url("葫芦兄弟", page) for page in (1, 2, 2)]
    assert record.items_ok == 2 and record.deadline_reached
    assert record.status == "deadline"
