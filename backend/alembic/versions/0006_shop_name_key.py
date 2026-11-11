"""aggregate by platform group + normalized shop name

Revision ID: 0006_shop_name_key
Revises: 0005_tier_and_drop_verify
Create Date: 2026-11-06

口径：不再用 shop_id 作为聚合主键。``products`` 持久化店名/类型/标签；
``leads`` 增加 platform_group / shop_key / shop_name，唯一键改为
(platform_group, shop_key, shop_id, snapshot_date)。``whitelist`` 增加
店铺名与规范化列，旧 shop_id 白名单继续按 ID 兼容。
"""

from __future__ import annotations

import unicodedata

from alembic import op
import sqlalchemy as sa

from pipeline.shopkey import decode_shop_name, normalize_shop_name, platform_group
from pipeline.views import (
    CREATE_VIEWS_SQL,
    DROP_VIEWS_SQL,
    NEAR_THRESHOLD_VIEW_SQL,
)

revision = "0006_shop_name_key"
down_revision = "0005_tier_and_drop_verify"
branch_labels = None
depends_on = None


def _run_statements(statements) -> None:
    for statement in statements:
        for part in str(statement).strip().split(";"):
            if part.strip():
                op.execute(part)


def _add_columns() -> None:
    op.add_column("products", sa.Column("shop_name", sa.String(length=255)))
    op.add_column(
        "products",
        sa.Column("shop_type", sa.String(length=16), nullable=False, server_default="未知"),
    )
    op.add_column("products", sa.Column("shop_label_raw", sa.String(length=255)))

    op.add_column(
        "leads",
        sa.Column("platform_group", sa.String(length=16), nullable=False, server_default=""),
    )
    op.add_column(
        "leads",
        sa.Column("shop_key", sa.String(length=512), nullable=False, server_default=""),
    )
    op.add_column("leads", sa.Column("shop_name", sa.String(length=255)))
    op.add_column(
        "leads",
        sa.Column("shop_type", sa.String(length=16), nullable=False, server_default="未知"),
    )
    op.add_column("leads", sa.Column("shop_label_raw", sa.String(length=255)))

    op.add_column("whitelist", sa.Column("shop_name", sa.String(length=255)))
    op.add_column("whitelist", sa.Column("shop_name_norm", sa.String(length=255)))


def _backfill() -> None:
    bind = op.get_bind()
    # products: 从 shops 回填店名/类型/标签
    bind.execute(
        sa.text(
            """
            UPDATE products
               SET shop_name = (
                       SELECT s.shop_name FROM shops s
                        WHERE s.platform = products.platform
                          AND s.shop_id = products.shop_id
                   ),
                   shop_type = COALESCE((
                       SELECT s.shop_type FROM shops s
                        WHERE s.platform = products.platform
                          AND s.shop_id = products.shop_id
                   ), '未知'),
                   shop_label_raw = (
                       SELECT s.shop_label_raw FROM shops s
                        WHERE s.platform = products.platform
                          AND s.shop_id = products.shop_id
                   )
             WHERE shop_name IS NULL
            """
        )
    )
    bind.execute(
        sa.text(
            """
            UPDATE leads
               SET shop_name = (
                       SELECT s.shop_name FROM shops s
                        WHERE s.platform = leads.platform
                          AND s.shop_id = leads.shop_id
                   ),
                   shop_type = COALESCE((
                       SELECT s.shop_type FROM shops s
                        WHERE s.platform = leads.platform
                          AND s.shop_id = leads.shop_id
                   ), '未知'),
                   shop_label_raw = (
                       SELECT s.shop_label_raw FROM shops s
                        WHERE s.platform = leads.platform
                          AND s.shop_id = leads.shop_id
                   )
             WHERE shop_name IS NULL
            """
        )
    )
    bind.execute(sa.text("UPDATE whitelist SET platform = CASE platform WHEN 'tmall' THEN 'taobao' ELSE platform END"))
    # 1688 旧数据里的店名可能是 URL 编码：迁移时一并解码。
    for table in ("shops", "products", "leads"):
        for row in bind.execute(
            sa.text(f"SELECT id, shop_name FROM {table} WHERE platform = '1688' AND shop_name IS NOT NULL")
        ).all():
            decoded = decode_shop_name(row.shop_name)
            if decoded and decoded != row.shop_name:
                bind.execute(
                    sa.text(f"UPDATE {table} SET shop_name = :name WHERE id = :id"),
                    {"name": decoded, "id": row.id},
                )
    for row in bind.execute(sa.text("SELECT id, platform, shop_name FROM leads")).all():
        group = platform_group(row.platform)
        key = f"{group}|{normalize_shop_name(row.shop_name)}"
        bind.execute(
            sa.text(
                "UPDATE leads SET platform_group = :group, shop_key = :key WHERE id = :id"
            ),
            {"group": group, "key": key, "id": row.id},
        )


def upgrade() -> None:
    _run_statements([DROP_VIEWS_SQL])
    _add_columns()
    _backfill()
    with op.batch_alter_table("leads") as batch:
        batch.drop_constraint("uq_leads_shop_date", type_="unique")
        batch.create_unique_constraint(
            "uq_leads_shopkey_date",
            ["platform_group", "shop_key", "shop_id", "snapshot_date"],
        )
    _run_statements(CREATE_VIEWS_SQL)
    _run_statements([NEAR_THRESHOLD_VIEW_SQL])


def downgrade() -> None:
    _run_statements([DROP_VIEWS_SQL])
    with op.batch_alter_table("leads") as batch:
        batch.drop_constraint("uq_leads_shopkey_date", type_="unique")
        batch.create_unique_constraint(
            "uq_leads_shop_date", ["platform", "shop_id", "snapshot_date"]
        )
        batch.drop_column("shop_label_raw")
        batch.drop_column("shop_type")
        batch.drop_column("shop_name")
        batch.drop_column("shop_key")
        batch.drop_column("platform_group")
    with op.batch_alter_table("products") as batch:
        batch.drop_column("shop_label_raw")
        batch.drop_column("shop_type")
        batch.drop_column("shop_name")
    with op.batch_alter_table("whitelist") as batch:
        batch.drop_column("shop_name_norm")
        batch.drop_column("shop_name")
