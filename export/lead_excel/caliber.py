"""「口径说明」Sheet 的固定文案生成。

版本号由调用方从汇总视图首行提取后通过 ``versions`` 传入；本模块只拼接
固定文案和本次导出统计，不读视图、不重算金额。
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Mapping

from .config import ExportSettings

if TYPE_CHECKING:  # pragma: no cover
    from .workbook import ExportStats


def _fmt_number(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:g}"


def caliber_rows(
    *,
    snapshot: date,
    output_path: Path,
    settings: ExportSettings,
    versions: Mapping[str, str],
    stats: "ExportStats",
) -> list[tuple[str, str]]:
    threshold = _fmt_number(settings.threshold)
    threshold_wan = _fmt_number(settings.threshold / 10_000.0)
    multiplier = _fmt_number(settings.comment_multiplier)
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    caliber_version = (
        versions.get("caliber_version") or settings.caliber_version or "未提供"
    )
    config_version = (
        versions.get("config_version") or settings.config_version or "未提供"
    )

    rows: list[tuple[str, str]] = [
        (
            "文件用途",
            "本文件用于人工复核店铺级侵权线索，并作为录入 SaaS 前的核对底稿；"
            "系统不自动认定侵权，最终结论由人工填写。",
        ),
        (
            "金额公式",
            "单链接金额 = 单价 × 销量下限。评论数不参与计算；金额保留 2 位小数。",
        ),
        (
            "降级规则",
            "价格或销量缺失、无法识别：金额留空并标记待核验，不用评论推算；"
            "真实零销量计算为零。评论缺失不影响金额。",
        ),
        (
            "模糊值",
            "销量原文含「1万+」「300+」等模糊值时，按解析下限计算，"
            "计算式注明「按下限估算」，原文保留在销量原文/评论数原文列。",
        ),
        (
            "店铺聚合",
            "同平台组（淘宝含天猫 / 京东 / 1688）+ 规范化店名在同一日快照、"
            "当日在架的侵权链接金额求和；可跨该店多个委托 IP；不跨日累计；"
            "下架链接不计入。店名规范化：NFKC、去所有空白、英文小写；"
            "1688 店名先 URL 解码。店名为空的链接只出现在链接明细并标记「店铺名缺失」，"
            "不参与店铺合计。同名店仅当双方都有 shop_id 且不同时拆开并标记「同名不同店」。",
        ),
        (
            "线索门槛",
            "保留全部合格候选店铺；列表支持不限制、5万、10万、20万、40万、50万元以上筛选，包含门槛等值。",
        ),
        (
            "档位规则",
            "档位由 v_lead_summary.tier 算好，导出只读不重算：金额存在待核验项时为待核验；"
            f"否则合计 ≥{threshold} 为达标，低于门槛为临界；评论不影响档位。",
        ),
        (
            "排序口径",
            "线索汇总：店铺类型优先级（企业 > 个体户 > 未知 > 个人）→ 置信度降序 → "
            "店铺合计金额降序。链接明细：按汇总线索顺序，同线索内金额降序。",
        ),
        (
            "数据来源",
            "只读 SQLite 视图 v_lead_summary / v_lead_links；导出模块不读底层表、"
            "不重算金额、不重新判定。视图只包含状态为「待复核」的线索。",
        ),
        (
            "复核列",
            "「复核结论」「复核人」两列导出时一律为空，由 Xuan 人工填写；"
            "合法值为 已确认 / 驳回，后续由 review 流程回写数据库。",
        ),
        (
            "快照日期",
            f"{snapshot.isoformat()}（文件名日期：{snapshot.strftime('%Y%m%d')}）",
        ),
        ("caliber_version", str(caliber_version)),
        ("config_version", str(config_version)),
        ("生成时间", generated_at),
        ("生成文件", str(output_path)),
        (
            "本次数据量",
            f"线索 {stats.summary_count} 条；链接 {stats.link_count} 条；"
            f"嵌入缩略图 {stats.image_count} 张。",
        ),
    ]

    warnings = stats.quality_warnings()
    if warnings:
        for index, note in enumerate(warnings, start=1):
            rows.append((f"导出提示 {index}", note))
    else:
        rows.append(("导出提示", "无；命中原因与缩略图检查均通过。"))

    rows.append(
        (
            "免责声明",
            "金额为估算，不构成法律意见；是否构成侵权及赔偿金额以人工复核和法律意见为准。",
        )
    )
    return rows
