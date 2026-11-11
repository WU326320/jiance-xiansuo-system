"""initial schema and read-only views

Revision ID: 0001_initial
Revises:
Create Date: 2026-11-05
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from pipeline.views import CREATE_VIEWS_SQL, DROP_VIEWS_SQL

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "config_versions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("config_version", sa.String(length=32), nullable=False, unique=True),
        sa.Column("caliber_version", sa.String(length=32), nullable=False),
        sa.Column("yaml_text", sa.Text(), nullable=False),
        sa.Column("first_used_at", sa.DateTime(), nullable=False),
    )

    op.create_table(
        "shops",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("platform", sa.String(length=16), nullable=False),
        sa.Column("shop_id", sa.String(length=64), nullable=False),
        sa.Column("shop_name", sa.String(length=255), nullable=True),
        sa.Column("shop_type", sa.String(length=16), nullable=False),
        sa.Column("shop_label_raw", sa.String(length=255), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("platform", "shop_id", name="uq_shops_platform_shop"),
    )
    op.create_index("ix_shops_platform", "shops", ["platform"])

    op.create_table(
        "products",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("platform", sa.String(length=16), nullable=False),
        sa.Column("product_id", sa.String(length=64), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("shop_id", sa.String(length=64), nullable=True),
        sa.Column("shop_id_missing", sa.Boolean(), nullable=False),
        sa.Column("main_image_url", sa.Text(), nullable=True),
        sa.Column("image_path", sa.Text(), nullable=True),
        sa.Column("image_phash", sa.String(length=64), nullable=True),
        sa.Column("category", sa.String(length=128), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("platform", "product_id", name="uq_products_platform_product"),
    )
    op.create_index("ix_products_phash", "products", ["image_phash"])

    op.create_table(
        "product_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "product_id",
            sa.Integer(),
            sa.ForeignKey("products.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("crawled_at", sa.DateTime(), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("ip_code", sa.String(length=64), nullable=True),
        sa.Column("search_keyword", sa.String(length=255), nullable=True),
        sa.Column("price", sa.Numeric(12, 2), nullable=True),
        sa.Column("sales_raw", sa.Text(), nullable=True),
        sa.Column("sales", sa.Integer(), nullable=True),
        sa.Column("comments_raw", sa.Text(), nullable=True),
        sa.Column("comments", sa.Integer(), nullable=True),
        sa.Column("listed", sa.Boolean(), nullable=False),
        sa.Column("text_score", sa.Float(), nullable=True),
        sa.Column("visual_score", sa.Float(), nullable=True),
        sa.Column("final_score", sa.Float(), nullable=True),
        sa.Column("need_vision", sa.Boolean(), nullable=False),
        sa.Column("eligible", sa.Boolean(), nullable=False),
        sa.Column("exclusion_reason", sa.Text(), nullable=True),
        sa.Column("hit_reasons", sa.Text(), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("formula", sa.Text(), nullable=False),
        sa.Column("estimated_sales", sa.Integer(), nullable=True),
        sa.Column("missing_flags", sa.Text(), nullable=False),
        sa.Column("config_version", sa.String(length=32), nullable=False),
        sa.Column("caliber_version", sa.String(length=32), nullable=False),
        sa.UniqueConstraint(
            "product_id", "snapshot_date", name="uq_snapshots_product_date"
        ),
    )
    op.create_index("ix_snapshots_date", "product_snapshots", ["snapshot_date"])
    op.create_index("ix_snapshots_run", "product_snapshots", ["run_id"])
    op.create_index("ix_snapshots_ip_code", "product_snapshots", ["ip_code"])

    op.create_table(
        "vision_cache",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("image_phash", sa.String(length=64), nullable=False),
        sa.Column("ip_code", sa.String(length=64), nullable=False),
        sa.Column("visual_score", sa.Float(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("config_version", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("image_phash", "ip_code", name="uq_vision_phash_ip"),
    )

    op.create_table(
        "whitelist",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("platform", sa.String(length=16), nullable=False),
        sa.Column("shop_id", sa.String(length=64), nullable=False),
        sa.Column("ip_code", sa.String(length=64), nullable=False),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column("added_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "platform", "shop_id", "ip_code", name="uq_whitelist_platform_shop_ip"
        ),
    )

    op.create_table(
        "leads",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("lead_no", sa.String(length=64), nullable=False, unique=True),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("platform", sa.String(length=16), nullable=False),
        sa.Column("shop_id", sa.String(length=64), nullable=False),
        sa.Column("total_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("link_count", sa.Integer(), nullable=False),
        sa.Column("ip_codes", sa.Text(), nullable=False),
        sa.Column("payment_tags", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("hit_summary", sa.Text(), nullable=False),
        sa.Column("missing_flags", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("review_result", sa.String(length=16), nullable=True),
        sa.Column("reviewer", sa.String(length=64), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("saas_entered_at", sa.DateTime(), nullable=True),
        sa.Column("caliber_version", sa.String(length=32), nullable=False),
        sa.Column("config_version", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "platform", "shop_id", "snapshot_date", name="uq_leads_shop_date"
        ),
        sa.CheckConstraint(
            "status IN ('待复核','已确认','驳回','已录入SaaS')",
            name="ck_leads_status",
        ),
    )
    op.create_index("ix_leads_date_status", "leads", ["snapshot_date", "status"])

    op.create_table(
        "lead_links",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "lead_id",
            sa.Integer(),
            sa.ForeignKey("leads.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "snapshot_id",
            sa.Integer(),
            sa.ForeignKey("product_snapshots.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ip_code", sa.String(length=64), nullable=True),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("formula", sa.Text(), nullable=False),
        sa.UniqueConstraint("lead_id", "snapshot_id", name="uq_lead_links_lead_snapshot"),
    )

    op.create_table(
        "lead_status_history",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "lead_id",
            sa.Integer(),
            sa.ForeignKey("leads.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("old_status", sa.String(length=16), nullable=True),
        sa.Column("new_status", sa.String(length=16), nullable=False),
        sa.Column("operator", sa.String(length=64), nullable=False),
        sa.Column("changed_at", sa.DateTime(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
    )

    op.create_table(
        "run_logs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("platform", sa.String(length=16), nullable=True),
        sa.Column("stage", sa.String(length=32), nullable=False),
        sa.Column("login_ok", sa.Boolean(), nullable=True),
        sa.Column("items_count", sa.Integer(), nullable=False),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
    )
    op.create_index("ix_run_logs_run", "run_logs", ["run_id"])

    for statement in DROP_VIEWS_SQL.strip().split(";"):
        if statement.strip():
            op.execute(statement)
    for statement in CREATE_VIEWS_SQL:
        op.execute(statement)


def downgrade() -> None:
    for statement in DROP_VIEWS_SQL.strip().split(";"):
        if statement.strip():
            op.execute(statement)
    op.drop_table("run_logs")
    op.drop_table("lead_status_history")
    op.drop_table("lead_links")
    op.drop_table("leads")
    op.drop_table("whitelist")
    op.drop_table("vision_cache")
    op.drop_table("product_snapshots")
    op.drop_table("products")
    op.drop_table("shops")
    op.drop_table("config_versions")
