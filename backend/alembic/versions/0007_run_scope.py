"""scope leads and snapshots by pipeline run

Revision ID: 0007_run_scope
Revises: 0006_shop_name_key
Create Date: 2026-11-06
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from pipeline.views import CREATE_VIEWS_SQL, DROP_VIEWS_SQL, NEAR_THRESHOLD_VIEW_SQL

revision = "0007_run_scope"
down_revision = "0006_shop_name_key"
branch_labels = None
depends_on = None


def _run_statements(statements) -> None:
    for statement in statements:
        for part in str(statement).strip().split(";"):
            if part.strip():
                op.execute(part)


def upgrade() -> None:
    _run_statements([DROP_VIEWS_SQL])

    with op.batch_alter_table("product_snapshots") as batch:
        batch.drop_constraint("uq_snapshots_product_date", type_="unique")
        batch.create_unique_constraint(
            "uq_snapshots_product_run_ip_date",
            ["product_id", "snapshot_date", "run_id", "ip_code"],
        )

    op.add_column("leads", sa.Column("run_id", sa.String(length=64), nullable=True))
    op.execute("UPDATE leads SET run_id = 'legacy-' || id WHERE run_id IS NULL")
    with op.batch_alter_table("leads") as batch:
        batch.alter_column("run_id", existing_type=sa.String(length=64), nullable=False)
        batch.drop_constraint("uq_leads_shopkey_date", type_="unique")
        batch.create_unique_constraint(
            "uq_leads_run_shopkey",
            ["run_id", "platform_group", "shop_key", "shop_id"],
        )
        batch.create_index("ix_leads_run", ["run_id"])

    _run_statements(CREATE_VIEWS_SQL)
    _run_statements([NEAR_THRESHOLD_VIEW_SQL])


def downgrade() -> None:
    _run_statements([DROP_VIEWS_SQL])
    with op.batch_alter_table("leads") as batch:
        batch.drop_index("ix_leads_run")
        batch.drop_constraint("uq_leads_run_shopkey", type_="unique")
        batch.create_unique_constraint(
            "uq_leads_shopkey_date",
            ["platform_group", "shop_key", "shop_id", "snapshot_date"],
        )
        batch.drop_column("run_id")
    with op.batch_alter_table("product_snapshots") as batch:
        batch.drop_constraint("uq_snapshots_product_run_ip_date", type_="unique")
        batch.create_unique_constraint(
            "uq_snapshots_product_date", ["product_id", "snapshot_date"]
        )
    _run_statements(CREATE_VIEWS_SQL)
    _run_statements([NEAR_THRESHOLD_VIEW_SQL])
