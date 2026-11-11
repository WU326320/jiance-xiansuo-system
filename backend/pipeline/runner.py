"""一键运行编排：锁 → 备份 → 迁移 → 逐平台采集入库 → 判定 → 聚合 → 报警。"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import uuid
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pipeline import jd_detail as jd_detail_module
from pipeline.aggregate import (
    aggregate_snapshots,
    jd_near_band_targets,
    near_threshold_shops,
)
from pipeline.backup import backup_database
from pipeline.config import Settings
from pipeline.crawler_adapter import (
    CrawlerAdapter,
    FileCrawlerAdapter,
    SubprocessCrawlerAdapter,
)
from pipeline.db import (
    create_engine_for,
    ensure_schema,
    make_session_factory,
    session_scope,
)
from pipeline.excel_export import export_leads_for_run
from pipeline.ingest import PlatformData
from pipeline.jd_detail import (
    DetailFetchError,
    JdDetailFetcher,
    JdDetailTarget,
    apply_jd_detail_patches,
    rescore_jd_targets,
)
from pipeline.lock import AlreadyRunning, run_lock
from pipeline.logging_utils import StepLogger, setup_run_logger
from pipeline.models import (
    ConfigVersion,
    Lead,
    Product,
    ProductSnapshot,
    RunLog,
    Shop,
    now_local,
)
from pipeline.notify import send_completion_email, send_email
from pipeline.scoring import score_snapshot
from pipeline.store import upsert_many
from pipeline.taobao_detail import (
    TaobaoDetailFetcher,
    TaobaoDetailTarget,
    apply_taobao_detail_patches,
    make_taobao_detail_fetcher as _make_taobao_detail_fetcher,
    select_taobao_detail_targets,
)
from pipeline.whitelist import sync_whitelist


def make_jd_detail_fetcher(
    settings: Settings, *, logger: logging.Logger | None = None
) -> JdDetailFetcher:
    """默认入口/直接构造 runner 时的京东详情补抓器；测试可替换本函数。"""

    return jd_detail_module.make_jd_detail_fetcher(settings, logger=logger)


def make_taobao_detail_fetcher(
    settings: Settings, *, logger: logging.Logger | None = None
) -> TaobaoDetailFetcher:
    """默认入口/直接构造 runner 时的淘宝详情补抓器；测试可替换本函数。"""

    return _make_taobao_detail_fetcher(settings, logger=logger)


@dataclass
class PlatformSummary:
    platform: str
    login_ok: bool = False
    items: int = 0
    leads: int = 0
    status: str = "ok"
    error: str = ""
    duration_seconds: float = 0.0
    detail_inline: bool = False
    amount_complete: int = 0
    amount_missing: int = 0
    missing_fields: dict[str, int] = field(default_factory=dict)
    detail_failures: dict[str, str] = field(default_factory=dict)


@dataclass
class RunSummary:
    run_id: str
    run_date: date
    platforms: dict[str, PlatformSummary] = field(default_factory=dict)
    leads: int = 0
    backup: str | None = None
    excel: str | None = None
    excel_error: str | None = None
    alerted: bool = False
    dry_run: bool = False
    failed: bool = False
    deadline_reached: bool = False
    near_threshold: list[dict[str, object]] = field(default_factory=list)

    def lines(self) -> list[str]:
        lines = [f"运行编号：{self.run_id}", f"快照日期：{self.run_date.isoformat()}"]
        for name, item in self.platforms.items():
            login = "正常" if item.login_ok else "失败"
            lines.append(
                f"  {name}: 登录{login}，采集 {item.items} 条，线索 {item.leads} 条，"
                f"状态 {item.status}"
                + (f"，错误 {item.error[:120]}" if item.error else "")
            )
        lines.append(f"生成线索：{self.leads} 条")
        if self.near_threshold:
            lines.append(f"近门槛未过线店铺：{len(self.near_threshold)} 家")
            for row in self.near_threshold:
                lines.append(
                    f"  近门槛 {row['platform']}/{row['shop_id']} "
                    f"{row['shop_name'] or ''} 合计 {row['shop_amount']} "
                    f"链接 {row['link_count']}"
                )
        if self.excel_error:
            lines.append(f"Excel：导出失败 - {self.excel_error}")
        else:
            lines.append(f"Excel：{self.excel or '未启用（由 export 模块生成）'}")
        lines.append(f"完成通知：{'已发送' if self.alerted else '未发送'}")
        if self.backup:
            lines.append(f"备份：{self.backup}")
        if self.dry_run:
            lines.append("模式：dry-run（真实数据库未写入、未发邮件）")
        return lines


class PipelineRunner:
    def __init__(
        self,
        settings: Settings,
        *,
        adapter: CrawlerAdapter | None = None,
        logger: logging.Logger | None = None,
        exporter: Callable[[Session, Settings, date], str | None] | None = None,
        jd_detail_fetcher: JdDetailFetcher | None = None,
        taobao_detail_fetcher: TaobaoDetailFetcher | None = None,
        notify_email: str | None = None,
        ip_name: str | None = None,
    ):
        self.settings = settings
        self.logger = logger
        self.adapter = adapter
        self.exporter = exporter
        self.jd_detail_fetcher = jd_detail_fetcher
        self.taobao_detail_fetcher = taobao_detail_fetcher
        self.notify_email = notify_email
        self.ip_name = ip_name
        self._default_jd_detail_fetcher: JdDetailFetcher | None = None
        self._default_taobao_detail_fetcher: TaobaoDetailFetcher | None = None

    def _prepare_jd_detail_fetcher(self) -> JdDetailFetcher:
        if self.jd_detail_fetcher is not None:
            return self.jd_detail_fetcher
        if self._default_jd_detail_fetcher is None:
            self._default_jd_detail_fetcher = make_jd_detail_fetcher(
                self.settings, logger=self.logger
            )
        return self._default_jd_detail_fetcher

    def _prepare_taobao_detail_fetcher(self) -> TaobaoDetailFetcher:
        if self.taobao_detail_fetcher is not None:
            return self.taobao_detail_fetcher
        if self._default_taobao_detail_fetcher is None:
            self._default_taobao_detail_fetcher = make_taobao_detail_fetcher(
                self.settings, logger=self.logger
            )
        return self._default_taobao_detail_fetcher

    # ------------------------------------------------------------------
    def _prepare_adapter(self) -> CrawlerAdapter:
        if self.adapter is not None:
            return self.adapter
        return SubprocessCrawlerAdapter(self.settings, logger=self.logger)

    def _register_config(self, session: Session) -> None:
        exists = session.scalar(
            select(ConfigVersion).where(
                ConfigVersion.config_version == self.settings.config_version
            )
        )
        if exists is None:
            session.add(
                ConfigVersion(
                    config_version=self.settings.config_version,
                    caliber_version=self.settings.caliber_version,
                    yaml_text=self.settings.yaml_text,
                    first_used_at=now_local(),
                )
            )
            session.flush()

    def _log_platform(
        self,
        session: Session,
        *,
        run_id: str,
        platform: str,
        stage: str,
        login_ok: bool | None,
        items_count: int,
        error_summary: str,
        started_at: datetime,
        status: str,
    ) -> RunLog:
        row = RunLog(
            run_id=run_id,
            platform=platform,
            stage=stage,
            login_ok=login_ok,
            items_count=items_count,
            error_summary=error_summary[:4000] if error_summary else None,
            started_at=started_at,
            ended_at=now_local(),
            status=status,
        )
        session.add(row)
        session.flush()
        return row

    def _log_overall(
        self,
        session: Session,
        *,
        run_id: str,
        started_at: datetime,
        items_count: int,
        error_summary: str,
        status: str,
    ) -> RunLog:
        row = RunLog(
            run_id=run_id,
            platform=None,
            stage="overall",
            login_ok=None,
            items_count=items_count,
            error_summary=error_summary[:4000] if error_summary else None,
            started_at=started_at,
            ended_at=now_local(),
            status=status,
        )
        session.add(row)
        session.flush()
        return row

    def _log_export_failure(
        self,
        session: Session,
        *,
        run_id: str,
        started_at: datetime,
        error_summary: str,
    ) -> RunLog:
        """Excel 导出失败单独写一行 run_logs，供第 7 节报警读取。"""

        row = RunLog(
            run_id=run_id,
            platform=None,
            stage="export",
            login_ok=None,
            items_count=0,
            error_summary=error_summary[:4000],
            started_at=started_at,
            ended_at=now_local(),
            status="error",
        )
        session.add(row)
        session.flush()
        return row

    def _log_jd_detail_failure(
        self,
        session: Session,
        *,
        run_id: str,
        started_at: datetime,
        error_summary: str,
    ) -> RunLog:
        """京东详情补抓失败单独写一行 run_logs，并进入本次报警邮件。"""

        row = RunLog(
            run_id=run_id,
            platform=None,
            stage="jd_detail",
            login_ok=None,
            items_count=0,
            error_summary=error_summary[:4000],
            started_at=started_at,
            ended_at=now_local(),
            status="error",
        )
        session.add(row)
        session.flush()
        return row

    def _log_taobao_detail_failure(
        self,
        session: Session,
        *,
        run_id: str,
        started_at: datetime,
        error_summary: str,
    ) -> RunLog:
        """淘宝/天猫详情补抓失败写 run_logs，和导出失败一样进入报警。"""

        row = RunLog(
            run_id=run_id,
            platform=None,
            stage="taobao_detail",
            login_ok=None,
            items_count=0,
            error_summary=error_summary[:4000],
            started_at=started_at,
            ended_at=now_local(),
            status="error",
        )
        session.add(row)
        session.flush()
        return row

    def _score_platform(
        self,
        session: Session,
        *,
        platform: str,
        snapshots: Iterable[ProductSnapshot],
    ) -> int:
        seen: set[int] = set()
        scored = 0
        for snapshot in snapshots:
            if snapshot.id in seen:
                continue
            seen.add(snapshot.id)
            product = session.get(Product, snapshot.product_id)
            if product is None:
                continue
            shop = (
                session.scalar(
                    select(Shop).where(
                        Shop.platform == product.platform,
                        Shop.shop_id == product.shop_id,
                    )
                )
                if product.shop_id
                else None
            )
            score_snapshot(
                session,
                settings=self.settings,
                snapshot=snapshot,
                product=product,
                shop=shop,
            )
            scored += 1
        return scored

    def _complete_platform_details(
        self,
        session: Session,
        snapshots: Iterable[ProductSnapshot],
        summary: PlatformSummary,
        run_date: date,
        run_id: str,
    ) -> None:
        """旧采集输出/文件适配器在单平台入库后补采；逐页模式不再开新预算。"""
        if summary.detail_inline or summary.platform not in ("taobao", "jd"):
            return
        targets: list[tuple[TaobaoDetailTarget, ...] | JdDetailTarget] = []
        if summary.platform == "taobao":
            plan = select_taobao_detail_targets(
                session, settings=self.settings, snapshots=snapshots
            )
            targets = [plan.targets] if plan.targets else []
        else:
            targets.extend(
                jd_near_band_targets(
                    session,
                    settings=self.settings,
                    snapshots=snapshots,
                    snapshot_date=run_date,
                )
            )
        for target in targets:
            started = now_local()
            try:
                detail_error = None
                try:
                    if isinstance(target, tuple):
                        fetched = self._prepare_taobao_detail_fetcher()(
                            target, run_date
                        )
                    else:
                        fetched = self._prepare_jd_detail_fetcher()(target, run_date)
                except DetailFetchError as exc:
                    fetched = exc.patches
                    detail_error = exc
                patches = (
                    []
                    if fetched is None
                    else [fetched]
                    if isinstance(fetched, dict) or hasattr(fetched, "product_id")
                    else list(fetched)
                )
                if isinstance(target, tuple):
                    apply_taobao_detail_patches(
                        session,
                        settings=self.settings,
                        patches=patches,
                        targets=target,
                        snapshot_date=run_date,
                        run_id=run_id,
                    )
                else:
                    apply_jd_detail_patches(
                        session,
                        settings=self.settings,
                        patches=patches,
                        target=target,
                        snapshot_date=run_date,
                        run_id=run_id,
                    )
                    rescore_jd_targets(
                        session,
                        settings=self.settings,
                        target=target,
                        snapshot_date=run_date,
                        run_id=run_id,
                    )
                if detail_error is not None:
                    raise detail_error
            except Exception as exc:
                label = "淘宝/天猫" if summary.platform == "taobao" else "京东"
                message = f"{label}详情补抓失败 {target}: {type(exc).__name__}: {exc}"
                summary.error = "；".join(filter(None, [summary.error, message]))
                summary.status = "partial"
                log_failure = (
                    self._log_taobao_detail_failure
                    if summary.platform == "taobao"
                    else self._log_jd_detail_failure
                )
                log_failure(
                    session, run_id=run_id, started_at=started, error_summary=message
                )
                if self.logger:
                    self.logger.error(message)
        session.flush()

    def _run_platform(
        self,
        session: Session,
        adapter: CrawlerAdapter,
        *,
        platform: str,
        run_id: str,
        run_date: date,
        collected: Future | None = None,
    ) -> PlatformSummary:
        summary = PlatformSummary(platform=platform)
        started_at = now_local()
        step_started = datetime.now()
        try:
            data: PlatformData = (
                collected.result()
                if collected is not None
                else adapter.run(platform, run_date)
            )
            summary.login_ok = bool(data.login_ok)
            summary.status = data.status
            summary.error = data.error_summary
            summary.detail_inline = data.run_record.get("detail_status") == "inline"
            summary.detail_failures = dict(data.run_record.get("detail_failures") or {})
            if data.status == "disabled":
                summary.items = 0
                self._log_platform(
                    session,
                    run_id=run_id,
                    platform=platform,
                    stage="crawl",
                    login_ok=True,
                    items_count=0,
                    error_summary="平台未启用",
                    started_at=started_at,
                    status="disabled",
                )
                session.commit()
                return summary
            if data.status == "deadline" and not data.items:
                self._log_platform(
                    session,
                    run_id=run_id,
                    platform=platform,
                    stage="crawl",
                    login_ok=data.login_ok,
                    items_count=0,
                    error_summary="到达设定结束时间",
                    started_at=started_at,
                    status="deadline",
                )
                session.commit()
                return summary
            if not data.login_ok and not data.items:
                summary.items = 0
                summary.status = "login_failed"
                self._log_platform(
                    session,
                    run_id=run_id,
                    platform=platform,
                    stage="crawl",
                    login_ok=False,
                    items_count=0,
                    error_summary=data.error_summary or "登录态失效，已停止该平台",
                    started_at=started_at,
                    status="login_failed",
                )
                session.commit()
                return summary

            snapshots, dropped = upsert_many(
                session,
                platform=platform,
                items=data.items,
                settings=self.settings,
                run_id=run_id,
                snapshot_date=run_date,
            )
            scored = self._score_platform(
                session, platform=platform, snapshots=snapshots
            )
            summary.items = len(data.items)
            if not summary.items:
                # 采集模块直接写表、没有 JSONL 时，用当天该平台已存在的快照数做条数。
                # 淘宝通道同时承载天猫商品（platform=tmall），计数要合并。
                platform_values = (
                    ("taobao", "tmall") if platform == "taobao" else (platform,)
                )
                summary.items = int(
                    session.scalar(
                        select(func.count())
                        .select_from(ProductSnapshot)
                        .join(Product, Product.id == ProductSnapshot.product_id)
                        .where(
                            ProductSnapshot.snapshot_date == run_date,
                            ProductSnapshot.run_id == run_id,
                            Product.platform.in_(platform_values),
                        )
                    )
                    or 0
                )
            summary.status = (
                data.status
                if data.status
                in (
                    "ok",
                    "risk_blocked",
                    "error",
                    "deadline",
                    "timeout",
                    "daily_limit",
                    "login_failed",
                    "partial",
                )
                else "ok"
            )
            if dropped and not summary.error:
                summary.error = f"丢弃 {dropped} 条"
            # 列表已提交，详情失败不能回滚已采商品。
            session.commit()
            self._complete_platform_details(
                session, snapshots, summary, run_date, run_id
            )
            self._log_platform(
                session,
                run_id=run_id,
                platform=platform,
                stage="crawl",
                login_ok=True,
                items_count=summary.items,
                error_summary=summary.error,
                started_at=started_at,
                status=summary.status,
            )
            session.commit()
            self.logger.info(
                "平台入库 platform=%s items=%d snapshots_scored=%d dropped=%d",
                platform,
                summary.items,
                scored,
                dropped,
            )
        except Exception as exc:  # noqa: BLE001 - 单平台失败不影响其他平台
            session.rollback()
            summary.status = "error"
            summary.error = f"{type(exc).__name__}: {exc}"
            self.logger.exception("平台处理失败 platform=%s", platform)
            try:
                self._log_platform(
                    session,
                    run_id=run_id,
                    platform=platform,
                    stage="crawl",
                    login_ok=summary.login_ok,
                    items_count=0,
                    error_summary=summary.error,
                    started_at=started_at,
                    status="error",
                )
                session.commit()
            except Exception:  # pragma: no cover - 数据库整体不可用时由外层兜底
                session.rollback()
        finally:
            summary.duration_seconds = (datetime.now() - step_started).total_seconds()
        return summary

    # ------------------------------------------------------------------
    def run(
        self,
        *,
        platforms: Iterable[str] | None = None,
        run_date: date | str | None = None,
        dry_run: bool = False,
        allow_alert: bool = True,
        run_id: str | None = None,
    ) -> RunSummary:
        if isinstance(run_date, str):
            run_date = date.fromisoformat(run_date)
        run_date = run_date or date.today()
        run_id = run_id or f"{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}"
        names = list(platforms) if platforms else list(self.settings.platform_names)
        if not names:
            names = ["taobao", "jd", "1688"]

        temp_dir: str | None = None
        old_db_env = os.environ.get("IP_LEAD_DB_PATH")
        if dry_run:
            temp_dir = tempfile.mkdtemp(prefix="ip-lead-dry-run-")
            os.environ["IP_LEAD_DB_PATH"] = str(Path(temp_dir) / "leads.db")

        summary = RunSummary(run_id=run_id, run_date=run_date, dry_run=dry_run)
        try:
            with run_lock(
                self.settings.lock_path,
                command=f"pipeline run date={run_date.isoformat()} dry_run={dry_run}",
            ):
                self.logger = self.logger or setup_run_logger(self.settings, run_id)
                if not dry_run:
                    backup = backup_database(self.settings)
                    if backup is not None:
                        summary.backup = str(backup)
                        self.logger.info("运行前备份: %s", backup)
                ensure_schema(self.settings)
                engine = create_engine_for(self.settings)
                factory = make_session_factory(engine)
                adapter = self._prepare_adapter()
                started_at = now_local()
                total_items = 0
                errors: list[str] = []
                try:
                    with session_scope(factory) as session:
                        self._register_config(session)
                        sync_whitelist(
                            session,
                            self.settings,
                            logger=self.logger,
                            default_ip=self.settings.get("run.ip_code"),
                        )
                        session.commit()
                    # 线程仅等待各平台独立子进程；数据库 Session 始终在当前线程使用。
                    names = list(dict.fromkeys(names))
                    with ThreadPoolExecutor(max_workers=len(names)) as pool:
                        pending = {}
                        for platform in names:
                            if self.settings.deadline_reached:
                                summary.deadline_reached = True
                                summary.platforms[platform] = PlatformSummary(
                                    platform=platform, status="deadline"
                                )
                            else:
                                pending[
                                    pool.submit(adapter.run, platform, run_date)
                                ] = platform
                        for future in as_completed(pending):
                            platform = pending[future]
                            with StepLogger(
                                self.logger, f"platform.{platform}"
                            ) as step:
                                with session_scope(factory) as session:
                                    item_summary = self._run_platform(
                                        session,
                                        adapter,
                                        platform=platform,
                                        run_id=run_id,
                                        run_date=run_date,
                                        collected=future,
                                    )
                                step.count = item_summary.items
                            summary.platforms[platform] = item_summary
                            if self.settings.deadline_reached:
                                summary.deadline_reached = True
                            total_items += item_summary.items
                            if item_summary.status == "deadline":
                                summary.deadline_reached = True
                            elif item_summary.status not in ("ok", "disabled"):
                                errors.append(
                                    f"{platform}: {item_summary.error or item_summary.status}"
                                )

                    with StepLogger(self.logger, "aggregate") as step:
                        with session_scope(factory) as session:
                            eligible_platforms: list[str] = []
                            for name, item in summary.platforms.items():
                                if not (
                                    (
                                        item.login_ok
                                        and item.status in ("ok", "deadline")
                                    )
                                    or item.items > 0
                                ):
                                    continue
                                if name not in eligible_platforms:
                                    eligible_platforms.append(name)
                                # 淘宝通道的 JSONL 含天猫商品，聚合时必须纳入 tmall。
                                if (
                                    name == "taobao"
                                    and "tmall" not in eligible_platforms
                                ):
                                    eligible_platforms.append("tmall")
                            if eligible_platforms:
                                snapshot_query = (
                                    select(ProductSnapshot)
                                    .join(
                                        Product,
                                        Product.id == ProductSnapshot.product_id,
                                    )
                                    .where(
                                        ProductSnapshot.snapshot_date == run_date,
                                        ProductSnapshot.run_id == run_id,
                                        Product.platform.in_(eligible_platforms),
                                    )
                                )
                            else:
                                snapshot_query = select(ProductSnapshot).where(
                                    ProductSnapshot.id == -1
                                )
                            snapshots = list(session.scalars(snapshot_query).all())
                            # 采集模块直接写表时可能只写原始行；这里补跑尚未判定的快照。
                            for snapshot in snapshots:
                                if snapshot.final_score is not None:
                                    continue
                                product = session.get(Product, snapshot.product_id)
                                if product is None:
                                    continue
                                shop = (
                                    session.scalar(
                                        select(Shop).where(
                                            Shop.platform == product.platform,
                                            Shop.shop_id == product.shop_id,
                                        )
                                    )
                                    if product.shop_id
                                    else None
                                )
                                score_snapshot(
                                    session,
                                    settings=self.settings,
                                    snapshot=snapshot,
                                    product=product,
                                    shop=shop,
                                )
                            session.flush()
                            for snapshot in snapshots:
                                product = session.get(Product, snapshot.product_id)
                                if product is None:
                                    continue
                                channel = (
                                    "taobao"
                                    if product.platform == "tmall"
                                    else product.platform
                                )
                                item = summary.platforms[channel]
                                item.missing_fields.setdefault("title", 0)
                                if not str(product.title or "").strip():
                                    item.missing_fields["title"] += 1
                                item.missing_fields.setdefault("shop_name", 0)
                                if not str(product.shop_name or "").strip():
                                    item.missing_fields["shop_name"] += 1
                                if snapshot.amount is None:
                                    item.amount_missing += 1
                                else:
                                    item.amount_complete += 1
                                for key in ("price", "sales", "comments"):
                                    if getattr(snapshot, key) is None:
                                        item.missing_fields[key] = (
                                            item.missing_fields.get(key, 0) + 1
                                        )
                            for name, item in summary.platforms.items():
                                if item.missing_fields.get("title", 0):
                                    message = f"{name} 标题待补齐：{item.missing_fields['title']} 条"
                                    item.error = "；".join(
                                        filter(None, [item.error, message])
                                    )
                                    errors.append(message)
                                if item.missing_fields.get("shop_name", 0):
                                    message = f"{name} 店名待补齐：{item.missing_fields['shop_name']} 条"
                                    item.error = "；".join(
                                        filter(None, [item.error, message])
                                    )
                                    errors.append(message)
                                if item.amount_missing:
                                    message = f"{name} 金额数据不完整：可计算 {item.amount_complete} 条，待核验 {item.amount_missing} 条"
                                    item.error = "；".join(
                                        filter(None, [item.error, message])
                                    )
                                    errors.append(message)
                            produced = aggregate_snapshots(
                                session,
                                settings=self.settings,
                                snapshots=snapshots,
                                snapshot_date=run_date,
                                run_id=run_id,
                            )
                            step.count = len(produced)
                            summary.leads = len(produced)
                            for lead in produced:
                                if lead.platform in summary.platforms:
                                    summary.platforms[lead.platform].leads += 1
                                elif (
                                    lead.platform == "tmall"
                                    and "taobao" in summary.platforms
                                ):
                                    # 淘宝通道同时承载天猫商品，线索数归到该通道。
                                    summary.platforms["taobao"].leads += 1
                            summary.near_threshold = near_threshold_shops(
                                session,
                                settings=self.settings,
                                snapshots=snapshots,
                                snapshot_date=run_date,
                            )
                            if summary.near_threshold:
                                self.logger.info(
                                    "近门槛未过线店铺 %d 家: %s",
                                    len(summary.near_threshold),
                                    "；".join(
                                        f"{row['platform']}/{row['shop_id']}"
                                        f"={row['shop_amount']}"
                                        for row in summary.near_threshold
                                    ),
                                )
                            session.commit()

                    export_error: str | None = None
                    exporter = self.exporter
                    if exporter is None and self.settings.export_enabled:
                        exporter = export_leads_for_run
                    if exporter is not None:
                        export_started = now_local()
                        with StepLogger(self.logger, "export") as step:
                            try:
                                with session_scope(factory) as session:
                                    summary.excel = exporter(
                                        session, self.settings, run_date
                                    )
                                step.count = 1
                            except Exception as exc:  # noqa: BLE001 - 导出失败也必须留痕并报警
                                export_error = (
                                    f"Excel 导出失败: {type(exc).__name__}: {exc}"
                                )
                                summary.excel_error = export_error
                                errors.append(export_error)
                                self.logger.exception(
                                    "Excel 导出失败，已记 run_logs 并报警"
                                )
                                try:
                                    with session_scope(factory) as session:
                                        self._log_export_failure(
                                            session,
                                            run_id=run_id,
                                            started_at=export_started,
                                            error_summary=export_error,
                                        )
                                        session.commit()
                                except Exception:  # noqa: BLE001 - 数据库不可用时仍有整体异常兜底
                                    self.logger.exception(
                                        "写入 Excel 导出失败 run_logs 时出错，已忽略"
                                    )

                    with session_scope(factory) as session:
                        overall_status = "partial" if errors else "ok"
                        self._log_overall(
                            session,
                            run_id=run_id,
                            started_at=started_at,
                            items_count=total_items,
                            error_summary="；".join(errors),
                            status=overall_status,
                        )
                        session.commit()
                    summary.failed = bool(errors)

                    if allow_alert and not dry_run:
                        # 原来的「出现高风险才发预警」改为每轮结束都发一封完成通知；
                        # 成功、0 条线索、部分失败都发，异常中断由调用方/CLI 兜底。
                        ip_name = self.ip_name
                        if not ip_name:
                            configured = (
                                self.settings.ip_config(
                                    self.settings.get("run.ip_code")
                                )
                                or {}
                            )
                            ip_name = str(
                                configured.get("name")
                                or self.settings.get("run.ip_code")
                                or ""
                            )
                        with StepLogger(self.logger, "notify") as step:
                            try:
                                with session_scope(factory) as session:
                                    result = send_completion_email(
                                        self.settings,
                                        run_id=run_id,
                                        summary=summary,
                                        session=session,
                                        ip_name=ip_name,
                                        notify_email=self.notify_email,
                                        status="partial" if summary.failed else "ok",
                                        logger=self.logger,
                                    )
                                    summary.alerted = result.sent
                            except Exception:  # noqa: BLE001 - 通知失败不影响主流程
                                self.logger.exception("完成通知发送失败，已忽略")
                            step.count = 1 if summary.alerted else 0
                finally:
                    engine.dispose()
            return summary
        finally:
            if dry_run:
                if old_db_env is not None:
                    os.environ["IP_LEAD_DB_PATH"] = old_db_env
                else:
                    os.environ.pop("IP_LEAD_DB_PATH", None)
                if temp_dir:
                    shutil.rmtree(temp_dir, ignore_errors=True)


def exception_alert(
    settings: Settings, exc: BaseException, *, logger: logging.Logger
) -> bool:
    """未捕获异常也发一封报警邮件；发送失败只记日志。"""

    subject = f"{settings.smtp.get('subject_prefix', '[侵权线索]')} 运行异常"
    body = (
        "本次运行出现未捕获异常，已停止。\n\n"
        f"{type(exc).__name__}: {exc}\n\n"
        "请查看 logs/run_YYYYMMDD.log。"
    )
    return send_email(settings, subject=subject, body=body, logger=logger)


__all__ = ["PipelineRunner", "PlatformSummary", "RunSummary", "exception_alert"]
