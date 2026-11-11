"""淘宝/天猫详情按条件补抓（C4）。

采集层只负责 ``python -m crawler detail --platform taobao --ids-file ...``；
是否补抓由后端按 ``fetch.taobao.detail_min_text_score``（默认 35）与
``need_vision`` 决定。天猫商品按采集端约定写成 ``tmall:<product_id>``。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.config import Settings
from pipeline.crawler_python import crawler_python
from pipeline.jd_detail import (
    DetailFetchError,
    _clean_flags,
    _patch_int,
    _patch_value,
    _read_jsonl_since,
    _resolve_detail_run_id,
)
from pipeline.models import Product, ProductSnapshot, Shop, now_local
from pipeline.scoring import score_snapshot
from pipeline.shopkey import shop_name_missing
from pipeline.store import (
    dumps_json,
    loads_json,
    normalize_shop_id,
    normalize_shop_type,
    parse_decimal,
    upsert_shop,
)

TAOBAO_PLATFORMS = ("taobao", "tmall")


@dataclass(frozen=True)
class TaobaoDetailTarget:
    """一个需要补抓详情页的淘宝/天猫商品。"""

    platform: str
    product_id: str

    @property
    def detail_id(self) -> str:
        # 采集端约定：天猫详情页用 tmall:<id> 指定。
        if self.platform == "tmall":
            return f"tmall:{self.product_id}"
        return self.product_id

    def __str__(self) -> str:  # 方便日志
        return self.detail_id


@dataclass(frozen=True)
class TaobaoDetailPlan:
    targets: tuple[TaobaoDetailTarget, ...]
    scanned: int
    skipped: int


TaobaoDetailFetcher = Callable[
    [Iterable[TaobaoDetailTarget], date], Iterable[Any] | None
]


def _parse_run_record(stdout: str | None) -> dict[str, Any] | None:
    for line in reversed((stdout or "").splitlines()):
        text = line.strip()
        if not (text.startswith("{") and text.endswith("}")):
            continue
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("platform"):
            return value
    return None


def _format_errors(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return "；".join(str(item) for item in value if item)
    return str(value or "")


def select_taobao_detail_targets(
    session: Session,
    *,
    settings: Settings,
    snapshots: Iterable[ProductSnapshot],
) -> TaobaoDetailPlan:
    """按 C4 策略挑选详情补抓目标。

    只对 eligible 且未被硬排除、仍缺金额字段的商品补抓；
    缺失字段不再受详情文字评分门槛二次拦截，同一商品只触发一次。
    """

    targets: list[TaobaoDetailTarget] = []
    seen: set[tuple[str, str]] = set()
    scanned = 0
    skipped = 0
    for snapshot in snapshots:
        product = session.get(Product, snapshot.product_id)
        if product is None or product.platform not in TAOBAO_PLATFORMS:
            continue
        scanned += 1
        if (
            not snapshot.eligible
            or snapshot.exclusion_reason
            or snapshot.final_score is None
            or not snapshot.listed
        ):
            skipped += 1
            continue
        missing = any(
            getattr(snapshot, field) is None for field in ("price", "sales")
        ) or shop_name_missing(product.shop_name)
        if not missing:
            # 价格与销量齐全时不因评论缺失重复访问详情页。
            skipped += 1
            continue
        key = (product.platform, str(product.product_id))
        if key in seen:
            continue
        seen.add(key)
        targets.append(
            TaobaoDetailTarget(
                platform=product.platform, product_id=str(product.product_id)
            )
        )
    return TaobaoDetailPlan(targets=tuple(targets), scanned=scanned, skipped=skipped)


class CrawlerTaobaoDetailFetcher:
    """通过 ``python -m crawler detail --platform taobao`` 补抓评论等详情字段。"""

    def __init__(self, settings: Settings, *, logger: logging.Logger | None = None):
        self.settings = settings
        self.logger = logger or logging.getLogger("pipeline")

    def _output_path(self, run_date: date) -> Path:
        return (
            self.settings.crawler_output_dir
            / run_date.isoformat()
            / "taobao_detail.jsonl"
        )

    def _environment(self) -> dict[str, str]:
        env = dict(os.environ)
        paths = [str(self.settings.crawler_dir), str(self.settings.root)]
        env["PYTHONPATH"] = os.pathsep.join([*paths, env.get("PYTHONPATH", "")]).strip(
            os.pathsep
        )
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        env["CRAWLER_OUTPUT_DIR"] = str(self.settings.crawler_output_dir)
        env.pop("CRAWLER_ENDS_AT", None)
        return env

    def __call__(
        self, targets: Iterable[TaobaoDetailTarget], run_date: date
    ) -> list[dict[str, Any]]:
        target_list = list(targets)
        if not target_list:
            return []
        if not self.settings.crawler_dir.is_dir():
            raise RuntimeError(f"采集模块目录不存在: {self.settings.crawler_dir}")

        wanted = {item.product_id for item in target_list}
        detail_ids = [item.detail_id for item in target_list]
        timeout = None
        output_path = self._output_path(run_date)
        before_size = output_path.stat().st_size if output_path.exists() else 0

        def current_patches():
            return [
                row
                for row in _read_jsonl_since(output_path, before_size)
                if str(row.get("platform") or "taobao") in TAOBAO_PLATFORMS
                and str(row.get("product_id") or "") in wanted
            ]

        ids_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                suffix=".txt",
                delete=False,
                prefix="taobao-detail-ids-",
            ) as handle:
                ids_path = Path(handle.name)
                handle.write("\n".join(detail_ids))
                handle.write("\n")

            command = [
                crawler_python(self.settings),
                "-m",
                "crawler",
                "detail",
                "--platform",
                "taobao",
                "--ids-file",
                str(ids_path),
                "--date",
                run_date.isoformat(),
                "--config",
                str(self.settings.crawler_config),
            ]
            self.logger.info(
                "启动淘宝/天猫详情补抓 ids=%d tmall=%d",
                len(detail_ids),
                sum(1 for item in target_list if item.platform == "tmall"),
            )
            try:
                completed = subprocess.run(
                    command,
                    cwd=str(self.settings.crawler_dir),
                    env=self._environment(),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                self.logger.error("淘宝/天猫详情补抓超过独立超时，按补采失败报告")
                raise DetailFetchError(
                    "淘宝/天猫详情补抓超过独立超时", current_patches()
                ) from exc
            record = _parse_run_record(completed.stdout)
            status = str((record or {}).get("status") or "unknown")
            login_ok = (record or {}).get("login_ok")
            errors = _format_errors((record or {}).get("errors"))
            stderr = (completed.stderr or "").strip()
            if completed.returncode == 0 and status in ("disabled", "skipped"):
                self.logger.info("淘宝/天猫详情补抓已跳过 status=%s", status)
                return []
            if (
                completed.returncode != 0
                or record is None
                or status not in ("ok", "disabled")
                or login_ok is False
            ):
                message = (
                    "淘宝/天猫详情补抓失败 "
                    f"exit={completed.returncode} "
                    f"status={status} "
                    f"login_ok={login_ok} "
                    f"errors={errors or '无'} "
                    f"stderr={stderr[-1000:] or '无'}"
                )
                self.logger.error("%s", message)
                raise DetailFetchError(message, current_patches())
            self.logger.info(
                "淘宝/天猫详情补抓进程完成 status=%s ids=%d", status, len(detail_ids)
            )
        finally:
            if ids_path is not None:
                try:
                    ids_path.unlink()
                except OSError:
                    pass

        rows = current_patches()
        self.logger.info(
            "淘宝/天猫详情补抓结果 rows=%d file=%s", len(rows), output_path
        )
        return rows


def make_taobao_detail_fetcher(
    settings: Settings, *, logger: logging.Logger | None = None
) -> TaobaoDetailFetcher:
    return CrawlerTaobaoDetailFetcher(settings, logger=logger)


def _find_product(
    session: Session, *, platform: str, product_id: str
) -> Product | None:
    candidates: list[str] = []
    if platform in TAOBAO_PLATFORMS:
        candidates.append(platform)
    candidates.extend(item for item in TAOBAO_PLATFORMS if item not in candidates)
    for name in candidates:
        product = session.scalar(
            select(Product).where(
                Product.platform == name, Product.product_id == product_id
            )
        )
        if product is not None:
            return product
    return None


def apply_taobao_detail_patches(
    session: Session,
    *,
    settings: Settings,
    patches: Iterable[Any] | None,
    snapshot_date: date,
    targets: Iterable[TaobaoDetailTarget] | None = None,
    run_id: str | None = None,
) -> int:
    """把详情 patch 写回快照并立即重算金额/判定，返回变更条数。"""

    if not patches:
        return 0
    run_id = _resolve_detail_run_id(
        session, snapshot_date=snapshot_date, platforms=TAOBAO_PLATFORMS, run_id=run_id
    )
    wanted = {(item.platform, item.product_id) for item in (targets or [])}
    changed = 0
    for patch in patches:
        product_id = str(_patch_value(patch, "product_id") or "").strip()
        if not product_id:
            continue
        patch_platform = str(_patch_value(patch, "platform") or "").strip()
        if patch_platform and patch_platform not in TAOBAO_PLATFORMS:
            continue
        product = _find_product(session, platform=patch_platform, product_id=product_id)
        if product is None or product.platform not in TAOBAO_PLATFORMS:
            continue
        if wanted and (product.platform, product.product_id) not in wanted:
            continue
        snapshots = session.scalars(
            select(ProductSnapshot).where(
                ProductSnapshot.product_id == product.id,
                ProductSnapshot.snapshot_date == snapshot_date,
                ProductSnapshot.run_id == run_id,
            )
        ).all()
        if not snapshots:
            continue

        sales = _patch_value(patch, "sales")
        sales_raw = _patch_value(patch, "sales_raw")
        comments = _patch_value(patch, "comments")
        comments_raw = _patch_value(patch, "comments_raw")
        price = _patch_value(patch, "price")
        price_raw = _patch_value(patch, "price_raw")
        listed = _patch_value(patch, "listed")
        title = _patch_value(patch, "title")
        patch_shop_id = normalize_shop_id(_patch_value(patch, "shop_id"))
        patch_shop_name = _patch_value(patch, "shop_name")
        if patch_shop_name is not None:
            patch_shop_name = str(patch_shop_name).strip() or None
            if (patch_shop_name or "").lower() in {
                "0",
                "null",
                "none",
                "nil",
                "undefined",
                "-",
                "--",
            }:
                patch_shop_name = None
        patch_flags = _patch_value(patch, "flags") or []
        if isinstance(patch_flags, str):
            patch_flags = [patch_flags]

        if title:
            product.title = str(title)
        if patch_shop_name:
            product.shop_name = patch_shop_name
        shop_backfilled = bool(
            patch_shop_id and normalize_shop_id(product.shop_id) is None
        )
        if shop_backfilled:
            # 详情补抓到合法非零 shop_id 时写回 Product，后续聚合才能按店铺汇总。
            product.shop_id = patch_shop_id
            product.shop_id_missing = False
        shop = None
        if product.shop_id and (shop_backfilled or patch_shop_name):
            shop = upsert_shop(
                session,
                platform=product.platform,
                shop_id=product.shop_id,
                shop_name=patch_shop_name,
                shop_type=normalize_shop_type(
                    product.platform,
                    _patch_value(patch, "shop_type_raw_hint"),
                    _patch_value(patch, "shop_label_raw"),
                    settings,
                ),
                shop_label_raw=_patch_value(patch, "shop_label_raw"),
            )
        elif product.shop_id:
            shop = session.scalar(
                select(Shop).where(
                    Shop.platform == product.platform,
                    Shop.shop_id == product.shop_id,
                )
            )
        for snapshot in snapshots:
            from utils.commerce_numbers import count_field

            if sales_raw not in (None, ""):
                snapshot.sales_raw = str(sales_raw)
            if sales is not None or sales_raw not in (None, ""):
                snapshot.sales, _ = count_field(sales, sales_raw, field="sales")
                snapshot.sales_raw = (
                    str(sales_raw) if sales_raw not in (None, "") else str(sales)
                )
            if comments_raw not in (None, ""):
                snapshot.comments_raw = str(comments_raw)
            if comments is not None or comments_raw not in (None, ""):
                snapshot.comments, _ = count_field(
                    comments, comments_raw, field="comments"
                )
                snapshot.comments_raw = (
                    str(comments_raw)
                    if comments_raw not in (None, "")
                    else str(comments)
                )
            if price is not None or price_raw not in (None, ""):
                snapshot.price = parse_decimal(
                    price_raw if price_raw not in (None, "") else price
                )
                snapshot.price_raw = (
                    str(price_raw) if price_raw not in (None, "") else str(price)
                )
            if listed is not None:
                snapshot.listed = bool(listed)
            old_flags = loads_json(snapshot.missing_flags, [])
            if shop_backfilled:
                old_flags = [flag for flag in old_flags if flag != "店铺ID缺失"]
                patch_flags = [flag for flag in patch_flags if flag != "店铺ID缺失"]
            snapshot.missing_flags = dumps_json(_clean_flags(old_flags, patch_flags))
            snapshot.crawled_at = now_local()
            score_snapshot(
                session,
                settings=settings,
                snapshot=snapshot,
                product=product,
                shop=shop,
            )
            changed += 1
    session.flush()
    return changed


__all__ = [
    "CrawlerTaobaoDetailFetcher",
    "TAOBAO_PLATFORMS",
    "TaobaoDetailFetcher",
    "TaobaoDetailPlan",
    "TaobaoDetailTarget",
    "apply_taobao_detail_patches",
    "make_taobao_detail_fetcher",
    "select_taobao_detail_targets",
]
