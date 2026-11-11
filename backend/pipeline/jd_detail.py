"""京东临界带详情补抓。

默认实现只读调用同级采集模块 ``../crawler`` 的公开 CLI：

    python -m crawler detail --platform jd --ids-file <临时文件> --date <日期>

采集模块把补抓结果写到 ``out/<date>/jd_detail.jsonl``；本模块读取其中的
``sales`` / ``comments`` 等字段，交给 :func:`apply_jd_detail_patches` 写回
快照，再重新判定/聚合。测试可以注入自己的 fetcher，不访问真实网站。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.config import Settings
from pipeline.crawler_python import crawler_python
from pipeline.models import Product, ProductSnapshot, Shop, now_local
from pipeline.scoring import score_snapshot
from pipeline.store import (
    dumps_json,
    loads_json,
    parse_decimal,
    parse_int,
    unique_list,
    upsert_shop,
)


@dataclass(frozen=True)
class JdDetailTarget:
    """一个落在 0.6–1.0 门槛带内的京东店铺。"""

    shop_id: str
    total_amount: Decimal
    product_ids: tuple[str, ...]

    def __str__(self) -> str:  # 方便日志/旧钩子打印
        return self.shop_id


JdDetailFetcher = Callable[[JdDetailTarget, date], Iterable[Any] | None]

# 评分/金额重新计算时由 score_snapshot 重新生成的派生标记；补抓后先清掉，
# 避免旧的「销量缺失」等标记压过详情页新值。
_DERIVED_AMOUNT_FLAGS = {
    "销量缺失",
    "评论缺失",
    "评论缺失,待核验",
    "评论数缺失-未校正",
    "价格缺失",
    "按下限估算",
    "待核验",
    "待人工补齐",
}


def _patch_value(patch: Any, key: str, default: Any = None) -> Any:
    if isinstance(patch, dict):
        return patch.get(key, default)
    return getattr(patch, key, default)


def _patch_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    return parse_int(value)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return rows
    for line in lines:
        text = line.strip()
        if not text:
            continue
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _read_jsonl_since(path: Path, offset: int) -> list[dict[str, Any]]:
    """只读取本次子进程新增的 JSONL 行，避免读同日旧运行残留数据。"""

    if not path.exists():
        return []
    try:
        size = path.stat().st_size
        start = offset if 0 <= offset <= size else 0
        with path.open("rb") as handle:
            handle.seek(start)
            payload = handle.read()
    except OSError:
        return []
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = payload.decode("utf-8", errors="replace")
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _parse_run_record(stdout: str | None) -> dict[str, Any] | None:
    """从子进程 stdout 末尾解析采集模块输出的 RunRecord JSON。"""

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


class DetailFetchError(RuntimeError):
    """有限补采失败，携带本次已落盘补丁，供调用方保留成功结果。"""

    def __init__(self, message: str, patches: list[dict[str, Any]]):
        super().__init__(message)
        self.patches = patches


class CrawlerJdDetailFetcher:
    """通过 ``python -m crawler detail`` 调用同级采集模块。"""

    def __init__(self, settings: Settings, *, logger: logging.Logger | None = None):
        self.settings = settings
        self.logger = logger or logging.getLogger("pipeline")

    def _output_path(self, run_date: date) -> Path:
        return (
            self.settings.crawler_output_dir / run_date.isoformat() / "jd_detail.jsonl"
        )

    def _environment(self) -> dict[str, str]:
        env = dict(os.environ)
        paths = [str(self.settings.crawler_dir), str(self.settings.root)]
        env["PYTHONPATH"] = os.pathsep.join([*paths, env.get("PYTHONPATH", "")]).strip(
            os.pathsep
        )
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        # 后端按 settings.crawler_output_dir 读取结果，强制采集端写到同一目录。
        env["CRAWLER_OUTPUT_DIR"] = str(self.settings.crawler_output_dir)
        # 补采不受整批时限影响，也不继承常规搜索截止时间。
        env.pop("CRAWLER_ENDS_AT", None)
        return env

    def __call__(self, target: JdDetailTarget, run_date: date) -> list[dict[str, Any]]:
        product_ids = [
            str(item).strip() for item in target.product_ids if str(item).strip()
        ]
        if not product_ids:
            return []
        if not self.settings.crawler_dir.is_dir():
            raise RuntimeError(f"采集模块目录不存在: {self.settings.crawler_dir}")

        timeout = None
        output_path = self._output_path(run_date)
        before_size = output_path.stat().st_size if output_path.exists() else 0
        wanted = set(product_ids)

        def current_patches():
            return [
                row
                for row in _read_jsonl_since(output_path, before_size)
                if str(row.get("platform") or "jd") == "jd"
                and str(row.get("product_id") or "") in wanted
            ]

        ids_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                suffix=".txt",
                delete=False,
                prefix="jd-detail-ids-",
            ) as handle:
                ids_path = Path(handle.name)
                handle.write("\n".join(product_ids))
                handle.write("\n")

            command = [
                crawler_python(self.settings),
                "-m",
                "crawler",
                "detail",
                "--platform",
                "jd",
                "--ids-file",
                str(ids_path),
                "--date",
                run_date.isoformat(),
                "--config",
                str(self.settings.crawler_config),
            ]
            self.logger.info(
                "启动京东详情补抓 shop=%s total=%s ids=%d",
                target.shop_id,
                target.total_amount,
                len(product_ids),
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
                self.logger.error("京东详情补抓超过独立超时，按补采失败报告")
                raise DetailFetchError(
                    "京东详情补抓超过独立超时", current_patches()
                ) from exc
            record = _parse_run_record(completed.stdout)
            status = str((record or {}).get("status") or "unknown")
            login_ok = (record or {}).get("login_ok")
            errors = _format_errors((record or {}).get("errors"))
            stderr = (completed.stderr or "").strip()
            failure = (
                completed.returncode != 0
                or record is None
                or status not in ("ok", "disabled")
                or login_ok is False
            )
            if failure:
                message = (
                    "京东详情补抓失败 "
                    f"shop={target.shop_id} "
                    f"exit={completed.returncode} "
                    f"status={status} "
                    f"login_ok={login_ok} "
                    f"errors={errors or '无'} "
                    f"stderr={stderr[-1000:] or '无'}"
                )
                self.logger.error("%s", message)
                raise DetailFetchError(message, current_patches())
            self.logger.info(
                "京东详情补抓进程完成 shop=%s status=%s", target.shop_id, status
            )
        finally:
            if ids_path is not None:
                try:
                    ids_path.unlink()
                except OSError:
                    pass

        rows = current_patches()
        self.logger.info(
            "京东详情补抓结果 shop=%s rows=%d file=%s",
            target.shop_id,
            len(rows),
            output_path,
        )
        return rows


def make_jd_detail_fetcher(
    settings: Settings, *, logger: logging.Logger | None = None
) -> JdDetailFetcher:
    """构造默认京东详情补抓器；仅在实际触发临界带时调用采集模块。"""

    return CrawlerJdDetailFetcher(settings, logger=logger)


def _clean_flags(flags: Iterable[Any], patch_flags: Iterable[Any]) -> list[str]:
    result = [str(flag) for flag in flags if str(flag) not in _DERIVED_AMOUNT_FLAGS]
    result.extend(str(flag) for flag in patch_flags if flag not in (None, ""))
    return unique_list(result)


def _resolve_detail_run_id(
    session: Session,
    *,
    snapshot_date: date,
    platforms: tuple[str, ...],
    run_id: str | None,
) -> str | None:
    """兼容单任务旧调用；同日同平台有多个任务时禁止猜测写回范围。"""
    if run_id is not None:
        return run_id
    runs = session.scalars(
        select(ProductSnapshot.run_id)
        .join(Product, Product.id == ProductSnapshot.product_id)
        .where(
            ProductSnapshot.snapshot_date == snapshot_date,
            Product.platform.in_(platforms),
        )
        .distinct()
        .limit(2)
    ).all()
    if len(runs) > 1:
        raise ValueError("同日存在多个任务，详情写回和评分必须指定 run_id")
    return runs[0] if runs else None


def apply_jd_detail_patches(
    session: Session,
    *,
    settings: Settings,
    patches: Iterable[Any] | None,
    target: JdDetailTarget,
    snapshot_date: date,
    run_id: str | None = None,
) -> int:
    """把详情 patch 的销量/评论/价格等字段写回目标店铺的快照。"""

    if not patches:
        return 0
    run_id = _resolve_detail_run_id(
        session, snapshot_date=snapshot_date, platforms=("jd",), run_id=run_id
    )
    wanted = set(target.product_ids)
    changed = 0
    for patch in patches:
        product_id = str(_patch_value(patch, "product_id") or "").strip()
        if _patch_value(patch, "platform") not in (None, "", "jd"):
            continue
        if not product_id or (wanted and product_id not in wanted):
            continue
        product = session.scalar(
            select(Product).where(
                Product.platform == "jd", Product.product_id == product_id
            )
        )
        if product is None:
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
        patch_flags = _patch_value(patch, "flags") or []
        if isinstance(patch_flags, str):
            patch_flags = [patch_flags]

        if title:
            product.title = str(title)
        shop_name = str(_patch_value(patch, "shop_name") or "").strip()
        if shop_name.lower() not in {
            "",
            "0",
            "null",
            "none",
            "nil",
            "undefined",
            "-",
            "--",
        }:
            product.shop_name = shop_name
            if product.shop_id:
                upsert_shop(
                    session,
                    platform=product.platform,
                    shop_id=product.shop_id,
                    shop_name=shop_name,
                    shop_type=product.shop_type,
                    shop_label_raw=product.shop_label_raw,
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
            snapshot.missing_flags = dumps_json(_clean_flags(old_flags, patch_flags))
            snapshot.crawled_at = now_local()
            changed += 1
    session.flush()
    return changed


def rescore_jd_targets(
    session: Session,
    *,
    settings: Settings,
    target: JdDetailTarget,
    snapshot_date: date,
    run_id: str | None = None,
) -> int:
    """补抓后重算目标链接的销量/评论金额，供重新聚合使用。"""

    if not target.product_ids:
        return 0
    run_id = _resolve_detail_run_id(
        session, snapshot_date=snapshot_date, platforms=("jd",), run_id=run_id
    )
    products = list(
        session.scalars(
            select(Product).where(
                Product.platform == "jd",
                Product.product_id.in_(list(target.product_ids)),
            )
        ).all()
    )
    scored = 0
    for product in products:
        snapshots = session.scalars(
            select(ProductSnapshot).where(
                ProductSnapshot.product_id == product.id,
                ProductSnapshot.snapshot_date == snapshot_date,
                ProductSnapshot.run_id == run_id,
            )
        ).all()
        if not snapshots:
            continue
        shop = (
            session.scalar(
                select(Shop).where(
                    Shop.platform == "jd", Shop.shop_id == product.shop_id
                )
            )
            if product.shop_id
            else None
        )
        for snapshot in snapshots:
            old_flags = loads_json(snapshot.missing_flags, [])
            snapshot.missing_flags = dumps_json(_clean_flags(old_flags, []))
            score_snapshot(
                session,
                settings=settings,
                snapshot=snapshot,
                product=product,
                shop=shop,
            )
            scored += 1
    session.flush()
    return scored


__all__ = [
    "CrawlerJdDetailFetcher",
    "JdDetailFetcher",
    "JdDetailTarget",
    "apply_jd_detail_patches",
    "make_jd_detail_fetcher",
    "rescore_jd_targets",
]
