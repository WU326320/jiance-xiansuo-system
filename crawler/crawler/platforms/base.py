"""三平台统一采集接口与公共流程。

子类只需实现：build_search_url / detail_url / parse_search / parse_detail，
以及可选的 page_action 与 after_search。
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from datetime import datetime
from itertools import count
from typing import Any, Callable, Iterable

from crawler.config import AppConfig
from crawler.core.browser import BrowserClient, PageResult
from crawler.core.errors import (
    DailyLimitExceeded,
    LoginRequired,
    PaginationError,
    RetryableError,
    RetryExhausted,
    RiskBlocked,
)
from crawler.core.jsonl import relative_or_absolute
from crawler.core.jsonutil import (
    extract_embedded_json,
    extract_records,
    parse_json_text,
)
from crawler.core.parse import now_local
from crawler.core.retry import RetryPolicy, call_with_retry
from crawler.core.risk import classify_page, detect_payload_risk
from crawler.core.schema import apply_patch, enrich_flags, validate_item
from crawler.core.stats import RunStats
from crawler.core.throttle import Throttle
from crawler.core.progress import (
    SearchProgress,
    DeadlineReached,
    check_deadline,
    remaining_seconds,
    supplemental_work,
)
from crawler.models import Item, ItemPatch
from crawler.platforms.selectors import (
    SALES_SORT,
    SEARCH_PAGINATION,
    TAOBAO,
    JD,
    ALI1688,
)


@dataclass
class _SalesSortState:
    confirmed: bool = False
    clicked: bool = False
    changed: bool = False
    started_at: float | None = None
    reason: str = "未取得页面排序确认信息"


_CARD_SIGNATURE = """nodes => nodes.map(node =>
    node.getAttribute('data-nid') || node.getAttribute('data-item-id') ||
    node.getAttribute('data-sku') || node.getAttribute('data-offer-id') ||
    node.getAttribute('href') || node.querySelector('a[href]')?.getAttribute('href') ||
    node.textContent?.trim() || '').filter(Boolean).join('\\n')"""

_SALES_SELECTED = """node => {
    for (let depth = 0; node && depth < 3; depth++, node = node.parentElement) {
        const direction = (node.getAttribute('aria-sort') || node.getAttribute('data-order') ||
                           node.getAttribute('data-sort-order') || '').toLowerCase();
        if (['ascending', 'asc'].includes(direction)) return false;
        if (direction === 'descending' || direction === 'desc' ||
            node.getAttribute('aria-selected') === 'true' ||
            node.getAttribute('aria-pressed') === 'true' ||
            /(?:^|[\\s_-])(?:active|selected|current|curr|on|checked)(?:$|[\\s_-])/i.test(node.className || ''))
            return true;
    }
    return false;
}"""


class PlatformBase(ABC):
    name: str = ""
    login_url: str = ""
    login_check_url: str = ""
    login_cookie_names: tuple[str, ...] = ()
    referer: str = ""
    search_wait_selector: str | None = None
    search_capture_patterns: tuple[str, ...] = ()
    detail_capture_patterns: tuple[str, ...] = ()

    def __init__(
        self,
        config: AppConfig,
        browser: BrowserClient,
        *,
        throttle: Throttle | None = None,
        stats: RunStats | None = None,
        logger: logging.Logger | None = None,
        date: str | None = None,
    ):
        self.config = config
        self.browser = browser
        self.date = date or now_local(config.crawl.timezone).strftime("%Y-%m-%d")
        self.stats = stats or RunStats()
        self.logger = logger or logging.getLogger(f"crawler.{self.name}")
        rate = config.crawl.rate_limit_for(self.name)
        self.throttle = throttle or Throttle(
            platform=self.name,
            date=self.date,
            state_dir=config.state_root,
            daily_max_pages=0,
            requests_per_minute=rate.requests_per_minute,
            page_interval=rate.page_interval_seconds,
            keyword_interval=rate.keyword_interval_seconds,
            detail_interval=rate.detail_interval_seconds,
        )
        self.retry_policy = RetryPolicy(
            attempts=config.crawl.retry.attempts,
            backoff_seconds=config.crawl.retry.backoff_seconds,
            jitter_seconds=config.crawl.retry.jitter_seconds,
        )
        self.last_login_reason: str | None = None
        self.last_login_risk = False
        self.last_screenshot_path: str | None = None
        self._debug_snapshots: dict[int, dict[str, str | None]] = {}
        self._now = now_local(config.crawl.timezone)
        self.checkpoint_items: Callable[[list[Item]], Any] | None = None
        self.checkpoint_patch: Callable[[ItemPatch], Any] | None = None
        self._sales_sort = _SalesSortState()
        self.search_progress: SearchProgress | None = None
        self.continuous = False
        self.complete_fields = (
            os.environ.get("CRAWLER_AMOUNT_DETAIL") == "1"
            or remaining_seconds() is not None
        )
        self.defer_after_search = False
        self.search_deadline_reached = False
        self.search_stop_reason: str | None = None
        self.detail_failures: dict[str, str] = {}
        self.detail_attempted: set[str] = set()

    # ---------- 必须实现 ----------
    @abstractmethod
    def build_search_url(self, keyword: str, page: int) -> str: ...

    @abstractmethod
    def detail_url(self, product_id: str) -> str: ...

    @abstractmethod
    def parse_search(
        self, result: PageResult, keyword: str, page: int, ip_code: str | None
    ) -> list[Item]: ...

    @abstractmethod
    def parse_detail(self, result: PageResult, product_id: str) -> ItemPatch | None: ...

    # ---------- 可选钩子 ----------
    def page_action(self, page) -> None:
        """搜索页加载后的动作（京东滚动等）。"""

    def after_search(self, keyword: str, items: list[Item]) -> None:
        """关键词级后处理（1688 命中率 warning 等）。"""

    def on_empty_result(self, keyword: str, page: int) -> None:
        """单页解析 0 条时的平台钩子；默认不做事（京东/1688 保持原行为）。"""

    def on_non_empty_result(self, keyword: str, page: int) -> None:
        """单页解析到商品时的平台钩子；默认不做事。"""

    # ---------- 登录 ----------
    def login_cookie_ok(self) -> bool | None:
        """平台特定的 Cookie 判定；None 表示沿用 ``login_cookie_names``。"""
        return None

    def check_login(self) -> bool:
        """打开需登录页面；被重定向到登录页 / 风控页即判失败。"""
        self.last_login_reason = None
        check_deadline()
        self.throttle.reserve_request()
        check_deadline()
        result = self.browser.fetch_page(
            self.login_check_url,
            capture_patterns=(),
            wait_ms=self.config.crawl.wait_ms(),
            extra_headers={"Referer": self.referer},
            wait_selector=None,
            wait_until="domcontentloaded",
            timeout_ms=60_000,
        )
        self.stats.page_fetched()
        state = classify_page(
            result.url, result.html, platform=self.name, dom_hints=result.dom_hints
        )
        self.last_login_risk = state.is_risk
        if state.is_risk:
            screenshot = self._capture_screenshot("login_risk")
            self.last_login_reason = state.reason
            self.last_screenshot_path = screenshot
            self.logger.error(
                "check_login risk platform=%s reason=%s", self.name, state.reason
            )
            return False
        if state.is_login:
            self.last_login_reason = state.reason
            self.logger.warning(
                "check_login failed platform=%s reason=%s", self.name, state.reason
            )
            return False
        # 有些账号页不会重定向但会显示“请登录”；cookie 是辅助信号。
        # CDP 下淘宝/1688 会返回明确的 True/False，此时必须拿到有效登录 Cookie。
        content = result.html or ""
        cookie_decision = self.login_cookie_ok()
        has_login_cookie = (
            bool(cookie_decision)
            if cookie_decision is not None
            else self.browser.has_cookie(self.login_cookie_names)
        )
        if not has_login_cookie:
            if cookie_decision is not None:
                self.last_login_reason = "缺少有效的登录 Cookie（CDP 严格校验）"
            elif "请登录" in content or "登录后" in content:
                self.last_login_reason = "账号页显示未登录"
            else:
                return True
            self.logger.warning(
                "check_login failed platform=%s reason=%s",
                self.name,
                self.last_login_reason,
            )
            return False
        self.logger.info(
            "check_login ok platform=%s final_url=%s", self.name, result.url
        )
        return True

    # ---------- 搜索 ----------
    def _sales_control(self, page):
        controls = page.locator(SALES_SORT[self.name]["control"])
        try:
            controls.first.wait_for(state="visible", timeout=3000)
        except Exception:
            pass
        for index in range(min(controls.count(), 20)):
            control = controls.nth(index)
            if control.is_visible():
                return control
        return None

    def _sort_search_page(self, page) -> None:
        state = self._sales_sort
        if page is None or self.name not in SALES_SORT:
            return
        control = self._sales_control(page)
        if control is None:
            state.reason = "未找到可见的销量排序控件，已使用销量排序 URL"
            return
        if control.evaluate(_SALES_SELECTED):
            # 避免再次点击已选中的控件导致升/降序反转；仅界面选中不宣称确认生效。
            state.reason = "销量控件已选中，未取得列表切换证据"
            return
        selector = SALES_SORT[self.name]["cards"]
        before = page.locator(selector).evaluate_all(_CARD_SIGNATURE)
        state.started_at = time.monotonic()
        control.click(timeout=4000)
        state.clicked = True
        try:
            page.wait_for_function(
                """({selector, before}) => {
                    const signature = ("""
                + _CARD_SIGNATURE
                + """);
                    const current = signature([...document.querySelectorAll(selector)]);
                    return !!current && current !== before;
                }""",
                arg={"selector": selector, "before": before},
                timeout=6000,
            )
            state.changed = True
        except Exception:
            state.reason = "已点击销量排序，未确认商品列表更新"
            return
        # 列表未变化时保留原响应，排序等待超时仍能按原流程采集接口商品。
        discard = getattr(self.browser, "discard_responses_before", None)
        if callable(discard):
            discard(state.started_at)
        control = self._sales_control(page)
        state.confirmed = control is not None and bool(
            control.evaluate(_SALES_SELECTED)
        )
        state.reason = (
            "销量控件选中且商品列表已更新"
            if state.confirmed
            else "列表已更新，未确认销量控件选中"
        )

    def _search_page_action(self, page) -> None:
        # 每次请求/重试单独记录，不能复用上一页的成功状态。
        self._sales_sort = _SalesSortState()
        try:
            self._sort_search_page(page)
        except Exception as exc:  # noqa: BLE001 - 排序失败继续原采集流程
            self._sales_sort.reason = f"自动销量排序未确认：{type(exc).__name__}: {exc}"
        self.page_action(page)

    def _prepare_search_result(
        self, result: PageResult, keyword: str, page: int
    ) -> PageResult:
        state = self._sales_sort
        if not state.confirmed:
            self.log_warning(
                f"销量排序未确认 platform={self.name} keyword={keyword!r} page={page}："
                f"{state.reason}；继续按原流程采集"
            )
        else:
            self.logger.info(
                "销量排序已确认 platform=%s keyword=%s page=%s",
                self.name,
                keyword,
                page,
            )
        if state.clicked and state.changed and state.started_at is not None:
            fresh = [
                item
                for item in result.captured
                if item.requested_at is not None
                and item.requested_at >= state.started_at
            ]
            # CSR 页面旧的内嵌初始 JSON 不会随排序更新，不能再拼回新列表。
            # 仅收到无关 JSON 不代表列表切换，不能因此丢弃原商品数据。
            return replace(result, captured=fresh, search_refreshed=True)
        return result

    def search(
        self, keyword: str, max_pages: int | None = None, *, ip_code: str | None = None
    ) -> list[Item]:
        """逐页采到真实末页；max_pages 仅保留旧调用兼容，不再截断。"""
        keyword = (keyword or "").strip()
        if not keyword:
            return []
        progress = self.search_progress
        position = progress.position(keyword, ip_code) if progress else {}
        if position.get("done"):
            return []
        first_page = int(position.get("next_page", 1))
        last_signature = position.get("signature")
        seen_signatures = set(position.get("signatures", []))
        if last_signature:
            seen_signatures.add(SearchProgress.signature_digest(last_signature))
        seen_items: set[tuple[str, str]] = set()
        self.search_stop_reason = None
        items: list[Item] = []
        try:
            check_deadline()
            self.throttle.wait_keyword_interval()
            for page in count(first_page):
                check_deadline()
                if progress:
                    progress.begin(keyword, ip_code, page)
                if page > 1:
                    self.throttle.wait_page_interval()
                check_deadline()
                url = self.build_search_url(keyword, page)
                first_request = page == first_page
                if first_request:
                    self._sales_sort = _SalesSortState()
                else:
                    self._sales_sort.clicked = False
                    self._sales_sort.changed = False
                result = self._request(
                    url,
                    action=self._search_page_action
                    if first_request
                    else self.page_action,
                    capture_patterns=self.search_capture_patterns,
                    extra_headers={"Referer": self.referer},
                    wait_selector=self.search_wait_selector,
                    search_page_number=page,
                    search_start=first_request,
                )
                blocked = self._classify_result(result)
                if blocked.is_risk:
                    raise RiskBlocked(
                        self.name,
                        blocked.reason,
                        evidence=blocked.evidence,
                        screenshot_path=self._capture_screenshot("risk"),
                    )
                if blocked.is_login:
                    self.last_login_reason = blocked.reason
                    raise LoginRequired(self.name, blocked.reason)
                result = self._prepare_search_result(result, keyword, page)
                self.stats.page_fetched()
                missing_before = self.stats.drop_reasons.get("product_id解析失败", 0)
                try:
                    parsed = self.parse_search(result, keyword, page, ip_code)
                    for rank, item in enumerate(parsed, start=1):
                        item.rank = rank
                        if not self._sales_sort.confirmed:
                            item.flags = list(
                                dict.fromkeys([*item.flags, "销量排序未确认"])
                            )
                except Exception as exc:  # noqa: BLE001 - 单页解析失败不拖垮平台
                    self.stats.error(
                        f"parse_search page={page} error={type(exc).__name__}: {exc}"
                    )
                    self.logger.exception(
                        "parse_search failed platform=%s page=%s", self.name, page
                    )
                    raise
                missing_ids = (
                    self.stats.drop_reasons.get("product_id解析失败", 0)
                    - missing_before
                )
                self.logger.info(
                    "page parsed platform=%s keyword=%s page=%s items=%s missing_product_id=%s",
                    self.name,
                    keyword,
                    page,
                    len(parsed),
                    missing_ids,
                )
                if missing_ids > 0:
                    self.save_debug_snapshot(
                        result,
                        f"product_id 缺失 {missing_ids} 条（{self.name} 搜索页）",
                        keyword=keyword,
                        page=page,
                    )
                if not parsed:
                    self.save_debug_snapshot(
                        result,
                        f"列表解析 0 条（{self.name} 搜索页）",
                        keyword=keyword,
                        page=page,
                    )
                    if result.pagination and result.pagination.empty_confirmed:
                        self.search_stop_reason = "no_results"
                        if progress:
                            progress.commit(
                                keyword,
                                ip_code,
                                page,
                                [],
                                done=True,
                                stop_reason="no_results",
                            )
                        break
                    self.on_empty_result(keyword, page)
                    raise PaginationError(
                        f"第 {page} 页解析为空且没有无结果证据；该页及后续页未完成"
                    )
                self.on_non_empty_result(keyword, page)
                signature = sorted(
                    {f"{item.platform}:{item.product_id}" for item in parsed}
                )
                digest = SearchProgress.signature_digest(signature)
                if digest in seen_signatures:
                    self.save_debug_snapshot(
                        result, "翻页后商品集合重复", keyword=keyword, page=page
                    )
                    raise PaginationError(
                        f"第 {page} 页商品集合与已采集页重复，翻页未确认；该页及后续页未完成"
                    )
                seen_signatures.add(digest)
                terminal = bool(
                    result.pagination and result.pagination.has_next is False
                )
                if terminal:
                    self.search_stop_reason = "last_page"
                if progress:
                    progress.commit(
                        keyword,
                        ip_code,
                        page,
                        parsed,
                        done=terminal,
                        signature=signature,
                        stop_reason=self.search_stop_reason,
                    )
                last_signature = signature
                for item in parsed:
                    identity = (item.platform, item.product_id)
                    if identity not in seen_items:
                        items.append(item)
                        seen_items.add(identity)
                if terminal:
                    if self.checkpoint_items is not None:
                        self.checkpoint_items(items)
                    break
                if self.checkpoint_items is not None:
                    self.checkpoint_items(items)
        except DeadlineReached as exc:
            self.search_deadline_reached = True
            self.search_stop_reason = "deadline"
            self.log_warning(str(exc))
        except PaginationError as exc:
            self.search_stop_reason = "pagination_blocked"
            if progress:
                progress.block(keyword, ip_code, page, str(exc))
            self.stats.error(str(exc))
            raise
        except (RiskBlocked, LoginRequired, DailyLimitExceeded):
            raise
        # 截止前发出的请求完成解析和提交；定时任务由runner在全部搜索
        # 停止后统一后处理，普通批次仍在当前关键词结束时后处理。
        if progress:
            saved_patches = progress.state.get("detail_patches", {})
            items = [
                apply_patch(
                    item,
                    ItemPatch.model_validate(saved_patches[progress.detail_key(item)]),
                )
                if progress.detail_key(item) in saved_patches
                else item
                for item in items
            ]
        if not self.defer_after_search:
            with supplemental_work():
                self.after_search(keyword, items)
        if self.checkpoint_items is not None:
            self.checkpoint_items(items)
        if progress and not self.search_deadline_reached:
            progress.finish_keyword(keyword, ip_code, reason=self.search_stop_reason)
        # 去重/合并可能保留旧的派生 flags；输出前统一按最终字段重算。
        return [enrich_flags(item) for item in items]

    # ---------- 详情 ----------
    @supplemental_work()
    def fetch_detail(self, product_ids: Iterable[str]) -> list[ItemPatch]:
        patches: list[ItemPatch] = []
        detail_cfg = self.config.crawl.detail
        capture_limit = int(detail_cfg.debug_capture_limit or 0)
        capture_platforms = set(detail_cfg.debug_capture_platforms or [])
        for index, product_id in enumerate(product_ids):
            check_deadline()
            product_id = str(product_id).strip()
            if not product_id:
                continue
            self.detail_attempted.add(product_id)
            self.throttle.wait_detail_interval()
            url = self.detail_url(product_id)
            try:
                wait_ms = None
                if detail_cfg.detail_wait_ms_range:
                    low, high = detail_cfg.detail_wait_ms_range
                    wait_ms = int(random.uniform(low, high))
                result = self._request(
                    url,
                    action=None,
                    capture_patterns=self.detail_capture_patterns,
                    extra_headers={"Referer": self.referer},
                    wait_selector=None,
                    wait_ms=wait_ms,
                )
                if (
                    capture_limit > 0
                    and index < capture_limit
                    and self.name in capture_platforms
                ):
                    self.save_debug_payloads(
                        result,
                        f"详情页原始响应 product_id={product_id}",
                        product_id=product_id,
                    )
            except (RetryableError, RetryExhausted) as exc:
                self.detail_failures[f"{self.name}:{product_id}"] = str(exc)
                self.stats.error(
                    f"detail request failed product_id={product_id}: {exc}"
                )
                continue
            self.stats.page_fetched()
            if result.status in (404, 410):
                prefix, _, raw_id = product_id.partition(":")
                pid = raw_id if prefix == "tmall" and raw_id else product_id
                patch_platform = "tmall" if prefix == "tmall" else self.name
                patches.append(
                    ItemPatch(
                        platform=patch_platform,
                        product_id=pid,
                        listed=False,
                        flags=["商品下架"],
                    )
                )
                if self.checkpoint_patch is not None:
                    self.checkpoint_patch(patches[-1])
                continue
            blocked = self._classify_result(result)
            if blocked.is_risk:
                raise RiskBlocked(
                    self.name,
                    blocked.reason,
                    evidence=blocked.evidence,
                    screenshot_path=self._capture_screenshot("detail_risk"),
                )
            if blocked.is_login:
                self.last_login_reason = blocked.reason
                raise LoginRequired(self.name, blocked.reason)
            try:
                patch = self.parse_detail(result, product_id)
            except Exception as exc:  # noqa: BLE001
                self.stats.error(
                    f"parse_detail failed product_id={product_id}: {type(exc).__name__}: {exc}"
                )
                patch = None
            if patch is not None:
                patches.append(patch)
                if self.checkpoint_patch is not None:
                    self.checkpoint_patch(patch)
                self.logger.info(
                    "detail parsed platform=%s product_id=%s", self.name, product_id
                )
            else:
                self.detail_failures[f"{self.name}:{product_id}"] = "详情解析失败"
                self.stats.drop("详情解析失败", product_id)
        return patches

    # ---------- 请求 / 风控 ----------
    def _request(
        self,
        url: str,
        *,
        action,
        capture_patterns: Iterable[str],
        extra_headers: dict[str, str] | None = None,
        wait_selector: str | None = None,
        wait_ms: int | None = None,
        wait_until: str | None = None,
        timeout_ms: int | None = None,
        search_page_number: int | None = None,
        search_start: bool = False,
    ) -> PageResult:
        def once() -> PageResult:
            check_deadline()
            navigation = {}
            if wait_until is not None:
                navigation["wait_until"] = wait_until
            fetch = self.browser.fetch_page
            search_fetch = getattr(self.browser, "fetch_search_page", None)
            if search_page_number is not None and callable(search_fetch):
                fetch = search_fetch
                navigation.update(
                    page_number=search_page_number,
                    start=search_start,
                    cards=", ".join(
                        {"taobao": TAOBAO, "jd": JD, "1688": ALI1688}[self.name]["card"]
                    ),
                    selectors=SEARCH_PAGINATION,
                )
            result = fetch(
                url,
                action=action,
                capture_patterns=capture_patterns,
                extra_headers=extra_headers,
                wait_selector=wait_selector,
                wait_ms=self.config.crawl.wait_ms() if wait_ms is None else wait_ms,
                timeout_ms=self.config.crawl.timeout_ms
                if timeout_ms is None
                else timeout_ms,
                **navigation,
            )
            if result.status == 429:
                raise RetryableError(f"{url} HTTP 429", status=429)
            if result.status >= 500:
                raise RetryableError(
                    f"{url} HTTP {result.status}", status=result.status
                )
            return result

        def reserve(attempt):
            check_deadline()
            self.throttle.reserve_request()

        return call_with_retry(
            once,
            policy=self.retry_policy,
            sleep=self.throttle.sleep,
            logger=self.logger,
            description=f"{self.name} {url}",
            on_attempt=reserve,
        )

    def _classify_result(self, result: PageResult):
        payloads = self.payloads(result)
        decision = detect_payload_risk(payloads)
        if decision.blocked:
            from crawler.core.risk import PageClassification

            return PageClassification("risk", decision.reason, decision.evidence)
        state = classify_page(
            result.url, result.html, platform=self.name, dom_hints=result.dom_hints
        )
        if state.is_risk or state.is_login:
            return state
        if result.status in (401,):
            from crawler.core.risk import PageClassification

            return PageClassification("login", f"HTTP {result.status}")
        if result.status in (403, 412):
            from crawler.core.risk import PageClassification

            return PageClassification("risk", f"HTTP {result.status} 访问受限")
        return state

    def payloads(self, result: PageResult) -> list[Any]:
        """XHR/JSONP 响应 + 页面内嵌 JSON，作为列表解析的数据源。"""
        payloads: list[Any] = []
        for captured in result.captured:
            value = parse_json_text(captured.text)
            if value is not None:
                payloads.append(value)
        if not result.search_refreshed:
            payloads.extend(extract_embedded_json(result.html))
        return payloads

    def records(
        self,
        payloads: list[Any],
        *,
        list_keys: Iterable[str],
        id_keys: Iterable[str],
    ) -> list[dict]:
        return extract_records(payloads, list_keys=list_keys, id_keys=id_keys)

    def save_debug_snapshot(
        self,
        result: PageResult,
        reason: str,
        *,
        keyword: str | None = None,
        page: int | None = None,
    ) -> dict[str, str | None]:
        """保存完整 HTML + 截图到 ``logs/debug/<platform>_<时间>.html/.png``。

        同一 PageResult 只落一次盘（解析器可能先因 product_id 缺失调用，
        搜索流程随后又因 0 条调用），warning 也只在首次落盘时写。
        """
        cached = self._debug_snapshots.get(id(result))
        if cached is not None:
            return cached
        debug_dir = self.config.log_root / "debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        stamp = now_local(self.config.crawl.timezone).strftime("%Y%m%d_%H%M%S_%f")
        stem = f"{self.name}_{stamp}"
        html_path = debug_dir / f"{stem}.html"
        png_path = debug_dir / f"{stem}.png"
        suffix = 1
        while html_path.exists() or png_path.exists():
            stem = f"{self.name}_{stamp}_{suffix}"
            html_path = debug_dir / f"{stem}.html"
            png_path = debug_dir / f"{stem}.png"
            suffix += 1

        html_rel: str | None = None
        png_rel: str | None = None
        try:
            with html_path.open("w", encoding="utf-8", errors="replace") as handle:
                handle.write(result.html or "")
            html_rel = relative_or_absolute(html_path)
        except OSError as exc:
            self.stats.error(f"debug HTML 保存失败 reason={reason}: {exc}")
        try:
            saved = self.browser.screenshot(png_path)
            if saved:
                png_rel = relative_or_absolute(saved)
        except Exception as exc:  # noqa: BLE001 - 截图失败不能影响采集
            self.stats.error(
                f"debug 截图保存失败 reason={reason}: {type(exc).__name__}: {exc}"
            )

        paths: dict[str, str | None] = {"html": html_rel, "png": png_rel}
        self._debug_snapshots[id(result)] = paths
        self.log_warning(
            f"{reason} keyword={keyword!r} page={page} url={result.url} "
            f"debug_html={html_rel or '-'} debug_png={png_rel or '-'}"
        )
        return paths

    def save_debug_payloads(
        self,
        result: PageResult,
        reason: str,
        *,
        product_id: str | None = None,
    ) -> dict[str, str | None]:
        """把详情页 HTML + 本次捕获的原始 JSON 响应存到 logs/debug。

        只由配置 ``crawl.detail.debug_capture_limit`` 控制前 N 个详情页；
        不修改页面、不额外请求，纯留存已有响应，供定位评论数字段。
        """
        debug_dir = self.config.log_root / "debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        stamp = now_local(self.config.crawl.timezone).strftime("%Y%m%d_%H%M%S_%f")
        safe_id = str(product_id or "detail").replace(":", "_")
        stem = f"detail_{self.name}_{safe_id}_{stamp}"
        html_path = debug_dir / f"{stem}.html"
        json_path = debug_dir / f"{stem}.json"
        html_rel: str | None = None
        json_rel: str | None = None
        try:
            html_path.write_text(result.html or "", encoding="utf-8", errors="replace")
            html_rel = relative_or_absolute(html_path)
        except OSError as exc:
            self.stats.error(f"debug 详情 HTML 保存失败 reason={reason}: {exc}")
        payloads = [
            {
                "url": item.url,
                "status": item.status,
                "content_type": item.content_type,
                "text": item.text,
            }
            for item in result.captured
        ]
        try:
            json_path.write_text(
                json.dumps(payloads, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            json_rel = relative_or_absolute(json_path)
        except OSError as exc:
            self.stats.error(f"debug 详情 JSON 保存失败 reason={reason}: {exc}")
        self.logger.info(
            "%s platform=%s product_id=%s captured=%d debug_html=%s debug_json=%s",
            reason,
            self.name,
            product_id,
            len(payloads),
            html_rel or "-",
            json_rel or "-",
        )
        return {"html": html_rel, "json": json_rel}

    def _capture_screenshot(self, tag: str) -> str | None:
        path = (
            self.config.log_root
            / "screenshots"
            / f"{self.date}_{self.name}_{tag}_{int(time.time() * 1000) % 1000000}.png"
        )
        saved = self.browser.screenshot(path)
        if saved:
            return relative_or_absolute(saved)
        return None

    def now(self) -> datetime:
        return now_local(self.config.crawl.timezone)

    def validate(self, item: Item) -> Item | None:
        valid, reason = validate_item(item, allow_missing_title=self.complete_fields)
        if valid is None:
            self.stats.drop(
                reason or "schema校验失败", f"{item.platform}/{item.product_id}"
            )
            return None
        return valid

    def log_warning(self, message: str) -> None:
        self.stats.warning(message)
        self.logger.warning(message)
