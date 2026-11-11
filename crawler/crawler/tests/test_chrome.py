from __future__ import annotations

from pathlib import Path

import pytest

from crawler.core.chrome import (
    CHROME_CANDIDATES,
    ChromeHandle,
    ensure_chrome,
    find_chrome_executable,
    is_cdp_available,
)
from crawler.core.errors import BrowserUnavailable, ConfigError
from crawler.tests.conftest import assert_private_mode


@pytest.fixture
def posix_chrome(monkeypatch):
    """强制走 POSIX 查找/启动分支。

    Windows 上真实安装的 Chrome 会先于 which 命中，导致这两个 Linux 路径
    单测无法验证；这里只替换平台判断，不依赖宿主是否安装 Chrome。
    """
    monkeypatch.setattr("crawler.core.chrome.sys.platform", "linux")


class FakeProcess:
    def __init__(self, *, exit_code=None):
        self.pid = 4321
        self.exit_code = exit_code
        self.terminated = False
        self.killed = False
        self.waited = []

    def poll(self):
        return self.exit_code

    def terminate(self):
        self.terminated = True
        if self.exit_code is None:
            self.exit_code = -15

    def kill(self):
        self.killed = True
        self.exit_code = -9

    def wait(self, timeout=None):
        self.waited.append(timeout)
        return self.exit_code


def test_browser_config_defaults_and_ports(make_config):
    config = make_config()
    assert config.browser.mode == "playwright"
    assert config.browser_mode("jd") == "cdp"
    assert config.browser_mode("taobao") == "playwright"
    assert config.browser_mode("1688") == "playwright"
    assert [config.chrome_port(name) for name in ("taobao", "jd", "1688")] == [
        9222,
        9223,
        9224,
    ]
    assert config.cdp_endpoint("jd") == "http://127.0.0.1:9223"
    assert config.browser_profile_path("jd").name == "jd-chrome"
    assert config.browser_profile_dir("jd").is_dir()
    assert_private_mode(config.browser_profile_dir("jd"), 0o700)


def test_browser_config_global_cdp_and_explicit_jd_playwright(make_config):
    config = make_config(browser={"mode": "cdp"})
    assert [config.browser_mode(name) for name in ("taobao", "jd", "1688")] == [
        "cdp",
        "cdp",
        "cdp",
    ]

    config = make_config(browser={"platform_modes": {"jd": "playwright"}})
    assert config.browser_mode("jd") == "playwright"
    assert config.browser_mode("taobao") == "playwright"


def test_browser_config_custom_ports(make_config):
    config = make_config(browser={"port": 9300, "ports": {"jd": 9399}})
    assert config.chrome_port("taobao") == 9300
    assert config.chrome_port("jd") == 9399
    assert config.chrome_port("1688") == 9302


def test_browser_config_rejects_unknown_mode(make_config):
    with pytest.raises(ConfigError):
        make_config(browser={"mode": "firefox"})


def test_find_chrome_executable_prefers_which_order(
    make_config, monkeypatch, posix_chrome
):
    calls = []

    def fake_which(name):
        calls.append(name)
        return "/opt/google/chrome" if name == "google-chrome-stable" else None

    monkeypatch.setattr("crawler.core.chrome.shutil.which", fake_which)
    assert find_chrome_executable(make_config()) == "/opt/google/chrome"
    assert calls == list(CHROME_CANDIDATES[:2])


def test_find_chrome_executable_uses_configured_path(
    make_config, monkeypatch, tmp_path
):
    chrome = tmp_path / "chrome"
    chrome.write_text("#!/bin/sh\n", encoding="utf-8")
    chrome.chmod(0o755)
    config = make_config(browser={"chrome_executable": str(chrome)})
    monkeypatch.setattr("crawler.core.chrome.shutil.which", lambda name: None)
    assert find_chrome_executable(config) == str(chrome)


def test_find_chrome_executable_missing(make_config, monkeypatch, posix_chrome):
    monkeypatch.setattr("crawler.core.chrome.shutil.which", lambda name: None)
    with pytest.raises(BrowserUnavailable) as excinfo:
        find_chrome_executable(make_config())
    assert "google-chrome" in str(excinfo.value)


def test_is_cdp_available_handles_connection_error(monkeypatch):
    monkeypatch.setattr(
        "crawler.core.chrome.urllib.request.urlopen",
        lambda *a, **k: (_ for _ in ()).throw(OSError("refused")),
    )
    assert is_cdp_available("http://127.0.0.1:9223") is False


def test_ensure_chrome_reuses_already_running(make_config, monkeypatch):
    config = make_config()
    monkeypatch.setattr(
        "crawler.core.chrome.is_cdp_available", lambda endpoint, timeout=0.5: True
    )

    def unexpected(*a, **k):  # pragma: no cover - 不应调用
        raise AssertionError("已有 Chrome 时不应启动新进程")

    monkeypatch.setattr("crawler.core.chrome.subprocess.Popen", unexpected)
    handle = ensure_chrome(config, "jd", display=":6")
    assert handle.process is None
    assert handle.launched is False
    assert handle.endpoint == "http://127.0.0.1:9223"
    assert handle.port == 9223


def test_ensure_chrome_launches_only_fixed_arguments(
    make_config, monkeypatch, posix_chrome
):
    config = make_config()
    launched = FakeProcess()
    calls = []

    monkeypatch.setattr(
        "crawler.core.chrome.shutil.which",
        lambda candidate: (
            "/usr/bin/google-chrome" if candidate == "google-chrome" else None
        ),
    )
    probes = iter([False, False, True])
    monkeypatch.setattr(
        "crawler.core.chrome.is_cdp_available",
        lambda endpoint, timeout=0.5: next(probes),
    )
    monkeypatch.setattr("crawler.core.chrome.time.sleep", lambda seconds: None)

    def fake_popen(arguments, **kwargs):
        calls.append((arguments, kwargs))
        return launched

    monkeypatch.setattr("crawler.core.chrome.subprocess.Popen", fake_popen)
    handle = ensure_chrome(config, "jd", display=":6")

    assert handle.launched is True and handle.process is launched
    arguments, kwargs = calls[0]
    profile = config.browser_profile_path("jd")
    assert arguments == [
        "/usr/bin/google-chrome",
        f"--user-data-dir={profile}",
        "--remote-debugging-port=9223",
    ]
    assert not any(
        arg.startswith("--headless") or "enable-automation" in arg for arg in arguments
    )
    assert kwargs["env"]["DISPLAY"] == ":6"
    assert kwargs["start_new_session"] is True
    assert profile.is_dir()
    assert_private_mode(profile, 0o700)


def test_ensure_chrome_timeout_terminates_owned_process(make_config, monkeypatch):
    config = make_config(
        browser={"startup_timeout_seconds": 1, "poll_interval_seconds": 1}
    )
    launched = FakeProcess()
    clock = [0.0]
    monkeypatch.setattr(
        "crawler.core.chrome.shutil.which", lambda name: "/usr/bin/google-chrome"
    )
    monkeypatch.setattr(
        "crawler.core.chrome.is_cdp_available", lambda endpoint, timeout=0.5: False
    )
    monkeypatch.setattr(
        "crawler.core.chrome.subprocess.Popen", lambda *a, **k: launched
    )
    monkeypatch.setattr("crawler.core.chrome.time.monotonic", lambda: clock[0])
    monkeypatch.setattr(
        "crawler.core.chrome.time.sleep",
        lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )

    with pytest.raises(BrowserUnavailable) as excinfo:
        ensure_chrome(config, "jd", display=":6")
    assert "超时" in str(excinfo.value)
    assert launched.terminated is True


def test_ensure_chrome_immediate_exit_reports_error(make_config, monkeypatch):
    config = make_config()
    launched = FakeProcess(exit_code=1)
    monkeypatch.setattr(
        "crawler.core.chrome.shutil.which", lambda name: "/usr/bin/google-chrome"
    )
    monkeypatch.setattr(
        "crawler.core.chrome.is_cdp_available", lambda endpoint, timeout=0.5: False
    )
    monkeypatch.setattr(
        "crawler.core.chrome.subprocess.Popen", lambda *a, **k: launched
    )
    with pytest.raises(BrowserUnavailable) as excinfo:
        ensure_chrome(config, "jd", display=":6")
    assert "立即退出" in str(excinfo.value)


def test_ensure_chrome_zero_launcher_exit_waits_for_ready_cdp(make_config, monkeypatch):
    config = make_config(
        browser={"startup_timeout_seconds": 3, "poll_interval_seconds": 0.5}
    )
    launched = FakeProcess(exit_code=0)
    clock = [0.0]
    probes = []

    def probe(endpoint, timeout=0.5):
        probes.append(clock[0])
        return clock[0] >= 1.0

    monkeypatch.setattr(
        "crawler.core.chrome.find_chrome_executable", lambda config: "msedge.exe"
    )
    monkeypatch.setattr("crawler.core.chrome.is_cdp_available", probe)
    monkeypatch.setattr(
        "crawler.core.chrome.subprocess.Popen", lambda *a, **k: launched
    )
    monkeypatch.setattr("crawler.core.chrome.time.monotonic", lambda: clock[0])
    monkeypatch.setattr(
        "crawler.core.chrome.time.sleep",
        lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )

    handle = ensure_chrome(config, "jd")
    assert handle.endpoint == config.cdp_endpoint("jd")
    assert handle.process is launched
    assert probes[-1] == 1.0
    assert not launched.terminated and not launched.killed


def test_ensure_chrome_zero_launcher_exit_without_cdp_times_out(
    make_config, monkeypatch
):
    config = make_config(
        browser={"startup_timeout_seconds": 2, "poll_interval_seconds": 0.5}
    )
    launched = FakeProcess(exit_code=0)
    clock = [0.0]
    monkeypatch.setattr(
        "crawler.core.chrome.find_chrome_executable", lambda config: "msedge.exe"
    )
    monkeypatch.setattr(
        "crawler.core.chrome.is_cdp_available", lambda endpoint, timeout=0.5: False
    )
    monkeypatch.setattr(
        "crawler.core.chrome.subprocess.Popen", lambda *a, **k: launched
    )
    monkeypatch.setattr("crawler.core.chrome.time.monotonic", lambda: clock[0])
    monkeypatch.setattr(
        "crawler.core.chrome.time.sleep",
        lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )

    with pytest.raises(BrowserUnavailable, match="等待 Chrome CDP 端口超时"):
        ensure_chrome(config, "jd")
    assert clock[0] == 2.0
    assert not launched.terminated and not launched.killed


def test_terminate_handle_only_touches_owned_process():
    from crawler.core.chrome import terminate_handle

    process = FakeProcess()
    handle = ChromeHandle(
        endpoint="http://127.0.0.1:9223",
        port=9223,
        profile_dir=Path("/tmp/x"),
        process=process,
    )
    terminate_handle(handle)
    assert process.terminated is True
    terminate_handle(None)  # 不应抛异常
