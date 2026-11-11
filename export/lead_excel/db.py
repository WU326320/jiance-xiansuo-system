"""只读 SQLite 视图访问层。

安全约束：
- 以 ``mode=ro`` + ``PRAGMA query_only=ON`` 打开，导出过程不写库。
- 只允许查询 ``v_lead_summary`` / ``v_lead_links`` 两个视图；
  若同名对象是表而不是视图，直接报错，避免误读底层表。
- 只 SELECT 列名映射里声明的列，不 SELECT *。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from .columns import (
    LINK_EXCEL_FIELDS,
    LINKS_VIEW,
    SUMMARY_EXCEL_FIELDS,
    SUMMARY_VIEW,
    VIEW_COLUMNS,
)
from .errors import ExportDatabaseError


@dataclass
class ViewRows:
    rows: list[dict[str, Any]] = field(default_factory=list)


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    """打开只读连接；中文/空格路径通过 file URI 处理。"""

    resolved = db_path.resolve()
    uri = f"{resolved.as_uri()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=10.0)
    except sqlite3.Error as exc:
        raise ExportDatabaseError(
            f"无法以只读方式打开数据库 {resolved}：{exc}"
        ) from exc
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
    except sqlite3.Error as exc:
        conn.close()
        raise ExportDatabaseError(f"初始化只读数据库连接失败：{exc}") from exc
    return conn


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _view_columns(conn: sqlite3.Connection, view: str) -> set[str]:
    row = conn.execute(
        "SELECT type FROM sqlite_master WHERE type IN ('table','view') AND name = ?",
        (view,),
    ).fetchone()
    if row is None:
        raise ExportDatabaseError(
            f"数据库缺少视图 {view}。请确认后端已执行迁移并创建该只读视图。"
        )
    if str(row[0]).lower() != "view":
        raise ExportDatabaseError(
            f"{view} 在数据库中是 {row[0]} 而不是视图；导出模块只允许读取只读视图。"
        )

    try:
        info = conn.execute(f"PRAGMA table_info({_quote(view)})").fetchall()
    except sqlite3.Error as exc:
        raise ExportDatabaseError(f"读取视图 {view} 的列信息失败：{exc}") from exc
    columns = {str(item[1]) for item in info}
    if not columns:
        raise ExportDatabaseError(f"视图 {view} 没有可读取的列。")
    return columns


def _select_sql(view: str, columns: dict[str, str]) -> str:
    parts = [
        f"{_quote(real)} AS {_quote(logical)}" for logical, real in columns.items()
    ]
    return f"SELECT {', '.join(parts)} FROM {_quote(view)}"


def read_view(
    conn: sqlite3.Connection,
    view: str,
    snapshot: date,
    *,
    run_id: str | None = None,
) -> ViewRows:
    """读取当日视图数据；提供 run_id 时只读取该任务。"""

    columns = VIEW_COLUMNS[view]
    available = _view_columns(conn, view)

    # 业务列必须存在；辅助列（shop_type_rank / caliber_version /
    # config_version）缺失时优雅降级：排序回落到 shop_type，版本回落到 rules.yaml。
    required = set(SUMMARY_EXCEL_FIELDS if view == SUMMARY_VIEW else LINK_EXCEL_FIELDS)
    resolved: dict[str, str] = {}
    missing: list[str] = []
    for logical, real in columns.items():
        if real in available:
            resolved[logical] = real
        elif logical in required:
            missing.append(real)
    if missing:
        raise ExportDatabaseError(
            f"视图 {view} 缺少导出所需列：{', '.join(missing)}。"
            "请对照 lead_excel/columns.py 的 VIEW_COLUMNS 与后端方案_v0.1 §6 对齐。"
        )

    date_column = VIEW_COLUMNS[SUMMARY_VIEW]["snapshot_date"]
    if date_column not in available:
        raise ExportDatabaseError(
            f"视图 {view} 缺少日期列 {date_column}，无法按 snapshot_date 过滤。"
        )

    # 规格要求只按本次快照日过滤；两个视图本身只包含「待复核」线索，
    # 导出侧不再额外拼 status 条件。
    where = f"{_quote(date_column)} = ?"
    params: list[Any] = [snapshot.isoformat()]
    if run_id is not None:
        if "run_id" not in available:
            raise ExportDatabaseError(
                f"视图 {view} 缺少 run_id，无法按任务导出，请更新后端视图。"
            )
        where += ' AND "run_id" = ?'
        params.append(run_id)

    sql = f"{_select_sql(view, resolved)} WHERE {where}"
    try:
        rows = conn.execute(sql, tuple(params)).fetchall()
    except sqlite3.Error as exc:
        raise ExportDatabaseError(f"查询视图 {view} 失败：{exc}") from exc

    result = ViewRows()
    for row in rows:
        result.rows.append(
            {
                logical: row[logical] if logical in resolved else None
                for logical in columns
            }
        )
    return result
