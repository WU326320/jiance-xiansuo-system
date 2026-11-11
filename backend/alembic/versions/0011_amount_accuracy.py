"""金额可为空并保存价格原文，保留历史计算值。"""

from alembic import op
import sqlalchemy as sa
from pipeline.views import CREATE_VIEWS_SQL, DROP_VIEWS_SQL, NEAR_THRESHOLD_VIEW_SQL

revision = "0011_amount_accuracy"
down_revision = "0010_export_shop_fields"
branch_labels = None
depends_on = None


def upgrade():
    for statement in DROP_VIEWS_SQL.split(";"):
        if statement.strip():
            op.execute(statement)
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("product_snapshots")
    }
    with op.batch_alter_table("product_snapshots") as batch:
        if "price_raw" not in columns:
            batch.add_column(sa.Column("price_raw", sa.Text(), nullable=True))
        batch.alter_column("amount", existing_type=sa.Numeric(14, 2), nullable=True)
    with op.batch_alter_table("lead_links") as batch:
        batch.alter_column("amount", existing_type=sa.Numeric(14, 2), nullable=True)
    for statement in CREATE_VIEWS_SQL:
        op.execute(statement)
    op.execute(NEAR_THRESHOLD_VIEW_SQL)


def downgrade():
    raise RuntimeError("请使用迁移前备份恢复，避免把未知金额改成零")
