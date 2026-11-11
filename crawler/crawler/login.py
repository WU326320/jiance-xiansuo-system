"""有界面人工登录 / 登录态检查。

登录只打开持久化 profile 的登录页，等人工完成；程序不输入账号、不破解验证码。
成功后关闭浏览器，Cookie 只保留在 profile 目录内，不导出。
"""

from __future__ import annotations

import inspect
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from crawler.config import AppConfig
from crawler.core.browser import ScraplingBrowser, is_navigation_timeout
from crawler.core.cookies import (
    ali1688_logged_in,
    read_cookie_names,
    read_cookie_values,
    taobao_logged_in,
)
from crawler.core.risk import classify_page
from crawler.platforms import create_platform

_LOGIN_HOSTS = {
    "taobao": ("login.taobao.com", "login.tmall.com", "passport.taobao.com"),
    "jd": ("passport.jd.com", "login.jd.com"),
    # 1688 登录有时会被导到淘宝统一登录域；这仍是登录页，不能算成功。
    "1688": (
        "login.1688.com",
        "passport.1688.com",
        "login.taobao.com",
        "login.tmall.com",
        "passport.taobao.com",
    ),
}

# 登录页可能一直不触发 load（例如京东黑屏页面），因此只等 DOMContentLoaded；
# 导航超时也放宽到 60s，并且超时后不退出，继续被动等待人工登录。
LOGIN_WAIT_UNTIL = "domcontentloaded"
LOGIN_NAVIGATION_TIMEOUT_MS = 60_000

# 登录成功只在已打开的那个页面上被动观察，绝不为了“确认”再导航一次。
# 上限固定 10 分钟，避免 CLI 传入更大的值时无限等待。
MAX_LOGIN_WAIT_SECONDS = 600.0
_LOGIN_COOKIE_NAMES = {
    "taobao": ("_nk_", "cookie2"),
    "jd": ("pt_key", "thor"),
    "1688": ("__cn_logon__",),
}
# cookie2 在未登录的淘宝页面也可能存在；能拿到具体 Cookie 名时，只有在页面
# 已不像登录页或 URL 已离开登录域名时才把它当作成功信号。
_LOGIN_STRONG_COOKIE_NAMES = {
    "taobao": ("_nk_",),
    # 京东登录后 passport 会写入 pt_key / thor；两者任一出现即可视为已登录。
    "jd": ("pt_key", "thor"),
    "1688": ("__cn_logon__",),
}
_LOGIN_WEAK_COOKIE_NAMES = {"taobao": ("cookie2",)}


def _requires_x11_display() -> bool:
    """Windows/macOS 使用原生窗口，跳过所有 DISPLAY/X server 检查。"""
    return not (sys.platform.startswith("win") or sys.platform == "darwin")


def x11_socket(display: str, base: str | Path | None = None) -> Path:
    base = Path(base or os.environ.get("CRAWLER_X11_DIR", "/tmp/.X11-unix"))
    number = (display or "").lstrip(":").split(".", 1)[0]
    return base / f"X{number}"


def validate_display(display: str, base: str | Path | None = None) -> tuple[bool, str]:
    """确认 DISPLAY 对应的 X server 可用；Windows/macOS 直接视为不需要。"""
    if not _requires_x11_display():
        return True, ""
    if not display:
        return False, "未设置 DISPLAY"
    if not display.startswith(":"):
        return False, f"仅支持本地 DISPLAY=:N，收到 {display!r}"
    socket = x11_socket(display, base)
    if socket.exists():
        return True, ""
    if shutil.which("xdpyinfo"):
        try:
            result = subprocess.run(
                ["xdpyinfo", "-display", display],
                capture_output=True,
                timeout=5,
                check=False,
            )
            if result.returncode == 0:
                return True, ""
        except (OSError, subprocess.SubprocessError):
            pass
    return False, (
        f"DISPLAY={display} 不可用（未找到 {socket}）。请先启动 X server，例如：\n"
        f"  Xvfb {display} -screen 0 1440x900x24 &\n"
        f"或把 DISPLAY 指向可用的桌面会话。"
    )


def _is_login_url(platform: str, url: str) -> bool:
    host = ""
    try:
        from urllib.parse import urlparse

        host = (urlparse(url).netloc or "").lower()
    except Exception:
        return False
    return any(
        host == suffix or host.endswith("." + suffix)
        for suffix in _LOGIN_HOSTS.get(platform, ())
    )


def _accepts_wait_until(fetch_page) -> bool:
    """检查浏览器客户端是否接受 wait_until；兼容旧测试替身/旧封装。"""
    try:
        parameters = inspect.signature(fetch_page).parameters.values()
    except (TypeError, ValueError):
        return True
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        or parameter.name == "wait_until"
        for parameter in parameters
    )


def _open_login_page(browser, url: str, config: AppConfig) -> None:
    """只导航一次到登录页，使用 DOMContentLoaded 并容忍导航超时。"""
    kwargs = {
        "capture_patterns": (),
        "wait_ms": config.crawl.wait_ms(),
        "timeout_ms": LOGIN_NAVIGATION_TIMEOUT_MS,
    }
    if _accepts_wait_until(browser.fetch_page):
        kwargs["wait_until"] = LOGIN_WAIT_UNTIL
    try:
        browser.fetch_page(url, **kwargs)
    except Exception as exc:  # noqa: BLE001
        # 导航只等 DOMContentLoaded 仍可能超时；此时页面往往已经打开。
        # 不退出、不重新导航，继续在已打开的页面上被动等待人工登录。
        if not is_navigation_timeout(exc):
            raise
        print(
            "[login] 登录页导航超时，页面已打开则继续被动等待人工登录（不会重复刷新）。"
        )


def login_platform(
    platform: str,
    config: AppConfig,
    *,
    timeout: float = 600.0,
    display: str = ":6",
    poll_seconds: float = 3.0,
    browser_factory=None,
) -> int:
    requires_display = _requires_x11_display()
    if requires_display:
        ok, message = validate_display(display)
        if not ok:
            print(message)
            return 2
        os.environ["DISPLAY"] = display
    else:
        # Windows/macOS 原生窗口：不检查 X server，也不写入 DISPLAY。
        display = None
    mode = config.browser_mode(platform)
    browser = (
        browser_factory(config, platform, headless=False)
        if browser_factory
        else ScraplingBrowser(config, platform, headless=False, display=display)
    )
    try:
        browser.start()
        profile = getattr(browser, "profile_dir", None) or config.browser_profile_path(
            platform
        )
        display_note = f"DISPLAY={display}" if requires_display else "DISPLAY=skipped"
        print(f"[login] 平台={platform} mode={mode} profile={profile} {display_note}")
        print(
            "[login] 正在打开登录页；请在弹出的浏览器中人工完成登录（含验证码/滑块，如有）。"
        )
        print(
            "[login] 程序不会自动输入账号、不会破解验证码；登录成功后会自动检测并退出。"
        )
        # 只导航这一次。之后只读取当前 URL / HTML / 同一 context 的 Cookie，
        # 不再调用 fetch_page，避免登录页被反复重新加载导致二维码失效。
        _open_login_page(browser, _login_page(platform), config)
        wait_seconds = _effective_timeout(timeout)
        deadline = time.monotonic() + wait_seconds
        last_notice = 0.0
        while True:
            url = browser.current_url() or ""
            html = browser.page_html() or ""
            state = classify_page(url, html, platform=platform)
            now = time.monotonic()

            # 被动判断：URL 已离开登录域名，或同一浏览器 context 已出现登录 Cookie。
            # 全程不导航、不重新 fetch、不做任何自动输入。
            left_login = bool(url) and not _is_login_url(platform, url)
            cookie_ok = _passive_cookie_success(
                browser,
                platform,
                left_login=left_login,
                is_login_page=state.is_login,
                mode=mode,
                current_url=url,
            )
            url_ok = left_login
            # CDP 下淘宝/1688 只看登录 Cookie；1688 站内跳转 login.taobao.com 仍是登录页。
            if mode == "cdp" and platform in {"taobao", "1688"}:
                url_ok = False
            # 1688 被导到淘宝统一登录域时仍是登录页，即使有旧 Cookie 也不算成功。
            if platform == "1688" and _is_login_url("taobao", url):
                url_ok = False
                cookie_ok = False
            if not state.is_risk and (url_ok or cookie_ok):
                detected_by = "url_left_login" if url_ok else "login_cookie"
                marker_path = browser.save_login_marker(
                    {
                        "platform": platform,
                        "login_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                        "display": display if requires_display else None,
                        "profile_dir": str(profile),
                        "login_url": _login_page(platform),
                        "check_url": _check_page(platform),
                        "check_url_visited": False,
                        "final_url": url,
                        "detected_by": detected_by,
                        "cookies_exported": False,
                        "note": "Cookie 仅保存在浏览器 profile 目录内；本文件不含 Cookie。",
                    }
                )
                print(f"[login] 登录成功，profile 已保存：{profile}")
                print(f"[login] 登录标记：{marker_path}（不含 Cookie）")
                return 0

            if now - last_notice >= 20:
                if state.is_risk:
                    print(
                        "[login] 检测到验证码/滑块页：请人工完成验证（程序会继续等待，不自动处理）。"
                    )
                elif _is_login_url(platform, url):
                    print("[login] 等待人工登录中……")
                else:
                    print(f"[login] 当前页面：{url or '(空白)'}")
                last_notice = now

            if now >= deadline:
                screenshot = (
                    profile.parent / f"login_timeout_{platform}_{int(time.time())}.png"
                )
                saved = browser.screenshot(screenshot)
                print(
                    f"[login] 等待超时（{wait_seconds:.0f}s）。"
                    + (f" 截图：{saved}" if saved else "")
                )
                return 1
            time.sleep(max(0.5, poll_seconds))
    except KeyboardInterrupt:
        print("[login] 已取消。")
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"[login] 失败：{type(exc).__name__}: {exc}")
        return 2
    finally:
        browser.close()


def check_login_platform(
    platform: str,
    config: AppConfig,
    *,
    headed: bool = False,
    browser_factory=None,
) -> int:
    browser = (
        browser_factory(config, platform, headless=not headed)
        if browser_factory
        else ScraplingBrowser(config, platform, headless=not headed)
    )
    try:
        browser.start()
        crawler = create_platform(platform, config, browser)
        ok = crawler.check_login()
        if ok:
            print(f"[check-login] {platform}: OK")
            return 0
        reason = crawler.last_login_reason or "登录态无效"
        print(f"[check-login] {platform}: FAILED - {reason}")
        if crawler.last_screenshot_path:
            print(f"[check-login] 截图：{crawler.last_screenshot_path}")
        return 1
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"[check-login] {platform}: ERROR - {type(exc).__name__}: {exc}")
        return 1
    finally:
        browser.close()


def _login_page(platform: str) -> str:
    return {
        "taobao": "https://login.taobao.com/member/login.jhtml",
        "jd": "https://passport.jd.com/new/login.aspx",
        "1688": "https://login.1688.com/member/signin.htm",
    }[platform]


def _check_page(platform: str) -> str:
    return {
        "taobao": "https://i.taobao.com/my_taobao.htm",
        "jd": "https://home.jd.com/",
        "1688": "https://work.1688.com/",
    }[platform]


def _effective_timeout(timeout: float) -> float:
    """登录等待上限固定为 10 分钟；CLI 传入更小值可缩短等待。"""
    try:
        value = float(timeout)
    except (TypeError, ValueError):
        value = MAX_LOGIN_WAIT_SECONDS
    return max(0.0, min(value, MAX_LOGIN_WAIT_SECONDS))


def check_task_login(crawler, *, recheck: bool = True) -> bool:
    """定时任务在同一窗口等待授权至截止；恢复途中不刷新登录/验证页面。"""
    from crawler.core.errors import RetryableError, RetryExhausted
    from crawler.core.progress import DeadlineReached, check_deadline, remaining_seconds

    timed = remaining_seconds() is not None
    try:
        while recheck:
            try:
                if crawler.check_login():
                    return True
                break
            except Exception as exc:
                if not timed or not (
                    isinstance(exc, (RetryableError, RetryExhausted, OSError))
                    or is_navigation_timeout(exc)
                ):
                    raise
                check_deadline()
                crawler.logger.warning(
                    "登录检查网络异常，等待后重试 platform=%s: %s", crawler.name, exc
                )
                time.sleep(min(30.0, remaining_seconds() or 0))
                check_deadline()
    except DeadlineReached:
        crawler.last_login_reason = "登录检查时已到任务结束时间"
        return False
    browser = crawler.browser
    if crawler.last_login_risk or (
        getattr(browser, "headless", True) and getattr(browser, "mode", None) != "cdp"
    ):
        return False

    deadline = time.monotonic() + (
        remaining_seconds() if timed else MAX_LOGIN_WAIT_SECONDS
    )
    initial_cookies = read_cookie_values(browser)
    crawler.logger.warning(
        "等待登录或人工验证 platform=%s，%s；完成后自动继续",
        crawler.name,
        "等待至任务截止时间" if timed else "最多 10 分钟",
    )
    while time.monotonic() < deadline:
        try:
            check_deadline()
        except DeadlineReached:
            crawler.last_login_reason = "等待扫码授权时已到常规采集截止时间"
            return False
        url = browser.current_url() or ""
        html = browser.page_html()
        # 页面导航中可能暂时读不到内容，不能将空白页视为授权完成。
        if html:
            state = classify_page(url, html, platform=crawler.name)
            if state.is_risk:
                if not crawler.last_login_risk:
                    crawler.last_screenshot_path = crawler._capture_screenshot(
                        "login_risk"
                    )
                crawler.last_login_risk = True
                crawler.last_login_reason = state.reason
                return False
            cookies = read_cookie_values(browser)
            left_login = bool(url) and not _is_login_url(crawler.name, url)
            cookie_ok = _passive_cookie_success(
                browser,
                crawler.name,
                left_login=left_login,
                is_login_page=state.is_login,
                mode=crawler.config.browser_mode(crawler.name),
                current_url=url,
            )
            # 登录页残留的旧 Cookie 不应触发重复刷新二维码。
            if cookie_ok and (not state.is_login or cookies != initial_cookies):
                crawler.last_login_reason = None
                crawler.last_login_risk = False
                crawler.logger.info("扫码授权完成，继续采集 platform=%s", crawler.name)
                return True
        time.sleep(min(3.0, max(0.0, deadline - time.monotonic())))
    crawler.last_login_reason = (
        "等待登录或人工验证时已到任务截止时间"
        if timed
        else "等待扫码授权超时（10 分钟），请重新运行并完成登录"
    )
    crawler.logger.warning("%s platform=%s", crawler.last_login_reason, crawler.name)
    return False


def _observed_cookie_names(browser) -> set[str] | None:
    """读取同一 context 的 Cookie 名；浏览器不支持时返回 None 走兼容分支。"""
    return read_cookie_names(browser)


def _cdp_cookie_success(browser, platform: str) -> bool | None:
    """CDP 模式下的强 Cookie 判定；返回 None 表示该平台不适用。"""
    if platform == "taobao":
        values = read_cookie_values(browser)
        if values is not None:
            return taobao_logged_in(values)
        names = _observed_cookie_names(browser)
        if names is not None:
            return taobao_logged_in({name: "" for name in names})
        # 旧客户端只能问单个名字；cookie2 必须与 unb 同时出现。
        has = getattr(browser, "has_cookie", None)
        if not callable(has):
            return False
        return bool(has(("_nk_",))) or (bool(has(("cookie2",))) and bool(has(("unb",))))
    if platform == "1688":
        values = read_cookie_values(browser)
        # 只有名字、读不到值时不能证明 __cn_logon__=true，按未登录处理。
        return ali1688_logged_in(values) if values is not None else False
    return None


def _passive_cookie_success(
    browser,
    platform: str,
    *,
    left_login: bool,
    is_login_page: bool,
    mode: str | None = None,
    current_url: str = "",
) -> bool:
    """只读取 Cookie，不做导航确认；弱 Cookie 不单独作为成功信号。"""
    if (
        mode == "cdp"
        and platform == "1688"
        and current_url
        and _is_login_url("taobao", current_url)
    ):
        # 1688 跳到淘宝统一登录域仍是登录页，即使残留旧 Cookie 也不算成功。
        return False
    if mode == "cdp":
        strict = _cdp_cookie_success(browser, platform)
        if strict is not None:
            return strict

    observed = _observed_cookie_names(browser)
    if observed is None:
        # 测试替身/旧浏览器客户端只提供 has_cookie：按配置名单兼容判断。
        return browser.has_cookie(_login_cookie_names(platform))
    if observed.intersection(_LOGIN_STRONG_COOKIE_NAMES.get(platform, ())):
        return True
    weak = observed.intersection(_LOGIN_WEAK_COOKIE_NAMES.get(platform, ()))
    return bool(weak) and (left_login or not is_login_page)


def _login_cookie_names(platform: str) -> tuple[str, ...]:
    """用于被动判断登录成功的 Cookie 名单，不做导航确认。"""
    return _LOGIN_COOKIE_NAMES.get(platform, ())


def _cookie_names(platform: str) -> tuple[str, ...]:
    """兼容旧调用名；登录被动检测使用 ``_login_cookie_names``。"""
    return _login_cookie_names(platform)
