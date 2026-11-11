"""Refresh task-scoped links for databases already stamped with revision 0008."""

from alembic import op

from pipeline.views import LEAD_LINKS_VIEW_SQL

revision = "0009_refresh_link_task_scope"
down_revision = "0008_link_view_run_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 仅重建只读视图；商品、快照和线索表不变。
    op.execute("DROP VIEW IF EXISTS v_lead_links")
    op.execute(LEAD_LINKS_VIEW_SQL)


def downgrade() -> None:
    # 正确的 0008 与此版本具有相同视图契约。
    pass
