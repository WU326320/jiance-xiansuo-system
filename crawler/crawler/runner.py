"""运行编排：登录检查 → 关键词搜索 → 图片/JSONL → 运行记录。

单个平台失败不会影响其他平台；定时任务等待授权或重试至统一截止。
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

from crawler.config import AppConfig
from crawler.core.batchstate import read_batch_state, write_batch_state
from crawler.core.browser import BrowserClient, ScraplingBrowser
from crawler.core.errors import (
    BrowserUnavailable,
    ConfigError,
    DailyLimitExceeded,
    KeywordEncodingError,
    LoginRequired,
    RetryExhausted,
    RiskBlocked,
)
from crawler.core.images import ImageDownloader
from crawler.core.jsonl import (
    JsonlWriter,
    read_jsonl,
    relative_or_absolute,
    write_json_atomic,
    write_run_record,
)
from crawler.core.logging_utils import platform_logger
from crawler.core.parse import now_local
from crawler.core.riskstate import mark_risk_blocked, read_risk_marker
from crawler.core.schema import enrich_flags
from crawler.core.scoring import score_item_row
from crawler.core.stats import RunStats
from crawler.core.throttle import Throttle
from crawler.core.progress import (
    SearchProgress,
    DeadlineReached,
    check_deadline,
    remaining_seconds,
    supplemental_work,
)
from crawler.models import Item, ItemPatch, RunRecord, item_json_dict
from crawler.login import check_task_login
from crawler.platforms import PLATFORM_NAMES, create_platform

BrowserFactory = Callable[[AppConfig, str, bool | None], BrowserClient]


def _default_browser_factory(
    config: AppConfig, platform: str, headless: bool | None
) -> BrowserClient:
    return ScraplingBrowser(config, platform, headless=headless)


def validate_date(value: str) -> str:
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise ConfigError(f"日期格式必须是 YYYY-MM-DD: {value!r}") from exc
    return value


def today(config: AppConfig) -> str:
    return now_local(config.crawl.timezone).strftime("%Y-%m-%d")


def _match_ip_code(config: AppConfig, keyword: str) -> str | None:
    needle = keyword.strip().lower()
    for ip in config.ips:
        words = [ip.name, *ip.aliases, *ip.variants]
        if any((word or "").strip().lower() == needle for word in words):
            return ip.code
    return None


def build_keyword_matrix(
    config: AppConfig,
    overrides: Iterable[str] | None = None,
) -> list[dict[str, str | None]]:
    """关键词矩阵：显式 --keyword > crawl.keywords > ips 的名称/别名/变体。"""
    entries: list[dict[str, str | None]] = []
    overrides = [value for value in (overrides or []) if value and value.strip()]
    if overrides:
        for keyword in overrides:
            entries.append(
                {
                    "keyword": keyword.strip(),
                    "ip_code": _match_ip_code(config, keyword) or "manual",
                }
            )
    elif config.crawl.keywords:
        for spec in config.crawl.keywords:
            entries.append(
                {
                    "keyword": spec.keyword,
                    "ip_code": spec.ip_code or _match_ip_code(config, spec.keyword),
                }
            )
    else:
        for ip in config.ips:
            for word in [ip.name, *ip.aliases, *ip.variants]:
                keyword = (word or "").strip()
                if keyword:
                    entries.append({"keyword": keyword, "ip_code": ip.code})
    deduped: list[dict[str, str | None]] = []
    seen: set[tuple[str, str | None]] = set()
    for entry in entries:
        key = (str(entry["keyword"]), entry.get("ip_code"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(entry)
    if not deduped:
        raise ConfigError("没有可搜索的关键词：请在 crawl.keywords 或 ips 中配置")
    return deduped


def _taobao_batch_selection(
    config: AppConfig,
    date: str,
    total_keywords: int,
    batch: int | str | None,
) -> tuple[int, int, int]:
    """解析淘宝批次：返回 (本批序号, 总批数, 批大小)，序号从 1 开始。"""
    size = int(config.crawl.taobao.batch.size)
    total_batches = max(1, (int(total_keywords) + size - 1) // size)
    if batch == "next":
        state = read_batch_state(config.state_root, "taobao", date) or {}
        try:
            index = int(state.get("next_batch") or 1)
        except (TypeError, ValueError):
            index = 1
    elif batch == "all":
        index = 1
    else:
        try:
            index = int(batch)
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"--batch 必须是正整数、next 或 all，收到: {batch!r}"
            ) from exc
    if index < 1 or index > total_batches:
        raise ConfigError(
            f"淘宝批次 {index} 超出范围 1..{total_batches}"
            f"（关键词 {total_keywords} 个，批大小 {size}）"
        )
    return index, total_batches, size


def _taobao_detail_candidates(
    config: AppConfig,
    platform: str,
    date: str,
    ids: list[str],
    logger: logging.Logger,
    stats: RunStats,
) -> list[str]:
    """淘宝详情候选：按已有业务条件筛选，不截断候选数量。

    列表 JSONL 存在时按 ``text_score`` 配置打分，低于
    ``crawl.taobao.detail.detail_min_text_score`` 的直接跳过；当天没有列表数据
    （人工/后端显式传 ID）时保留。京东/1688 原样返回，不受影响。
    """
    if platform != "taobao":
        return list(ids)
    detail = config.crawl.taobao.detail
    if not detail.enabled:
        stats.warning("淘宝详情补抓已按 crawl.taobao.detail.enabled=false 停用")
        return []
    scores: dict[str, int] = {}
    missing_amount: set[str] = set()
    list_path = config.output_root / date / "taobao.jsonl"
    list_available = list_path.exists()
    if detail.filter_by_score and list_available:
        try:
            for row in read_jsonl(list_path):
                pid = str(row.get("product_id") or "").strip()
                if pid:
                    scores[pid] = score_item_row(config, row)
                    if (
                        any(row.get(field) is None for field in ("price", "sales"))
                        or not str(row.get("shop_name") or "").strip()
                    ):
                        missing_amount.add(pid)
        except Exception as exc:  # noqa: BLE001 - 列表读不到时退回人工 ID 语义
            logger.warning("读取淘宝列表分数失败 path=%s error=%s", list_path, exc)
    kept: list[str] = []
    skipped_low = 0
    skipped_unscored = 0
    for raw_id in ids:
        text = str(raw_id).strip()
        if not text:
            continue
        pid = text.partition(":")[2] if text.startswith("tmall:") else text
        if pid in scores:
            score = scores[pid]
            if (
                detail.filter_by_score
                and pid not in missing_amount
                and score < float(detail.detail_min_text_score)
            ):
                skipped_low += 1
                logger.info(
                    "skip taobao detail product_id=%s score=%s threshold=%s",
                    pid,
                    score,
                    detail.detail_min_text_score,
                )
                continue
        else:
            # 当天完全没有列表数据时按人工/后端显式 ID 处理；有列表数据但
            # 这个 ID 不在列表里，说明无法证明高分，默认跳过。
            if detail.filter_by_score and list_available and not detail.allow_unscored:
                skipped_unscored += 1
                logger.info("skip taobao detail product_id=%s not in list output", pid)
                continue
        kept.append(text)
    if skipped_low or skipped_unscored:
        stats.warning(
            f"淘宝详情候选按分数过滤：低于阈值跳过 {skipped_low} 个，"
            f"无列表分数跳过 {skipped_unscored} 个（阈值 {detail.detail_min_text_score}）"
        )
    logger.info(
        "taobao detail candidates requested=%s kept=%s threshold=%s",
        len(ids),
        len(kept),
        detail.detail_min_text_score,
    )
    return kept


def _new_run_id(started_at: datetime) -> str:
    """运行编号：日期时间 + 短随机后缀，列表/详情每次运行都不同。"""
    return f"{started_at.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"


def _base_record(platform: str, config: AppConfig, started_at: datetime) -> RunRecord:
    return RunRecord(
        platform=platform,
        run_id=_new_run_id(started_at),
        started_at=started_at,
        ended_at=started_at,
        config_version=config.config_version,
        caliber_version=config.caliber_version,
    )


def _set_output(record: RunRecord, **kwargs) -> None:
    for key, value in kwargs.items():
        setattr(record, key, value)


def _write_risk_marker(
    config: AppConfig,
    platform: str,
    date: str,
    reason: str,
    screenshot: str | None,
    logger: logging.Logger,
):
    try:
        return mark_risk_blocked(
            config.state_root,
            platform,
            date,
            reason=reason,
            screenshot_path=screenshot,
        )
    except Exception as exc:  # noqa: BLE001 - 熔断标记失败不应掩盖风控停机
        logger.error("写风控熔断标记失败 platform=%s error=%s", platform, exc)
        return None


def _browser_launched(browser) -> bool:
    """仅当本次进程真的启动了 Chrome 时为 True（复用用户浏览器为 False）。"""
    handle = getattr(browser, "chrome_handle", None)
    return bool(getattr(handle, "launched", False))


def _finalize_record(
    record: RunRecord,
    stats: RunStats,
    *,
    status: str,
    started_at: datetime,
    config: AppConfig,
    output_path: Path | None,
    log_path: Path | None,
    logger: logging.Logger,
) -> RunRecord:
    record.ended_at = now_local(config.crawl.timezone)
    record.pages_fetched = stats.pages_fetched
    record.items_dropped = stats.items_dropped
    record.items_duplicate = stats.items_duplicate
    record.drop_reasons = dict(stats.drop_reasons)
    record.errors = list(stats.errors)[:200]
    record.warnings = list(stats.warnings)[:200]
    record.status = status
    record.deadline_reached = record.deadline_reached or remaining_seconds() == 0
    if output_path is not None and output_path.name.endswith("_detail.jsonl"):
        # 详情运行把最终状态同步到 detail_status；列表运行保持 skipped。
        record.detail_status = status
    if output_path is not None:
        record.output_path = relative_or_absolute(output_path)
    if log_path is not None:
        record.log_path = relative_or_absolute(log_path)
    # 淘宝分批进度：只有分批运行才写；成功才把 next_batch 推进到下一批。
    if record.platform == "taobao" and record.batch_index is not None:
        total = int(record.batch_total or record.batch_index)
        index = int(record.batch_index)
        if record.status == "ok":
            record.batch_next = min(index + 1, total + 1)
        else:
            record.batch_next = index
        try:
            state_path = write_batch_state(
                config.state_root,
                record.platform,
                record.batch_date or record.started_at.strftime("%Y-%m-%d"),
                batch_size=int(config.crawl.taobao.batch.size),
                batch_total=total,
                last_completed_batch=index if record.status == "ok" else index - 1,
                next_batch=int(record.batch_next),
                status=record.status,
                run_id=record.run_id,
                keywords=record.batch_keywords,
            )
            logger.info(
                "batch progress platform=taobao batch=%s/%s next=%s status=%s path=%s",
                index,
                total,
                record.batch_next,
                record.status,
                state_path,
            )
        except Exception as exc:  # noqa: BLE001 - 进度写失败不丢已采数据
            logger.error("写淘宝分批进度失败 batch=%s error=%s", index, exc)
    try:
        run_dir = (
            output_path.parent
            if output_path is not None
            else config.output_root / record.started_at.strftime("%Y-%m-%d")
        )
        write_run_record(run_dir, record)
    except Exception as exc:  # noqa: BLE001 - 运行记录失败不应丢已采数据
        logger.error("写运行记录失败 platform=%s error=%s", record.platform, exc)
    logger.info(
        "run finished platform=%s status=%s login_ok=%s risk=%s pages=%s items_ok=%s dropped=%s duplicate=%s errors=%s",
        record.platform,
        record.status,
        record.login_ok,
        record.risk_blocked,
        record.pages_fetched,
        record.items_ok,
        record.items_dropped,
        record.items_duplicate,
        len(record.errors),
    )
    return record


def _inline_detail_missing_fields(row: dict) -> list[str]:
    """四项关键字段独立补缺；评论数不是销量，真实0仍然有效。"""
    missing = [field for field in ("price", "sales") if row.get(field) is None]
    if len(str(row.get("title") or "").strip()) < 2:
        missing.append("title")
    if not str(row.get("shop_name") or "").strip():
        missing.append("shop_name")
    return missing


def run_platform(
    platform: str,
    config: AppConfig,
    *,
    date: str | None = None,
    keywords: Iterable[str] | None = None,
    max_pages: int | None = None,
    headless: bool | None = None,
    download_images: bool | None = None,
    limit_keywords: int | None = None,
    batch: int | str | None = None,
    browser_factory: BrowserFactory | None = None,
) -> RunRecord:
    if platform not in PLATFORM_NAMES:
        raise ConfigError(f"未知平台: {platform}")
    if not config.platform_enabled(platform):
        started = now_local(config.crawl.timezone)
        date = validate_date(date or today(config))
        logger, log_path = platform_logger(platform, date, config.log_root)
        stats = RunStats()
        stats.error(f"{platform} 未启用")
        record = _base_record(platform, config, started)
        return _finalize_record(
            record,
            stats,
            status="disabled",
            started_at=started,
            config=config,
            output_path=config.output_root / date / f"{platform}.jsonl",
            log_path=log_path,
            logger=logger,
        )
    date = validate_date(date or today(config))
    started_at = now_local(config.crawl.timezone)
    logger, log_path = platform_logger(platform, date, config.log_root)
    stats = RunStats()
    output_dir = config.output_root / date
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{platform}.jsonl"
    record = _base_record(platform, config, started_at)
    continuous = remaining_seconds() is not None
    previous_risk = read_risk_marker(config.state_root, platform, date)
    if previous_risk is not None and not continuous:
        stats.warning(
            f"{date} 该平台已因风控熔断，本次跳过: {previous_risk.get('reason', '')}"
        )
        record.risk_blocked = True
        record.screenshot_path = previous_risk.get("screenshot_path")
        output_path.touch(exist_ok=True)
        return _finalize_record(
            record,
            stats,
            status="risk_blocked",
            started_at=started_at,
            config=config,
            output_path=output_path,
            log_path=log_path,
            logger=logger,
        )
    browser = (browser_factory or _default_browser_factory)(config, platform, headless)
    writer: JsonlWriter | None = None
    progress: SearchProgress | None = None
    status = "ok"
    seen: set[tuple[str, str, str | None]] = set()
    try:
        try:
            browser.start()
            record.browser_started = _browser_launched(browser)
        except BrowserUnavailable as exc:
            stats.error(str(exc))
            status = "error"
            output_path.touch(exist_ok=True)
            return _finalize_record(
                record,
                stats,
                status=status,
                started_at=started_at,
                config=config,
                output_path=output_path,
                log_path=log_path,
                logger=logger,
            )

        crawler = create_platform(
            platform,
            config,
            browser,
            throttle=Throttle(
                platform=platform,
                date=date,
                state_dir=config.state_root,
                daily_max_pages=0,
                requests_per_minute=config.crawl.rate_limit_for(
                    platform
                ).requests_per_minute,
                page_interval=config.crawl.rate_limit_for(
                    platform
                ).page_interval_seconds,
                keyword_interval=config.crawl.rate_limit_for(
                    platform
                ).keyword_interval_seconds,
                detail_interval=config.crawl.rate_limit_for(
                    platform
                ).detail_interval_seconds,
            ),
            stats=stats,
            logger=logger,
            date=date,
        )
        logger.info(
            "run started platform=%s run_id=%s config_version=%s caliber=%s",
            platform,
            record.run_id,
            config.config_version,
            config.caliber_version,
        )

        # 有界面任务未登录时保留窗口，等待人工扫码授权。
        try:
            login_ok = check_task_login(crawler)
            if not login_ok:
                check_deadline()
        except DeadlineReached as exc:
            stats.warning(str(exc))
            status = "deadline"
            login_ok = False
        except DailyLimitExceeded as exc:
            stats.error(str(exc))
            status = "daily_limit"
            login_ok = False
        except RetryExhausted as exc:
            stats.error(f"登录检查网络失败: {exc}")
            status = "error"
            login_ok = False
        except Exception as exc:  # noqa: BLE001
            stats.error(f"登录检查异常: {type(exc).__name__}: {exc}")
            status = "error"
            login_ok = False
        record.login_ok = bool(login_ok)
        record.login_reason = crawler.last_login_reason
        record.screenshot_path = crawler.last_screenshot_path
        if not login_ok:
            if crawler.last_login_risk:
                record.risk_blocked = True
                marker = _write_risk_marker(
                    config,
                    platform,
                    date,
                    crawler.last_login_reason or "登录检查触发风控",
                    crawler.last_screenshot_path,
                    logger,
                )
                logger.error(
                    "risk marker written platform=%s path=%s", platform, marker
                )
                status = "risk_blocked" if status == "ok" else status
            elif status == "ok":
                status = "login_failed"
            # 保留空输出文件，后端可以按 run_logs 判断当天无数据。
            output_path.touch(exist_ok=True)
            return _finalize_record(
                record,
                stats,
                status=status,
                started_at=started_at,
                config=config,
                output_path=output_path,
                log_path=log_path,
                logger=logger,
            )

        crawler.continuous = continuous
        crawler.complete_fields = (
            continuous or os.environ.get("CRAWLER_AMOUNT_DETAIL") == "1"
        )
        crawler.defer_after_search = continuous
        matrix = build_keyword_matrix(config, keywords)
        if platform == "taobao" and batch is not None:
            logger.warning(
                "旧 --batch=%s 仅保留兼容；按完整关键词矩阵及逐页检查点续采",
                batch,
            )
        # 旧数量参数仅保留调用兼容；任务中的关键词全部执行。
        if not matrix:
            stats.error("关键词矩阵为空")
            return _finalize_record(
                record,
                stats,
                status="error",
                started_at=started_at,
                config=config,
                output_path=None,
                log_path=log_path,
                logger=logger,
            )
        if platform == "taobao" and record.batch_index is not None:
            record.batch_keywords = [str(entry["keyword"]) for entry in matrix]
        record.keywords = [str(entry["keyword"]) for entry in matrix]

        image_downloader: ImageDownloader | None = None
        want_images = (
            config.crawl.image.enabled if download_images is None else download_images
        )
        if want_images:
            image_downloader = ImageDownloader(
                config.output_root / date / "images",
                platform,
                max_bytes=config.crawl.image.max_bytes,
                timeout=config.crawl.image.timeout_seconds,
            )

        progress = SearchProgress(
            config.state_root, platform, matrix, continuous=continuous
        )
        crawler.search_progress = progress
        if continuous:
            logger.info(
                "定时持续采集：截止前持续翻页，搜索停止后统一补采和下载图片 platform=%s ends_at=%s",
                platform,
                os.environ.get("CRAWLER_ENDS_AT"),
            )
        writer = JsonlWriter(output_path, append=True)
        processed_keys = set()
        inline_detail = crawler.complete_fields
        if inline_detail:
            # 后端仅在此进程完成补采收尾后读取结果，不重复开启详情预算。
            record.detail_status = "inline"
            write_run_record(output_dir, record)
        attempted = progress.state.setdefault("detail_attempted", [])
        attempted_this_run: set[str] = set()
        shop_only_details: set[str] = set()
        detail_enabled = (
            inline_detail
            and config.crawl.detail.platform_enabled(platform)
            and (platform != "taobao" or config.crawl.taobao.detail.enabled)
        )
        detail_results = progress.state.setdefault("detail_results", {})
        # 旧检查点的失败尝试不应阻止重试；优先处理尚未尝试过的商品。
        from collections import Counter

        attempt_counts = Counter(attempted)

        def detail_reason(key):
            if key in crawler.detail_failures:
                return crawler.detail_failures[key]
            if not detail_enabled:
                return "详情补采未启用"
            return detail_results.get(key, {}).get(
                "reason",
                "等待详情补采" if status == "ok" else f"补采因平台状态 {status} 未完成",
            )

        def persist_items(items):
            progress.update_items([enrich_flags(item) for item in items])
            rows = [
                item_json_dict(enrich_flags(Item.model_validate(row)))
                for row in progress.rows
            ]
            pending = []
            if inline_detail:
                for row in rows:
                    key = f"{row['platform']}:{row['product_id']}"
                    missing = _inline_detail_missing_fields(row)
                    row["flags"] = [
                        flag
                        for flag in row["flags"]
                        if not flag.startswith("金额补采：")
                    ]
                    if missing and row.get("listed", True):
                        # 配置关闭也必须在结果中说明缺失，不能记为全部成功。
                        pending.append(key)
                        reason = detail_reason(key)
                        row["flags"].append(
                            f"金额补采：{reason}；缺少{','.join(missing)}，待核验"
                        )
                progress.state["detail_pending"] = list(dict.fromkeys(pending))
                progress.save()
            writer.sync_rows(rows)
            write_json_atomic(output_dir / f"{platform}.partial.json", rows)
            record.items_ok = len(progress.rows)

        def persist_patch(patch):
            if progress.detail_key(patch) in shop_only_details:
                # 仅为补店名访问详情时，不覆盖列表已有标题和金额依据。
                patch = ItemPatch.model_validate(
                    patch.model_dump(
                        include={
                            "platform",
                            "product_id",
                            "shop_id",
                            "shop_name",
                            "shop_id_raw",
                            "shop_label_raw",
                            "shop_type_raw_hint",
                            "listed",
                        }
                    )
                )
            if platform in ("taobao", "jd", "1688"):
                # 内联任务是补缺：详情中的组件标题、推荐图片和其他非空值
                # 不得覆盖列表中已经取得的商品字段。
                existing = [
                    item_json_dict(Item.model_validate(row))
                    for row in progress.rows
                    if row["platform"] == patch.platform
                    and row["product_id"] == patch.product_id
                ]
                exclude = set()
                for field in (
                    "title",
                    "main_image_url",
                    "price",
                    "sales",
                    "comments",
                    "shop_name",
                    "shop_id",
                ):
                    if any(row.get(field) not in (None, "") for row in existing):
                        exclude.add(field)
                        if field in ("price", "sales", "comments"):
                            exclude.add(field + "_raw")
                patch = ItemPatch.model_validate(patch.model_dump(exclude=exclude))
            progress.apply_detail_patch(patch)
            persist_items([])

        def download_item_image(item):
            if image_downloader is None or not item.main_image_url or item.image_path:
                return item
            image_path, image_error = image_downloader.download(
                item.main_image_url, item.product_id
            )
            if image_path:
                return item.model_copy(update={"image_path": image_path})
            logger.warning(
                "image download failed platform=%s product_id=%s error=%s",
                platform,
                item.product_id,
                image_error,
            )
            return item.model_copy(
                update={"flags": list(dict.fromkeys([*item.flags, "主图下载失败"]))}
            )

        def checkpoint(items):
            persist_items(items)
            if not detail_enabled:
                return
            for row in sorted(
                progress.rows,
                key=lambda row: (
                    # 京东先补齐店名；同级仍优先未尝试项，避免失败重试占满预算。
                    bool(platform == "jd" and str(row.get("shop_name") or "").strip()),
                    attempt_counts[f"{row['platform']}:{row['product_id']}"],
                ),
            ):
                item = Item.model_validate(row)
                key = progress.detail_key(item)
                if key in attempted_this_run or not item.listed:
                    continue
                # 旧检查点可能含有未校验的数值，补采须与最终保存口径一致。
                missing = _inline_detail_missing_fields(item_json_dict(item))
                if not missing:
                    continue
                attempted.append(key)
                attempted_this_run.add(key)
                attempt_counts[key] += 1
                if platform == "jd" and missing == ["shop_name"]:
                    shop_only_details.add(key)
                detail_results[key] = {"reason": "详情处理中或中断，可续采"}
                progress.save()
                detail_id = (
                    f"tmall:{item.product_id}"
                    if item.platform == "tmall"
                    else item.product_id
                )
                try:
                    with supplemental_work():
                        patches = crawler.fetch_detail([detail_id])
                    # fetch_detail的回调负责即时落盘；返回对象并不表示字段齐全。
                    detail_results[key] = {
                        "reason": "详情已解析但字段未补齐"
                        if patches
                        else "详情未返回有效补丁，获取或解析失败"
                    }
                except (
                    DeadlineReached,
                    RiskBlocked,
                    LoginRequired,
                    DailyLimitExceeded,
                ):
                    raise
                except Exception as exc:  # 单个详情失败不丢列表，也不重复消耗额度
                    detail_results[key] = {
                        "reason": f"本次详情请求最终失败（{type(exc).__name__}: {exc}）"
                    }
                    stats.warning(
                        f"金额详情补采失败 product_id={item.product_id}: {exc}"
                    )
                finally:
                    persist_items([])

        def finish_timed_collection():
            nonlocal status
            logger.info(
                "搜索阶段结束，开始补采收尾 platform=%s status=%s items=%s",
                platform,
                status,
                len(progress.rows),
            )
            # 安全停止后不再发起平台请求。到点仅停止搜索，补采仍受
            # 登录、风控、每日预算与有限重试约束。
            if status not in ("login_failed", "risk_blocked", "daily_limit"):
                try:
                    checkpoint([])
                    for entry in matrix:
                        restored = [
                            Item.model_validate(row)
                            for row in progress.rows
                            if row.get("search_keyword") == entry["keyword"]
                            and row.get("ip_code") == entry.get("ip_code")
                        ]
                        if restored:
                            with supplemental_work():
                                crawler.after_search(str(entry["keyword"]), restored)
                            persist_items(restored)
                except LoginRequired as exc:
                    record.login_ok = False
                    record.login_reason = exc.reason
                    status = "login_failed"
                    stats.error(str(exc))
                except RiskBlocked as exc:
                    record.risk_blocked = True
                    record.screenshot_path = (
                        exc.screenshot_path or record.screenshot_path
                    )
                    status = "risk_blocked"
                    stats.error(str(exc))
                    _write_risk_marker(
                        config,
                        platform,
                        date,
                        exc.reason,
                        record.screenshot_path,
                        logger,
                    )
                except DailyLimitExceeded as exc:
                    status = "daily_limit"
                    stats.warning(str(exc))
                except Exception as exc:  # noqa: BLE001 - 保留已落盘商品及补采原因
                    status = "error"
                    stats.error(f"补采收尾异常: {type(exc).__name__}: {exc}")
                    logger.exception("supplemental work failed platform=%s", platform)
            if image_downloader is not None and status not in (
                "login_failed",
                "risk_blocked",
                "daily_limit",
            ):
                for row in progress.rows:
                    item = Item.model_validate(row)
                    if item.main_image_url and not item.image_path:
                        persist_items([download_item_image(item)])

        crawler.checkpoint_patch = persist_patch
        persist_items([])
        if progress.resumed:
            stats.warning("已恢复采集进度：" + progress.describe())
            logger.info(
                "resume platform=%s %s file=%s",
                platform,
                progress.describe(),
                progress.path,
            )
        crawler.checkpoint_items = persist_items if continuous else checkpoint
        record.keywords_done = 0

        def search_entries():
            nonlocal status
            while True:
                for entry in matrix:
                    while True:
                        yield entry
                        if (
                            not continuous
                            or status != "ok"
                            or progress.position(
                                str(entry["keyword"]), entry.get("ip_code")
                            ).get("done")
                        ):
                            break
                if not continuous or status != "ok":
                    return
                try:
                    check_deadline()
                    # 空页/重复页仅结束本轮；轮间等待保留平台原有节奏。
                    delay = max(
                        1.0,
                        config.crawl.rate_limit_for(platform).keyword_interval_seconds[
                            0
                        ],
                    )
                    progress.state["phase"] = "waiting_next_round"
                    progress.save()
                    logger.info(
                        "本轮关键词已完成，等待后继续下一轮 platform=%s round=%s",
                        platform,
                        progress.state["round"],
                    )
                    time.sleep(min(delay, remaining_seconds() or 0))
                    check_deadline()
                except DeadlineReached as exc:
                    status = "deadline"
                    stats.warning(str(exc))
                    return
                progress.next_round()
                progress.state["phase"] = "searching"
                record.keywords_done = 0

        def wait_for_retry(reason):
            nonlocal status
            if not continuous:
                return False
            progress.state["phase"] = "waiting_retry"
            progress.state["retry_reason"] = reason
            progress.save()
            delay = max(
                5.0,
                min(
                    60.0,
                    config.crawl.rate_limit_for(platform).keyword_interval_seconds[0],
                ),
            )
            logger.warning(
                "暂时失败，保留当前页并等待重试 platform=%s: %s", platform, reason
            )
            time.sleep(min(delay, remaining_seconds() or 0))
            if remaining_seconds() == 0:
                status = "deadline"
                return False
            progress.state["phase"] = "searching"
            return True

        def wait_for_authorization(reason, *, risk=False):
            if not continuous:
                return False
            progress.state["phase"] = "waiting_authorization"
            progress.state["wait_reason"] = reason
            progress.save()
            crawler.last_login_reason = reason
            crawler.last_login_risk = risk
            restored = check_task_login(crawler, recheck=False)
            record.login_ok = restored
            record.login_reason = crawler.last_login_reason
            if restored:
                record.risk_blocked = False
                progress.state["phase"] = "searching"
            progress.save()
            return restored

        for entry in search_entries():
            keyword = str(entry["keyword"])
            ip_code = entry.get("ip_code")
            try:
                if progress.resumed and not continuous:
                    checkpoint([])
                    restored = [
                        Item.model_validate(row)
                        for row in progress.rows
                        if row.get("search_keyword") == keyword
                        and row.get("ip_code") == ip_code
                    ]
                    with supplemental_work():
                        crawler.after_search(keyword, restored)
                    persist_items(restored)
                if progress.position(keyword, ip_code).get("done"):
                    logger.info("resume skip completed keyword=%s", keyword)
                    record.keywords_done += 1
                    continue
                items = crawler.search(keyword, ip_code=ip_code)
            except DeadlineReached as exc:
                status = "deadline"
                stats.warning(str(exc))
                break
            except LoginRequired as exc:
                if wait_for_authorization(exc.reason):
                    continue
                record.login_ok = False
                record.login_reason = exc.reason
                status = "login_failed"
                stats.error(str(exc))
                logger.error("login required, stop platform url=%s", exc)
                break
            except RiskBlocked as exc:
                record.risk_blocked = True
                record.screenshot_path = exc.screenshot_path or record.screenshot_path
                status = "risk_blocked"
                stats.error(str(exc))
                marker = _write_risk_marker(
                    config,
                    platform,
                    date,
                    exc.reason,
                    record.screenshot_path,
                    logger,
                )
                logger.error(
                    "risk blocked, stop platform reason=%s screenshot=%s marker=%s",
                    exc.reason,
                    exc.screenshot_path,
                    marker,
                )
                break
            except DailyLimitExceeded as exc:
                status = "daily_limit"
                stats.warning(str(exc))
                logger.warning("daily limit reached: %s", exc)
                break
            except KeywordEncodingError as exc:
                stats.error(f"关键词编码失败 keyword={keyword!r}: {exc}")
                status = "error"
                break
            except RetryExhausted as exc:
                stats.error(f"关键词请求重试耗尽 keyword={keyword!r}: {exc}")
                if wait_for_retry(str(exc)):
                    continue
                status = "error" if status == "ok" else status
                break
            except Exception as exc:  # noqa: BLE001 - 保存失败页供重试
                stats.error(
                    f"关键词采集异常 keyword={keyword!r}: {type(exc).__name__}: {exc}"
                )
                logger.exception(
                    "keyword failed platform=%s keyword=%s", platform, keyword
                )
                if wait_for_retry(str(exc)):
                    continue
                status = "error" if status == "ok" else status
                break

            if not continuous or progress.position(keyword, ip_code).get("done"):
                record.keywords_done += 1
            for item in items:
                key = (item.platform, item.product_id, item.ip_code)
                if key in seen:
                    stats.items_duplicate += 1
                    continue
                seen.add(key)
                if not continuous:
                    item = download_item_image(item)
                progress.update_items([enrich_flags(item)], save=False)
                if (item.platform, item.product_id) not in processed_keys:
                    processed_keys.add((item.platform, item.product_id))
                else:
                    stats.items_duplicate += 1
            crawler.checkpoint_items([])

            if crawler.search_deadline_reached or remaining_seconds() == 0:
                status = "deadline"
                break

        if continuous:
            finish_timed_collection()

        record.items_ok = len(progress.rows)
        record.deadline_reached = (
            crawler.search_deadline_reached
            or status == "deadline"
            or remaining_seconds() == 0
        )
        # checkpoint 已同步等待每个详情及其有限重试结束。剩余缺失项是
        # 最终失败/配置额度受限，分开记录，不能保留为仍在排队或报全部成功。
        unresolved = list(progress.state.get("detail_pending", []))
        record.detail_failures = dict(crawler.detail_failures)
        for key in unresolved:
            record.detail_failures.setdefault(key, detail_reason(key))
        persist_items([])
        progress.state["detail_unresolved"] = list(record.detail_failures)
        progress.state["detail_pending"] = []
        progress.save()
        if record.detail_failures:
            stats.error(
                f"补采未完成 {len(record.detail_failures)} 条："
                + "；".join(
                    f"{key}: {reason}" for key, reason in record.detail_failures.items()
                )
            )
            if status in ("ok", "deadline"):
                status = "partial"
        elif record.deadline_reached and status == "ok":
            status = "deadline"
        if status != "ok":
            stats.error("采集进度已保存：" + progress.describe())
        if (
            status == "ok"
            and record.login_ok
            and record.keywords_done == len(matrix)
            and record.items_ok == 0
        ):
            stats.warning("所有关键词均未采到商品，检查选择器 / 登录 / 风控")
        return _finalize_record(
            record,
            stats,
            status=status,
            started_at=started_at,
            config=config,
            output_path=output_path,
            log_path=log_path,
            logger=logger,
        )
    except KeyboardInterrupt:
        status = "interrupted"
        raise
    except Exception as exc:  # noqa: BLE001 - 平台级兜底
        status = "error"
        stats.error(f"平台运行异常: {type(exc).__name__}: {exc}")
        logger.exception("platform run failed platform=%s", platform)
        return _finalize_record(
            record,
            stats,
            status="error",
            started_at=started_at,
            config=config,
            output_path=output_path,
            log_path=log_path,
            logger=logger,
        )
    finally:
        if progress is not None:
            progress.finish(status)
        if writer is not None:
            writer.close()
        try:
            browser.close()
        except Exception:
            pass


def run_platforms(
    platforms: Iterable[str],
    config: AppConfig,
    *,
    date: str | None = None,
    keywords: Iterable[str] | None = None,
    max_pages: int | None = None,
    headless: bool | None = None,
    download_images: bool | None = None,
    limit_keywords: int | None = None,
    batch: int | str | None = None,
    batch_sleep: Callable[[float], None] = time.sleep,
    browser_factory: BrowserFactory | None = None,
) -> list[RunRecord]:
    names: list[str] = []
    for name in platforms:
        if name == "all":
            names.extend(config.enabled_platforms())
        elif name in PLATFORM_NAMES:
            names.append(name)
        else:
            raise ConfigError(f"未知平台: {name}")
    names = list(dict.fromkeys(names))
    if not names:
        raise ConfigError("没有启用的平台")
    if batch is not None and names != ["taobao"]:
        raise ConfigError("--batch 只支持淘宝（避免改变京东/1688 采集节奏）")
    kwargs = dict(
        date=date,
        keywords=keywords,
        max_pages=max_pages,
        headless=headless,
        download_images=download_images,
        limit_keywords=limit_keywords,
        browser_factory=browser_factory,
    )
    if batch is not None:
        kwargs["batch"] = batch
    if (config.crawl.parallel_platforms or remaining_seconds() is not None) and len(
        names
    ) > 1:
        with ThreadPoolExecutor(max_workers=min(len(names), 3)) as pool:
            futures = [
                pool.submit(run_platform, name, config, **kwargs) for name in names
            ]
            return [future.result() for future in futures]
    return [run_platform(name, config, **kwargs) for name in names]


@supplemental_work()
def run_detail(
    platform: str,
    config: AppConfig,
    *,
    ids: Iterable[str],
    date: str | None = None,
    headless: bool | None = None,
    browser_factory: BrowserFactory | None = None,
) -> RunRecord:
    if platform not in PLATFORM_NAMES:
        raise ConfigError(f"未知平台: {platform}")
    date = validate_date(date or today(config))
    started_at = now_local(config.crawl.timezone)
    logger, log_path = platform_logger(platform, date, config.log_root)
    stats = RunStats()
    output_dir = config.output_root / date
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{platform}_detail.jsonl"
    record = _base_record(platform, config, started_at)
    if not config.crawl.detail.platform_enabled(platform):
        message = (
            f"{date} {platform} 详情补抓已按配置停用"
            f"（crawl.detail.platforms.{platform}=false），列表采集不受影响"
        )
        stats.warning(message)
        logger.warning(message)
        output_path.touch(exist_ok=True)
        return _finalize_record(
            record,
            stats,
            status="disabled",
            started_at=started_at,
            config=config,
            output_path=output_path,
            log_path=log_path,
            logger=logger,
        )
    previous_risk = read_risk_marker(config.state_root, platform, date)
    if previous_risk is not None:
        stats.warning(
            f"{date} 该平台已因风控熔断，详情页也跳过: {previous_risk.get('reason', '')}"
        )
        record.risk_blocked = True
        record.screenshot_path = previous_risk.get("screenshot_path")
        output_path.touch(exist_ok=True)
        return _finalize_record(
            record,
            stats,
            status="risk_blocked",
            started_at=started_at,
            config=config,
            output_path=output_path,
            log_path=log_path,
            logger=logger,
        )
    # 淘宝按业务条件筛选目标；对全部目标逐条补采，不截断数量。
    ids = _taobao_detail_candidates(config, platform, date, list(ids), logger, stats)
    if platform == "taobao" and not ids:
        stats.warning("淘宝详情候选为空（未达阈值或无候选），本次不打开详情页")
        output_path.touch(exist_ok=True)
        # 未检查登录，用 skipped 明确告知后端未发起详情请求。
        return _finalize_record(
            record,
            stats,
            status="skipped",
            started_at=started_at,
            config=config,
            output_path=output_path,
            log_path=log_path,
            logger=logger,
        )
    browser = (browser_factory or _default_browser_factory)(config, platform, headless)
    writer: JsonlWriter | None = None
    try:
        try:
            browser.start()
            record.browser_started = _browser_launched(browser)
        except BrowserUnavailable as exc:
            stats.error(str(exc))
            output_path.touch(exist_ok=True)
            return _finalize_record(
                record,
                stats,
                status="error",
                started_at=started_at,
                config=config,
                output_path=output_path,
                log_path=log_path,
                logger=logger,
            )
        crawler = create_platform(
            platform, config, browser, stats=stats, logger=logger, date=date
        )
        login_status = "ok"
        try:
            record.login_ok = check_task_login(crawler)
            check_deadline()
        except DeadlineReached as exc:
            stats.warning(str(exc))
            record.login_ok = False
            login_status = "deadline"
        except DailyLimitExceeded as exc:
            stats.warning(str(exc))
            record.login_ok = False
            login_status = "daily_limit"
        except RetryExhausted as exc:
            stats.error(f"登录检查网络失败: {exc}")
            record.login_ok = False
            login_status = "error"
        except Exception as exc:  # noqa: BLE001
            stats.error(f"登录检查异常: {type(exc).__name__}: {exc}")
            record.login_ok = False
            login_status = "error"
        record.login_reason = crawler.last_login_reason
        if not record.login_ok:
            record.risk_blocked = crawler.last_login_risk
            record.screenshot_path = crawler.last_screenshot_path
            output_path.touch(exist_ok=True)
            if login_status != "ok":
                status = login_status
            else:
                status = "risk_blocked" if crawler.last_login_risk else "login_failed"
            return _finalize_record(
                record,
                stats,
                status=status,
                started_at=started_at,
                config=config,
                output_path=output_path,
                log_path=log_path,
                logger=logger,
            )
        writer = JsonlWriter(output_path, append=True)

        def persist_patch(patch):
            if writer.write_patch(patch):
                record.items_ok += 1

        crawler.checkpoint_patch = persist_patch
        try:
            patches = crawler.fetch_detail(ids)
        except DeadlineReached as exc:
            stats.warning(str(exc))
            return _finalize_record(
                record,
                stats,
                status="deadline",
                started_at=started_at,
                config=config,
                output_path=output_path,
                log_path=log_path,
                logger=logger,
            )
        except LoginRequired as exc:
            record.login_ok = False
            record.login_reason = exc.reason
            stats.error(str(exc))
            return _finalize_record(
                record,
                stats,
                status="login_failed",
                started_at=started_at,
                config=config,
                output_path=output_path,
                log_path=log_path,
                logger=logger,
            )
        except RiskBlocked as exc:
            record.risk_blocked = True
            record.screenshot_path = exc.screenshot_path or record.screenshot_path
            stats.error(str(exc))
            _write_risk_marker(
                config, platform, date, exc.reason, record.screenshot_path, logger
            )
            return _finalize_record(
                record,
                stats,
                status="risk_blocked",
                started_at=started_at,
                config=config,
                output_path=output_path,
                log_path=log_path,
                logger=logger,
            )
        except DailyLimitExceeded as exc:
            stats.warning(str(exc))
            return _finalize_record(
                record,
                stats,
                status="daily_limit",
                started_at=started_at,
                config=config,
                output_path=output_path,
                log_path=log_path,
                logger=logger,
            )
        for patch in patches:
            if crawler.checkpoint_patch is not None:
                continue
            if writer.write_patch(patch):
                record.items_ok += 1
            else:
                stats.items_duplicate += 1
        record.detail_failures = dict(crawler.detail_failures)
        return _finalize_record(
            record,
            stats,
            status="partial" if record.detail_failures else "ok",
            started_at=started_at,
            config=config,
            output_path=output_path,
            log_path=log_path,
            logger=logger,
        )
    except KeyboardInterrupt:
        raise
    except Exception as exc:  # noqa: BLE001
        stats.error(f"详情运行异常: {type(exc).__name__}: {exc}")
        logger.exception("detail run failed platform=%s", platform)
        return _finalize_record(
            record,
            stats,
            status="error",
            started_at=started_at,
            config=config,
            output_path=output_path,
            log_path=log_path,
            logger=logger,
        )
    finally:
        if writer is not None:
            writer.close()
        try:
            browser.close()
        except Exception:
            pass


def records_to_json(records: Iterable[RunRecord]) -> str:
    from crawler.models import run_record_json_dict

    return "\n".join(
        json.dumps(run_record_json_dict(record), ensure_ascii=False)
        for record in records
    )
