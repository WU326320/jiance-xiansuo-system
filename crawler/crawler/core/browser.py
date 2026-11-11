"""浏览器会话封装。

- ``browser.mode=playwright``（默认）：Scrapling ``StealthySession`` + 每平台
  独立 ``user_data_dir`` 持久化 profile。
- ``browser.mode=cdp``（京东默认）：连接系统真实 Chrome 的 CDP 端口
  （Scrapling ``RemoteSession`` -> Playwright ``connect_over_cdp``），共享人工
  登录后的默认 context；Chrome 未运行时自动启动。
- 测试：``StaticBrowser`` 回放 fixture，不访问真实电商站点。
- 统一捕获页面最终 URL / HTML / JSON 响应 / DOM 风险选择器命中。
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

from crawler.config import AppConfig
from crawler.core.chrome import ChromeHandle, ensure_chrome, terminate_handle
from crawler.core.errors import BrowserUnavailable, PaginationError, RetryableError
from crawler.core.risk import DOM_LOGIN_SELECTORS, DOM_RISK_SELECTORS

DOM_HINT_SELECTORS = tuple(dict.fromkeys([*DOM_RISK_SELECTORS, *DOM_LOGIN_SELECTORS]))

DEFAULT_BROWSERS_PATH = "/workspace/tools/ms-playwright"


@dataclass
class CapturedResponse:
    url: str
    text: str
    content_type: str = ""
    status: int = 0
    requested_at: float | None = None


@dataclass
class PaginationState:
    has_next: bool | None = None
    current_page: int | None = None
    total_pages: int | None = None
    selector: str | None = None
    index: int = 0
    kind: str = "next"
    reason: str = "未找到可确认的分页控件"
    empty_confirmed: bool = False


@dataclass
class PageResult:
    url: str
    html: str
    status: int = 200
    captured: list[CapturedResponse] = field(default_factory=list)
    dom_hints: list[str] = field(default_factory=list)
    search_refreshed: bool = False
    pagination: PaginationState | None = None


# 商品标识集合，不使用价格、销量、排序或 URL 的页码充当翻页成功证据。
SEARCH_CARD_SIGNATURE = r"""nodes => {
    const identity = node => {
        for (const attr of ['data-nid', 'data-item-id', 'data-sku', 'data-offer-id',
                            'data-offerid', 'data-spm-act-id']) {
            const id = node.getAttribute(attr);
            if (id && /^\d+$/.test(id)) return id;
        }
        const elementId = (node.id || '').match(/^item_id_(\d+)$/);
        if (elementId) return elementId[1];
        const links = [node, ...node.querySelectorAll('a[href]')];
        for (const link of links) {
            const href = link.getAttribute('href');
            if (!href) continue;
            try {
                const url = new URL(href, document.baseURI);
                let id;
                if (/(?:^|\.)(?:taobao|tmall)\.com$/.test(url.hostname)) {
                    id = url.searchParams.get('id');
                } else if (/(?:^|\.)1688\.com$/.test(url.hostname)) {
                    id = url.pathname.match(/\/offer\/(\d+)\.html/)?.[1] ||
                        url.searchParams.get('offerId') || url.searchParams.get('offerIds');
                } else if (url.hostname === 'item.jd.com') {
                    id = url.pathname.match(/\/(\d+)\.html/)?.[1];
                }
                if (id && /^\d+$/.test(id)) return id;
            } catch (_) {}
        }
        return '';
    };
    return [...new Set(nodes.map(identity).filter(Boolean))].sort().join('\n');
}"""


def search_pagination(page, selectors) -> PaginationState:
    """只使用当前可见 DOM 提供的下一页、加载更多和终态证据。"""
    state = PaginationState()
    current = page.locator(selectors["current"])
    for index in range(current.count()):
        node = current.nth(index)
        text = node.inner_text().strip() if node.is_visible() else ""
        if text.isdigit():
            state.current_page = int(text)
            break
    roots = page.locator(selectors["root"])
    for index in range(roots.count()):
        node = roots.nth(index)
        if not node.is_visible():
            continue
        text = node.inner_text()
        total = re.search(r"共\s*(\d+)\s*页", text)
        fraction = re.search(r"(\d+)\s*/\s*(\d+)", text)
        if total:
            state.total_pages = int(total.group(1))
        elif fraction:
            state.current_page = state.current_page or int(fraction.group(1))
            state.total_pages = int(fraction.group(2))
    disabled_found = False
    for kind in ("next", "more"):
        nodes = page.locator(selectors[kind])
        for index in range(nodes.count()):
            node = nodes.nth(index)
            if not node.is_visible():
                continue
            disabled = node.evaluate("""node => {
                for (let depth=0; node && depth<3; depth++,node=node.parentElement) {
                    if (node.hasAttribute('disabled') || node.getAttribute('aria-disabled') === 'true' ||
                        /(?:^|[\\s_-])(?:arrow)?disabled(?:$|[\\s_-])/i.test(node.className || '')) return true;
                }
                return false;
            }""")
            if disabled:
                disabled_found = True
                continue
            state.has_next = True
            state.selector, state.index, state.kind = selectors[kind], index, kind
            state.reason = "页面提供加载更多" if kind == "more" else "页面提供下一页"
            return state
    if (
        state.current_page
        and state.total_pages
        and state.current_page >= state.total_pages
    ):
        state.has_next, state.reason = False, "当前页达到页面标示的末页"
        return state
    if disabled_found and not (state.total_pages and state.current_page):
        state.has_next, state.reason = False, "下一页控件已禁用"
        return state
    for key, expression in (
        ("end", r"没有更多|已到底|到底了|全部加载|no more"),
        ("empty", r"暂无|没有找到|未找到|无相关|没有相关|no results"),
    ):
        nodes = page.locator(selectors[key])
        for index in range(nodes.count()):
            node = nodes.nth(index)
            if node.is_visible() and re.search(expression, node.inner_text(), re.I):
                state.has_next, state.reason = (
                    False,
                    "页面明确显示无结果" if key == "empty" else "页面明确显示没有更多",
                )
                state.empty_confirmed = key == "empty"
                return state
    return state


def is_navigation_timeout(exc: BaseException) -> bool:
    """判断异常是否来自导航超时（不同 Playwright 版本的类名不一致）。"""
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    return (
        "timeout" in name or "timeout" in text or "timed out" in text or "超时" in text
    )


class BrowserClient(Protocol):
    def fetch_page(
        self,
        url: str,
        *,
        action: Callable[[Any], None] | None = None,
        capture_patterns: Iterable[str] = (),
        extra_headers: dict[str, str] | None = None,
        wait_selector: str | None = None,
        wait_ms: int | None = None,
        timeout_ms: int | None = None,
        wait_until: str | None = None,
    ) -> PageResult: ...

    def screenshot(self, path: str | Path) -> str | None: ...

    def current_url(self) -> str | None: ...

    def page_html(self) -> str | None: ...

    def close(self) -> None: ...


class ResponseRecorder:
    """收集导航期间匹配的响应对象，导航完成后统一读取 body。"""

    def __init__(
        self,
        patterns: Iterable[str] = (),
        *,
        max_responses: int = 40,
        max_body_bytes: int = 8 * 1024 * 1024,
    ):
        self.patterns = tuple(p for p in patterns if p)
        self.max_responses = max_responses
        self.max_body_bytes = max_body_bytes
        self._responses: list[tuple[Any, float | None]] = []
        self._seen_urls: set[tuple] = set()
        self._requests: dict[Any, float] = {}
        self._finished: set[Any] = set()
        self._pending_requests: set[Any] = set()
        self._capture_after: float | None = None
        self.captured: list[CapturedResponse] = []

    def attach(self, page) -> None:
        page.on("request", self._request_started)
        page.on("requestfailed", self._request_failed)
        page.on("requestfinished", self._request_finished)
        page.on("response", self._handle)

    def _request_started(self, request) -> None:
        self._requests[request] = time.monotonic()

    def _request_failed(self, request) -> None:
        self._requests.pop(request, None)
        self._finished.discard(request)
        self._pending_requests.discard(request)
        self._responses = [
            entry
            for entry in self._responses
            if getattr(entry[0], "request", None) != request
        ]

    def _request_finished(self, request) -> None:
        if request in self._pending_requests:
            self._finished.add(request)
        self._requests.pop(request, None)

    def discard_before(self, boundary: float) -> None:
        """切换排序后仅保留新请求；排序前发起、切换后才返回的请求也排除。"""
        self._capture_after = boundary
        self._responses = [
            (response, started)
            for response, started in self._responses
            if started is not None and started >= boundary
        ]
        self.captured = [
            item
            for item in self.captured
            if item.requested_at is not None and item.requested_at >= boundary
        ]
        self._seen_urls.clear()
        self._finished.intersection_update(
            getattr(response, "request", None) for response, _ in self._responses
        )
        self._pending_requests.intersection_update(
            getattr(response, "request", None) for response, _ in self._responses
        )

    def _handle(self, response) -> None:
        try:
            request = getattr(response, "request", None)
            started = self._requests.pop(request, None)
            if self._capture_after is not None and (
                started is None or started < self._capture_after
            ):
                return
            url = response.url or ""
            headers = response.headers or {}
            content_type = (headers.get("content-type") or "").lower()
            matched = bool(self.patterns) and any(
                pattern in url for pattern in self.patterns
            )
            is_json = "json" in content_type or url.endswith(".json")
            if not matched and not is_json:
                return
            # 同 URL 的 POST 可以对应不同的排序/翻页，不能只按 URL 去重。
            key = (url, content_type, request)
            if key in self._seen_urls or len(self._responses) >= self.max_responses:
                return
            self._seen_urls.add(key)
            self._responses.append((response, started))
            if request is not None:
                self._pending_requests.add(request)
        except Exception:
            return

    def collect(self) -> list[CapturedResponse]:
        pending, self._responses = self._responses, []
        for response, started in pending:
            request = getattr(response, "request", None)
            if request is not None and request not in self._pending_requests:
                continue
            if request is not None and request not in self._finished:
                # response 只保证响应头到达，text() 会无限等待尚未结束的正文。
                self._responses.append((response, started))
                continue
            self._finished.discard(request)
            self._pending_requests.discard(request)
            try:
                headers = response.headers or {}
                content_type = (headers.get("content-type") or "").lower()
                body = response.text()
                if isinstance(body, bytes):
                    body = body.decode("utf-8", errors="replace")
                if (
                    not body
                    or len(body.encode("utf-8", errors="ignore")) > self.max_body_bytes
                ):
                    continue
                self.captured.append(
                    CapturedResponse(
                        url=response.url,
                        text=body,
                        content_type=content_type,
                        status=int(response.status or 0),
                        requested_at=started,
                    )
                )
            except Exception:
                continue
        return self.captured


def _locator_is_visible(page, selector: str) -> bool:
    """只把可见节点当作 DOM 线索，隐藏的预加载验证层不算风控。"""
    try:
        locator = page.locator(selector)
        if locator.count() <= 0:
            return False
        first = getattr(locator, "first", None)
        if first is None:
            # 兼容只实现 count() 的测试替身。
            return True
        checker = getattr(first, "is_visible", None)
        if not callable(checker):
            return True
        try:
            return bool(checker())
        except Exception:
            return False
    except Exception:
        return False


def _collect_dom_hints(page) -> list[str]:
    hints: list[str] = []
    for selector in DOM_HINT_SELECTORS:
        try:
            if _locator_is_visible(page, selector):
                hints.append(selector)
        except Exception:
            continue
    return hints


class ScraplingBrowser:
    """Scrapling 会话薄封装。

    ``mode="cdp"`` 时使用 ``RemoteSession`` 连接真实 Chrome（共享默认 context）；
    其他情况沿用 ``StealthySession`` 的持久化 profile。两种模式对上层暴露相同接口。
    """

    def __init__(
        self,
        config: AppConfig,
        platform: str,
        *,
        headless: bool | None = None,
        logger=None,
        profile_dir: str | Path | None = None,
        mode: str | None = None,
        display: str | None = None,
    ):
        self.config = config
        self.platform = platform
        self.headless = config.crawl.headless if headless is None else bool(headless)
        self.logger = logger
        self.mode = mode or config.browser_mode(platform)
        self.display = display or config.browser.display
        if profile_dir is not None:
            self.profile_dir = Path(profile_dir)
        elif self.mode == "cdp":
            self.profile_dir = config.browser_profile_dir(platform)
        else:
            self.profile_dir = config.profile_dir(platform)
        self._session = None
        self._last_page = None
        self._chrome: ChromeHandle | None = None
        self._active_recorder: ResponseRecorder | None = None
        self._search_page = None
        self._search_recorder: ResponseRecorder | None = None
        self._search_number: int | None = None
        self._search_pending: tuple[str, int, str] | None = None

    def _wait_search_content(self, page, *, cards, selectors, before, number, timeout):
        """等待商品集合更新并稳定；页码变化而商品未变不算成功。"""
        expires = time.monotonic() + timeout / 1000
        last, stable_since = None, time.monotonic()
        while time.monotonic() < expires:
            if _collect_dom_hints(page):
                return  # 交给平台统一识别登录/验证，不能误记成正常末页。
            signature = page.locator(cards).evaluate_all(SEARCH_CARD_SIGNATURE)
            state = search_pagination(page, selectors)
            changed = bool(signature) and (before is None or signature != before)
            correct_page = state.current_page is None or state.current_page == number
            if signature != last:
                last, stable_since = signature, time.monotonic()
            if (
                (changed and correct_page) or (not signature and state.empty_confirmed)
            ) and time.monotonic() - stable_since >= 0.7:
                return
            page.wait_for_timeout(100)
        raise PaginationError(
            f"第 {number} 页加载未确认：商品列表未更新、未稳定或页码不符；该页及后续页未完成"
        )

    def fetch_search_page(
        self,
        url,
        *,
        page_number,
        start,
        cards,
        selectors,
        action=None,
        capture_patterns=(),
        extra_headers=None,
        wait_selector=None,
        wait_ms=None,
        timeout_ms=None,
    ) -> PageResult:
        """复用独立搜索标签页，点击页面实际提供的控件，不构造后续页请求。"""
        self._ensure()
        timeout = timeout_ms or self.config.crawl.timeout_ms
        page = self._search_page
        reuse_pending = self._search_pending is not None and self._search_pending[
            :2
        ] == (url, page_number)
        status = 200
        if page is None or page.is_closed():
            if not start:
                raise PaginationError(
                    f"搜索标签页已关闭，第 {page_number} 页及后续页未完成"
                )
            page = self._open_direct_page(extra_headers)
            self._search_page = page
            self._search_recorder = ResponseRecorder(capture_patterns)
            self._search_recorder.attach(page)
            reuse_pending = False
        self._last_page = page
        recorder = self._search_recorder
        self._active_recorder = recorder
        before = None
        if reuse_pending:
            before = self._search_pending[2]
        elif start:
            self._search_pending = None
            recorder.discard_before(time.monotonic())
            try:
                response = page.goto(
                    url, wait_until="domcontentloaded", timeout=timeout
                )
            except Exception as exc:
                raise RetryableError(
                    f"搜索页导航失败：{type(exc).__name__}: {exc}"
                ) from exc
            status = response.status if response is not None else 0
        else:
            if self._search_number != page_number - 1:
                raise PaginationError(
                    f"分页位置不一致：已加载 {self._search_number}，请求 {page_number}"
                )
            state = search_pagination(page, selectors)
            if state.has_next is not True or not state.selector:
                raise PaginationError(
                    f"无法翻至第 {page_number} 页：{state.reason}；后续范围未完成"
                )
            before = page.locator(cards).evaluate_all(SEARCH_CARD_SIGNATURE)
            recorder.discard_before(time.monotonic())
            self._search_pending = (url, page_number, before)
            try:
                page.locator(state.selector).nth(state.index).click(timeout=timeout)
            except Exception as exc:
                raise PaginationError(
                    f"第 {page_number} 页翻页点击失败：{exc}"
                ) from exc
        if status < 400:
            if start and not reuse_pending:
                # 排序只在首个搜索请求执行；后续页再排序可能把页面重置回第一页。
                if wait_ms:
                    page.wait_for_timeout(wait_ms)
                if action:
                    action(page)
                state = search_pagination(page, selectors)
                if page_number > 1 and state.current_page != page_number:
                    # URL 可能被 CSR 忽略，优先使用实际存在的目标页按钮恢复。
                    target = page.locator(selectors["root"]).get_by_text(
                        str(page_number), exact=True
                    )
                    visible = [
                        target.nth(i)
                        for i in range(target.count())
                        if target.nth(i).is_visible()
                    ]
                    if not visible:
                        raise PaginationError(
                            f"无法恢复第 {page_number} 页：页面实际为 {state.current_page}，目标页控件不可用"
                        )
                    before = page.locator(cards).evaluate_all(SEARCH_CARD_SIGNATURE)
                    recorder.discard_before(time.monotonic())
                    self._search_pending = (url, page_number, before)
                    visible[0].click(timeout=timeout)
            self._wait_search_content(
                page,
                cards=cards,
                selectors=selectors,
                before=before,
                number=page_number,
                timeout=timeout,
            )
            if not start and action:
                action(page)
                self._wait_search_content(
                    page,
                    cards=cards,
                    selectors=selectors,
                    before=before,
                    number=page_number,
                    timeout=timeout,
                )
        recorder.collect()
        result = PageResult(
            url=page.url,
            html=page.content(),
            status=status,
            captured=list(recorder.captured),
            dom_hints=_collect_dom_hints(page),
            search_refreshed=before is not None,
            pagination=search_pagination(page, selectors),
        )
        self._search_number = page_number
        self._search_pending = None
        if self.logger:
            self.logger.info(
                "搜索页已加载 platform=%s page=%s current=%s has_next=%s reason=%s",
                self.platform,
                page_number,
                result.pagination.current_page,
                result.pagination.has_next,
                result.pagination.reason,
            )
        return result

    def discard_responses_before(self, boundary: float) -> None:
        if self._active_recorder is not None:
            self._active_recorder.discard_before(boundary)

    @property
    def chrome_handle(self) -> ChromeHandle | None:
        return self._chrome

    def start(self) -> None:
        if self._session is not None:
            return
        if self.mode == "cdp":
            self._start_cdp()
            return
        self._start_playwright()

    def _start_playwright(self) -> None:
        if (
            not os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
            and Path(DEFAULT_BROWSERS_PATH).exists()
        ):
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = DEFAULT_BROWSERS_PATH
        try:
            from scrapling.fetchers import StealthySession
        except Exception as exc:  # pragma: no cover - 依赖缺失
            raise BrowserUnavailable(
                "无法导入 Scrapling。请用 /workspace/tools/scrapling-venv/bin/python 运行，"
                "或安装 scrapling[fetchers]==0.5.1"
            ) from exc
        options: dict[str, Any] = {
            "headless": self.headless,
            "user_data_dir": str(self.profile_dir),
            "locale": "zh-CN",
            "timezone_id": self.config.crawl.timezone,
            "google_search": False,
            "load_dom": True,
            "network_idle": False,
            "timeout": self.config.crawl.timeout_ms,
            "retries": 1,
            "retry_delay": 0,
            "real_chrome": bool(self.config.crawl.real_chrome),
            "max_pages": 1,
            "additional_args": {"viewport": {"width": 1440, "height": 900}},
        }
        executable = getattr(self.config.crawl, "browser_executable", None)
        if executable:
            options["executable_path"] = str(executable)
        try:
            session = StealthySession(**options)
            session.start()
        except Exception as exc:  # pragma: no cover - 需要浏览器
            raise BrowserUnavailable(
                f"浏览器启动失败（profile={self.profile_dir}）: {type(exc).__name__}: {exc}. "
                f"检查 PLAYWRIGHT_BROWSERS_PATH={os.environ.get('PLAYWRIGHT_BROWSERS_PATH', '')}"
            ) from exc
        self._session = session
        if self.logger:
            self.logger.info(
                "browser started platform=%s headless=%s profile=%s",
                self.platform,
                self.headless,
                self.profile_dir,
            )

    def _start_cdp(self) -> None:
        """启动/复用真实 Chrome，并通过 Scrapling RemoteSession 连上默认 context。"""
        self._chrome = ensure_chrome(
            self.config,
            self.platform,
            display=self.display,
            logger=self.logger,
        )
        try:
            from scrapling.fetchers import RemoteSession
        except Exception as exc:  # pragma: no cover - 依赖缺失
            terminate_handle(self._chrome, logger=self.logger)
            self._chrome = None
            raise BrowserUnavailable(
                "当前 Scrapling 不支持 RemoteSession/CDP。请升级 scrapling[fetchers]>=0.5.1。"
            ) from exc
        options: dict[str, Any] = {
            # 复用 Chrome 里已登录的默认 context，不创建空白 context、不关闭它。
            "new_context": False,
            "headless": False,
            "google_search": False,
            "load_dom": True,
            "network_idle": False,
            "timeout": self.config.crawl.timeout_ms,
            "retries": 1,
            "retry_delay": 0,
            "max_pages": 1,
        }
        try:
            session = RemoteSession(self._chrome.endpoint, **options)
            session.start()
        except Exception as exc:
            # 只清理本次自己启动的 Chrome；用户原有的 Chrome 不会被结束。
            terminate_handle(self._chrome, logger=self.logger)
            self._chrome = None
            raise BrowserUnavailable(
                f"连接 Chrome CDP 失败（endpoint={self.config.cdp_endpoint(self.platform)}，"
                f"profile={self.profile_dir}）: {type(exc).__name__}: {exc}"
            ) from exc
        self._session = session
        if self.logger:
            self.logger.info(
                "browser started platform=%s mode=cdp endpoint=%s profile=%s launched=%s",
                self.platform,
                self._chrome.endpoint,
                self.profile_dir,
                self._chrome.launched,
            )

    def _ensure(self) -> None:
        if self._session is None:
            self.start()

    def _open_direct_page(self, extra_headers: dict[str, str] | None):
        """直接拿一个 Playwright Page，用于自定义 wait_until 的导航。

        Scrapling 的 ``session.fetch`` 固定等待 ``load`` 事件，无法传入
        ``wait_until``；登录页（尤其是京东）可能永远不触发 load，因此这条路径
        必须绕过它，但页面仍然复用同一个持久化 context / profile。
        """
        context = getattr(self._session, "context", None)
        new_page = getattr(context, "new_page", None)
        if callable(new_page):
            page = new_page()
        else:
            get_page = getattr(self._session, "_get_page", None)
            if not callable(get_page):
                raise BrowserUnavailable("当前浏览器会话不支持按 wait_until 打开页面")
            page_info = get_page(
                self.config.crawl.timeout_ms, extra_headers or {}, False
            )
            page = getattr(page_info, "page", page_info)
        if extra_headers:
            setter = getattr(page, "set_extra_http_headers", None)
            if callable(setter):
                try:
                    setter(extra_headers)
                except Exception:
                    pass
        return page

    def _fetch_page_direct(
        self,
        url: str,
        *,
        wait_until: str,
        action: Callable[[Any], None] | None = None,
        capture_patterns: Iterable[str] = (),
        extra_headers: dict[str, str] | None = None,
        wait_selector: str | None = None,
        wait_ms: int | None = None,
        timeout_ms: int | None = None,
    ) -> PageResult:
        """按指定的 wait_until 导航一次；导航超时仍返回已打开的页面。"""
        self._ensure()
        recorder = ResponseRecorder(capture_patterns)
        self._active_recorder = recorder
        hints: list[str] = []
        timeout = self.config.crawl.timeout_ms if timeout_ms is None else timeout_ms
        page = self._open_direct_page(extra_headers)
        self._last_page = page
        try:
            recorder.attach(page)
        except Exception:
            pass

        started = time.monotonic()
        navigation_response = None
        try:
            navigation_response = page.goto(url, wait_until=wait_until, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            # DOMContentLoaded 超时只说明导航没有等到该事件；页面通常已经打开，
            # 调用方（人工登录）应继续在当前页面上被动等待用户操作，而不是退出。
            if not is_navigation_timeout(exc):
                raise
            if self.logger:
                self.logger.warning(
                    "navigation timed out platform=%s url=%s wait_until=%s timeout=%s",
                    self.platform,
                    url,
                    wait_until,
                    timeout,
                )

        if wait_ms is not None and wait_ms > 0:
            try:
                page.wait_for_timeout(wait_ms)
            except Exception:
                pass
        if wait_selector:
            try:
                page.locator(wait_selector).first.wait_for()
            except Exception as exc:  # noqa: BLE001
                if self.logger:
                    self.logger.warning(
                        "wait_selector failed url=%s selector=%s error=%s",
                        url,
                        wait_selector,
                        exc,
                    )
        if action is not None:
            try:
                action(page)
            except Exception as exc:  # noqa: BLE001 - 页面动作失败不阻断登录等待
                if self.logger:
                    self.logger.warning("page_action failed url=%s error=%s", url, exc)
        try:
            hints = _collect_dom_hints(page)
        except Exception:
            pass
        try:
            recorder.collect()
        except Exception:
            pass
        try:
            html = page.content()
        except Exception:
            html = ""

        result = PageResult(
            url=getattr(page, "url", None) or url,
            html=html,
            status=int(getattr(navigation_response, "status", 0) or 0),
            captured=list(recorder.captured),
            dom_hints=hints,
        )
        if self.logger:
            self.logger.info(
                "request platform=%s url=%s final_url=%s status=%s elapsed=%.2fs wait_until=%s",
                self.platform,
                url,
                result.url,
                result.status,
                time.monotonic() - started,
                wait_until,
            )
        return result

    def fetch_page(
        self,
        url: str,
        *,
        action: Callable[[Any], None] | None = None,
        capture_patterns: Iterable[str] = (),
        extra_headers: dict[str, str] | None = None,
        wait_selector: str | None = None,
        wait_ms: int | None = None,
        timeout_ms: int | None = None,
        wait_until: str | None = None,
    ) -> PageResult:
        self._ensure()
        if wait_until is not None:
            return self._fetch_page_direct(
                url,
                wait_until=wait_until,
                action=action,
                capture_patterns=capture_patterns,
                extra_headers=extra_headers,
                wait_selector=wait_selector,
                wait_ms=self.config.crawl.wait_ms() if wait_ms is None else wait_ms,
                timeout_ms=timeout_ms,
            )
        recorder = ResponseRecorder(capture_patterns)
        self._active_recorder = recorder
        hints: list[str] = []

        def page_setup(page) -> None:
            recorder.attach(page)

        def page_action(page) -> None:
            nonlocal hints
            self._last_page = page
            try:
                if action is not None:
                    action(page)
            except Exception as exc:  # noqa: BLE001 - 页面动作失败不阻断解析
                if self.logger:
                    self.logger.warning("page_action failed url=%s error=%s", url, exc)
            finally:
                hints = _collect_dom_hints(page)
                try:
                    recorder.collect()
                except Exception:
                    pass

        kwargs: dict[str, Any] = {
            "page_setup": page_setup,
            "page_action": page_action,
            "wait_selector": wait_selector,
            "wait": self.config.crawl.wait_ms() if wait_ms is None else wait_ms,
            "timeout": self.config.crawl.timeout_ms
            if timeout_ms is None
            else timeout_ms,
            "google_search": False,
            "extra_headers": extra_headers or {},
        }
        started = time.monotonic()
        try:
            response = self._session.fetch(url, **kwargs)
        except Exception as exc:  # noqa: BLE001
            name = type(exc).__name__.lower()
            if "timeout" in name or "connection" in name or "proxy" in name:
                raise RetryableError(
                    f"{url} 请求失败: {type(exc).__name__}: {exc}"
                ) from exc
            raise
        elapsed = time.monotonic() - started
        # page_action 收集后 Scrapling 还会等待 page_wait_ms；这段窗口里
        # 懒加载/翻页 XHR 可能才返回。再收一次，避免漏掉真正的列表接口。
        try:
            recorder.collect()
        except Exception:
            pass
        body = response.body
        if isinstance(body, bytes):
            encoding = getattr(response, "encoding", "utf-8") or "utf-8"
            raw_html = body.decode(encoding, errors="replace")
        else:
            raw_html = str(body or "")
        # Scrapling 的 ``response.body`` 是最初那份导航响应体。淘宝新版搜索页是
        # CSR 空壳：原始 HTML 只有骨架屏（boneClass_/ice-container），商品由
        # 页面加载后的 mtop XHR 渲染。必须优先取 Playwright 当前 DOM，否则
        # 即使页面已渲染，解析器也只能看到空壳并整页 0 条（11/6 WU 实测）。
        live_html = self.page_html()
        body_is_html = raw_html.lstrip()[:1] == "<"
        if (
            body_is_html
            and live_html
            and live_html.strip()
            and live_html.strip() != raw_html.strip()
        ):
            html = live_html
            html_source = "dom"
        else:
            html = raw_html
            html_source = "response"
        result = PageResult(
            url=self.current_url() or getattr(response, "url", url) or url,
            html=html,
            status=int(getattr(response, "status", 0) or 0),
            captured=list(recorder.captured),
            dom_hints=hints,
        )
        if self.logger:
            self.logger.info(
                "request platform=%s url=%s final_url=%s status=%s elapsed=%.2fs captured=%s hints=%s html_source=%s html_bytes=%s",
                self.platform,
                url,
                result.url,
                result.status,
                elapsed,
                len(result.captured),
                result.dom_hints,
                html_source,
                len(result.html or ""),
            )
        return result

    def screenshot(self, path: str | Path) -> str | None:
        page = self._last_page
        if page is None:
            return None
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            page.screenshot(path=str(path), full_page=True)
            return str(path)
        except Exception as exc:  # pragma: no cover - 页面已关闭
            if self.logger:
                self.logger.warning("screenshot failed path=%s error=%s", path, exc)
            return None

    def current_url(self) -> str | None:
        try:
            return self._last_page.url if self._last_page is not None else None
        except Exception:
            return None

    def page_html(self) -> str | None:
        try:
            return self._last_page.content() if self._last_page is not None else None
        except Exception:
            return None

    def cookie_values(self) -> dict[str, str]:
        """返回同一浏览器 context 当前 ``{name: value}``；只读，不触发导航。"""
        if self._session is None or self._session.context is None:
            return {}
        try:
            cookies = self._session.context.cookies()
        except Exception:
            return {}
        values: dict[str, str] = {}
        for cookie in cookies:
            name = cookie.get("name")
            if not name:
                continue
            values[str(name)] = (
                "" if cookie.get("value") is None else str(cookie.get("value"))
            )
        return values

    def cookie_names(self) -> set[str]:
        """返回同一浏览器 context 当前 Cookie 名；只读，不触发导航。"""
        return set(self.cookie_values())

    def has_cookie(self, names: Iterable[str]) -> bool:
        found = self.cookie_names()
        return any(name in found for name in names)

    def save_login_marker(self, data: dict[str, Any]) -> Path:
        path = self.profile_dir / "login.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        return path

    def close(self) -> None:
        if self._search_page is not None:
            try:
                if not self._search_page.is_closed():
                    self._search_page.close()
            finally:
                self._search_page = None
                self._search_recorder = None
        if self._session is not None:
            try:
                self._session.close()
            finally:
                self._session = None
                self._last_page = None


class StaticBrowser:
    """离线测试 / 样例生成用浏览器：按顺序回放 PageResult。"""

    def __init__(self, pages: Iterable[PageResult], *, logger=None):
        self.pages = list(pages)
        self.calls: list[str] = []
        self.screenshots: list[str] = []
        self.logger = logger
        self._last: PageResult | None = None
        self.closed = False

    def start(self) -> None:
        return None

    def _next_page(self, url: str) -> PageResult:
        if not self.pages:
            raise AssertionError(f"StaticBrowser 没有可用页面，请求 URL={url}")
        for index, page in enumerate(self.pages):
            if page.url == url:
                self._last = self.pages.pop(index)
                return self._last
        self._last = self.pages.pop(0)
        return self._last

    def fetch_page(
        self,
        url: str,
        *,
        action=None,
        capture_patterns=(),
        extra_headers=None,
        wait_selector=None,
        wait_ms=None,
        timeout_ms=None,
        wait_until=None,
    ) -> PageResult:
        self.calls.append(url)
        page = self._next_page(url)
        if action is not None:
            # 回放页面已经是最终态，不需要滚动；但仍记录 action 被调用。
            try:
                action(None)
            except Exception:
                pass
        return page

    def screenshot(self, path: str | Path) -> str | None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
        self.screenshots.append(str(path))
        return str(path)

    def current_url(self) -> str | None:
        return self._last.url if self._last else None

    def page_html(self) -> str | None:
        return self._last.html if self._last else None

    def cookie_values(self) -> dict[str, str]:
        return {}

    def cookie_names(self) -> set[str]:
        return set()

    def has_cookie(self, names: Iterable[str]) -> bool:
        return False

    def save_login_marker(self, data: dict[str, Any]) -> Path:
        return Path("login.json")

    def close(self) -> None:
        self.closed = True
