"""Excel 列定义与数据库视图列名映射。

后端对齐说明（重要）
====================
本文件是 Excel 导出模块**唯一**的视图列名映射处。视图定义以
《后端方案_v0.1.md》第 6 节（2026-11-05 更新版）与《Excel导出方案_v0.1》
文内 v0.1.1 为准：**英文列名**，`lead_id` 是 `leads.lead_no` 的别名。

- `VIEW_COLUMNS` 同时包含写入 Excel 的业务列和辅助列：
  `shop_type_rank`、`caliber_version`、`config_version` 只用于排序/口径说明，
  不写入「线索汇总」业务列。
- `SUMMARY_EXCEL_FIELDS` / `LINK_EXCEL_FIELDS` 是实际写入 Excel 的列顺序。
- 「线索编号」「复核结论」「复核人」三个表头固定在 `FIXED_SUMMARY_HEADERS`，
  `review.bat` 回写依赖它们；任何情况下都不随视图列名变化。
- 视图本身只含 `status='待复核'`；导出只加 `WHERE snapshot_date=?`。
"""

from __future__ import annotations

from collections import OrderedDict

# ---------------------------------------------------------------------------
# Sheet 名称
# ---------------------------------------------------------------------------
SUMMARY_SHEET = "线索汇总"
LINKS_SHEET = "链接明细"
CALIBER_SHEET = "口径说明"

# ---------------------------------------------------------------------------
# 视图名（只允许读这两个视图；不读 leads / lead_links 等底层表）
# ---------------------------------------------------------------------------
SUMMARY_VIEW = "v_lead_summary"
LINKS_VIEW = "v_lead_links"

# ---------------------------------------------------------------------------
# 唯一列名映射常量：logical field -> 视图真实列名
# ---------------------------------------------------------------------------
VIEW_COLUMNS: dict[str, OrderedDict[str, str]] = {
    SUMMARY_VIEW: OrderedDict(
        [
            # 17 个 Excel 业务列（顺序即 Sheet 顺序）
            ("lead_id", "lead_id"),
            ("snapshot_date", "snapshot_date"),
            ("platform", "platform"),
            ("shop_name", "shop_name"),
            ("shop_type", "shop_type"),
            ("shop_label_raw", "shop_label_raw"),
            ("ip_codes", "ip_codes"),
            ("link_count", "link_count"),
            ("shop_amount", "shop_amount"),
            ("tier", "tier"),
            ("payment_ability", "payment_ability"),
            ("confidence", "confidence"),
            ("hit_reasons_summary", "hit_reasons_summary"),
            ("missing_flags", "missing_flags"),
            ("review_result", "review_result"),
            ("reviewer", "reviewer"),
            # 店铺辅助字段：旧视图缺失时仍可使用 lead_id 隔离店铺。
            ("shop_key", "shop_key"),
            ("shop_id", "shop_id"),
            ("platform_group", "platform_group"),
            # 视图辅助列：排序 + 口径说明；不写入 Excel 业务列
            ("shop_type_rank", "shop_type_rank"),
            ("caliber_version", "caliber_version"),
            ("config_version", "config_version"),
        ]
    ),
    LINKS_VIEW: OrderedDict(
        [
            ("lead_id", "lead_id"),
            ("ip_code", "ip_code"),
            ("product_id", "product_id"),
            ("shop_name", "shop_name"),
            ("image_path", "image_path"),
            ("main_image_url", "main_image_url"),
            ("title", "title"),
            ("url", "url"),
            ("price", "price"),
            ("sales_raw", "sales_raw"),
            ("comments_raw", "comments_raw"),
            ("estimated_sales", "estimated_sales"),
            ("amount", "amount"),
            ("amount_formula", "amount_formula"),
            ("text_score", "text_score"),
            ("visual_score", "visual_score"),
            ("final_score", "final_score"),
            ("hit_reasons", "hit_reasons"),
            ("missing_flags", "missing_flags"),
            ("platform", "platform"),
            ("shop_id", "shop_id"),
        ]
    ),
}

# 可选列：存在则读取，用于口径说明的版本号。缺失不报错。
# 后端两个视图本身只含「待复核」线索，导出侧只按 snapshot_date 过滤，
# 不再读取/追加 status 条件。
OPTIONAL_VIEW_COLUMNS: dict[str, OrderedDict[str, str]] = {
    SUMMARY_VIEW: OrderedDict(
        [
            ("caliber_version", "caliber_version"),
            ("config_version", "config_version"),
        ]
    ),
    LINKS_VIEW: OrderedDict(),
}


# ---------------------------------------------------------------------------
# 中文表头（需求文档 v0.3 §8.1 / §8.2；顺序即 Excel 列顺序）
# ---------------------------------------------------------------------------
SUMMARY_HEADERS: OrderedDict[str, str] = OrderedDict(
    [
        ("lead_id", "线索编号"),
        ("snapshot_date", "快照日期"),
        ("platform", "平台"),
        ("shop_name", "店铺名"),
        ("shop_type", "店铺类型"),
        ("shop_label_raw", "平台原始标签"),
        ("ip_codes", "涉及IP"),
        ("link_count", "链接数"),
        ("shop_amount", "店铺合计金额"),
        ("tier", "档位"),
        ("payment_ability", "赔付能力标签"),
        ("confidence", "置信度"),
        ("hit_reasons_summary", "命中原因摘要"),
        ("missing_flags", "缺失标记"),
        ("review_result", "复核结论"),
        ("reviewer", "复核人"),
    ]
)

# review.bat 回写依赖这三列表头，必须在任何情况下保持不变。
# 值固定写死；workbook 写入时会用本映射覆盖 SUMMARY_HEADERS 里的同名项。
FIXED_SUMMARY_HEADERS: OrderedDict[str, str] = OrderedDict(
    [
        ("lead_id", "线索编号"),
        ("review_result", "复核结论"),
        ("reviewer", "复核人"),
    ]
)

LINK_HEADERS: OrderedDict[str, str] = OrderedDict(
    [
        ("lead_id", "线索编号"),
        ("ip_code", "IP"),
        ("product_id", "商品ID"),
        ("shop_name", "店铺名"),
        ("image_path", "缩略图"),
        ("main_image_url", "原图链接"),
        ("title", "标题"),
        ("url", "商品链接"),
        ("price", "单价"),
        ("sales_raw", "销量原文"),
        ("comments_raw", "评论数原文"),
        ("estimated_sales", "估算销量"),
        ("amount", "金额"),
        ("amount_formula", "计算式"),
        ("text_score", "文字分"),
        ("visual_score", "视觉分"),
        ("final_score", "综合分"),
        ("hit_reasons", "命中原因"),
        ("missing_flags", "缺失标记"),
    ]
)

HEADERS: dict[str, OrderedDict[str, str]] = {
    SUMMARY_VIEW: SUMMARY_HEADERS,
    LINKS_VIEW: LINK_HEADERS,
}

# 实际写入 Excel 的列顺序（辅助列不写）
SUMMARY_EXCEL_FIELDS: tuple[str, ...] = tuple(SUMMARY_HEADERS)
LINK_EXCEL_FIELDS: tuple[str, ...] = tuple(LINK_HEADERS)

# 当前用户导出格式；以上旧列定义仅保留为视图字段校验契约。
REPORT_HEADERS: OrderedDict[str, str] = OrderedDict(
    [
        ("platform", "平台"),
        ("ip_code", "IP"),
        ("shop_name", "店铺名"),
        ("title", "标题"),
        ("image_path", "缩略图"),
        ("url", "商品链接"),
        ("price", "单价"),
        ("sales_raw", "销量"),
        ("comments_raw", "评论"),
        ("amount", "总金额"),
    ]
)
REPORT_FIELDS: tuple[str, ...] = tuple(REPORT_HEADERS)
REPORT_WIDTHS = {
    "platform": 12,
    "ip_code": 22,
    "shop_name": 30,
    "title": 48,
    "image_path": 15,
    "url": 44,
    "price": 12,
    "sales_raw": 17,
    "comments_raw": 17,
    "amount": 18,
}

# ---------------------------------------------------------------------------
# 列宽（Excel 字符宽）
# ---------------------------------------------------------------------------
SUMMARY_WIDTHS: dict[str, float] = {
    "lead_id": 23,
    "snapshot_date": 12,
    "platform": 9,
    "shop_name": 30,
    "shop_type": 10,
    "shop_label_raw": 18,
    "ip_codes": 18,
    "link_count": 8,
    "shop_amount": 15,
    "tier": 9,
    "payment_ability": 24,
    "confidence": 9,
    "hit_reasons_summary": 52,
    "missing_flags": 26,
    "review_result": 12,
    "reviewer": 10,
}

LINK_WIDTHS: dict[str, float] = {
    "lead_id": 23,
    "ip_code": 13,
    "product_id": 18,
    "shop_name": 30,
    "image_path": 13.5,
    "main_image_url": 18,
    "title": 48,
    "url": 44,
    "price": 10,
    "sales_raw": 17,
    "comments_raw": 15,
    "estimated_sales": 11,
    "amount": 14,
    "amount_formula": 50,
    "text_score": 8,
    "visual_score": 8,
    "final_score": 8,
    "hit_reasons": 48,
    "missing_flags": 22,
}

WIDTHS: dict[str, dict[str, float]] = {
    SUMMARY_VIEW: SUMMARY_WIDTHS,
    LINKS_VIEW: LINK_WIDTHS,
}

# ---------------------------------------------------------------------------
# 字段格式化规则
# ---------------------------------------------------------------------------
MONEY_FIELDS = frozenset({"shop_amount", "price", "amount"})
SCORE_FIELDS = frozenset({"confidence", "text_score", "visual_score", "final_score"})
INTEGER_FIELDS = frozenset({"link_count", "estimated_sales"})

# 复核列在导出时一律留空，供 Xuan 手填；即使视图里意外有值也不输出。
FORCE_BLANK_FIELDS = frozenset({"review_result", "reviewer"})

# 需要写成可点击超链接的字段
HYPERLINK_FIELDS = frozenset({"main_image_url", "url"})

# 这些文本列允许自动换行（长标题/命中原因/计算式）
WRAP_TEXT_FIELDS = frozenset(
    {
        "shop_name",
        "payment_ability",
        "hit_reasons_summary",
        "missing_flags",
        "title",
        "amount_formula",
        "hit_reasons",
        "ip_codes",
    }
)

# 居中显示的短列
CENTER_FIELDS = frozenset(
    {
        "snapshot_date",
        "platform",
        "shop_type",
        "link_count",
        "tier",
        "confidence",
        "price",
        "estimated_sales",
        "amount",
        "text_score",
        "visual_score",
        "final_score",
        "review_result",
        "reviewer",
    }
)

# 店铺类型排序优先级：企业 > 个体户 > 未知 > 个人（需求 v0.3 §5.6）
SHOP_TYPE_RANK: dict[str, int] = {
    "企业": 1,
    "个体户": 2,
    "未知": 3,
    "个人": 4,
}

DEFAULT_THUMBNAIL_WIDTH = 80
DEFAULT_THUMBNAIL_MAX_HEIGHT = 120
