#!/usr/bin/env python3
"""只读统计奥特曼打分结果，输出 ultraman_score.md / ultraman_score.json。

统计口径与 backend 判定一致：
  * text_score 取 product_snapshots.text_score；
  * 候选 = product_snapshots.eligible = 1（已过硬排除 + 综合分门槛，
    或 need_vision 触发视觉救援）；
  * need_vision 取 product_snapshots.need_vision。

报告固定拆成两个口径：
  * 本轮采集：``product_snapshots.snapshot_date == --run-date``；
  * 含历史数据：库内全部快照（WU dry-run 会合并 2026-11-05 样例）。
两个口径分别列出快照数、候选数等指标，避免把历史数据混进本轮结论。

只读 sqlite，不启动 backend、不发邮件、不改数据库。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

BINS = (
    ("0", lambda score: score <= 0),
    ("1-19", lambda score: 0 < score < 20),
    ("20-34", lambda score: 20 <= score < 35),
    ("35-59", lambda score: 35 <= score < 60),
    ("60+", lambda score: score >= 60),
)


def _bin_name(score: float | None) -> str:
    if score is None:
        return "未评分"
    value = float(score)
    for name, predicate in BINS:
        if predicate(value):
            return name
    return "未评分"


def _escape(text: Any, limit: int = 80) -> str:
    value = str(text or "").replace("|", "\\|").replace("\r", " ").replace("\n", " ")
    return value if len(value) <= limit else value[: limit - 3] + "..."


def load_rows(db_path: Path) -> list[dict[str, Any]]:
    connection = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        cursor = connection.execute(
            """
            SELECT
                s.id AS snapshot_id,
                p.platform AS platform,
                p.product_id AS product_id,
                p.title AS title,
                s.snapshot_date AS snapshot_date,
                s.text_score AS text_score,
                s.final_score AS final_score,
                s.need_vision AS need_vision,
                s.eligible AS eligible,
                s.exclusion_reason AS exclusion_reason,
                s.hit_reasons AS hit_reasons
            FROM product_snapshots AS s
            JOIN products AS p ON p.id = s.product_id
            ORDER BY s.id
            """
        )
        return [dict(row) for row in cursor.fetchall()]
    finally:
        connection.close()


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """一个口径（本轮 / 含历史）的汇总指标。"""
    total = len(rows)
    bins: Counter[str] = Counter()
    platforms: dict[str, dict[str, int]] = {}
    candidates = 0
    need_vision = 0
    ip_hit = 0
    ultraman_hit = 0
    for row in rows:
        bins[_bin_name(row.get("text_score"))] += 1
        platform = str(row.get("platform") or "unknown")
        stats = platforms.setdefault(
            platform, {"total": 0, "candidates": 0, "need_vision": 0}
        )
        stats["total"] += 1
        if bool(row.get("eligible")):
            candidates += 1
            stats["candidates"] += 1
        if bool(row.get("need_vision")):
            need_vision += 1
            stats["need_vision"] += 1
        reasons_text = str(row.get("hit_reasons") or "")
        if "ip_hit:" in reasons_text:
            ip_hit += 1
        if "ip_hit:奥特曼" in reasons_text:
            ultraman_hit += 1
    return {
        "total": total,
        "candidates": candidates,
        "candidate_rate": (candidates / total) if total else None,
        "need_vision": need_vision,
        "ip_hit": ip_hit,
        "ultraman_hit": ultraman_hit,
        "bins": {name: bins.get(name, 0) for name, _ in BINS},
        "unscored": bins.get("未评分", 0),
        "platforms": platforms,
    }


def build_report(
    db_path: Path,
    rows: list[dict[str, Any]],
    sample_limit: int,
    run_date: str | None = None,
) -> dict[str, Any]:
    dates = sorted(
        {
            str(row.get("snapshot_date")).strip()
            for row in rows
            if str(row.get("snapshot_date") or "").strip()
        }
    )
    current_date = str(run_date).strip() if run_date else (dates[-1] if dates else None)
    if current_date:
        current_rows = [
            row
            for row in rows
            if str(row.get("snapshot_date") or "").strip() == current_date
        ]
    else:
        current_rows = list(rows)
    history_dates = [
        date for date in dates if current_date is not None and date < current_date
    ]
    history_label = "含历史数据（{0}）".format(
        "、".join(history_dates) if history_dates else "无历史"
    )
    current_summary = _summary(current_rows)
    all_summary = _summary(rows)

    # 分层抽样：每个分数区间先取最多 2 条，再按分数补齐到 10 条。
    selected: list[dict[str, Any]] = []
    selected_ids: set[int] = set()
    for name, _ in BINS:
        bucket = [row for row in rows if _bin_name(row.get("text_score")) == name]
        bucket.sort(key=lambda row: (-(row.get("text_score") or 0), str(row.get("title") or "")))
        for row in bucket[:2]:
            snapshot_id = int(row["snapshot_id"])
            if snapshot_id in selected_ids:
                continue
            selected.append(row)
            selected_ids.add(snapshot_id)
    if len(selected) < sample_limit:
        remaining = [row for row in rows if int(row["snapshot_id"]) not in selected_ids]
        remaining.sort(key=lambda row: (-(row.get("text_score") or 0), str(row.get("title") or "")))
        for row in remaining:
            if len(selected) >= sample_limit:
                break
            selected.append(row)
            selected_ids.add(int(row["snapshot_id"]))

    ultraman_rows = [
        row for row in rows if "ip_hit:奥特曼" in str(row.get("hit_reasons") or "")
    ]
    ultraman_rows.sort(
        key=lambda row: (-(row.get("text_score") or 0), str(row.get("title") or ""))
    )
    ultraman_samples = [
        {
            "title": row.get("title"),
            "platform": row.get("platform"),
            "text_score": row.get("text_score"),
            "final_score": row.get("final_score"),
            "candidate": bool(row.get("eligible")),
        }
        for row in ultraman_rows[:sample_limit]
    ]

    samples = []
    for row in selected[:sample_limit]:
        samples.append(
            {
                "title": row.get("title"),
                "platform": row.get("platform"),
                "text_score": row.get("text_score"),
                "final_score": row.get("final_score"),
                "candidate": bool(row.get("eligible")),
                "need_vision": bool(row.get("need_vision")),
                "exclusion_reason": row.get("exclusion_reason"),
            }
        )

    # 顶层字段保持旧消费者兼容（默认取“含历史数据”口径）。
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "db": str(db_path),
        "run_date": current_date,
        "history_dates": history_dates,
        "history_label": history_label,
        "total": all_summary["total"],
        "candidates": all_summary["candidates"],
        "candidate_rate": all_summary["candidate_rate"],
        "need_vision": all_summary["need_vision"],
        "ip_hit": all_summary["ip_hit"],
        "ultraman_hit": all_summary["ultraman_hit"],
        "bins": all_summary["bins"],
        "unscored": all_summary["unscored"],
        "platforms": all_summary["platforms"],
        "current": current_summary,
        "all": all_summary,
        "ultraman_samples": ultraman_samples,
        "samples": samples,
    }


def _render_summary_table(lines: list[str], summary: dict[str, Any]) -> None:
    total = int(summary["total"])
    candidates = int(summary["candidates"])
    lines.extend(
        [
            "| 指标 | 数值 |",
            "| --- | ---: |",
            f"| 快照数 | {total} |",
            f"| 候选数 | {candidates} |",
            f"| 候选占比 | {('n/a' if not total else f'{candidates / total * 100:.1f}%')} |",
            f"| need_vision 数 | {int(summary['need_vision'])} |",
            f"| 命中 IP 词（ip_hit） | {int(summary['ip_hit'])} |",
            f"| 其中命中“奥特曼”（ip_hit:奥特曼） | {int(summary.get('ultraman_hit', 0))} |",
            f"| 未评分 | {int(summary['unscored'])} |",
            "",
        ]
    )


def _render_bins(lines: list[str], summary: dict[str, Any]) -> None:
    total = int(summary["total"])
    lines.extend(
        [
            "| 区间 | 条数 | 占比 |",
            "| --- | ---: | ---: |",
        ]
    )
    for name, _ in BINS:
        count = int(summary["bins"].get(name, 0))
        rate = (count / total * 100) if total else 0.0
        lines.append(f"| {name} | {count} | {rate:.1f}% |")
    if summary["unscored"]:
        lines.append(f"| 未评分 | {int(summary['unscored'])} | 0.0% |")
    lines.append("")


def _render_platforms(lines: list[str], summary: dict[str, Any]) -> None:
    lines.extend(
        [
            "| 平台 | 快照数 | 候选数 | need_vision |",
            "| --- | ---: | ---: | ---: |",
        ]
    )
    for platform in sorted(summary["platforms"]):
        stats = summary["platforms"][platform]
        lines.append(
            f"| {platform} | {stats['total']} | {stats['candidates']} | {stats['need_vision']} |"
        )
    if not summary["platforms"]:
        lines.append("| - | 0 | 0 | 0 |")
    lines.append("")


def _render_caliber(title: str, summary: dict[str, Any]) -> list[str]:
    lines = [f"## {title}", "", "### 总览", ""]
    _render_summary_table(lines, summary)
    lines.extend(["### text_score 分布", ""])
    _render_bins(lines, summary)
    lines.extend(["### 平台分布", ""])
    _render_platforms(lines, summary)
    return lines


def render_markdown(report: dict[str, Any]) -> str:
    current = report.get("current") or {}
    all_summary = report.get("all") or report
    run_date = report.get("run_date") or "-"
    history_label = report.get("history_label") or "含历史数据（无历史）"
    lines: list[str] = [
        "# 奥特曼打分验证",
        "",
        f"- 生成时间：{report['generated_at']}",
        f"- 临时数据库：`{report['db']}`",
        f"- 本轮采集日期：`{run_date}`",
        "- 候选口径：`product_snapshots.eligible = 1`（过硬排除 + 综合分门槛，或 need_vision 触发视觉救援）",
        "",
        "## 口径对比",
        "",
        "| 口径 | 快照数 | 候选数 | 候选占比 | need_vision |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for label, summary in (
        (f"本轮采集（{run_date}）", current),
        (history_label, all_summary),
    ):
        total = int(summary.get("total", 0))
        candidates = int(summary.get("candidates", 0))
        rate = "n/a" if not total else f"{candidates / total * 100:.1f}%"
        lines.append(
            f"| {label} | {total} | {candidates} | {rate} | "
            f"{int(summary.get('need_vision', 0))} |"
        )
    lines.append("")
    lines.extend(_render_caliber(f"本轮采集（{run_date}）", current))
    lines.extend(_render_caliber(history_label, all_summary))
    lines.extend(
        [
            f"## {len(report['samples'])} 条样本",
            "",
            "| # | 标题 | 平台 | text_score | final_score | 候选 | need_vision |",
            "| ---: | --- | --- | ---: | ---: | --- | --- |",
        ]
    )
    for index, sample in enumerate(report["samples"], start=1):
        score = sample["text_score"]
        final = sample["final_score"]
        lines.append(
            "| {0} | {1} | {2} | {3} | {4} | {5} | {6} |".format(
                index,
                _escape(sample["title"]),
                _escape(sample["platform"], 20),
                "-" if score is None else f"{float(score):.2f}",
                "-" if final is None else f"{float(final):.2f}",
                "是" if sample["candidate"] else "否",
                "是" if sample["need_vision"] else "否",
            )
        )
    if not report["samples"]:
        lines.append("| - | 无数据 | - | - | - | - | - |")
    lines.extend(
        [
            "",
            "## 奥特曼命中样本",
            "",
            "| # | 标题 | 平台 | text_score | final_score | 候选 |",
            "| ---: | --- | --- | ---: | ---: | --- |",
        ]
    )
    for index, sample in enumerate(report.get("ultraman_samples", []), start=1):
        score = sample["text_score"]
        final = sample["final_score"]
        lines.append(
            "| {0} | {1} | {2} | {3} | {4} | {5} |".format(
                index,
                _escape(sample["title"]),
                _escape(sample["platform"], 20),
                "-" if score is None else f"{float(score):.2f}",
                "-" if final is None else f"{float(final):.2f}",
                "是" if sample["candidate"] else "否",
            )
        )
    if not report.get("ultraman_samples"):
        lines.append("| - | 无 ip_hit:奥特曼 记录（本次采集可能未命中该关键词） | - | - | - | - |")
    lines.extend(
        [
            "",
            "## 说明",
            "",
            "- 本报告只读 `--dry-run` 保留的临时 sqlite；`backend/data/leads.db` 未被打开或写入。",
            "- “本轮采集”只统计 `snapshot_date == --run-date` 的快照；“含历史数据”统计库内全部快照。",
            "- 样本按分数区间分层抽取（每区间最多 2 条），不足 10 条时按 text_score 补齐。",
            "- `候选=是` 仅表示通过 backend 判定门槛，不代表人工复核结论。",
            "",
        ]
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="统计奥特曼打分结果")
    parser.add_argument("--db", required=True, help="dry-run 保留的 sqlite 路径")
    parser.add_argument("--out-md", required=True, help="Markdown 输出路径")
    parser.add_argument("--out-json", required=True, help="JSON 输出路径")
    parser.add_argument("--samples", type=int, default=10, help="样本条数，默认 10")
    parser.add_argument(
        "--run-date",
        default="",
        help="本轮采集日期 YYYY-MM-DD；用于拆“本轮采集/含历史数据”两个口径",
    )
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.is_file():
        print(f"数据库不存在: {db_path}", file=sys.stderr)
        return 2
    rows = load_rows(db_path)
    report = build_report(
        db_path,
        rows,
        max(1, args.samples),
        run_date=args.run_date.strip() or None,
    )
    out_md = Path(args.out_md)
    out_json = Path(args.out_json)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(render_markdown(report) + "\n", encoding="utf-8")
    out_json.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        "total={0} candidates={1} current_total={2} current_candidates={3} "
        "need_vision={4} bins={5}".format(
            report["total"],
            report["candidates"],
            report["current"]["total"],
            report["current"]["candidates"],
            report["need_vision"],
            report["bins"],
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
