"""add lead status 待核实 and pending-verification read-only views

Revision ID: 0004_pending_verify_status
Revises: 0003_near_threshold_view
Create Date: 2026-11-06

评论数缺失的链接金额按销量估算，但未做评论校正，不能直接计入 >50 万
正式线索。聚合时这类店铺线索写 status='待核实'，并用独立只读视图给
Excel 导出最小展示。
"""

from __future__ import annotations

from alembic import op

from pipeline.views import (
    CREATE_VIEWS_SQL,
    DROP_VERIFY_VIEWS_SQL,
    DROP_VIEWS_SQL,
    NEAR_THRESHOLD_VIEW_SQL,
)

revision = "0004_pending_verify_status"
down_revision = "0003_near_threshold_view"
branch_labels = None
depends_on = None

OLD_CHECK = "status IN ('待复核','已确认','驳回','已录入SaaS')"
NEW_CHECK = "status IN ('待复核','待核实','已确认','驳回','已录入SaaS')"


def _run_statements(statements) -> None:
    for statement in statements:
        for part in str(statement).strip().split(";"):
            if part.strip():
                op.execute(part)


def _drop_views() -> None:
    # SQLite batch 重建 leads 时，依赖该表的视图会阻塞 RENAME；先全部删掉。
    _run_statements([DROP_VIEWS_SQL])


def _create_standard_views() -> None:
    _run_statements(CREATE_VIEWS_SQL)
    _run_statements([NEAR_THRESHOLD_VIEW_SQL])


def upgrade() -> None:
    _drop_views()
    with op.batch_alter_table("leads") as batch:
        batch.drop_constraint("ck_leads_status", type_="check")
        batch.create_check_constraint("ck_leads_status", NEW_CHECK)
    _create_standard_views()


def downgrade() -> None:
    _drop_views()
    op.execute("UPDATE leads SET status = '待复核' WHERE status = '待核实'")
    with op.batch_alter_table("leads") as batch:
        batch.drop_constraint("ck_leads_status", type_="check")
        batch.create_check_constraint("ck_leads_status", OLD_CHECK)
    _create_standard_views()
    # 0003 还没有「待核实」档，降级时删掉新增视图。
    _run_statements([DROP_VERIFY_VIEWS_SQL])
