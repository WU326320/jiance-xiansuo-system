"""add read-only view v_near_threshold_shops

Revision ID: 0003_near_threshold_view
Revises: 0002_english_views
Create Date: 2026-11-05
"""

from __future__ import annotations

from alembic import op

from pipeline.views import NEAR_THRESHOLD_VIEW_SQL

revision = "0003_near_threshold_view"
down_revision = "0002_english_views"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP VIEW IF EXISTS v_near_threshold_shops")
    op.execute(NEAR_THRESHOLD_VIEW_SQL)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS v_near_threshold_shops")
