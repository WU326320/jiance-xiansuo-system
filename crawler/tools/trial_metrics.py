#!/usr/bin/env python3
"""试采指标统计：读取 ``out/<date>/*.jsonl`` 并输出 JSON + Markdown。

用法（Windows / Linux 相同）::

    python tools/trial_metrics.py --date 2026-11-05
    python tools/trial_metrics.py out/2026-11-05
    python tools/trial_metrics.py --input-dir out/2026-11-05 \
        --json-out out/2026-11-05/trial_metrics.json \
        --md-out out/2026-11-05/trial_metrics.md

统计口径：
- 先按 ``platform + product_id`` 去重；同键多次出现时合并非空字段、合并 flags。
  详情文件（``*_detail.jsonl``）中的评论数/销量等会覆盖/补齐列表行。
- flags 按合并后的最终字段重算派生缺失标记，避免旧行残留造成“有值却标缺失”。
- “预筛后进入详情页”按 ``*_detail.jsonl`` 中出现的唯一 ``platform+product_id`` 计数。
- “候选店名非空率”的候选口径=标题包含本次 ``search_keyword`` 的链接；
  1688 店名先解码并清理源数据前缀，再判断是否为空。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from crawler.core.parse import is_lower_bound, normalize_shop_id  # noqa: E402
from crawler.platforms.ali1688 import decode_shop_name  # noqa: E402

AUTO_MISSING_FLAGS = {
    "价格缺失",
    "主图缺失",
    "店铺ID缺失",
    "店铺名缺失",
    "销量缺失",
    "评论缺失",
    "按下限估算",
}
PLATFORM_ORDER = ("taobao", "tmall", "jd", "1688")
METRIC_FIELDS = (
    "shop_id",
    "shop_id_raw",
    "sales_raw",
    "comments_raw",
    "shop_type_raw_hint",
    "price",
    "title",
    "main_image_url",
)
SAMPLE_FIELDS = (
    "platform",
    "product_id",
    "title",
    "shop_id",
    "shop_name",
    "shop_id_raw",
    "shop_type_raw_hint",
    "price_raw",
    "price",
    "sales_raw",
    "sales",
    "comments_raw",
    "comments",
    "main_image_url",
    "flags",
)
_PLATFORM_ALIASES = {"淘宝": "taobao", "天猫": "tmall", "京东": "jd", "1688": "1688"}


def _is_empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _shop_name_for_metric(row: dict[str, Any], platform: str) -> str:
    """1688 店名先解码（含前缀清理）再判空；其他平台原样。"""
    value = decode_shop_name(row.get("shop_name")) if platform == "1688" else row.get("shop_name")
    return str(value or "").strip()


def _keyword_hit(row: dict[str, Any]) -> bool:
    keyword = str(row.get("search_keyword") or "").strip().lower()
    title = str(row.get("title") or "").strip().lower()
    return bool(keyword and title and keyword in title)


def _is_candidate(row: dict[str, Any]) -> bool:
    """候选口径：标题包含本次搜索关键词。

    与采集端 ``1688`` 店名补抓的候选筛选保持一致（命中关键词才补抓），
    这样“候选店名非空率”能直接验证补抓效果。
    """
    return _keyword_hit(row)


def _refresh_flags(row: dict[str, Any]) -> list[str]:
    flags = [
        str(flag)
        for flag in dict.fromkeys(row.get("flags") or [])
        if str(flag) not in AUTO_MISSING_FLAGS
    ]

    def add(flag: str) -> None:
        if flag not in flags:
            flags.append(flag)

    if _is_empty(row.get("price")):
        add("价格缺失")
    if _is_empty(row.get("main_image_url")):
        add("主图缺失")
    if not str(row.get("shop_name") or "").strip():
        add("店铺名缺失")
    if _is_empty(row.get("sales")):
        add("销量缺失")
    if _is_empty(row.get("comments")):
        add("评论缺失")
    if is_lower_bound(row.get("sales_raw")) or is_lower_bound(row.get("comments_raw")):
        add("按下限估算")
    return flags


def _merge_row(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key, value in source.items():
        if key == "flags":
            continue
        if _is_empty(target.get(key)) and not _is_empty(value):
            target[key] = value
    flags = list(dict.fromkeys([*(target.get("flags") or []), *(source.get("flags") or [])]))
    target["flags"] = flags


def _platform_key(value: Any) -> str:
    text = str(value or "").strip()
    return _PLATFORM_ALIASES.get(text, text)


def _read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    try:
        handle = path.open(encoding="utf-8-sig", errors="replace")
    except OSError:
        return
    with handle:
        for line in handle:
            text = line.strip()
            if not text:
                continue
            try:
                value = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                yield value


def _jsonable(value: Any) -> Any:
    if hasattr(value, "quantize"):
        try:
            return float(value)
        except (TypeError, ValueError):
            return str(value)
    return value


_DETAIL_CONTENT_FIELDS = (
    "title",
    "shop_id",
    "shop_name",
    "price",
    "comments",
    "sales",
    "main_image_url",
)


def _detail_opened(row: dict[str, Any]) -> bool:
    """详情行是否真的打开了页面并解析出内容（区别于仅进入/风控失败）。

    ``*_detail.jsonl`` 只在 ``parse_detail`` 返回 patch 时写行；下架页会写
    ``listed=false``，全空 patch 也没有可用字段。两者都不算成功打开。
    """
    if row.get("listed") is False:
        return False
    return any(not _is_empty(row.get(field)) for field in _DETAIL_CONTENT_FIELDS)


def _shop_map(input_dir: str | Path) -> dict[str, dict[str, Any]]:
    """按 shop_name 汇总某次运行的 shop_id 集合（只看列表文件）。"""
    input_dir = Path(input_dir)
    shops: dict[str, dict[str, Any]] = {}
    for path in sorted(input_dir.glob("*.jsonl")):
        if path.stem.endswith("_detail"):
            continue
        for row in _read_jsonl(path):
            name = str(row.get("shop_name") or "").strip()
            if _platform_key(row.get("platform")) == "1688":
                name = decode_shop_name(name) or ""
            if not name:
                continue
            shop_id = normalize_shop_id(row.get("shop_id"))
            entry = shops.setdefault(
                name,
                {"shop_ids": set(), "rows": 0, "platforms": set()},
            )
            entry["rows"] += 1
            platform = _platform_key(row.get("platform"))
            if platform:
                entry["platforms"].add(platform)
            if shop_id:
                entry["shop_ids"].add(shop_id)
    return shops


def collect_shop_consistency(
    run_b: str | Path,
    run_c: str | Path,
) -> dict[str, Any]:
    """比较两次输出目录中同店（按 shop_name）的 shop_id 是否一致。

    只比较有 shop_name 的列表行；店名在单次运行里出现多个 shop_id 时保留
    全部值，表格里用 ``/`` 连接，结果标为“多值”，不做猜测。
    """
    map_b = _shop_map(run_b)
    map_c = _shop_map(run_c)
    names = sorted(set(map_b) | set(map_c), key=lambda value: (value not in map_b or value not in map_c, value))
    rows: list[dict[str, Any]] = []
    consistent = inconsistent = both_missing = only_a = only_b = 0
    for name in names:
        info_b = map_b.get(name)
        info_c = map_c.get(name)
        ids_b = sorted(info_b["shop_ids"]) if info_b else []
        ids_c = sorted(info_c["shop_ids"]) if info_c else []
        if info_b and info_c:
            if ids_b and ids_c:
                if ids_b == ids_c:
                    status = "一致"
                    consistent += 1
                else:
                    status = "不一致"
                    inconsistent += 1
            elif not ids_b and not ids_c:
                # 两边都没有有效 shop_id：不算“不一致”，单独标“均缺失”。
                status = "均缺失"
                both_missing += 1
            else:
                # 只有一边有 shop_id：无法证明同店 ID 一致，按缺失/不一致计。
                status = "缺失"
                inconsistent += 1
        elif info_b:
            status = "仅 b"
            only_a += 1
        else:
            status = "仅 c"
            only_b += 1
        platforms = sorted((info_b or {}).get("platforms", set()) | (info_c or {}).get("platforms", set()))
        rows.append(
            {
                "shop_name": name,
                "shop_id_b": "/".join(ids_b) if ids_b else None,
                "shop_id_c": "/".join(ids_c) if ids_c else None,
                "status": status,
                "rows_b": info_b["rows"] if info_b else 0,
                "rows_c": info_c["rows"] if info_c else 0,
                "platforms": platforms,
            }
        )
    return {
        "run_b": str(Path(run_b)),
        "run_c": str(Path(run_c)),
        "common": consistent + inconsistent + both_missing,
        "consistent": consistent,
        "inconsistent": inconsistent,
        "both_missing": both_missing,
        "only_b": only_a,
        "only_c": only_b,
        "rows": rows,
    }


def render_shop_consistency(metrics: dict[str, Any]) -> str:
    """渲染 b/c 两次运行的同店 shop_id 一致性表格。"""
    lines = [
        "## 同店跨运行 shop_id 一致性（b vs c）",
        "",
        f"- b 目录：`{metrics['run_b']}`",
        f"- c 目录：`{metrics['run_c']}`",
        (
            f"- 共同店铺：{metrics['common']}；一致：{metrics['consistent']}；"
            f"不一致：{metrics['inconsistent']}；均缺失：{metrics['both_missing']}；"
            f"仅 b：{metrics['only_b']}；仅 c：{metrics['only_c']}"
        ),
        "",
        "| 店铺名 | b shop_id | c shop_id | 结果 | b 行数 | c 行数 | 平台 |",
        "| --- | --- | --- | --- | ---: | ---: | --- |",
    ]
    for row in metrics["rows"]:
        lines.append(
            "| {name} | {b} | {c} | {status} | {rb} | {rc} | {platforms} |".format(
                name=str(row["shop_name"]).replace("|", "\\|"),
                b=row["shop_id_b"] or "-",
                c=row["shop_id_c"] or "-",
                status=row["status"],
                rb=row["rows_b"],
                rc=row["rows_c"],
                platforms=",".join(row["platforms"]) or "-",
            )
        )
    if not metrics["rows"]:
        lines.append("| （无同名店铺） | - | - | - | 0 | 0 | - |")
    lines.append("")
    return "\n".join(lines)


def collect_metrics(input_dir: str | Path) -> dict[str, Any]:
    """读取目录内全部 JSONL，返回可直接写 JSON 的指标字典。"""
    input_dir = Path(input_dir)
    files = sorted(input_dir.glob("*.jsonl"))
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []
    detail_keys: set[tuple[str, str]] = set()
    detail_rows: dict[tuple[str, str], dict[str, Any]] = {}
    file_stats: list[dict[str, Any]] = []

    run_logs: list[dict[str, Any]] = []
    for path in files:
        if path.name == "run_logs.jsonl":
            run_logs.extend(dict(row) for row in _read_jsonl(path))
            file_stats.append(
                {
                    "file": path.name,
                    "kind": "run_logs",
                    "rows": len(run_logs),
                }
            )
            continue
        is_detail = path.stem.endswith("_detail")
        rows = 0
        for row in _read_jsonl(path):
            platform = _platform_key(row.get("platform"))
            product_id = str(row.get("product_id") or "").strip()
            if not platform or not product_id:
                continue
            key = (platform, product_id)
            rows += 1
            if key not in merged:
                merged[key] = dict(row)
                merged[key]["platform"] = platform
                order.append(key)
            else:
                _merge_row(merged[key], row)
            if is_detail:
                detail_keys.add(key)
                detail_rows[key] = row
        file_stats.append(
            {
                "file": path.name,
                "kind": "detail" if is_detail else "list",
                "rows": rows,
            }
        )

    detail_success_keys = {
        key for key, row in detail_rows.items() if _detail_opened(row)
    }

    rows_by_platform: dict[str, list[dict[str, Any]]] = {}
    for key in order:
        row = merged[key]
        row["flags"] = _refresh_flags(row)
        rows_by_platform.setdefault(key[0], []).append(row)

    platform_names = [name for name in PLATFORM_ORDER if name in rows_by_platform]
    platform_names.extend(
        sorted(name for name in rows_by_platform if name not in PLATFORM_ORDER)
    )

    platforms: dict[str, Any] = {}
    for platform in platform_names:
        rows = rows_by_platform[platform]
        total = len(rows)
        field_stats: dict[str, Any] = {}
        for field in METRIC_FIELDS:
            if field == "shop_id":
                non_empty = sum(1 for row in rows if normalize_shop_id(row.get(field)) is not None)
            elif field == "shop_id_raw":
                non_empty = sum(1 for row in rows if normalize_shop_id(row.get(field)) is not None)
            else:
                non_empty = sum(1 for row in rows if not _is_empty(row.get(field)))
            field_stats[field] = {
                "non_empty": non_empty,
                "total": total,
                "rate": (non_empty / total) if total else None,
            }

        fuzzy_total = 0
        fuzzy_success = 0
        rows_with_fuzzy = 0
        rows_parsed = 0
        for row in rows:
            row_fuzzy = 0
            row_ok = 0
            for raw_field, int_field in (
                ("sales_raw", "sales"),
                ("comments_raw", "comments"),
            ):
                raw = row.get(raw_field)
                if not is_lower_bound(raw):
                    continue
                row_fuzzy += 1
                value = row.get(int_field)
                if isinstance(value, int) and not isinstance(value, bool):
                    row_ok += 1
            fuzzy_total += row_fuzzy
            fuzzy_success += row_ok
            if row_fuzzy:
                rows_with_fuzzy += 1
                if row_ok == row_fuzzy:
                    rows_parsed += 1

        with_keyword = [row for row in rows if row.get("search_keyword") and row.get("title")]
        title_hits = sum(
            1
            for row in with_keyword
            if str(row["search_keyword"]).strip().lower()
            in str(row["title"]).strip().lower()
        )

        comments_actual = sum(
            1
            for row in rows
            if isinstance(row.get("comments"), int) and not isinstance(row.get("comments"), bool)
        )
        shop_id_actual = sum(1 for row in rows if normalize_shop_id(row.get("shop_id")) is not None)
        shop_name_actual = sum(1 for row in rows if _shop_name_for_metric(row, platform))
        candidate_rows = [row for row in rows if _is_candidate(row)]
        candidate_total = len(candidate_rows)
        candidate_shop_name_actual = sum(
            1 for row in candidate_rows if _shop_name_for_metric(row, platform)
        )
        detail_count = sum(1 for key in detail_keys if key[0] == platform)
        detail_success = sum(1 for key in detail_success_keys if key[0] == platform)

        flag_counts = Counter(
            str(flag) for row in rows for flag in (row.get("flags") or [])
        )
        samples = [
            {field: _jsonable(row.get(field)) for field in SAMPLE_FIELDS}
            for row in rows[:3]
        ]

        platforms[platform] = {
            "count": total,
            "list_count": total,
            "field_non_empty": field_stats,
            "fuzzy_parse": {
                "total": fuzzy_total,
                "success": fuzzy_success,
                "rate": (fuzzy_success / fuzzy_total) if fuzzy_total else None,
                "rows_with_fuzzy": rows_with_fuzzy,
                "rows_parsed": rows_parsed,
            },
            "title_keyword_hit": {
                "hits": title_hits,
                "total": len(with_keyword),
                "rate": (title_hits / len(with_keyword)) if with_keyword else None,
            },
            "detail_count": detail_count,
            "detail_success": detail_success,
            "comments_actual": comments_actual,
            "shop_id_actual": shop_id_actual,
            "shop_name_actual": shop_name_actual,
            "candidate_count": candidate_total,
            "candidate_shop_name_actual": candidate_shop_name_actual,
            "comments_rate": (comments_actual / total) if total else None,
            "shop_id_rate": (shop_id_actual / total) if total else None,
            "shop_name_rate": (shop_name_actual / total) if total else None,
            "candidate_shop_name_rate": (
                candidate_shop_name_actual / candidate_total if candidate_total else None
            ),
            "fuzzy_rate": (fuzzy_success / fuzzy_total) if fuzzy_total else None,
            "flags": dict(flag_counts.most_common()),
            "samples": samples,
        }

    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "input_dir": str(input_dir),
        "files": file_stats,
        "total_unique": len(order),
        "platforms": platforms,
        "run_logs": run_logs,
        "run_log_count": len(run_logs),
    }


def _fmt_rate(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _fmt_value(value: Any) -> str:
    if value is None:
        return "-"
    text = str(value).replace("|", "\\|").replace("\n", " ")
    return text if len(text) <= 120 else text[:117] + "..."


def render_markdown(metrics: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# 试采指标")
    lines.append("")
    lines.append(f"- 生成时间：{metrics['generated_at']}")
    lines.append(f"- 输入目录：`{metrics['input_dir']}`")
    lines.append(f"- 去重后总条数：{metrics['total_unique']}")
    files = ", ".join(
        f"{item['file']}({item['kind']},{item['rows']}行)" for item in metrics["files"]
    )
    lines.append(f"- 输入文件：{files or '无'}")
    lines.append("")

    if not metrics["platforms"]:
        lines.append("> 没有可统计的 JSONL 记录。")
        lines.append("")
        return "\n".join(lines)

    lines.append("## 总览")
    lines.append("")
    lines.append(
        "| 平台 | 列表条数 | 进入详情 | 详情成功 | 评论数取到 | 店铺名非空率 | 候选店名非空率 | 模糊解析成功率 |"
    )
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for platform, item in metrics["platforms"].items():
        fuzzy = item["fuzzy_parse"]
        lines.append(
            f"| {platform} | {item['count']} | {item['detail_count']} | "
            f"{item['detail_success']} | {item['comments_actual']} | "
            f"{_fmt_rate(item['shop_name_rate'])} | "
            f"{_fmt_rate(item.get('candidate_shop_name_rate'))} | "
            f"{_fmt_rate(fuzzy['rate'])} ({fuzzy['success']}/{fuzzy['total']}) |"
        )
    lines.append("")
    if metrics.get("run_logs"):
        lines.append(
            f"- run_logs：{metrics.get('run_log_count', 0)} 行"
            f"（每平台每次运行至少 1 行，含登录/条数/起止时间）"
        )
    else:
        lines.append("- run_logs：0 行（异常，检查 crawler 运行记录）")
    lines.append("")

    for platform, item in metrics["platforms"].items():
        lines.append(f"## {platform}")
        lines.append("")
        lines.append(
            f"条数 **{item['count']}**；预筛后进入详情页 **{item['detail_count']}** 条。"
        )
        lines.append("")
        lines.append("| 指标 | 数值 |")
        lines.append("| --- | ---: |")
        lines.append(f"| 列表条数 | {item['list_count']} |")
        lines.append(f"| 进入详情页条数 | {item['detail_count']} |")
        lines.append(f"| 详情页成功打开数 | {item['detail_success']} |")
        lines.append(f"| 评论数实际取到数 | {item['comments_actual']} |")
        lines.append(f"| shop_id 实际取到数 | {item['shop_id_actual']} |")
        lines.append(f"| 店铺名实际取到数 | {item['shop_name_actual']} |")
        lines.append(f"| 候选条数（标题命中关键词） | {item.get('candidate_count', 0)} |")
        lines.append(f"| 候选店名实际取到数 | {item.get('candidate_shop_name_actual', 0)} |")
        lines.append(f"| 评论数非空率 | {_fmt_rate(item['comments_rate'])} |")
        lines.append(f"| 店铺名非空率 | {_fmt_rate(item['shop_name_rate'])} |")
        lines.append(f"| 候选店名非空率 | {_fmt_rate(item.get('candidate_shop_name_rate'))} |")
        lines.append(f"| 模糊值解析成功率 | {_fmt_rate(item['fuzzy_rate'])} |")
        lines.append("")
        lines.append("### 字段非空率")
        lines.append("")
        lines.append("| 字段 | 非空 | 总数 | 比率 |")
        lines.append("| --- | ---: | ---: | ---: |")
        for field, stat in item["field_non_empty"].items():
            lines.append(
                f"| {field} | {stat['non_empty']} | {stat['total']} | {_fmt_rate(stat['rate'])} |"
            )
        lines.append("")
        fuzzy = item["fuzzy_parse"]
        hit = item["title_keyword_hit"]
        lines.append("### 解析与命中")
        lines.append("")
        lines.append(
            f"- 模糊值解析：成功 {fuzzy['success']}/{fuzzy['total']}，"
            f"成功率 {_fmt_rate(fuzzy['rate'])}；含模糊值行 {fuzzy['rows_with_fuzzy']}，"
            f"其中全部解析成功 {fuzzy['rows_parsed']} 行。"
        )
        lines.append(
            f"- 标题包含关键词：{hit['hits']}/{hit['total']}，命中率 {_fmt_rate(hit['rate'])}。"
        )
        lines.append(
            "- 候选店名非空率：候选=标题包含本次搜索关键词的链接；"
            "1688 店名按解码并清理前缀后的值判空；目标 ≥90%。"
        )
        lines.append("")
        lines.append("### Flag 分布")
        lines.append("")
        if item["flags"]:
            for flag, count in item["flags"].items():
                lines.append(f"- `{flag}`：{count}")
        else:
            lines.append("- 无")
        lines.append("")
        lines.append("### 样本（3 条）")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(item["samples"], ensure_ascii=False, indent=2, default=str))
        lines.append("```")
        lines.append("")
    return "\n".join(lines)


def write_outputs(
    metrics: dict[str, Any],
    *,
    json_path: str | Path,
    md_path: str | Path,
) -> tuple[Path, Path]:
    json_path = Path(json_path)
    md_path = Path(md_path)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    md_path.write_text(render_markdown(metrics) + "\n", encoding="utf-8")
    return json_path, md_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="统计 out/<date>/*.jsonl 试采指标")
    parser.add_argument("input_dir", nargs="?", help="输入目录（默认 out/<date>）")
    parser.add_argument("--input-dir", dest="input_dir_opt", help="输入目录（与位置参数等价）")
    parser.add_argument("--date", help="日期 YYYY-MM-DD（默认当天）")
    parser.add_argument("--out-root", default="out", help="输出根目录，默认 ./out")
    parser.add_argument("--json-out", help="JSON 输出路径（默认 <输入目录>/trial_metrics.json）")
    parser.add_argument("--md-out", help="Markdown 输出路径（默认 <输入目录>/trial_metrics.md）")
    parser.add_argument(
        "--compare-dir",
        action="append",
        default=[],
        metavar="DIR",
        help="同店 shop_id 一致性比对目录，需传两次（b 在前，c 在后）",
    )
    parser.add_argument("--consistency-md", help="一致性 Markdown 输出路径")
    parser.add_argument("--consistency-json", help="一致性 JSON 输出路径")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    input_arg = args.input_dir or args.input_dir_opt
    if input_arg:
        input_dir = Path(input_arg)
    else:
        date = args.date or datetime.now().astimezone().strftime("%Y-%m-%d")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            print(f"日期格式错误: {date!r}（应为 YYYY-MM-DD）", file=sys.stderr)
            return 2
        input_dir = Path(args.out_root) / date
    if not input_dir.exists():
        print(f"输入目录不存在: {input_dir}", file=sys.stderr)
        return 2

    metrics = collect_metrics(input_dir)
    json_path, md_path = write_outputs(
        metrics,
        json_path=args.json_out or (input_dir / "trial_metrics.json"),
        md_path=args.md_out or (input_dir / "trial_metrics.md"),
    )
    print(f"JSON: {json_path}")
    print(f"Markdown: {md_path}")
    print(f"去重后条数: {metrics['total_unique']}")
    for platform, item in metrics["platforms"].items():
        print(
            f"{platform}: count={item['count']} detail={item['detail_count']} "
            f"flags={item['flags']}"
        )

    if args.compare_dir:
        if len(args.compare_dir) != 2:
            print("--compare-dir 必须恰好传两次（b 在前，c 在后）", file=sys.stderr)
            return 2
        consistency = collect_shop_consistency(args.compare_dir[0], args.compare_dir[1])
        text = render_shop_consistency(consistency)
        md_path = Path(args.consistency_md or (input_dir / "shop_id_consistency.md"))
        json_out = Path(args.consistency_json or (input_dir / "shop_id_consistency.json"))
        md_path.parent.mkdir(parents=True, exist_ok=True)
        json_out.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(text + "\n", encoding="utf-8")
        json_out.write_text(
            json.dumps(consistency, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        print(text)
        print(f"一致性 JSON: {json_out}")
        print(f"一致性 Markdown: {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
