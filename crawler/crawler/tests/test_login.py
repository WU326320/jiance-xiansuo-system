from __future__ import annotations

import sys
from pathlib import Path

import pytest

from crawler.core.browser import PageResult, StaticBrowser
from crawler.login import (
    LOGIN_NAVIGATION_TIMEOUT_MS,
    LOGIN_WAIT_UNTIL,
    MAX_LOGIN_WAIT_SECONDS,
    _effective_timeout,
    _is_login_url,
    check_login_platform,
    check_task_login,
    login_platform,
    validate_display,
    x11_socket,
)
from crawler.tests.conftest import fixture_text


@pytest.mark.skipif(
    sys.platform.startswith("win") or sys.platform == "darwin",
    reason="Windows/macOS 不使用 X11，validate_display 有意直接返回可用；X11 分支由 Linux 覆盖",
)
def test_display_validation(tmp_path, monkeypatch):
    monkeypatch.setattr("crawler.login.shutil.which", lambda name: None)
    assert not validate_display("", base=tmp_path)[0]
    assert not validate_display("host:0", base=tmp_path)[0]
    assert not validate_display(":97", base=tmp_path)[0]
    (tmp_path / "X97").touch()
    assert validate_display(":97", base=tmp_path)[0]
    assert x11_socket(":97", tmp_path) == tmp_path / "X97"


def test_login_url_detection():
    assert _is_login_url("taobao", "https://login.taobao.com/member/login.jhtml")
    assert _is_login_url("jd", "https://passport.jd.com/new/login.aspx")
    assert not _is_login_url("1688", "https://work.1688.com/")


def test_login_wait_is_capped_at_ten_minutes():
    assert MAX_LOGIN_WAIT_SECONDS == 600.0
    assert _effective_timeout(60) == 60
    assert _effective_timeout(99999) == MAX_LOGIN_WAIT_SECONDS
    assert _effective_timeout(-1) == 0


@pytest.mark.parametrize("platform", ["taobao", "jd", "1688"])
def test_task_login_keeps_window_open_until_ten_minute_timeout(
    make_config, monkeypatch, platform
):
    from crawler.platforms import create_platform

    config = make_config()
    urls = {
        "taobao": "https://login.taobao.com/member/login.jhtml",
        "jd": "https://passport.jd.com/new/login.aspx",
        "1688": "https://login.1688.com/member/signin.htm",
    }

    class HeadedBrowser(StaticBrowser):
        headless = False

    browser = HeadedBrowser(
        [PageResult(url=urls[platform], html="<html>扫码登录</html>")]
    )
    crawler = create_platform(platform, config, browser)
    clock = [0.0]
    monkeypatch.delenv("CRAWLER_ENDS_AT", raising=False)
    monkeypatch.setattr("crawler.login.time.monotonic", lambda: clock[0])

    def sleep(seconds):
        assert not browser.closed and len(browser.calls) == 1
        clock[0] += seconds

    monkeypatch.setattr("crawler.login.time.sleep", sleep)
    assert not check_task_login(crawler)
    assert clock[0] == 600
    assert "10 分钟" in crawler.last_login_reason
    assert not browser.closed and len(browser.calls) == 1


def test_task_login_respects_task_deadline(make_config, monkeypatch):
    from crawler.platforms import create_platform

    class HeadedBrowser(StaticBrowser):
        headless = False

    browser = HeadedBrowser(
        [
            PageResult(
                url="https://passport.jd.com/new/login.aspx",
                html="<html>扫码登录</html>",
            )
        ]
    )
    crawler = create_platform("jd", make_config(), browser)
    monkeypatch.setenv("CRAWLER_ENDS_AT", "2000-01-01 00:00:00")
    monkeypatch.setattr(
        "crawler.login.time.sleep", lambda seconds: pytest.fail("截止后不应继续等待")
    )
    assert not check_task_login(crawler)
    assert "结束时间" in crawler.last_login_reason


@pytest.mark.parametrize("platform", ["taobao", "jd", "1688"])
@pytest.mark.parametrize("recover", [False, True])
def test_timed_task_login_waits_beyond_ten_minutes(
    make_config, monkeypatch, platform, recover
):
    from crawler.platforms import create_platform
    from crawler.login import _login_page, _check_page

    clock = [0.0]

    class HeadedBrowser(StaticBrowser):
        headless = False

        def current_url(self):
            return (
                _check_page(platform)
                if recover and clock[0] >= 603
                else _login_page(platform)
            )

        def page_html(self):
            return "账号正常" if recover and clock[0] >= 603 else "扫码登录"

        def has_cookie(self, names):
            return recover and clock[0] >= 603

        def cookie_values(self):
            return (
                {"_nk_": "test", "pt_key": "test", "__cn_logon__": "true"}
                if recover and clock[0] >= 603
                else {}
            )

        def cookie_names(self):
            return set(self.cookie_values())

    browser = HeadedBrowser([PageResult(url=_login_page(platform), html="扫码登录")])
    crawler = create_platform(platform, make_config(), browser)
    monkeypatch.setattr(
        "crawler.core.progress.remaining_seconds", lambda: max(0, 610 - clock[0])
    )
    monkeypatch.setattr("crawler.login.time.monotonic", lambda: clock[0])
    monkeypatch.setattr(
        "crawler.login.time.sleep",
        lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )
    assert check_task_login(crawler) is recover
    assert clock[0] == (603 if recover else 610)
    assert len(browser.calls) == 1 and not browser.closed


def test_check_login_platform_failure(make_config):
    config = make_config()
    browser = StaticBrowser(
        [
            PageResult(
                url="https://login.taobao.com/member/login.jhtml",
                html=fixture_text("login_taobao.html"),
            )
        ]
    )
    code = check_login_platform(
        "taobao", config, browser_factory=lambda *a, **k: browser
    )
    assert code == 1
    assert browser.closed


def test_check_login_platform_ok(make_config):
    config = make_config()
    browser = StaticBrowser(
        [
            PageResult(
                url="https://i.taobao.com/my_taobao.htm",
                html="<html><body>我的淘宝 账号正常</body></html>",
            )
        ]
    )
    code = check_login_platform(
        "taobao", config, browser_factory=lambda *a, **k: browser
    )
    assert code == 0


class PassiveLoginBrowser:
    """只允许首次 fetch；后续 current_url/page_html/has_cookie 都是被动读取。"""

    def __init__(self, *, urls, cookies=(), html="<html><body>登录页</body></html>"):
        self.urls = list(urls)
        self.cookies = set(cookies)
        self.html = html
        self.calls: list[str] = []
        self.fetch_kwargs: list[dict] = []
        self.screenshots: list[str] = []
        self.marker = None
        self.closed = False
        self._url_index = 0
        self._last_url = self.urls[0] if self.urls else ""

    def start(self):
        return None

    def fetch_page(self, url, **kwargs):
        self.calls.append(url)
        self.fetch_kwargs.append(kwargs)
        self._last_url = url
        return PageResult(url=url, html=self.html)

    def current_url(self):
        if self.urls:
            index = min(self._url_index, len(self.urls) - 1)
            self._url_index += 1
            self._last_url = self.urls[index]
        return self._last_url

    def page_html(self):
        return self.html

    def has_cookie(self, names):
        return bool(self.cookies.intersection(names))

    def save_login_marker(self, data):
        self.marker = data
        return Path("/tmp/login.json")

    def screenshot(self, path):
        self.screenshots.append(str(path))
        return str(path)

    def close(self):
        self.closed = True


LOGIN_CASES = (
    (
        "taobao",
        "https://login.taobao.com/member/login.jhtml",
        "https://i.taobao.com/my_taobao.htm",
        "cookie2",
    ),
    ("jd", "https://passport.jd.com/new/login.aspx", "https://home.jd.com/", "pt_key"),
    (
        "1688",
        "https://login.1688.com/member/signin.htm",
        "https://work.1688.com/",
        "__cn_logon__",
    ),
)


@pytest.mark.parametrize(("platform", "login_url", "check_url", "cookie"), LOGIN_CASES)
def test_login_detects_cookie_passively_without_renavigation(
    make_config, monkeypatch, platform, login_url, check_url, cookie
):
    config = make_config()
    fake = PassiveLoginBrowser(urls=[login_url], cookies={cookie})
    monkeypatch.setattr("crawler.login.validate_display", lambda *a, **k: (True, ""))

    code = login_platform(
        platform,
        config,
        timeout=60,
        display=":6",
        poll_seconds=0.01,
        browser_factory=lambda *a, **k: fake,
    )

    assert code == 0
    # 只打开登录页一次；cookie 命中后不再打开“需登录页”确认。
    assert fake.calls == [login_url]
    assert fake.fetch_kwargs[0]["wait_until"] == LOGIN_WAIT_UNTIL == "domcontentloaded"
    assert fake.fetch_kwargs[0]["timeout_ms"] == LOGIN_NAVIGATION_TIMEOUT_MS == 60_000
    assert fake.marker and fake.marker["platform"] == platform
    assert fake.marker["detected_by"] == "login_cookie"
    assert fake.marker["final_url"] == login_url
    assert fake.marker["cookies_exported"] is False
    assert fake.closed


@pytest.mark.parametrize(("platform", "login_url", "check_url", "cookie"), LOGIN_CASES)
def test_login_detects_url_leave_passively_without_renavigation(
    make_config, monkeypatch, platform, login_url, check_url, cookie
):
    config = make_config()
    fake = PassiveLoginBrowser(urls=[login_url, check_url])
    monkeypatch.setattr("crawler.login.validate_display", lambda *a, **k: (True, ""))

    code = login_platform(
        platform,
        config,
        timeout=60,
        display=":6",
        poll_seconds=0.01,
        browser_factory=lambda *a, **k: fake,
    )

    assert code == 0
    assert fake.calls == [login_url]
    assert fake.marker and fake.marker["detected_by"] == "url_left_login"
    assert fake.marker["final_url"] == check_url
    assert fake.closed


class NavigationTimeoutLoginBrowser(PassiveLoginBrowser):
    """模拟 Page.goto 等到 DOMContentLoaded 仍超时；页面实际上已经打开。"""

    def fetch_page(self, url, **kwargs):
        self.calls.append(url)
        self.fetch_kwargs.append(kwargs)
        self._last_url = url
        raise TimeoutError("Page.goto: Timeout 60000ms exceeded")


@pytest.mark.parametrize(("platform", "login_url", "check_url", "cookie"), LOGIN_CASES)
def test_login_navigation_timeout_keeps_waiting_on_opened_page(
    make_config, monkeypatch, platform, login_url, check_url, cookie
):
    config = make_config()
    fake = NavigationTimeoutLoginBrowser(urls=[login_url], cookies={cookie})
    monkeypatch.setattr("crawler.login.validate_display", lambda *a, **k: (True, ""))

    code = login_platform(
        platform,
        config,
        timeout=60,
        display=":6",
        poll_seconds=0.01,
        browser_factory=lambda *a, **k: fake,
    )

    assert code == 0
    # 导航超时不会让 login 退出，也不会再次导航/刷新。
    assert fake.calls == [login_url]
    assert fake.fetch_kwargs[0]["wait_until"] == "domcontentloaded"
    assert fake.fetch_kwargs[0]["timeout_ms"] == 60_000
    assert fake.marker and fake.marker["detected_by"] == "login_cookie"
    assert fake.closed


def test_login_navigation_timeout_then_wait_expiry_only_navigates_once(
    make_config, monkeypatch
):
    config = make_config()
    login_url = "https://passport.jd.com/new/login.aspx"
    fake = NavigationTimeoutLoginBrowser(urls=[login_url])
    monkeypatch.setattr("crawler.login.validate_display", lambda *a, **k: (True, ""))

    code = login_platform(
        "jd",
        config,
        timeout=0,
        display=":6",
        poll_seconds=0.01,
        browser_factory=lambda *a, **k: fake,
    )

    assert code == 1
    assert fake.calls == [login_url]
    assert fake.screenshots
    assert fake.closed


class LegacyLoginBrowser(PassiveLoginBrowser):
    """旧测试替身没有 wait_until 参数，登录流程仍应只传它支持的参数。"""

    def fetch_page(self, url, *, capture_patterns=(), wait_ms=None, timeout_ms=None):
        self.calls.append(url)
        self.fetch_kwargs.append(
            {
                "capture_patterns": capture_patterns,
                "wait_ms": wait_ms,
                "timeout_ms": timeout_ms,
            }
        )
        self._last_url = url
        return PageResult(url=url, html=self.html)


def test_login_compatible_with_browser_without_wait_until(make_config, monkeypatch):
    config = make_config()
    login_url = "https://passport.jd.com/new/login.aspx"
    fake = LegacyLoginBrowser(urls=[login_url], cookies={"pt_key"})
    monkeypatch.setattr("crawler.login.validate_display", lambda *a, **k: (True, ""))

    code = login_platform(
        "jd",
        config,
        timeout=60,
        display=":6",
        poll_seconds=0.01,
        browser_factory=lambda *a, **k: fake,
    )

    assert code == 0
    assert fake.calls == [login_url]
    assert fake.fetch_kwargs[0]["timeout_ms"] == 60_000
    assert "wait_until" not in fake.fetch_kwargs[0]


def test_login_weak_cookie_alone_does_not_finish_on_login_page(
    make_config, monkeypatch
):
    config = make_config()
    fake = PassiveLoginBrowser(
        urls=["https://login.taobao.com/member/login.jhtml"], cookies={"cookie2"}
    )
    fake.cookie_names = lambda: set(fake.cookies)
    monkeypatch.setattr("crawler.login.validate_display", lambda *a, **k: (True, ""))

    code = login_platform(
        "taobao",
        config,
        timeout=0,
        display=":6",
        poll_seconds=0.01,
        browser_factory=lambda *a, **k: fake,
    )

    assert code == 1
    assert fake.marker is None
    assert fake.calls == ["https://login.taobao.com/member/login.jhtml"]


def test_login_strong_cookie_with_names_finishes(make_config, monkeypatch):
    config = make_config()
    fake = PassiveLoginBrowser(
        urls=["https://login.taobao.com/member/login.jhtml"], cookies={"_nk_"}
    )
    fake.cookie_names = lambda: set(fake.cookies)
    monkeypatch.setattr("crawler.login.validate_display", lambda *a, **k: (True, ""))

    code = login_platform(
        "taobao",
        config,
        timeout=60,
        display=":6",
        poll_seconds=0.01,
        browser_factory=lambda *a, **k: fake,
    )

    assert code == 0
    assert fake.calls == ["https://login.taobao.com/member/login.jhtml"]
    assert fake.marker and fake.marker["detected_by"] == "login_cookie"


def test_login_risk_page_with_cookie_is_not_success(make_config, monkeypatch):
    config = make_config()
    fake = PassiveLoginBrowser(
        urls=["https://home.jd.com/"],
        cookies={"pt_key"},
        html="<html><body>安全验证 请拖动滑块</body></html>",
    )
    monkeypatch.setattr("crawler.login.validate_display", lambda *a, **k: (True, ""))

    code = login_platform(
        "jd",
        config,
        timeout=0,
        display=":6",
        poll_seconds=0.01,
        browser_factory=lambda *a, **k: fake,
    )

    assert code == 1
    assert fake.calls == ["https://passport.jd.com/new/login.aspx"]
    assert fake.marker is None
    assert fake.closed


def test_login_timeout_never_refetches(make_config, monkeypatch):
    config = make_config()
    login_url = "https://login.taobao.com/member/login.jhtml"
    fake = PassiveLoginBrowser(urls=[login_url])
    monkeypatch.setattr("crawler.login.validate_display", lambda *a, **k: (True, ""))
    clock = [0.0]
    sleeps: list[float] = []
    monkeypatch.setattr("crawler.login.time.monotonic", lambda: clock[0])

    def fake_sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr("crawler.login.time.sleep", fake_sleep)

    code = login_platform(
        "taobao",
        config,
        timeout=99999,
        display=":6",
        poll_seconds=1000,
        browser_factory=lambda *a, **k: fake,
    )

    assert code == 1
    # timeout 被压到 600s，一次 1000s 的 sleep 后即超时；未再次 fetch。
    assert sleeps == [1000]
    assert fake.calls == [login_url]
    assert fake.screenshots
    assert fake.closed
