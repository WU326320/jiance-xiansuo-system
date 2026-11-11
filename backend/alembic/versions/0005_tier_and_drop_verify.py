"""add tier to v_lead_summary and drop pending-verification views

Revision ID: 0005_tier_and_drop_verify
Revises: 0004_pending_verify_status
Create Date: 2026-11-06

口径 B（验收清单 v0.3.1c §3.5）：档位在 ``v_lead_summary.tier`` 中按
“剔除评论缺失链接 → 待核验 → 临界”的顺序算好；不再使用「待核实」
状态/视图。存量 ``status='待核实'`` 的线索统一回到 ``待复核``。
"""

from __future__ import annotations

from alembic import op

from pipeline.views import (
    CREATE_VIEWS_SQL,
    DROP_VIEWS_SQL,
    NEAR_THRESHOLD_VIEW_SQL,
)

revision = "0005_tier_and_drop_verify"
down_revision = "0004_pending_verify_status"
branch_labels = None
depends_on = None


def _run_statements(statements) -> None:
    for statement in statements:
        for part in str(statement).strip().split(";"):
            if part.strip():
                op.execute(part)


def _drop_views() -> None:
    _run_statements([DROP_VIEWS_SQL])


def _create_views() -> None:
    _run_statements(CREATE_VIEWS_SQL)
    _run_statements([NEAR_THRESHOLD_VIEW_SQL])


def upgrade() -> None:
    _drop_views()
    op.execute("UPDATE leads SET status = '待复核' WHERE status = '待核实'")
    with op.batch_alter_table("leads") as batch:
        batch.drop_constraint("ck_leads_status", type_="check")
        batch.create_check_constraint(
            "ck_leads_status",
            "status IN ('待复核','已确认','驳回','已录入SaaS')",
        )
    _create_views()


# ---------------------------------------------------------------------------
# 降级：恢复 0004 的两个正式视图 + 两个「待核实」视图（不含 tier）。
# SQL 在此冻结，避免未来 views.py 改动影响历史迁移。
# ---------------------------------------------------------------------------
_OLD_SUMMARY_SQL = """
CREATE VIEW v_lead_summary AS
SELECT
    CAST(l.lead_no AS TEXT)                      AS lead_id,
    CAST(l.snapshot_date AS TEXT)                AS snapshot_date,
    CAST(l.platform AS TEXT)                     AS platform,
    CAST(l.shop_id AS TEXT)                      AS shop_id,
    CAST(COALESCE(s.shop_name, '') AS TEXT)      AS shop_name,
    CAST(COALESCE(s.shop_type, '未知') AS TEXT)  AS shop_type,
    CAST(COALESCE(s.shop_label_raw, '') AS TEXT) AS shop_label_raw,
    CAST(l.ip_codes AS TEXT)                     AS ip_codes,
    CAST(l.link_count AS INTEGER)                AS link_count,
    CAST(ROUND(l.total_amount, 2) AS REAL)       AS shop_amount,
    CAST(l.payment_tags AS TEXT)                 AS payment_ability,
    CAST(ROUND(l.confidence, 2) AS REAL)         AS confidence,
    CAST(COALESCE(NULLIF(l.hit_summary, ''), '命中规则') AS TEXT)
                                                 AS hit_reasons_summary,
    CAST(NULLIF(l.missing_flags, '[]') AS TEXT)  AS missing_flags,
    CAST(COALESCE(l.review_result, '') AS TEXT)  AS review_result,
    CAST(COALESCE(l.reviewer, '') AS TEXT)       AS reviewer,
    CAST(CASE COALESCE(s.shop_type, '未知')
        WHEN '企业' THEN 1
        WHEN '个体户' THEN 2
        WHEN '未知' THEN 3
        WHEN '个人' THEN 4
        ELSE 5
    END AS INTEGER)                              AS shop_type_rank,
    CAST(l.caliber_version AS TEXT)              AS caliber_version,
    CAST(l.config_version AS TEXT)               AS config_version
FROM leads l
LEFT JOIN shops s
       ON s.platform = l.platform
      AND s.shop_id = l.shop_id
WHERE l.status = '待复核'
ORDER BY shop_type_rank ASC, confidence DESC, shop_amount DESC
"""

_OLD_LINKS_SQL = """
CREATE VIEW v_lead_links AS
SELECT
    CAST(l.lead_no AS TEXT)                      AS lead_id,
    CAST(l.snapshot_date AS TEXT)                AS snapshot_date,
    CAST(ll.ip_code AS TEXT)                     AS ip_code,
    CAST(p.product_id AS TEXT)                   AS product_id,
    CAST(p.image_path AS TEXT)                   AS image_path,
    CAST(p.main_image_url AS TEXT)               AS main_image_url,
    CAST(p.title AS TEXT)                        AS title,
    CAST(p.url AS TEXT)                          AS url,
    CAST(ROUND(ps.price, 2) AS REAL)             AS price,
    CAST(ps.sales_raw AS TEXT)                   AS sales_raw,
    CAST(ps.comments_raw AS TEXT)                AS comments_raw,
    CAST(ps.estimated_sales AS INTEGER)          AS estimated_sales,
    CAST(ROUND(ll.amount, 2) AS REAL)            AS amount,
    CAST(ll.formula AS TEXT)                     AS amount_formula,
    CAST(ROUND(ps.text_score, 2) AS REAL)        AS text_score,
    CAST(ROUND(ps.visual_score, 2) AS REAL)      AS visual_score,
    CAST(ROUND(ps.final_score, 2) AS REAL)       AS final_score,
    CAST(COALESCE(
        NULLIF(
            CASE
                WHEN json_valid(ps.hit_reasons)
                THEN (SELECT group_concat(value, '；')
                        FROM json_each(ps.hit_reasons))
                ELSE ps.hit_reasons
            END,
            ''
        ),
        '命中规则'
    ) AS TEXT)                                   AS hit_reasons,
    CAST(NULLIF(ps.missing_flags, '[]') AS TEXT) AS missing_flags
FROM lead_links ll
JOIN leads l              ON l.id = ll.lead_id
JOIN product_snapshots ps ON ps.id = ll.snapshot_id
JOIN products p           ON p.id = ps.product_id
WHERE l.status = '待复核'
ORDER BY snapshot_date DESC, lead_id ASC, amount DESC
"""

_OLD_VERIFY_SUMMARY_SQL = _OLD_SUMMARY_SQL.replace(
    "CREATE VIEW v_lead_summary AS", "CREATE VIEW v_lead_verify_summary AS"
).replace("WHERE l.status = '待复核'", "WHERE l.status = '待核实'")

_OLD_VERIFY_LINKS_SQL = _OLD_LINKS_SQL.replace(
    "CREATE VIEW v_lead_links AS", "CREATE VIEW v_lead_verify_links AS"
).replace("WHERE l.status = '待复核'", "WHERE l.status = '待核实'")


def downgrade() -> None:
    _drop_views()
    with op.batch_alter_table("leads") as batch:
        batch.drop_constraint("ck_leads_status", type_="check")
        batch.create_check_constraint(
            "ck_leads_status",
            "status IN ('待复核','待核实','已确认','驳回','已录入SaaS')",
        )
    _run_statements(
        [
            _OLD_SUMMARY_SQL,
            _OLD_LINKS_SQL,
            _OLD_VERIFY_SUMMARY_SQL,
            _OLD_VERIFY_LINKS_SQL,
            NEAR_THRESHOLD_VIEW_SQL,
        ]
    )
