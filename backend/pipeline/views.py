"""只读视图 SQL（英文列名，前端/Excel 已对齐）。

两个正式视图都只包含状态为「待复核」的线索。按日期过滤由导出侧
``WHERE snapshot_date = ?`` 完成。日期输出为 TEXT ``YYYY-MM-DD``，
金额/分数为 REAL（SQLite 下由 ROUND 保证 2 位小数）。

2026-11-06 聚合口径：一店一行按 ``平台组 + 规范化店名``；``shop_id`` 只
作为辅助列（同名不同店时用于拆分），Excel 不再显示店铺 ID。店名为空的
商品只进 ``v_lead_links``，不进入 ``v_lead_summary``。
"""

from __future__ import annotations

COMMENTS_MISSING_FLAG = "评论缺失,待核验"

_LEAD_LINKS_SELECT = """
SELECT
    CAST(l.lead_no AS TEXT)                      AS lead_id,
    CAST(l.run_id AS TEXT)                       AS run_id,
    CAST(l.snapshot_date AS TEXT)                AS snapshot_date,
    CAST(ll.ip_code AS TEXT)                     AS ip_code,
    CAST(p.product_id AS TEXT)                   AS product_id,
    CAST(p.platform AS TEXT)                     AS platform,
    CAST(p.shop_id AS TEXT)                      AS shop_id,
    CAST(COALESCE(l.shop_name, s.shop_name, '') AS TEXT)
                                                 AS shop_name,
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
LEFT JOIN shops s
       ON s.platform = p.platform
      AND s.shop_id = p.shop_id
WHERE l.status = '待复核'
"""

# 店名为空 / 当日未在架：仍留在链接明细，但 lead_id 为 NULL。
_STANDALONE_LINKS_SELECT = """
SELECT
    CAST(NULL AS TEXT)                           AS lead_id,
    CAST(ps.run_id AS TEXT)                      AS run_id,
    CAST(ps.snapshot_date AS TEXT)               AS snapshot_date,
    CAST(ps.ip_code AS TEXT)                     AS ip_code,
    CAST(p.product_id AS TEXT)                   AS product_id,
    CAST(p.platform AS TEXT)                     AS platform,
    CAST(p.shop_id AS TEXT)                      AS shop_id,
    CAST(COALESCE(p.shop_name, s.shop_name, '') AS TEXT)
                                                 AS shop_name,
    CAST(p.image_path AS TEXT)                   AS image_path,
    CAST(p.main_image_url AS TEXT)               AS main_image_url,
    CAST(p.title AS TEXT)                        AS title,
    CAST(p.url AS TEXT)                          AS url,
    CAST(ROUND(ps.price, 2) AS REAL)             AS price,
    CAST(ps.sales_raw AS TEXT)                   AS sales_raw,
    CAST(ps.comments_raw AS TEXT)                AS comments_raw,
    CAST(ps.estimated_sales AS INTEGER)          AS estimated_sales,
    CAST(ROUND(ps.amount, 2) AS REAL)            AS amount,
    CAST(ps.formula AS TEXT)                     AS amount_formula,
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
FROM product_snapshots ps
JOIN products p ON p.id = ps.product_id
LEFT JOIN shops s
       ON s.platform = p.platform
      AND s.shop_id = p.shop_id
WHERE (
        p.shop_name IS NULL
        OR TRIM(p.shop_name) = ''
        OR ps.listed = 0
        OR (ps.amount IS NULL AND ps.eligible = 1)
      )
  AND NOT EXISTS (
      SELECT 1 FROM lead_links ll2 WHERE ll2.snapshot_id = ps.id
  )
"""

LEAD_SUMMARY_VIEW_SQL = """
CREATE VIEW v_lead_summary AS
SELECT
    CAST(l.lead_no AS TEXT)                      AS lead_id,
    CAST(l.run_id AS TEXT)                       AS run_id,
    CAST(l.snapshot_date AS TEXT)                AS snapshot_date,
    CAST(l.platform AS TEXT)                     AS platform,
    CAST(l.platform_group AS TEXT)               AS platform_group,
    CAST(l.shop_key AS TEXT)                     AS shop_key,
    CAST(l.shop_id AS TEXT)                      AS shop_id,
    CAST(COALESCE(l.shop_name, '') AS TEXT)      AS shop_name,
    CAST(COALESCE(l.shop_type, '未知') AS TEXT)  AS shop_type,
    CAST(COALESCE(l.shop_label_raw, '') AS TEXT) AS shop_label_raw,
    CAST(l.ip_codes AS TEXT)                     AS ip_codes,
    CAST(l.link_count AS INTEGER)                AS link_count,
    CAST(ROUND(l.total_amount, 2) AS REAL)       AS shop_amount,
    CAST(
        CASE
            WHEN l.missing_flags LIKE '%金额待核验%'
              OR l.missing_flags LIKE '%金额超出范围,待核验%' THEN '待核验'
            WHEN l.total_amount >= 500000.0 THEN '达标'
            ELSE '临界'
        END AS TEXT
    )                                            AS tier,
    CAST(l.payment_tags AS TEXT)                 AS payment_ability,
    CAST(ROUND(l.confidence, 2) AS REAL)         AS confidence,
    CAST(COALESCE(NULLIF(l.hit_summary, ''), '命中规则') AS TEXT)
                                                 AS hit_reasons_summary,
    CAST(NULLIF(l.missing_flags, '[]') AS TEXT)  AS missing_flags,
    CAST(COALESCE(l.review_result, '') AS TEXT)  AS review_result,
    CAST(COALESCE(l.reviewer, '') AS TEXT)       AS reviewer,
    CAST(CASE COALESCE(l.shop_type, '未知')
        WHEN '企业' THEN 1
        WHEN '个体户' THEN 2
        WHEN '未知' THEN 3
        WHEN '个人' THEN 4
        ELSE 5
    END AS INTEGER)                              AS shop_type_rank,
    CAST(l.caliber_version AS TEXT)              AS caliber_version,
    CAST(l.config_version AS TEXT)               AS config_version
FROM leads l
WHERE l.status = '待复核'
ORDER BY
    shop_type_rank ASC,
    confidence DESC,
    shop_amount DESC
"""

LEAD_LINKS_VIEW_SQL = f"""
CREATE VIEW v_lead_links AS
SELECT
    CAST(lead_id AS TEXT)                        AS lead_id,
    CAST(run_id AS TEXT)                         AS run_id,
    CAST(snapshot_date AS TEXT)                  AS snapshot_date,
    CAST(ip_code AS TEXT)                        AS ip_code,
    CAST(product_id AS TEXT)                     AS product_id,
    CAST(platform AS TEXT)                       AS platform,
    CAST(shop_id AS TEXT)                        AS shop_id,
    CAST(shop_name AS TEXT)                      AS shop_name,
    CAST(image_path AS TEXT)                     AS image_path,
    CAST(main_image_url AS TEXT)                 AS main_image_url,
    CAST(title AS TEXT)                          AS title,
    CAST(url AS TEXT)                            AS url,
    CAST(ROUND(price, 2) AS REAL)                AS price,
    CAST(sales_raw AS TEXT)                      AS sales_raw,
    CAST(comments_raw AS TEXT)                   AS comments_raw,
    CAST(estimated_sales AS INTEGER)             AS estimated_sales,
    CAST(ROUND(amount, 2) AS REAL)               AS amount,
    CAST(amount_formula AS TEXT)                 AS amount_formula,
    CAST(ROUND(text_score, 2) AS REAL)           AS text_score,
    CAST(ROUND(visual_score, 2) AS REAL)         AS visual_score,
    CAST(ROUND(final_score, 2) AS REAL)          AS final_score,
    CAST(hit_reasons AS TEXT)                    AS hit_reasons,
    CAST(missing_flags AS TEXT)                  AS missing_flags
FROM (
{_LEAD_LINKS_SELECT}
UNION ALL
{_STANDALONE_LINKS_SELECT}
)
ORDER BY
    snapshot_date DESC,
    lead_id ASC,
    amount DESC
"""

NEAR_THRESHOLD_VIEW_SQL = """
CREATE VIEW v_near_threshold_shops AS
SELECT
    CAST(l.platform AS TEXT)                     AS platform,
    CAST(l.platform_group AS TEXT)               AS platform_group,
    CAST(l.shop_id AS TEXT)                      AS shop_id,
    CAST(COALESCE(l.shop_name, '') AS TEXT)      AS shop_name,
    CAST(COALESCE(l.shop_type, '未知') AS TEXT)  AS shop_type,
    CAST(l.snapshot_date AS TEXT)                AS snapshot_date,
    CAST(l.link_count AS INTEGER)                AS link_count,
    CAST(ROUND(l.total_amount, 2) AS REAL)       AS shop_amount,
    CAST(300000.0 AS REAL)                       AS near_threshold_low,
    CAST(500000.0 AS REAL)                       AS threshold_amount
FROM leads l
WHERE l.status = '待复核'
  AND l.total_amount >= 300000
  AND l.total_amount <= 500000
ORDER BY shop_amount DESC, l.platform_group ASC, l.shop_name ASC
"""

DROP_VIEWS_SQL = """
DROP VIEW IF EXISTS v_near_threshold_shops;
DROP VIEW IF EXISTS v_lead_verify_links;
DROP VIEW IF EXISTS v_lead_verify_summary;
DROP VIEW IF EXISTS v_lead_links;
DROP VIEW IF EXISTS v_lead_summary;
"""

DROP_VERIFY_VIEWS_SQL = """
DROP VIEW IF EXISTS v_lead_verify_links;
DROP VIEW IF EXISTS v_lead_verify_summary;
"""

CREATE_VIEWS_SQL = [
    LEAD_SUMMARY_VIEW_SQL,
    LEAD_LINKS_VIEW_SQL,
]

__all__ = [
    "COMMENTS_MISSING_FLAG",
    "CREATE_VIEWS_SQL",
    "DROP_VERIFY_VIEWS_SQL",
    "DROP_VIEWS_SQL",
    "LEAD_LINKS_VIEW_SQL",
    "LEAD_SUMMARY_VIEW_SQL",
    "NEAR_THRESHOLD_VIEW_SQL",
]
