"""公开接口：export_leads_excel(db_path, snapshot_date, config) -> 文件绝对路径。"""

from __future__ import annotations

import logging
import os
from datetime import date
from typing import Any, Mapping, Sequence

from .columns import (
    LINKS_VIEW,
    SHOP_TYPE_RANK,
    SUMMARY_VIEW,
)
from .config import (
    ExportSettings,
    parse_snapshot_date,
    resolve_db_path,
    resolve_settings,
)
from .db import connect_readonly, read_view
from .errors import (
    ExportConfigError,
    ExportDataError,
    ExportDatabaseError,
    ExportError,
    ExportWriteError,
)
from .workbook import ExportStats, save_workbook_atomic, write_workbook

logger = logging.getLogger("lead_excel")

__all__ = [
    "export_leads_excel",
    "ExportError",
    "ExportConfigError",
    "ExportDatabaseError",
    "ExportDataError",
    "ExportWriteError",
    "ExportStats",
]


def _number_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return None if value != value else float(value)
    try:
        number = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return None if number != number else number


def _sort_summary(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """店铺类型优先级 → 置信度降序 → 店铺合计金额降序。

    排序在导出侧再做一次，不依赖视图是否已 ORDER BY；排序不改变任何金额。
    """

    def key(record: Mapping[str, Any]):
        # 视图按 shop_type_rank 排序（企业1/个体户2/未知3/个人4）；
        # 视图缺失该辅助列时再按 shop_type 映射兜底。
        rank_value = _number_or_none(record.get("shop_type_rank"))
        if rank_value is None:
            shop_type = str(record.get("shop_type") or "").strip()
            rank = SHOP_TYPE_RANK.get(shop_type, 5)
        else:
            rank = int(rank_value)
        confidence = _number_or_none(record.get("confidence"))
        amount = _number_or_none(record.get("shop_amount"))
        return (
            rank,
            -confidence if confidence is not None else float("inf"),
            -amount if amount is not None else float("inf"),
            str(record.get("lead_id") or ""),
        )

    return [dict(row) for row in sorted(rows, key=key)]


def _order_links(
    summary_rows: Sequence[Mapping[str, Any]],
    link_rows: Sequence[Mapping[str, Any]],
    stats: ExportStats,
) -> list[dict[str, Any]]:
    """按汇总线索顺序排列；同线索内金额降序。

    ``lead_id`` 为空的链接（店名为空 / 当日未在架）只进「链接明细」，
    按金额降序追加在正式线索链接之后。
    """

    lead_order: dict[str, int] = {}
    for index, record in enumerate(summary_rows):
        lead_id = str(record.get("lead_id") or "").strip()
        if not lead_id:
            raise ExportDataError("线索汇总存在空的 lead_id，无法与链接明细关联。")
        if lead_id in lead_order:
            raise ExportDataError(f"线索汇总存在重复 lead_id：{lead_id}。")
        lead_order[lead_id] = index

    grouped: dict[str, list[dict[str, Any]]] = {lead_id: [] for lead_id in lead_order}
    standalone: list[dict[str, Any]] = []
    for record in link_rows:
        lead_id = str(record.get("lead_id") or "").strip()
        if not lead_id:
            standalone.append(dict(record))
            continue
        if lead_id not in grouped:
            stats.orphan_link_count += 1
            logger.warning("链接明细 lead_id=%s 不在本次汇总中，已跳过", lead_id)
            continue
        grouped[lead_id].append(dict(record))

    def link_key(record: Mapping[str, Any]):
        amount = _number_or_none(record.get("amount"))
        return (
            -amount if amount is not None else float("inf"),
            str(record.get("product_id") or ""),
        )

    for rows in grouped.values():
        rows.sort(key=link_key)
    standalone.sort(key=link_key)

    ordered: list[dict[str, Any]] = []
    for lead_id in sorted(grouped, key=lambda item: lead_order[item]):
        ordered.extend(grouped[lead_id])
    ordered.extend(standalone)
    return ordered


def _dedupe_link_rows(
    rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str, str, str]] = set()
    result: list[dict[str, Any]] = []
    for row in rows:
        key = (
            str(row.get("lead_id") or "").strip(),
            str(row.get("product_id") or "").strip(),
            str(row.get("url") or "").strip(),
            str(row.get("platform") or "").strip(),
            str(row.get("ip_code") or "").strip(),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(dict(row))
    return result


def _resolve_versions(
    summary_rows: Sequence[Mapping[str, Any]],
    settings: ExportSettings,
) -> dict[str, str]:
    """口径说明使用视图里的版本列（首行非空值）；视图无数据时才回落配置。"""

    versions: dict[str, str] = {}
    for key in ("caliber_version", "config_version"):
        from_view = ""
        for row in summary_rows:
            value = row.get(key)
            if value is not None and str(value).strip():
                from_view = str(value).strip()
                break
        configured = getattr(settings, key, "") or ""
        versions[key] = str(from_view or configured or "未提供")
    return versions


def _export_leads_excel(
    db_path: str | os.PathLike[str] | None = None,
    snapshot_date: str | date | None = None,
    config: Mapping[str, Any] | None = None,
) -> str:
    """导出一条日期快照的待复核线索 Excel（内部实现）。

    Parameters
    ----------
    db_path:
        SQLite 数据库路径。省略/为空时读取配置 ``storage.db_path``，
        默认 ``data/leads.db``；相对路径按当前工作目录或 ``storage.base_dir`` 解析。
    snapshot_date:
        ``YYYY-MM-DD`` / ``YYYYMMDD``，用于视图过滤和文件名。
    config:
        完整 rules.yaml dict（含 ``export`` 段）或仅 export 段 dict。

    Returns
    -------
    str
        生成的 xlsx 绝对路径。同日重跑覆盖同名文件。

    Raises
    ------
    ExportConfigError / ExportDatabaseError / ExportDataError
        均为 ``ExportError`` 子类；主流程应把异常摘要写入 run_logs。
    """

    snapshot = parse_snapshot_date(snapshot_date)
    settings = resolve_settings(config)
    database = resolve_db_path(db_path, config)
    output_path = settings.output_path(snapshot)

    logger.info(
        "开始导出 Excel：db=%s snapshot=%s out=%s", database, snapshot, output_path
    )

    summary_rows: list[dict[str, Any]] = []
    ordered_links: list[dict[str, Any]] = []
    versions: dict[str, str] = {}
    stats = ExportStats()

    conn = connect_readonly(database)
    try:
        export_config = (config or {}).get("export", config or {})
        run_id = export_config.get("run_id")
        summary_view = read_view(conn, SUMMARY_VIEW, snapshot, run_id=run_id)
        links_view = read_view(conn, LINKS_VIEW, snapshot, run_id=run_id)
    finally:
        conn.close()

    summary_rows = _sort_summary(summary_view.rows)
    # 链接明细是一张表：正式线索链接在前，店名为空 / 未在架链接在后。
    ordered_links = _order_links(
        summary_rows,
        _dedupe_link_rows(list(links_view.rows)),
        stats,
    )
    versions = _resolve_versions(summary_rows, settings)

    try:
        wb, stats = write_workbook(
            summary_rows,
            ordered_links,
            snapshot=snapshot,
            output_path=output_path,
            settings=settings,
            versions=versions,
            db_dir=database.parent,
            stats=stats,
        )
    except ExportError:
        raise
    except Exception as exc:  # noqa: BLE001 - 统一转成主流程可识别的导出异常
        raise ExportDataError(
            f"构建 Excel 工作簿失败（{type(exc).__name__}）：{exc}"
        ) from exc

    try:
        save_workbook_atomic(wb, output_path)
    except ExportError:
        raise
    except Exception as exc:  # noqa: BLE001 - 统一转成主流程可识别的导出异常
        raise ExportWriteError(
            f"写入 Excel 失败（{output_path}，{type(exc).__name__}）：{exc}"
        ) from exc
    absolute = str(output_path.resolve())

    logger.info(
        "导出完成：%s（线索 %d 条，链接 %d 条，缩略图 %d 张，缺失图片 %d 条）",
        absolute,
        stats.summary_count,
        stats.link_count,
        stats.image_count,
        stats.missing_image_count,
    )
    return absolute


def export_leads_excel(
    db_path: str | os.PathLike[str] | None = None,
    snapshot_date: str | date | None = None,
    config: Mapping[str, Any] | None = None,
) -> str:
    """导出一条日期快照的待复核线索 Excel。

    公开接口。``db_path`` 省略时读取配置 ``storage.db_path``（默认
    ``data/leads.db``）。所有失败都转换为 ``ExportError`` 子类，主流程可
    统一捕获后写入 ``run_logs.error_summary`` 并触发报警。
    """

    try:
        return _export_leads_excel(db_path, snapshot_date, config)
    except ExportError:
        raise
    except Exception as exc:  # noqa: BLE001 - 保证主流程能统一捕获导出失败
        raise ExportError(f"导出 Excel 失败（{type(exc).__name__}）：{exc}") from exc
