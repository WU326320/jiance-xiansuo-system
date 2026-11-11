"""单表商品导出：十列、店铺/IP 分组、缩略图及金额小计。

商品金额直接取后端视图，仅对已导出的商品金额求和，不重算估算销量。
"""

from __future__ import annotations

import logging
import math
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.comments import Comment
from openpyxl.drawing.image import Image as XLImage
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from .columns import (
    CENTER_FIELDS,
    HYPERLINK_FIELDS,
    LINKS_SHEET,
    MONEY_FIELDS,
    REPORT_FIELDS,
    REPORT_HEADERS,
    REPORT_WIDTHS,
    WRAP_TEXT_FIELDS,
)
from .config import ExportSettings
from .errors import ExportDataError, ExportError, ExportWriteError
from .images import make_thumbnail

logger = logging.getLogger("lead_excel")

MAX_CELL_TEXT = 32_767
HEADER_HEIGHT = 30
THUMBNAIL_ROW_HEIGHT = 63  # 80px ≈ 60pt，留 3pt 余量

_HEADER_FILL = PatternFill("solid", fgColor="1F4E78")
_THIN = Side(style="thin", color="BFBFBF")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)
_HEADER_FONT = Font(name="微软雅黑", size=10, bold=True, color="FFFFFF")
_BODY_FONT = Font(name="微软雅黑", size=10)
_LINK_FONT = Font(name="微软雅黑", size=10, color="0563C1", underline="single")
_GROUP_FILLS = tuple(
    PatternFill("solid", fgColor=color)
    for color in ("E2F0D9", "DDEBF7", "FFF2CC", "E4DFEC", "FCE4D6", "DDEFEA")
)


@dataclass
class ExportStats:
    summary_count: int = 0
    link_count: int = 0
    image_count: int = 0
    missing_image_count: int = 0
    orphan_link_count: int = 0
    empty_hit_summary_count: int = 0
    empty_hit_link_count: int = 0
    non_http_link_count: int = 0
    image_warnings: list[str] = field(default_factory=list)

    def quality_warnings(self) -> list[str]:
        notes: list[str] = []
        if self.summary_count == 0:
            notes.append("本次没有待复核线索。")
        if self.orphan_link_count:
            notes.append(
                f"未匹配到汇总线索的链接 {self.orphan_link_count} 条，已跳过（请检查视图过滤条件）。"
            )
        if self.empty_hit_summary_count:
            notes.append(
                f"线索汇总命中原因摘要为空 {self.empty_hit_summary_count} 条（C10 要求非空，请检查后端视图）。"
            )
        if self.empty_hit_link_count:
            notes.append(
                f"链接明细命中原因为空 {self.empty_hit_link_count} 条（C10 要求非空，请检查后端视图）。"
            )
        if self.missing_image_count:
            notes.append(
                f"缩略图缺失/不可读 {self.missing_image_count} 条，对应单元格留空；"
                "商品链接仍保留。"
            )
        if self.non_http_link_count:
            notes.append(
                f"非 http(s) 链接 {self.non_http_link_count} 条，仅写入文本、未设为可点击超链接。"
            )
        for message in self.image_warnings[:5]:
            notes.append(f"缩略图：{message}")
        if len(self.image_warnings) > 5:
            notes.append(f"其余 {len(self.image_warnings) - 5} 条缩略图问题省略。")
        return notes


def _safe_text(value: object) -> str:
    text = str(value)
    text = ILLEGAL_CHARACTERS_RE.sub("", text)
    if len(text) > MAX_CELL_TEXT:
        text = text[: MAX_CELL_TEXT - 1] + "…"
    return text


def _as_number(value: object) -> float | int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        # NaN 写进 xlsx 会变成非法数字；按空值处理。
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value
    try:
        text = str(value).strip().replace(",", "")
        if not text:
            return None
        number = float(text)
        if not math.isfinite(number):
            return None
        return int(number) if number.is_integer() else number
    except (TypeError, ValueError):
        return None


def _is_http_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlparse(value.strip())
    except ValueError:
        return False
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)


def _write_text(cell, value: object) -> None:
    text = "" if value is None else _safe_text(value)
    cell.value = text
    # 防止以 = 开头的标题/店铺名被 Excel 当成公式（公式注入）。
    if text.startswith("="):
        cell.data_type = "s"


def _write_header(ws: Worksheet, headers: Sequence[str], fill: PatternFill) -> None:
    for col_idx, header in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col_idx, value=header)
        cell.font = _HEADER_FONT
        cell.fill = fill
        cell.alignment = Alignment(
            horizontal="center", vertical="center", wrap_text=True
        )
        cell.border = _BORDER
    ws.row_dimensions[1].height = HEADER_HEIGHT


def _set_widths(
    ws: Worksheet, widths: Mapping[str, float], fields: Sequence[str]
) -> None:
    for col_idx, field in enumerate(fields, start=1):
        width = widths.get(field)
        if width:
            ws.column_dimensions[get_column_letter(col_idx)].width = width


def _normal_key(value: object) -> str:
    return "".join(unicodedata.normalize("NFKC", str(value or "")).split()).casefold()


def _report_groups(summary_rows, link_rows):
    """沿用后端店铺身份；无法识别店铺的商品保持独立。"""
    summaries = {str(row.get("lead_id") or ""): row for row in summary_rows}
    shops: dict[tuple, dict[tuple, list[dict[str, Any]]]] = {}
    for index, link in enumerate(link_rows):
        summary = summaries.get(str(link.get("lead_id") or ""), {})
        row = dict(link)
        row["platform"] = link.get("platform") or summary.get("platform") or ""
        row["shop_name"] = link.get("shop_name") or summary.get("shop_name") or ""
        platform = _normal_key(row["platform"])
        platform_group = summary.get("platform_group") or (
            "taobao" if platform in {"taobao", "tmall"} else platform
        )
        shop_id = str(summary.get("shop_id") or link.get("shop_id") or "").strip()
        shop_key = str(summary.get("shop_key") or "").strip()
        if not _normal_key(row["shop_name"]):
            identity = ("unknown", index)
        elif shop_key:
            identity = ("key", platform_group, shop_key, shop_id)
        elif shop_id and platform_group:
            identity = ("id", platform_group, shop_id)
        elif link.get("lead_id"):
            identity = ("lead", str(link["lead_id"]))
        elif platform_group:
            identity = ("name", platform_group, _normal_key(row["shop_name"]))
        else:
            identity = ("unknown", index)
        ip_key = tuple(
            sorted(
                {
                    _normal_key(part)
                    for part in re.split(r"[、,，;；|]+", str(row.get("ip_code") or ""))
                    if _normal_key(part)
                }
            )
        )
        groups = shops.setdefault(identity, {})
        groups.setdefault(ip_key or ("unknown", index), []).append(row)
    return [group for groups in shops.values() for group in groups.values()]


def _total_record(rows, title):
    known = []
    for row in rows:
        value = row.get("amount")
        if value not in (None, ""):
            try:
                amount = Decimal(str(value).replace(",", ""))
                if amount.is_finite():
                    known.append(amount)
            except (InvalidOperation, ValueError):
                pass
    return {
        "title": f"{title}（{len(rows)}条）",
        "amount": sum(known, Decimal("0")) if known else None,
        "_total": True,
        "_missing_amounts": len(rows) - len(known),
    }


def _report_rows(summary_rows, link_rows):
    rows = []
    products = []
    color_index = 0
    for group in _report_groups(summary_rows, link_rows):
        fill = None
        if len(group) > 1:
            fill = _GROUP_FILLS[color_index % len(_GROUP_FILLS)]
            color_index += 1
        for row in group:
            row["_fill"] = fill
            rows.append(row)
            products.append(row)
        if len(group) > 1:
            subtotal = _total_record(group, "同IP小计")
            subtotal.update(
                platform="、".join(
                    dict.fromkeys(str(row["platform"]) for row in group)
                ),
                ip_code=group[0].get("ip_code"),
                shop_name=group[0].get("shop_name"),
                _fill=fill,
            )
            rows.append(subtotal)
    if products:
        total = _total_record(products, "总计")
        total["_fill"] = _HEADER_FILL
        total["_grand_total"] = True
        rows.append(total)
    return rows


def _write_links(
    ws: Worksheet,
    rows: Sequence[Mapping[str, Any]],
    *,
    settings: ExportSettings,
    db_dir: Path,
    stats: ExportStats,
) -> None:
    fields = list(REPORT_FIELDS)
    _write_header(ws, list(REPORT_HEADERS.values()), _HEADER_FILL)
    _set_widths(ws, REPORT_WIDTHS, fields)
    ws["J1"].comment = Comment(
        "商品行使用后端估算金额，销量和评论保留采集原文。小计、总计只累计商品行；"
        "缺失金额不按零计算。",
        "侵权线索监控",
    )

    thumb_col = fields.index("image_path") + 1
    thumb_letter = get_column_letter(thumb_col)

    for row_idx, record in enumerate(rows, start=2):
        thumbnail = None
        if str(record.get("image_path") or "").strip():
            thumbnail, reason = make_thumbnail(
                record.get("image_path"),
                base_dirs=settings.image_base_dirs,
                db_dir=db_dir,
                target_width=settings.thumbnail_width,
                max_height=settings.thumbnail_max_height,
            )
            if thumbnail is None:
                stats.missing_image_count += 1
                if reason:
                    stats.image_warnings.append(reason)

        for col_idx, field in enumerate(fields, start=1):
            cell = ws.cell(row=row_idx, column=col_idx)
            value = record.get(field)

            if field == "image_path":
                # 缩略图列不写路径文本，只嵌入图片；缺失则留空。
                cell.value = None
            elif field in HYPERLINK_FIELDS:
                text = "" if value is None else _safe_text(value).strip()
                cell.value = text
                if _is_http_url(text):
                    cell.hyperlink = text
                    cell.font = _LINK_FONT
                else:
                    # 非 http(s) 只写文本，并强制字符串，避免 = 开头被当公式。
                    _write_text(cell, text)
                    cell.font = _BODY_FONT
                    if text:
                        stats.non_http_link_count += 1
            elif field in MONEY_FIELDS:
                number = _as_number(value)
                if number is not None:
                    cell.value = number
                    cell.number_format = "0.00"
                    cell.font = _BODY_FONT
                else:
                    _write_text(cell, value)
                    cell.font = _BODY_FONT
            else:
                _write_text(cell, value)
                cell.font = _BODY_FONT

            cell.border = _BORDER
            cell.alignment = Alignment(
                horizontal="center" if field in CENTER_FIELDS else "left",
                vertical="center",
                wrap_text=field in WRAP_TEXT_FIELDS,
            )
            if record.get("_fill") is not None:
                cell.fill = record["_fill"]
            if record.get("_total"):
                cell.font = Font(
                    name="微软雅黑",
                    size=10,
                    bold=True,
                    color="FFFFFF" if record.get("_grand_total") else "1F4E78",
                )

        if record.get("_missing_amounts"):
            ws.cell(row=row_idx, column=10).comment = Comment(
                f"另有 {record['_missing_amounts']} 条商品金额缺失，未纳入本次合计。",
                "侵权线索监控",
            )
        elif not record.get("_total"):
            detail = record.get("amount_formula") or record.get("formula") or ""
            flags = record.get("missing_flags") or ""
            if detail or flags or record.get("amount") is None:
                ws.cell(row=row_idx, column=10).comment = Comment(
                    f"{detail or '金额待核验'}\n{flags}".strip(), "侵权线索监控"
                )

        if thumbnail is not None:
            xl_image = XLImage(thumbnail.stream)
            xl_image.width = thumbnail.width
            xl_image.height = thumbnail.height
            ws.add_image(xl_image, f"{thumb_letter}{row_idx}")
            # 行高按图片实际像素换算（1px≈0.75pt），避免竖图压到下一行。
            ws.row_dimensions[row_idx].height = max(
                THUMBNAIL_ROW_HEIGHT, round(thumbnail.height * 0.75) + 3
            )
            stats.image_count += 1
        else:
            ws.row_dimensions[row_idx].height = 20

    last_col = get_column_letter(len(fields))
    # 总计在筛选范围之外；小计名称与商品标题清楚区分。
    last_row = max(1, len(rows))
    ws.auto_filter.ref = f"A1:{last_col}{last_row}"
    ws.freeze_panes = "A2"
    ws.sheet_properties.tabColor = "2E75B6"
    ws.print_title_rows = "1:1"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.sheet_properties.pageSetUpPr.fitToPage = True


def write_workbook(
    summary_rows: Sequence[Mapping[str, Any]],
    link_rows: Sequence[Mapping[str, Any]],
    *,
    snapshot: date,
    output_path: Path,
    settings: ExportSettings,
    versions: Mapping[str, str],
    db_dir: Path,
    stats: ExportStats | None = None,
) -> tuple[Workbook, ExportStats]:
    wb = Workbook()
    wb.remove(wb.worksheets[0])
    links_ws = wb.create_sheet(LINKS_SHEET)

    stats = stats or ExportStats()
    stats.summary_count = len(summary_rows)
    stats.link_count = len(link_rows)
    stats.empty_hit_summary_count = sum(
        1
        for row in summary_rows
        if not str(row.get("hit_reasons_summary") or "").strip()
    )
    stats.empty_hit_link_count = sum(
        1 for row in link_rows if not str(row.get("hit_reasons") or "").strip()
    )

    _write_links(
        links_ws,
        _report_rows(summary_rows, link_rows),
        settings=settings,
        db_dir=db_dir,
        stats=stats,
    )
    return wb, stats


def save_workbook_atomic(wb: Workbook, target: Path) -> None:
    """先写同目录临时文件，再 os.replace 覆盖，避免半成品 xlsx。"""

    if target.exists() and target.is_dir():
        raise ExportDataError(f"输出路径是目录而不是文件：{target}")

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ExportWriteError(f"无法创建导出目录 {target.parent}：{exc}") from exc

    try:
        fd, temp_name = tempfile.mkstemp(
            prefix=".lead_excel_", suffix=".xlsx", dir=str(target.parent)
        )
    except OSError as exc:
        raise ExportWriteError(f"无法在 {target.parent} 创建临时文件：{exc}") from exc
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        wb.save(temp_path)
        os.replace(temp_path, target)
    except ExportError:
        raise
    except PermissionError as exc:
        raise ExportWriteError(
            f"无法覆盖 {target}：文件可能正在 Excel 中打开，请关闭后重试。"
        ) from exc
    except OSError as exc:
        raise ExportWriteError(f"写入 Excel 失败（{target}）：{exc}") from exc
    except Exception as exc:  # noqa: BLE001 - openpyxl 内部异常也转成明确导出异常
        raise ExportWriteError(
            f"写入 Excel 失败（{target}，{type(exc).__name__}）：{exc}"
        ) from exc
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                logger.warning("临时文件清理失败：%s", temp_path)
