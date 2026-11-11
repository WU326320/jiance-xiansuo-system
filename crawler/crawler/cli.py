"""命令行入口。

用法：
  python -m crawler login --platform taobao
  python -m crawler check-login --platform taobao
  python -m crawler run --platform taobao --date 2026-11-05
  python -m crawler detail --platform jd --ids-file ids.txt
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from crawler import __version__
from crawler.config import AppConfig, load_config
from crawler.core.errors import BrowserUnavailable, ConfigError
from crawler.core.logging_utils import setup_logging
from crawler.login import check_login_platform, login_platform
from crawler.models import run_record_json_dict
from crawler.runner import (
    PLATFORM_NAMES,
    records_to_json,
    run_detail,
    run_platforms,
    today,
    validate_date,
)


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", help="rules.yaml 路径（默认 ./config/rules.yaml）")
    parser.add_argument("--verbose", action="store_true", help="输出 DEBUG 日志")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crawler", description="淘宝/京东/1688 商品采集"
    )
    parser.add_argument("--version", action="version", version=f"crawler {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    login = sub.add_parser("login", help="有界面打开平台登录页，等待人工登录")
    _common(login)
    login.add_argument(
        "platform_pos",
        nargs="?",
        choices=list(PLATFORM_NAMES),
        help="平台（也可用 --platform）",
    )
    login.add_argument(
        "--platform", choices=list(PLATFORM_NAMES), help="平台（兼容 --platform 写法）"
    )
    login.add_argument(
        "--timeout", type=float, default=600.0, help="等待登录秒数，默认 600"
    )
    login.add_argument("--display", default=None, help="X11 DISPLAY，默认环境变量或 :6")
    login.add_argument("--poll", type=float, default=3.0, help="轮询登录态间隔秒数")

    check = sub.add_parser("check-login", help="检查平台登录态")
    _common(check)
    check.add_argument(
        "platform_pos",
        nargs="?",
        default="all",
        choices=[*PLATFORM_NAMES, "all"],
        help="平台或 all（默认 all）",
    )
    check.add_argument(
        "--platform",
        choices=[*PLATFORM_NAMES, "all"],
        help="平台或 all（兼容 --platform 写法）",
    )
    check.add_argument("--headed", action="store_true", help="有界面检查（调试用）")

    run = sub.add_parser("run", help="按关键词矩阵采集列表页")
    _common(run)
    run.add_argument(
        "platform_pos",
        nargs="?",
        choices=[*PLATFORM_NAMES, "all"],
        help="平台或 all（默认 all）",
    )
    run.add_argument(
        "--platform",
        choices=[*PLATFORM_NAMES, "all"],
        help="平台或 all（兼容 --platform 写法）",
    )
    run.add_argument("--date", help="输出日期目录 YYYY-MM-DD，默认当天（配置时区）")
    run.add_argument(
        "--keyword",
        action="append",
        default=None,
        help="临时关键词，可重复；不传则用配置的 IP 矩阵",
    )
    run.add_argument(
        "--max-pages", type=int, default=None, help="旧参数兼容；不再截断页数"
    )
    run.add_argument(
        "--limit-keywords",
        type=int,
        default=None,
        help="旧参数兼容；不再截断关键词数量",
    )
    run.add_argument(
        "--batch",
        default=None,
        help="旧淘宝参数兼容（N/next/all）；按完整关键词矩阵及逐页检查点续采，不截断批次",
    )
    run.add_argument("--no-images", action="store_true", help="不下载主图")
    run.add_argument("--headed", action="store_true", help="有界面运行（调试用）")

    detail = sub.add_parser("detail", help="按 ID 列表抓详情页补字段")
    _common(detail)
    detail.add_argument(
        "platform_pos",
        nargs="?",
        choices=list(PLATFORM_NAMES),
        help="平台（也可用 --platform）",
    )
    detail.add_argument(
        "--platform", choices=list(PLATFORM_NAMES), help="平台（兼容 --platform 写法）"
    )
    detail_ids = detail.add_mutually_exclusive_group(required=False)
    detail_ids.add_argument(
        "--ids-file", help="每行一个商品 ID；淘宝可用 tmall:ID 指定天猫"
    )
    detail_ids.add_argument(
        "--ids",
        nargs="+",
        help="直接指定商品 ID（空格或逗号分隔）；供后端按 product_id 列表调用",
    )
    detail.add_argument("--date", help="输出日期目录 YYYY-MM-DD，默认当天")
    detail.add_argument("--headed", action="store_true", help="有界面运行（调试用）")

    return parser


def _load(args) -> AppConfig:
    return load_config(args.config)


def _platform(args, default: str | None = None) -> str:
    value = (
        getattr(args, "platform", None)
        or getattr(args, "platform_pos", None)
        or default
    )
    if not value:
        raise ConfigError("缺少平台参数：crawler <command> <platform>（或 --platform）")
    return value


def _print_records(records) -> None:
    for record in records:
        print(json.dumps(run_record_json_dict(record), ensure_ascii=False))


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(getattr(args, "verbose", False))
    try:
        if args.command == "login":
            config = _load(args)
            display = args.display or os.environ.get("DISPLAY", ":6")
            return login_platform(
                _platform(args),
                config,
                timeout=args.timeout,
                display=display,
                poll_seconds=args.poll,
            )

        if args.command == "check-login":
            config = _load(args)
            platform = _platform(args, "all")
            names = config.enabled_platforms() if platform == "all" else [platform]
            codes = [
                check_login_platform(name, config, headed=args.headed) for name in names
            ]
            return 0 if all(code == 0 for code in codes) else 1

        if args.command == "run":
            config = _load(args)
            date = validate_date(args.date) if args.date else today(config)
            if args.max_pages is not None and args.max_pages < 0:
                raise ConfigError("--max-pages 兼容值必须 >= 0")
            batch = args.batch
            if batch is not None:
                platform_name = _platform(args, "all")
                if platform_name != "taobao":
                    raise ConfigError(
                        "--batch 只支持淘宝（避免改变京东/1688 采集节奏）"
                    )
                if batch not in {"next", "all"}:
                    try:
                        batch = int(batch)
                    except (TypeError, ValueError) as exc:
                        raise ConfigError("--batch 必须是正整数、next 或 all") from exc
                    if batch < 1:
                        raise ConfigError("--batch 必须 >= 1")
            records = run_platforms(
                [_platform(args, "all")],
                config,
                date=date,
                keywords=args.keyword,
                max_pages=args.max_pages,
                headless=False if args.headed else None,
                download_images=False if args.no_images else None,
                limit_keywords=args.limit_keywords,
                batch=batch,
            )
            _print_records(records)
            return (
                0
                if all(record.status in {"ok", "disabled"} for record in records)
                else 1
            )

        if args.command == "detail":
            config = _load(args)
            date = validate_date(args.date) if args.date else today(config)
            ids: list[str] = []
            if args.ids:
                for chunk in args.ids:
                    ids.extend(
                        part.strip()
                        for part in str(chunk).replace("，", ",").split(",")
                        if part.strip()
                    )
            if args.ids_file:
                ids_path = Path(args.ids_file)
                if not ids_path.exists():
                    raise ConfigError(f"ID 文件不存在: {ids_path}")
                ids.extend(
                    line.strip()
                    for line in ids_path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                )
            ids = list(dict.fromkeys(ids))
            if not ids:
                raise ConfigError("缺少商品 ID：请用 --ids 或 --ids-file 指定")
            record = run_detail(
                _platform(args),
                config,
                ids=ids,
                date=date,
                headless=False if args.headed else None,
            )
            _print_records([record])
            return 0 if record.status in {"ok", "disabled", "skipped"} else 1

        parser.error(f"未知命令: {args.command}")
        return 2
    except ConfigError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2
    except BrowserUnavailable as exc:
        print(f"浏览器不可用: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("已中断", file=sys.stderr)
        return 130
    except ValueError as exc:
        print(f"参数错误: {exc}", file=sys.stderr)
        return 2


__all__ = ["build_parser", "main", "records_to_json", "PLATFORM_NAMES"]
