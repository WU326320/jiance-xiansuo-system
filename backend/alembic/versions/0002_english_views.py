"""align v_lead_summary / v_lead_links with backend plan §6 (English columns)

Revision ID: 0002_english_views
Revises: 0001_initial
Create Date: 2026-11-05
"""

from __future__ import annotations

from alembic import op

from pipeline.views import CREATE_VIEWS_SQL, DROP_VIEWS_SQL

revision = "0002_english_views"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


OLD_SUMMARY_SQL = """
CREATE VIEW v_lead_summary AS
SELECT
    l.lead_no                    AS "线索编号",
    l.snapshot_date              AS "快照日期",
    l.platform                   AS "平台",
    l.shop_id                    AS "店铺ID",
    COALESCE(s.shop_name, '')    AS "店铺名",
    COALESCE(s.shop_type, '未知') AS "店铺类型",
    COALESCE(s.shop_label_raw, '') AS "平台原始标签",
    l.ip_codes                   AS "涉及IP",
    l.link_count                 AS "链接数",
    l.total_amount               AS "店铺合计金额",
    l.payment_tags               AS "赔付能力标签",
    l.confidence                 AS "置信度",
    l.hit_summary                AS "命中原因摘要",
    l.missing_flags              AS "缺失标记",
    COALESCE(l.review_result, '') AS "复核结论",
    COALESCE(l.reviewer, '')     AS "复核人",
    l.snapshot_date              AS snapshot_date,
    l.status                     AS status
FROM leads l
LEFT JOIN shops s
       ON s.platform = l.platform
      AND s.shop_id = l.shop_id
ORDER BY
    CASE COALESCE(s.shop_type, '未知')
        WHEN '企业' THEN 4
        WHEN '个体户' THEN 3
        WHEN '未知' THEN 2
        WHEN '个人' THEN 1
        ELSE 0
    END DESC,
    l.confidence DESC,
    l.total_amount DESC
"""

OLD_LINKS_SQL = """
CREATE VIEW v_lead_links AS
SELECT
    l.lead_no                    AS "线索编号",
    COALESCE(ll.ip_code, '')     AS "IP",
    p.product_id                 AS "商品ID",
    COALESCE(p.image_path, '')   AS "缩略图",
    COALESCE(p.main_image_url, '') AS "原图链接",
    p.title                      AS "标题",
    p.url                        AS "商品链接",
    ps.price                     AS "单价",
    COALESCE(ps.sales_raw, '')   AS "销量原文",
    COALESCE(ps.comments_raw, '') AS "评论数原文",
    ps.estimated_sales           AS "估算销量",
    ll.amount                    AS "金额",
    ll.formula                   AS "计算式",
    ps.text_score                AS "文字分",
    ps.visual_score              AS "视觉分",
    ps.final_score               AS "综合分",
    ps.hit_reasons               AS "命中原因",
    ps.missing_flags             AS "缺失标记",
    l.snapshot_date              AS snapshot_date,
    l.status                     AS status
FROM lead_links ll
JOIN leads l              ON l.id = ll.lead_id
JOIN product_snapshots ps ON ps.id = ll.snapshot_id
JOIN products p           ON p.id = ps.product_id
ORDER BY
    l.snapshot_date DESC,
    l.platform ASC,
    l.shop_id ASC,
    ll.amount DESC
"""


def _drop_views() -> None:
    for statement in DROP_VIEWS_SQL.strip().split(";"):
        if statement.strip():
            op.execute(statement)


def upgrade() -> None:
    _drop_views()
    for statement in CREATE_VIEWS_SQL:
        op.execute(statement)


def downgrade() -> None:
    _drop_views()
    op.execute(OLD_SUMMARY_SQL)
    op.execute(OLD_LINKS_SQL)
