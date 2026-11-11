"""恢复早期金额迁移已标记数据库的临界店铺视图。"""

from alembic import op
import sqlalchemy as sa
from pipeline.views import NEAR_THRESHOLD_VIEW_SQL

revision = "0012_restore_amount_view"
down_revision = "0011_amount_accuracy"
branch_labels = None
depends_on = None


def upgrade():
    if "v_near_threshold_shops" not in sa.inspect(op.get_bind()).get_view_names():
        op.execute(NEAR_THRESHOLD_VIEW_SQL)


def downgrade():
    pass
