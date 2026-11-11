"""侵权线索 Excel 导出模块（openpyxl + Pillow）。

公开接口::

    from lead_excel import export_leads_excel
    path = export_leads_excel("data/leads.db", "2026-11-05", rules_config)
"""

from __future__ import annotations

from .api import export_leads_excel
from .errors import (
    ExportConfigError,
    ExportDataError,
    ExportDatabaseError,
    ExportError,
    ExportWriteError,
)
from .workbook import ExportStats

__all__ = [
    "export_leads_excel",
    "ExportStats",
    "ExportError",
    "ExportConfigError",
    "ExportDatabaseError",
    "ExportDataError",
    "ExportWriteError",
]

__version__ = "0.1.0"
