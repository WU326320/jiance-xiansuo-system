from __future__ import annotations

from pathlib import Path
import os
import pytest

from crawler.core.browser import (
    CapturedResponse,
    PageResult,
    ResponseRecorder,
    StaticBrowser,
    _collect_dom_hints,
)
from crawler.tests.conftest import assert_private_mode


class FakeResponse:
    def __init__(self, url, body, content_type="application/json", status=200):
        self.url = url
        self._body = body
        self.headers = {"content-type": content_type}
        self.status = status
        self.request: object | None = None

    def text(self):
        return self._body


@pytest.mark.skipif(
    os.environ.get("RUN_BROWSER_INTEGRATION") != "1",
    reason="显式运行本地浏览器慢响应测试",
)
def test_response_recorder_does_not_wait_for_unfinished_http_body():
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from playwright.sync_api import sync_playwright

    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.flush()
            release.wait(4)
            self.wfile.write(b'{"complete":true}')

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            try:
                page = browser.new_page()
                recorder = ResponseRecorder()
                recorder.attach(page)
                url = f"http://127.0.0.1:{server.server_port}/slow"
                with page.expect_response(url):
                    page.evaluate("url => { fetch(url); }", url)
                started = time.monotonic()
                captured = recorder.collect()
                elapsed = time.monotonic() - started
                assert elapsed < 1, f"响应体未结束时 collect 阻塞了 {elapsed:.2f}s"
                assert captured == []
                with page.expect_event("requestfinished"):
                    release.set()
                assert [entry.text for entry in recorder.collect()] == [
                    '{"complete":true}'
                ]
            finally:
                browser.close()
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class FakePage:
    def __init__(self):
        self.handlers = {}

    def on(self, event, handler):
        self.handlers[event] = handler


def test_response_recorder_filters_and_collects():
    page = FakePage()
    recorder = ResponseRecorder(("mtop",), max_responses=2)
    recorder.attach(page)
    page.handlers["response"](FakeResponse("https://h5api.m.taobao.com/a", '{"a":1}'))
    page.handlers["response"](
        FakeResponse("https://img.alicdn.com/a.jpg", "binary", "image/jpeg")
    )
    page.handlers["response"](
        FakeResponse("https://x.com/a.json", '{"b":2}', "text/plain")
    )
    page.handlers["response"](FakeResponse("https://h5api.m.taobao.com/b", '{"c":3}'))
    captured = recorder.collect()
    assert len(captured) == 2
    assert captured[0].url.endswith("/a")
    assert captured[1].url.endswith("/a.json")


def test_response_recorder_skips_unreadable_and_large():
    page = FakePage()
    recorder = ResponseRecorder(("x",), max_body_bytes=10)
    recorder.attach(page)
    page.handlers["response"](FakeResponse("https://x.com/a", "x" * 100))
    assert recorder.collect() == []


def test_response_recorder_retains_pending_and_discards_failed_request():
    page = FakePage()
    recorder = ResponseRecorder()
    recorder.attach(page)
    response = FakeResponse("https://api.example/slow", '{"ok":true}')
    response.request = object()
    reads = []
    response.text = lambda: reads.append(True) or response._body
    page.handlers["request"](response.request)
    page.handlers["response"](response)
    assert recorder.collect() == []
    assert reads == []
    page.handlers["requestfailed"](response.request)
    page.handlers["requestfinished"](response.request)
    assert recorder.collect() == []
    assert reads == []
    assert not recorder._requests
    assert not recorder._finished


@pytest.mark.parametrize("event", ["requestfinished", "requestfailed"])
def test_response_event_during_another_response_read(event):
    page = FakePage()
    recorder = ResponseRecorder()
    recorder.attach(page)
    first = FakeResponse("https://api.example/first", "first")
    second = FakeResponse("https://api.example/second", "second")
    for response in (first, second):
        response.request = object()
        page.handlers["request"](response.request)
        page.handlers["response"](response)
    page.handlers["requestfinished"](first.request)

    def read_first():
        page.handlers[event](second.request)
        return "first"

    first.text = read_first
    expected = ["first", "second"] if event == "requestfinished" else ["first"]
    assert [entry.text for entry in recorder.collect()] == expected
    assert [entry.text for entry in recorder.collect()] == expected
    assert not recorder._responses
    assert not recorder._finished


def test_response_recorder_sort_boundary_excludes_late_old_requests():
    import time

    page = FakePage()
    recorder = ResponseRecorder(("search",))
    recorder.attach(page)
    old_request = object()
    new_request = object()
    page.handlers["request"](old_request)
    boundary = time.monotonic()
    page.handlers["request"](new_request)
    old = FakeResponse("https://api.example/search", '{"old":true}')
    old.request = old_request
    new = FakeResponse("https://api.example/search", '{"new":true}')
    new.request = new_request
    page.handlers["response"](old)
    recorder.discard_before(boundary)
    page.handlers["response"](new)
    page.handlers["requestfinished"](new_request)
    assert [item.text for item in recorder.collect()] == ['{"new":true}']
    assert recorder.captured[0].requested_at >= boundary


def test_response_recorder_keeps_distinct_posts_to_same_url():
    page = FakePage()
    recorder = ResponseRecorder(("search",))
    recorder.attach(page)
    for body in ('{"old":true}', '{"new":true}'):
        request = object()
        page.handlers["request"](request)
        response = FakeResponse("https://api.example/search", body)
        response.request = request
        page.handlers["response"](response)
        page.handlers["requestfinished"](request)
    assert [item.text for item in recorder.collect()] == [
        '{"old":true}',
        '{"new":true}',
    ]


def test_static_browser_replays_pages_and_screenshot(tmp_path):
    pages = [
        PageResult(url="https://a.example/1", html="<html>1</html>"),
        PageResult(url="https://a.example/2", html="<html>2</html>"),
    ]
    browser = StaticBrowser(pages)
    result = browser.fetch_page("https://a.example/1")
    assert result.html == "<html>1</html>"
    assert browser.current_url() == "https://a.example/1"
    assert browser.page_html() == "<html>1</html>"
    shot = browser.screenshot(tmp_path / "s.png")
    assert Path(shot).exists()
    browser.fetch_page("https://a.example/2")
    browser.close()
    assert browser.closed is True


def test_captured_response_dataclass():
    captured = CapturedResponse(
        url="u", text="{}", content_type="application/json", status=200
    )
    assert captured.status == 200


class _HintLocator:
    def __init__(self, *, visible: bool):
        self._visible = visible

    def count(self):
        return 1

    @property
    def first(self):
        return self

    def is_visible(self):
        return self._visible


class _HintsPage:
    def locator(self, selector):
        return _HintLocator(visible=selector == "#nc_1_wrapper")


def test_collect_dom_hints_only_returns_visible_nodes():
    hints = _collect_dom_hints(_HintsPage())
    assert hints == ["#nc_1_wrapper"]


class _EmptyHintsLocator:
    def count(self):
        return 0


class _EmptyHintsPage:
    def locator(self, selector):
        return _EmptyHintsLocator()


def test_collect_dom_hints_ignores_hidden_nodes():
    assert _collect_dom_hints(_EmptyHintsPage()) == []


class FakePlaywrightResponse:
    def __init__(self, url="https://api.example/data", text='{"ok": true}'):
        self.url = url
        self.headers = {"content-type": "application/json"}
        self.status = 200
        self._text = text

    def text(self):
        return self._text


class FakeLocator:
    def __init__(self, count):
        self._count = count

    def count(self):
        return self._count


class FakePage:
    def __init__(self, risk_hint=False):
        self.url = "https://final.example/page"
        self.handlers = {}
        self.risk_hint = risk_hint
        self.shots = []

    def on(self, event, handler):
        self.handlers[event] = handler

    def locator(self, selector):
        if selector == "#nc_1_wrapper" and self.risk_hint:
            return FakeLocator(1)
        return FakeLocator(0)

    def screenshot(self, path, full_page=True):
        self.shots.append(path)
        Path(path).write_bytes(b"png")

    def content(self):
        return "<html>final</html>"


class FakeScraplingSession:
    instances = []

    def __init__(self, **options):
        self.options = options
        self.context = type("Ctx", (), {"cookies": lambda self: [{"name": "pt_key"}]})()
        self.closed = False
        self.risk_hint = False
        FakeScraplingSession.instances.append(self)

    def start(self):
        return None

    def fetch(self, url, **kwargs):
        page = FakePage(risk_hint=self.risk_hint)
        kwargs["page_setup"](page)
        if "api" in url:
            page.handlers["response"](FakePlaywrightResponse())
        kwargs["page_action"](page)
        return type(
            "Resp",
            (),
            {
                "body": b"<html>body</html>",
                "url": url,
                "status": 200,
                "encoding": "utf-8",
            },
        )()

    def close(self):
        self.closed = True


def test_scrapling_browser_wraps_session(make_config, tmp_path, monkeypatch):
    from crawler.core.browser import ScraplingBrowser
    import scrapling.fetchers as fetchers

    FakeScraplingSession.instances = []
    monkeypatch.setattr(fetchers, "StealthySession", FakeScraplingSession)
    config = make_config()
    browser = ScraplingBrowser(config, "taobao", headless=True)
    browser.start()
    session = FakeScraplingSession.instances[0]
    assert session.options["user_data_dir"] == str(config.profile_dir("taobao"))
    assert session.options["headless"] is True

    called = []
    result = browser.fetch_page(
        "https://api.example/search",
        action=lambda page: called.append(page),
        capture_patterns=("api",),
        wait_ms=10,
    )
    # 11/6 修复：CSR 页面必须回放 Playwright 当前 DOM，而不是最初那份
    # navigation response body（淘宝空壳页整页 0 条的根因）。
    assert result.html == "<html>final</html>"
    assert result.status == 200
    assert result.captured and result.captured[0].text == '{"ok": true}'
    assert called
    assert browser.current_url() == "https://final.example/page"
    assert browser.page_html() == "<html>final</html>"
    assert browser.cookie_names() == {"pt_key"}
    assert browser.has_cookie(["pt_key"]) is True
    shot = browser.screenshot(tmp_path / "shot.png")
    assert shot and Path(shot).exists()
    marker = browser.save_login_marker({"platform": "taobao"})
    assert_private_mode(marker, 0o600)
    browser.close()
    assert session.closed is True


class FakeGotoPage:
    def __init__(self, *, error=None):
        self.url = "about:blank"
        self.error = error
        self.goto_calls = []
        self.handlers = {}
        self.waits = []

    def on(self, event, handler):
        self.handlers[event] = handler

    def goto(self, url, **kwargs):
        self.goto_calls.append((url, kwargs))
        self.url = url
        if self.error is not None:
            raise self.error
        return type("NavigationResponse", (), {"status": 200})()

    def wait_for_timeout(self, milliseconds):
        self.waits.append(milliseconds)

    def locator(self, selector):
        return FakeLocator(0)

    def content(self):
        return "<html>opened</html>"


class FakeGotoContext:
    def __init__(self, page):
        self.page = page

    def new_page(self):
        return self.page

    def cookies(self):
        return []


class FakeGotoSession:
    def __init__(self, page):
        self.context = FakeGotoContext(page)
        self.closed = False

    def start(self):
        return None

    def close(self):
        self.closed = True


def test_scrapling_browser_direct_navigation_uses_domcontentloaded_and_survives_timeout(
    make_config, monkeypatch
):
    from crawler.core.browser import ScraplingBrowser
    import scrapling.fetchers as fetchers

    url = "https://passport.jd.com/new/login.aspx"
    page = FakeGotoPage(error=TimeoutError("Page.goto: Timeout 60000ms exceeded"))
    session = FakeGotoSession(page)
    monkeypatch.setattr(fetchers, "StealthySession", lambda **options: session)
    browser = ScraplingBrowser(make_config(), "jd", headless=False, mode="playwright")

    result = browser.fetch_page(
        url,
        capture_patterns=(),
        wait_ms=0,
        timeout_ms=60_000,
        wait_until="domcontentloaded",
    )

    # 只导航一次，并且不等待 load 事件。
    assert page.goto_calls == [
        (url, {"wait_until": "domcontentloaded", "timeout": 60_000})
    ]
    assert result.url == url
    assert result.html == "<html>opened</html>"
    assert browser.current_url() == url
    assert browser.page_html() == "<html>opened</html>"
    browser.close()
    assert session.closed is True


def test_scrapling_browser_no_session_methods(make_config):
    from crawler.core.browser import ScraplingBrowser

    browser = ScraplingBrowser(make_config(), "jd", headless=True)
    assert browser.current_url() is None
    assert browser.page_html() is None
    assert browser.screenshot("/tmp/none.png") is None
    assert browser.has_cookie(["pt_key"]) is False
    browser.close()


def test_scrapling_browser_start_failure(make_config, monkeypatch):
    from crawler.core.browser import ScraplingBrowser
    from crawler.core.errors import BrowserUnavailable
    import scrapling.fetchers as fetchers

    class Broken:
        def __init__(self, **options):
            pass

        def start(self):
            raise RuntimeError("browser missing")

    monkeypatch.setattr(fetchers, "StealthySession", Broken)
    browser = ScraplingBrowser(make_config(), "jd", headless=True, mode="playwright")
    try:
        browser.start()
    except BrowserUnavailable as exc:
        assert "browser missing" in str(exc)
    else:
        raise AssertionError("应抛 BrowserUnavailable")
