"""expose run_id in the link export view

Revision ID: 0008_link_view_run_id
Revises: 0007_run_scope
Create Date: 2026-11-08
"""

from alembic import op

from pipeline.views import LEAD_LINKS_VIEW_SQL

revision = "0008_link_view_run_id"
down_revision = "0007_run_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP VIEW IF EXISTS v_lead_links")
    op.execute(LEAD_LINKS_VIEW_SQL)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS v_lead_links")
    op.execute(
        LEAD_LINKS_VIEW_SQL.replace(
            "    CAST(run_id AS TEXT)                         AS run_id,\n", "", 1
        )
    )
