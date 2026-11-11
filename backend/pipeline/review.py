"""人工复核回写（后端方案第 5 节）。

只认「已确认 / 驳回」，其他值跳过并返回给窗口提示；每次状态变化都追加
``lead_status_history``，历史只增不改。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from openpyxl import load_workbook
from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.aggregate import (
    STATUS_CONFIRMED,
    STATUS_REJECTED,
    STATUS_SAAS,
)
from pipeline.models import Lead, LeadStatusHistory, now_local

VALID_RESULTS = (STATUS_CONFIRMED, STATUS_REJECTED)


@dataclass
class ReviewStats:
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    missing: int = 0
    errors: list[str] = field(default_factory=list)
    skipped_rows: list[tuple[int, str]] = field(default_factory=list)


def _find_headers(sheet) -> dict[str, int]:
    header = {}
    for index, cell in enumerate(sheet[1], start=0):
        value = str(cell.value or "").strip()
        if value:
            header[value] = index
    return header


def import_review_excel(
    session: Session,
    excel_path: str | Path,
    *,
    operator_fallback: str = "Excel复核",
) -> ReviewStats:
    path = Path(excel_path)
    if not path.exists():
        raise FileNotFoundError(f"Excel 不存在: {path}")
    workbook = load_workbook(path, data_only=True, read_only=True)
    try:
        if "线索汇总" not in workbook.sheetnames:
            raise ValueError("Excel 缺少「线索汇总」工作表")
        sheet = workbook["线索汇总"]
        headers = _find_headers(sheet)
        for required in ("线索编号", "复核结论", "复核人"):
            if required not in headers:
                raise ValueError(f"「线索汇总」缺少列: {required}")
        stats = ReviewStats()
        now = now_local()

        def cell(row, name: str):
            index = headers[name]
            return row[index] if index < len(row) else None

        for row_number, row in enumerate(sheet.iter_rows(min_row=2, values_only=True), start=2):
            lead_no = str(cell(row, "线索编号") or "").strip()
            result = str(cell(row, "复核结论") or "").strip()
            reviewer = str(cell(row, "复核人") or "").strip()
            if not lead_no:
                continue
            if not result:
                continue
            if result not in VALID_RESULTS:
                stats.skipped += 1
                stats.skipped_rows.append((row_number, f"非法复核结论: {result}"))
                continue
            lead = session.scalar(select(Lead).where(Lead.lead_no == lead_no))
            if lead is None:
                stats.missing += 1
                stats.errors.append(f"第 {row_number} 行线索不存在: {lead_no}")
                continue
            if lead.status == STATUS_SAAS:
                stats.skipped += 1
                stats.skipped_rows.append((row_number, f"{lead_no} 已录入SaaS，不覆盖"))
                continue
            old_status = lead.status
            if old_status == result and (lead.reviewer or "") == (reviewer or lead.reviewer or ""):
                stats.unchanged += 1
                continue
            lead.status = result
            lead.review_result = result
            lead.reviewer = reviewer or lead.reviewer or operator_fallback
            lead.reviewed_at = now
            session.add(
                LeadStatusHistory(
                    lead_id=lead.id,
                    old_status=old_status,
                    new_status=result,
                    operator=reviewer or operator_fallback,
                    changed_at=now,
                    note=f"review import: {path.name}",
                )
            )
            stats.updated += 1
        session.flush()
        return stats
    finally:
        workbook.close()


@dataclass
class MarkSaasStats:
    updated: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


def mark_saas(
    session: Session, lead_nos: Iterable[str], *, operator: str = "review.bat"
) -> MarkSaasStats:
    stats = MarkSaasStats()
    now = now_local()
    for raw in lead_nos:
        lead_no = str(raw).strip()
        if not lead_no:
            continue
        lead = session.scalar(select(Lead).where(Lead.lead_no == lead_no))
        if lead is None:
            stats.missing.append(lead_no)
            continue
        if lead.status == STATUS_SAAS:
            stats.skipped.append((lead_no, "已经是已录入SaaS"))
            continue
        if lead.status == STATUS_REJECTED:
            stats.skipped.append((lead_no, "驳回线索不标记已录入SaaS"))
            continue
        old_status = lead.status
        lead.status = STATUS_SAAS
        lead.saas_entered_at = now
        if lead.reviewed_at is None:
            lead.reviewed_at = now
        session.add(
            LeadStatusHistory(
                lead_id=lead.id,
                old_status=old_status,
                new_status=STATUS_SAAS,
                operator=operator,
                changed_at=now,
                note="review mark-saas",
            )
        )
        stats.updated.append(lead_no)
    session.flush()
    return stats


__all__ = [
    "MarkSaasStats",
    "ReviewStats",
    "import_review_excel",
    "mark_saas",
]
