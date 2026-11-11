"""真实 Google Chrome 的启动 / CDP 端口探测。

CDP 模式不启动 Playwright 自带浏览器，而是复用系统安装的 Chrome：

    google-chrome --user-data-dir=<profile>/<platform>-chrome \\
                  --remote-debugging-port=<9222 + 平台序号>

只传这两个参数，不加 ``--enable-automation`` / ``--headless`` 等自动化开关。
Chrome 未运行时自动启动；已经在本机 CDP 端口监听时直接复用，绝不结束用户
已有的 Chrome 进程。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from crawler.config import AppConfig
from crawler.core.errors import BrowserUnavailable

CHROME_CANDIDATES = ("google-chrome", "google-chrome-stable", "chromium")
WINDOWS_CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
)
WINDOWS_EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe",
)
MACOS_CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)
CDP_PROBE_TIMEOUT_SECONDS = 0.5
_PERCENT_VAR = re.compile(r"%([^%]+)%")


@dataclass(frozen=True)
class ChromeHandle:
    """一次 CDP 连接对应的本机 Chrome 信息。

    ``process`` 为 ``None`` 表示端口上已经有 Chrome，本次没有启动新进程；
    ``close()`` 只断开 CDP，不结束任何 Chrome（尤其是用户原有的窗口）。
    """

    endpoint: str
    port: int
    profile_dir: Path
    executable: str | None = None
    process: Any | None = None

    @property
    def launched(self) -> bool:
        return self.process is not None


def is_cdp_available(endpoint: str, timeout: float = CDP_PROBE_TIMEOUT_SECONDS) -> bool:
    """只读探测 ``/json/version``；连接失败/非 2xx 都视为未启动。"""
    url = endpoint.rstrip("/") + "/json/version"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - 仅本机回环
            status = int(getattr(response, "status", 200) or 200)
            return 200 <= status < 300
    except Exception:
        return False


def is_windows_platform() -> bool:
    return sys.platform.startswith("win")


def is_macos_platform() -> bool:
    return sys.platform == "darwin"


def needs_x11_display() -> bool:
    """只有 Linux/其他 POSIX 需要 DISPLAY；Windows/macOS 使用原生窗口。"""
    return not (is_windows_platform() or is_macos_platform())


def _expand_candidate(value: str) -> Path:
    # 同时支持 Windows 的 %VAR% 和 POSIX 的 $VAR（单测会在 Linux 上模拟 win32）。
    expanded = _PERCENT_VAR.sub(
        lambda match: os.environ.get(match.group(1), match.group(0)), value
    )
    expanded = os.path.expandvars(os.path.expanduser(expanded))
    if is_windows_platform() and os.sep != "\\":
        # 在 Linux/macOS 单测里模拟 win32 时，按本机分隔符解析 Windows 候选路径。
        expanded = expanded.replace("\\", os.sep)
    return Path(expanded)


def _is_executable_file(path: Path) -> bool:
    try:
        if not path.is_file():
            return False
        # Windows 以 .exe 后缀和文件存在性判定，没有 POSIX 的 X_OK 语义。
        return True if is_windows_platform() else os.access(path, os.X_OK)
    except OSError:
        return False


def _first_existing(candidates) -> str | None:
    for candidate in candidates:
        path = _expand_candidate(str(candidate))
        if _is_executable_file(path):
            return str(path)
    return None


def _windows_candidates() -> tuple[str, ...]:
    return (*WINDOWS_CHROME_CANDIDATES, *WINDOWS_EDGE_CANDIDATES)


def find_chrome_executable(config: AppConfig) -> str:
    """按配置、平台固定安装路径或 ``which`` 查找真实 Chrome（Windows 含 Edge 兜底）。"""
    configured = (config.browser.chrome_executable or "").strip()
    if configured:
        path = _expand_candidate(configured)
        if _is_executable_file(path):
            return str(path)
        raise BrowserUnavailable(f"browser.chrome_executable 不存在或不可执行: {path}")

    if is_windows_platform():
        found = _first_existing(_windows_candidates())
        if found:
            return found
        # 非默认安装目录时再查 PATH；Edge 只作为 Chrome 缺失时的兜底。
        for name in ("chrome", "chrome.exe", "msedge", "msedge.exe"):
            located = shutil.which(name)
            if located:
                return str(located)
        raise BrowserUnavailable(
            "未找到系统 Chrome/Edge：已尝试 "
            + " / ".join(_windows_candidates())
            + " 及 PATH。请安装 Google Chrome，或配置 browser.chrome_executable"
        )

    if is_macos_platform():
        found = _first_existing(MACOS_CHROME_CANDIDATES)
        if found:
            return found

    for name in CHROME_CANDIDATES:
        found = shutil.which(name)
        if found:
            return str(found)

    if is_macos_platform():
        tried = " / ".join(MACOS_CHROME_CANDIDATES + CHROME_CANDIDATES)
    else:
        tried = " / ".join(CHROME_CANDIDATES)
    raise BrowserUnavailable(
        "未找到系统 Chrome：已尝试 "
        + tried
        + "。请安装 Google Chrome，或配置 browser.chrome_executable"
    )


def _windows_creationflags() -> int:
    """让 Chrome 脱离当前控制台独立运行；常量在非 Windows 上可能不存在。"""
    return int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)) | int(
        getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
    )


def _terminate_owned(process: Any) -> None:
    """只结束本次 ``Popen`` 启动的进程；不会按名字杀 Chrome。"""
    if process is None:
        return
    try:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except Exception:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)
    except Exception:
        return


def ensure_chrome(
    config: AppConfig,
    platform: str,
    *,
    display: str | None = None,
    logger=None,
) -> ChromeHandle:
    """确保该平台 CDP 端口有真实 Chrome；未运行则按固定参数启动。"""
    endpoint = config.cdp_endpoint(platform)
    port = config.chrome_port(platform)
    profile = config.browser_profile_path(platform)

    if is_cdp_available(endpoint):
        if logger:
            logger.info(
                "cdp chrome already running platform=%s endpoint=%s profile=%s",
                platform,
                endpoint,
                profile,
            )
        return ChromeHandle(endpoint=endpoint, port=port, profile_dir=profile)

    executable = find_chrome_executable(config)
    try:
        profile = config.browser_profile_dir(platform)
    except Exception as exc:  # pragma: no cover - 权限/路径异常
        raise BrowserUnavailable(f"无法准备 Chrome profile {profile}: {exc}") from exc

    environment = os.environ.copy()
    chosen_display = display or config.browser.display
    display_required = needs_x11_display()
    if display_required and chosen_display:
        environment["DISPLAY"] = chosen_display
    elif not display_required:
        # Windows/macOS 原生窗口：不向子进程传递 DISPLAY。
        environment.pop("DISPLAY", None)

    # 严格只有这两个启动参数；不要加 --enable-automation / --headless。
    arguments = [
        executable,
        f"--user-data-dir={profile}",
        f"--remote-debugging-port={port}",
    ]
    popen_kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "env": environment,
    }
    if is_windows_platform():
        # Windows 没有 start_new_session；用独立进程组 + 脱离控制台。
        popen_kwargs["creationflags"] = _windows_creationflags()
    else:
        popen_kwargs["start_new_session"] = True
    try:
        process = subprocess.Popen(arguments, **popen_kwargs)
    except OSError as exc:
        raise BrowserUnavailable(f"启动 Chrome 失败（{executable}）: {exc}") from exc

    if logger:
        logger.info(
            "cdp chrome launching platform=%s executable=%s port=%s profile=%s display=%s",
            platform,
            executable,
            port,
            profile,
            chosen_display if display_required else None,
        )

    deadline = time.monotonic() + float(config.browser.startup_timeout_seconds)
    while time.monotonic() < deadline:
        if is_cdp_available(endpoint):
            if logger:
                logger.info(
                    "cdp chrome ready platform=%s endpoint=%s pid=%s",
                    platform,
                    endpoint,
                    getattr(process, "pid", None),
                )
            return ChromeHandle(
                endpoint=endpoint,
                port=port,
                profile_dir=profile,
                executable=executable,
                process=process,
            )
        exit_code = process.poll()
        # Edge 启动器可能以0退出后交由另一个进程打开浏览器。
        # 正常退出不等于CDP已经就绪，继续在原超时内探测；非零退出才立即失败。
        if exit_code not in (None, 0):
            raise BrowserUnavailable(
                f"Chrome 启动后立即退出（exit={exit_code}，profile={profile}，port={port}）。"
                + (
                    "请检查 DISPLAY 是否可用、端口是否被占用，或用 browser.chrome_executable 指定路径。"
                    if display_required
                    else "请检查 Chrome 路径、端口是否被占用，或用 browser.chrome_executable 指定路径。"
                )
            )
        time.sleep(float(config.browser.poll_interval_seconds))

    _terminate_owned(process)
    raise BrowserUnavailable(
        f"等待 Chrome CDP 端口超时（{config.browser.startup_timeout_seconds:.0f}s，endpoint={endpoint}）"
    )


def terminate_handle(handle: ChromeHandle | None, *, logger=None) -> None:
    """结束由 ``ensure_chrome`` 启动的进程（仅用于显式清理/冒烟）。"""
    if handle is None or handle.process is None:
        return
    if logger:
        logger.info(
            "cdp chrome terminating owned pid=%s", getattr(handle.process, "pid", None)
        )
    _terminate_owned(handle.process)


__all__ = [
    "CHROME_CANDIDATES",
    "MACOS_CHROME_CANDIDATES",
    "WINDOWS_CHROME_CANDIDATES",
    "WINDOWS_EDGE_CANDIDATES",
    "ChromeHandle",
    "ensure_chrome",
    "find_chrome_executable",
    "is_cdp_available",
    "is_macos_platform",
    "is_windows_platform",
    "needs_x11_display",
    "terminate_handle",
]
