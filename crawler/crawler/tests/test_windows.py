"""Windows/macOS 分支与 CDP 严格登录 Cookie 的离线单测。"""

from __future__ import annotations

import os
from pathlib import Path
import pytest

from crawler.cli import build_parser, main
from crawler.config import load_config
from crawler.core.browser import PageResult
from crawler.core.chrome import ensure_chrome, find_chrome_executable
from crawler.login import _passive_cookie_success, login_platform, validate_display
from crawler.platforms.ali1688 import Ali1688Platform
from crawler.platforms.taobao import TaobaoPlatform

ROOT = Path(__file__).resolve().parents[2]


class FakeProcess:
    def __init__(self):
        self.pid = 2468
        self._exit = None

    def poll(self):
        return self._exit

    def terminate(self):
        self._exit = -15

    def kill(self):
        self._exit = -9

    def wait(self, timeout=None):
        return self._exit


class CookieBrowser:
    """登录/检查所需的只读浏览器替身，Cookie 名和值都可控。"""

    def __init__(
        self,
        *,
        url: str,
        values: dict[str, str] | None = None,
        html: str = "<html><body>账号页正常</body></html>",
        profile_dir: Path | None = None,
    ):
        self.url = url
        self.values = dict(values or {})
        self.html = html
        self.profile_dir = profile_dir or Path("/tmp/win-profile")
        self.calls: list[str] = []
        self.marker: dict | None = None
        self.closed = False
        self.screenshots: list[str] = []

    def start(self):
        return None

    def fetch_page(self, url, **kwargs):
        self.calls.append(url)
        return PageResult(url=url, html=self.html)

    def current_url(self):
        return self.url

    def page_html(self):
        return self.html

    def cookie_values(self):
        return dict(self.values)

    def has_cookie(self, names):
        return bool(set(self.values).intersection(names))

    def save_login_marker(self, data):
        self.marker = dict(data)
        return self.profile_dir / "login.json"

    def screenshot(self, path):
        self.screenshots.append(str(path))
        return str(path)

    def close(self):
        self.closed = True


def _program_files_chrome(monkeypatch, suffix: str):
    monkeypatch.setattr("crawler.core.chrome.sys.platform", "win32")
    monkeypatch.setattr("crawler.core.chrome.shutil.which", lambda name: None)
    normalized_suffix = suffix.lower().replace("\\", "/")
    monkeypatch.setattr(
        "crawler.core.chrome._is_executable_file",
        lambda path: str(path).lower().replace("\\", "/").endswith(normalized_suffix),
    )


def test_find_chrome_windows_program_files(make_config, monkeypatch):
    _program_files_chrome(monkeypatch, r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe")
    found = find_chrome_executable(make_config())
    assert Path(found).name.lower() == "chrome.exe"
    assert "program files (x86)" in found.lower().replace("\\", "/")


def test_find_chrome_windows_localappdata(make_config, monkeypatch, tmp_path):
    monkeypatch.setattr("crawler.core.chrome.sys.platform", "win32")
    monkeypatch.setattr("crawler.core.chrome.shutil.which", lambda name: None)
    # 隔离宿主真实安装的 Chrome/Edge：只保留 %LOCALAPPDATA% 候选。
    # 否则在装有 Chrome 的 Windows 上会先命中 C:\Program Files\...，
    # 让本测试无法验证 %LOCALAPPDATA% 分支。
    monkeypatch.setattr(
        "crawler.core.chrome.WINDOWS_CHROME_CANDIDATES",
        (r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",),
    )
    monkeypatch.setattr("crawler.core.chrome.WINDOWS_EDGE_CANDIDATES", ())
    local = tmp_path / "LocalAppData"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    chrome = local / "Google" / "Chrome" / "Application" / "chrome.exe"
    chrome.parent.mkdir(parents=True)
    chrome.write_bytes(b"MZ")
    assert find_chrome_executable(make_config()) == str(chrome)


def test_find_chrome_windows_edge_fallback(make_config, monkeypatch):
    _program_files_chrome(monkeypatch, "msedge.exe")
    found = find_chrome_executable(make_config())
    assert Path(found).name.lower() == "msedge.exe"
    assert "microsoft/edge/application" in found.lower().replace("\\", "/")


def test_ensure_chrome_windows_detached_no_display(make_config, monkeypatch):
    config = make_config()
    monkeypatch.setattr("crawler.core.chrome.sys.platform", "win32")
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.setattr(
        "crawler.core.chrome.find_chrome_executable",
        lambda cfg: r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    )
    probes = iter([False, True])
    monkeypatch.setattr("crawler.core.chrome.is_cdp_available", lambda endpoint, timeout=0.5: next(probes))
    monkeypatch.setattr("crawler.core.chrome.time.sleep", lambda seconds: None)
    calls = []

    def fake_popen(arguments, **kwargs):
        calls.append((arguments, kwargs))
        return FakeProcess()

    monkeypatch.setattr("crawler.core.chrome.subprocess.Popen", fake_popen)
    ensure_chrome(config, "taobao", display=":6")

    arguments, kwargs = calls[0]
    assert arguments[1] == f"--user-data-dir={config.browser_profile_path('taobao')}"
    assert arguments[2] == "--remote-debugging-port=9222"
    assert kwargs["creationflags"] & 0x00000200  # CREATE_NEW_PROCESS_GROUP
    assert kwargs["creationflags"] & 0x00000008  # DETACHED_PROCESS
    assert "start_new_session" not in kwargs
    assert "DISPLAY" not in kwargs["env"]


def test_ensure_chrome_macos_no_display(make_config, monkeypatch):
    config = make_config()
    monkeypatch.setattr("crawler.core.chrome.sys.platform", "darwin")
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.setattr(
        "crawler.core.chrome.find_chrome_executable",
        lambda cfg: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    )
    probes = iter([False, True])
    monkeypatch.setattr("crawler.core.chrome.is_cdp_available", lambda endpoint, timeout=0.5: next(probes))
    monkeypatch.setattr("crawler.core.chrome.time.sleep", lambda seconds: None)
    calls = []

    monkeypatch.setattr(
        "crawler.core.chrome.subprocess.Popen",
        lambda arguments, **kwargs: calls.append((arguments, kwargs)) or FakeProcess(),
    )
    ensure_chrome(config, "taobao", display=":6")

    _, kwargs = calls[0]
    assert "DISPLAY" not in kwargs["env"]
    assert kwargs["start_new_session"] is True
    assert "creationflags" not in kwargs


@pytest.mark.parametrize("platform_name", ["win32", "darwin"])
def test_validate_display_skipped_on_native_platforms(monkeypatch, platform_name):
    monkeypatch.setattr("crawler.login.sys.platform", platform_name)
    assert validate_display("", base=Path("/nonexistent")) == (True, "")


@pytest.mark.parametrize("platform_name", ["win32", "darwin"])
def test_login_skips_display_checks_on_native_platforms(make_config, monkeypatch, platform_name):
    config = make_config(browser={"mode": "cdp"})
    monkeypatch.setattr("crawler.login.sys.platform", platform_name)
    monkeypatch.delenv("DISPLAY", raising=False)

    def unexpected(*args, **kwargs):  # pragma: no cover - 不应调用
        raise AssertionError("Windows/macOS 不应检查 DISPLAY")

    monkeypatch.setattr("crawler.login.validate_display", unexpected)
    fake = CookieBrowser(
        url="https://login.taobao.com/member/login.jhtml",
        values={"_nk_": "nick"},
    )
    code = login_platform(
        "taobao",
        config,
        timeout=60,
        display=":6",
        poll_seconds=0.01,
        browser_factory=lambda *a, **k: fake,
    )

    assert code == 0
    assert "DISPLAY" not in os.environ
    assert fake.marker and fake.marker["display"] is None
    assert fake.marker["detected_by"] == "login_cookie"
    assert fake.closed


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ({"_nk_": "nick"}, True),
        ({"cookie2": "c2", "unb": "123"}, True),
        ({"cookie2": "c2"}, False),
        ({"unb": "123"}, False),
    ],
)
def test_taobao_cdp_cookie_detection(values, expected):
    browser = CookieBrowser(url="https://login.taobao.com/member/login.jhtml", values=values)
    assert (
        _passive_cookie_success(
            browser,
            "taobao",
            left_login=False,
            is_login_page=True,
            mode="cdp",
        )
        is expected
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [("true", True), ("TRUE", True), ("false", False), ("1", False)],
)
def test_1688_cdp_requires_cn_logon_true(value, expected):
    browser = CookieBrowser(url="https://work.1688.com/", values={"__cn_logon__": value})
    assert (
        _passive_cookie_success(
            browser,
            "1688",
            left_login=False,
            is_login_page=False,
            mode="cdp",
        )
        is expected
    )


def test_1688_cdp_taobao_login_url_is_not_success():
    browser = CookieBrowser(url="https://login.taobao.com/member/login.jhtml", values={"__cn_logon__": "true"})
    assert (
        _passive_cookie_success(
            browser,
            "1688",
            left_login=False,
            is_login_page=True,
            mode="cdp",
            current_url="https://login.taobao.com/member/login.jhtml",
        )
        is False
    )


def test_1688_cdp_name_only_cannot_prove_logon():
    class NamesOnly:
        def cookie_names(self):
            return {"__cn_logon__"}

    assert (
        _passive_cookie_success(
            NamesOnly(),
            "1688",
            left_login=False,
            is_login_page=False,
            mode="cdp",
        )
        is False
    )


@pytest.mark.parametrize(("value", "expected_code"), [("true", 0), ("false", 1)])
def test_1688_login_flow_cdp_requires_true_cookie(make_config, monkeypatch, value, expected_code):
    config = make_config(browser={"mode": "cdp"})
    monkeypatch.setattr("crawler.login.sys.platform", "win32")
    fake = CookieBrowser(
        url="https://work.1688.com/",
        values={"__cn_logon__": value},
    )
    code = login_platform(
        "1688",
        config,
        timeout=0,
        display=":6",
        poll_seconds=0.01,
        browser_factory=lambda *a, **k: fake,
    )
    assert code == expected_code
    assert fake.calls == ["https://login.1688.com/member/signin.htm"]
    assert fake.closed


def test_1688_taobao_login_redirect_is_not_success(make_config, monkeypatch):
    config = make_config(browser={"mode": "cdp"})
    monkeypatch.setattr("crawler.login.sys.platform", "win32")
    fake = CookieBrowser(
        url="https://login.taobao.com/member/login.jhtml",
        values={"__cn_logon__": "true"},
    )
    code = login_platform(
        "1688",
        config,
        timeout=0,
        display=":6",
        poll_seconds=0.01,
        browser_factory=lambda *a, **k: fake,
    )
    assert code == 1
    assert fake.marker is None
    assert fake.calls == ["https://login.1688.com/member/signin.htm"]


@pytest.mark.parametrize(
    ("values", "expected"),
    [({"_nk_": "nick"}, True), ({"cookie2": "c2"}, False)],
)
def test_check_login_taobao_cdp_requires_strong_cookie(make_config, monkeypatch, values, expected):
    config = make_config(browser={"mode": "cdp"})
    browser = CookieBrowser(url="https://i.taobao.com/my_taobao.htm", values=values)
    crawler = TaobaoPlatform(config, browser)
    assert crawler.check_login() is expected
    if not expected:
        assert "Cookie" in (crawler.last_login_reason or "")


@pytest.mark.parametrize(
    ("values", "expected"),
    [({"__cn_logon__": "true"}, True), ({"__cn_logon__": "false"}, False)],
)
def test_check_login_1688_cdp_requires_true_value(make_config, monkeypatch, values, expected):
    config = make_config(browser={"mode": "cdp"})
    browser = CookieBrowser(url="https://work.1688.com/", values=values)
    crawler = Ali1688Platform(config, browser)
    assert crawler.check_login() is expected
    if not expected:
        assert "Cookie" in (crawler.last_login_reason or "")


def test_windows_config_uses_cdp_for_all_platforms_with_independent_profiles():
    config = load_config(ROOT / "config" / "windows.yaml")
    assert config.enabled_platforms() == ["taobao", "jd", "1688"]
    assert [config.browser_mode(name) for name in ("taobao", "jd", "1688")] == ["cdp", "cdp", "cdp"]
    assert [config.chrome_port(name) for name in ("taobao", "jd", "1688")] == [9222, 9223, 9224]
    assert [config.browser_profile_path(name).name for name in ("taobao", "jd", "1688")] == [
        "taobao-chrome",
        "jd-chrome",
        "1688-chrome",
    ]
    assert config.browser.display is None


def test_cli_check_login_accepts_windows_config(monkeypatch):
    seen: list[str] = []

    def fake_check(platform, config, **kwargs):
        seen.append(platform)
        return 0

    monkeypatch.setattr("crawler.cli.check_login_platform", fake_check)
    code = main(["check-login", "all", "--config", str(ROOT / "config" / "windows.yaml")])
    assert code == 0
    assert seen == ["taobao", "jd", "1688"]


@pytest.mark.parametrize(
    "argv",
    [
        ["login", "taobao", "--config", r".\config\windows.yaml"],
        ["check-login", "all", "--config", r".\config\windows.yaml"],
        ["run", "all", "--config", r".\config\windows.yaml"],
    ],
)
def test_login_check_login_run_accept_windows_config(argv):
    parser = build_parser()
    args = parser.parse_args(argv)
    assert args.config == r".\config\windows.yaml"
