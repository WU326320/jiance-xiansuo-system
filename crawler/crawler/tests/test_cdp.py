from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from crawler.core.browser import ScraplingBrowser
from crawler.core.chrome import ChromeHandle
from crawler.core.errors import BrowserUnavailable
from crawler.login import _passive_cookie_success, login_platform
from crawler.runner import _default_browser_factory


class FakeLocator:
    def count(self):
        return 0

    def first(self):
        return self

    def wait_for(self, **kwargs):
        return None


class FakePage:
    def __init__(self):
        self.url = "about:blank"
        self.goto_calls = []
        self.handlers = {}
        self.waits = []

    def on(self, event, handler):
        self.handlers[event] = handler

    def goto(self, url, **kwargs):
        self.goto_calls.append((url, kwargs))
        self.url = url
        return SimpleNamespace(status=200)

    def wait_for_timeout(self, milliseconds):
        self.waits.append(milliseconds)

    def locator(self, selector):
        return FakeLocator()

    def content(self):
        return "<html><body>京东登录页 DOM 已加载</body></html>"


class FakeContext:
    def __init__(self, page, cookies):
        self.page = page
        self._cookies = cookies

    def new_page(self):
        return self.page

    def cookies(self):
        return list(self._cookies)


class FakeRemoteSession:
    instances = []
    start_error = None

    def __init__(self, cdp_url, **options):
        self.cdp_url = cdp_url
        self.options = options
        self.page = FakePage()
        self.context = FakeContext(self.page, [{"name": "pt_key"}, {"name": "thor"}])
        self.started = False
        self.closed = False
        FakeRemoteSession.instances.append(self)

    def start(self):
        if FakeRemoteSession.start_error is not None:
            raise FakeRemoteSession.start_error
        self.started = True

    def fetch(self, url, **kwargs):  # pragma: no cover - 登录走 direct navigation
        raise AssertionError("CDP 登录不应走 session.fetch")

    def close(self):
        self.closed = True


class FakeProcess:
    def __init__(self):
        self.pid = 1234
        self.terminated = False
        self._exit = None

    def poll(self):
        return self._exit

    def terminate(self):
        self.terminated = True
        self._exit = -15

    def kill(self):
        self._exit = -9

    def wait(self, timeout=None):
        return self._exit


@pytest.fixture
def cdp_session(monkeypatch):
    import scrapling.fetchers as fetchers

    FakeRemoteSession.instances = []
    FakeRemoteSession.start_error = None
    monkeypatch.setattr(fetchers, "RemoteSession", FakeRemoteSession)
    return FakeRemoteSession


def _handle(config, process=None):
    return ChromeHandle(
        endpoint=config.cdp_endpoint("jd"),
        port=config.chrome_port("jd"),
        profile_dir=config.browser_profile_dir("jd"),
        executable="/usr/bin/google-chrome",
        process=process,
    )


def test_default_factory_uses_cdp_for_jd(make_config):
    config = make_config()
    jd = _default_browser_factory(config, "jd", None)
    taobao = _default_browser_factory(config, "taobao", None)
    assert jd.mode == "cdp"
    assert jd.profile_dir.name == "jd-chrome"
    assert taobao.mode == "playwright"
    assert taobao.profile_dir.name == "taobao"


def test_cdp_start_connects_to_real_chrome_default_context(make_config, monkeypatch, cdp_session):
    config = make_config()
    process = FakeProcess()
    handle = _handle(config, process)
    monkeypatch.setattr("crawler.core.browser.ensure_chrome", lambda *a, **k: handle)

    browser = ScraplingBrowser(config, "jd", headless=True)
    browser.start()

    session = cdp_session.instances[0]
    assert session.started is True
    assert session.cdp_url == "http://127.0.0.1:9223"
    assert session.options["new_context"] is False
    assert session.options["headless"] is False
    assert "user_data_dir" not in session.options
    assert browser.chrome_handle is handle

    browser.close()
    assert session.closed is True
    # 关闭只断开 CDP，不结束用户浏览器（即使 handle 由本次启动）。
    assert process.terminated is False


def test_cdp_start_failure_terminates_only_owned_chrome(make_config, monkeypatch, cdp_session):
    config = make_config()
    process = FakeProcess()
    handle = _handle(config, process)
    monkeypatch.setattr("crawler.core.browser.ensure_chrome", lambda *a, **k: handle)
    cdp_session.start_error = RuntimeError("cdp handshake failed")

    browser = ScraplingBrowser(config, "jd", headless=False)
    with pytest.raises(BrowserUnavailable) as excinfo:
        browser.start()
    assert "connect" in str(excinfo.value).lower() or "连接" in str(excinfo.value)
    assert process.terminated is True


def test_cdp_login_navigates_once_and_detects_cookie_passively(
    make_config, monkeypatch, cdp_session
):
    config = make_config()
    handle = _handle(config)
    monkeypatch.setattr("crawler.core.browser.ensure_chrome", lambda *a, **k: handle)
    monkeypatch.setattr("crawler.login.validate_display", lambda *a, **k: (True, ""))

    code = login_platform("jd", config, timeout=60, display=":6", poll_seconds=0.01)

    assert code == 0
    session = cdp_session.instances[0]
    assert session.cdp_url == "http://127.0.0.1:9223"
    # 只导航一次，且使用 domcontentloaded，避免京东登录页 load 事件不触发。
    assert session.page.goto_calls == [
        ("https://passport.jd.com/new/login.aspx", {"wait_until": "domcontentloaded", "timeout": 60_000})
    ]
    marker = config.browser_profile_path("jd") / "login.json"
    assert marker.exists()
    assert '"detected_by": "login_cookie"' in marker.read_text(encoding="utf-8")
    assert config.browser_profile_path("jd").is_dir()
    assert session.closed is True


def test_login_thor_cookie_is_strong_signal():
    class Browser:
        def cookie_names(self):
            return {"thor"}

    assert _passive_cookie_success(Browser(), "jd", left_login=False, is_login_page=True) is True


def test_cdp_login_url_leave_passport_is_success(make_config, monkeypatch, cdp_session):
    config = make_config()
    handle = _handle(config)
    monkeypatch.setattr("crawler.core.browser.ensure_chrome", lambda *a, **k: handle)
    monkeypatch.setattr("crawler.login.validate_display", lambda *a, **k: (True, ""))

    original_init = FakeRemoteSession.__init__

    def init_without_cookie(self, cdp_url, **options):
        original_init(self, cdp_url, **options)
        self.context._cookies = []

    monkeypatch.setattr(FakeRemoteSession, "__init__", init_without_cookie)
    # 登录后在同一个 page 上被动观察到 URL 离开 passport（不再发生第二次 goto）。
    original_goto = FakePage.goto

    def goto_and_leave(self, url, **kwargs):
        response = original_goto(self, url, **kwargs)
        self.url = "https://www.jd.com/"
        return response

    monkeypatch.setattr(FakePage, "goto", goto_and_leave)

    code = login_platform("jd", config, timeout=60, display=":6", poll_seconds=0.01)

    assert code == 0
    assert len(cdp_session.instances[0].page.goto_calls) == 1
    marker = Path(config.browser_profile_path("jd")) / "login.json"
    assert '"detected_by": "url_left_login"' in marker.read_text(encoding="utf-8")
