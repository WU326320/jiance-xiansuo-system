"""风险档位取消评论依赖；保留历史快照金额及人工复核记录。"""

from alembic import op
from pipeline.views import LEAD_SUMMARY_VIEW_SQL

revision = "0013_sales_amount_view"
down_revision = "0012_restore_amount_view"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("DROP VIEW IF EXISTS v_lead_summary")
    op.execute(LEAD_SUMMARY_VIEW_SQL)


def downgrade():
    pass
