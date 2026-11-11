"""Expose product platform and shop identity for the single-sheet export."""

from alembic import op

from pipeline.views import LEAD_LINKS_VIEW_SQL

revision = "0010_export_shop_fields"
down_revision = "0009_refresh_link_task_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP VIEW IF EXISTS v_lead_links")
    op.execute(LEAD_LINKS_VIEW_SQL)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS v_lead_links")
    sql = LEAD_LINKS_VIEW_SQL
    for line in (
        "    CAST(platform AS TEXT)                       AS platform,\n",
        "    CAST(shop_id AS TEXT)                        AS shop_id,\n",
    ):
        sql = sql.replace(line, "", 1)
    op.execute(sql)
