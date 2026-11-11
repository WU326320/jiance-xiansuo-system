"""命令行入口：python -m pipeline <command>。"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

from pipeline import jd_detail as jd_detail_module
from pipeline.config import ConfigError, Settings, load_config
from pipeline.crawler_adapter import FileCrawlerAdapter
from pipeline.db import (
    create_engine_for,
    ensure_schema,
    make_session_factory,
    session_scope,
)
from pipeline.ingest import load_platform_dir, read_jsonl, read_run_record
from pipeline.lock import AlreadyRunning
from pipeline.logging_utils import setup_run_logger
from pipeline.notify import send_test_email
from sqlalchemy import select

from pipeline.models import Product, ProductSnapshot, Shop
from pipeline.review import import_review_excel, mark_saas
from pipeline.runner import (
    PipelineRunner,
    exception_alert,
    make_taobao_detail_fetcher,
)
from pipeline.scoring import score_snapshot
from pipeline.smtp_setup import setup_smtp
from pipeline.store import upsert_many

logger = logging.getLogger("pipeline")


def _settings(args: argparse.Namespace) -> Settings:
    return load_config(getattr(args, "config", None))


def make_jd_detail_fetcher(settings: Settings, *, logger=None):
    """默认入口的京东详情补抓器；测试可 monkeypatch 本函数注入假 fetcher。"""

    return jd_detail_module.make_jd_detail_fetcher(settings, logger=logger)


def _parse_platforms(values: list[str] | None) -> list[str] | None:
    if not values:
        return None
    if isinstance(values, str):
        values = [values]
    result: list[str] = []
    for value in values:
        result.extend(part.strip() for part in str(value).split(",") if part.strip())
    return result or None


def _session(settings: Settings):
    ensure_schema(settings)
    engine = create_engine_for(settings)
    return engine, make_session_factory(engine)


def cmd_run(args: argparse.Namespace) -> int:
    settings = _settings(args)
    platforms = _parse_platforms(args.platform)
    adapter = None
    if args.input_dir:
        adapter = FileCrawlerAdapter(args.input_dir)
    runner = PipelineRunner(
        settings,
        adapter=adapter,
        jd_detail_fetcher=make_jd_detail_fetcher(settings),
        taobao_detail_fetcher=make_taobao_detail_fetcher(settings),
    )
    try:
        summary = runner.run(
            platforms=platforms,
            run_date=args.date,
            dry_run=args.dry_run,
            allow_alert=not args.no_alert,
        )
    except AlreadyRunning as exc:
        print(f"已在运行：{exc}")
        print("第二次运行已退出，本次未做任何写库操作。")
        return 2
    except Exception as exc:  # noqa: BLE001
        run_logger = setup_run_logger(settings, "exception")
        run_logger.exception("运行出现未捕获异常")
        exception_alert(settings, exc, logger=run_logger)
        print(f"运行失败：{type(exc).__name__}: {exc}")
        return 1
    for line in summary.lines():
        print(line)
    return 1 if summary.failed else 0


def cmd_setup_smtp(args: argparse.Namespace) -> int:
    settings = _settings(args)
    try:
        result = setup_smtp(settings, email=args.email)
    except Exception as exc:  # noqa: BLE001
        print(f"写入凭据管理器失败：{type(exc).__name__}: {exc}")
        return 1
    print(f"已写入 Windows 凭据管理器：服务名={result.service}，用户名={result.username}")
    print("授权码未写入任何文件；可用 python -m pipeline setup-smtp 重新设置。")
    # 保存成功后自动执行 test-smtp，避免“写入成功但实际发不出去”。
    test = send_test_email(settings)
    if test.sent:
        print("自动测试：已发送")
        return 0
    print(f"自动测试失败：{test.reason or '未知原因'}")
    return 1


def cmd_test_smtp(args: argparse.Namespace) -> int:
    """按配置发送一封测试邮件，窗口只输出已脱敏的原因。"""

    settings = _settings(args)
    result = send_test_email(settings)
    if result.sent:
        print("已发送")
        return 0
    print(f"发送失败：{result.reason or '未知原因'}")
    return 1


def cmd_review_import(args: argparse.Namespace) -> int:
    settings = _settings(args)
    engine, factory = _session(settings)
    try:
        with session_scope(factory) as session:
            stats = import_review_excel(session, args.excel)
            session.commit()
    except Exception as exc:  # noqa: BLE001
        print(f"复核回写失败：{type(exc).__name__}: {exc}")
        return 1
    finally:
        engine.dispose()
    print(
        f"复核回写完成：更新 {stats.updated} 条，未变 {stats.unchanged} 条，"
        f"跳过 {stats.skipped} 条，缺失 {stats.missing} 条。"
    )
    for row, reason in stats.skipped_rows:
        print(f"  第 {row} 行跳过：{reason}")
    for error in stats.errors:
        print(f"  {error}")
    return 0


def cmd_review_mark_saas(args: argparse.Namespace) -> int:
    settings = _settings(args)
    engine, factory = _session(settings)
    try:
        with session_scope(factory) as session:
            stats = mark_saas(session, args.lead_no, operator=args.operator)
            session.commit()
    finally:
        engine.dispose()
    print(f"标记已录入SaaS：成功 {len(stats.updated)} 条。")
    for lead_no, reason in stats.skipped:
        print(f"  {lead_no} 跳过：{reason}")
    for lead_no in stats.missing:
        print(f"  {lead_no} 不存在")
    return 0 if not stats.missing else 1


def cmd_ingest(args: argparse.Namespace) -> int:
    settings = _settings(args)
    run_date = date.fromisoformat(args.date) if args.date else date.today()
    platform = args.platform
    if args.jsonl:
        items = read_jsonl(Path(args.jsonl))
        record = read_run_record(Path(args.run_json)) if args.run_json else {}
    else:
        directory = Path(args.input_dir) if args.input_dir else settings.crawler_output_dir / run_date.isoformat()
        data = load_platform_dir(directory, platform)
        items, record = data.items, data.run_record
    engine, factory = _session(settings)
    try:
        with session_scope(factory) as session:
            snapshots, dropped = upsert_many(
                session,
                platform=platform,
                items=items,
                settings=settings,
                run_id=args.run_id or f"ingest_{run_date:%Y%m%d}",
                snapshot_date=run_date,
            )
            seen = set()
            for snapshot in snapshots:
                if snapshot.id in seen:
                    continue
                seen.add(snapshot.id)
                product = session.get(Product, snapshot.product_id)
                shop = (
                    session.scalar(
                        select(Shop).where(
                            Shop.platform == product.platform,
                            Shop.shop_id == product.shop_id,
                        )
                    )
                    if product and product.shop_id
                    else None
                )
                if product:
                    score_snapshot(
                        session,
                        settings=settings,
                        snapshot=snapshot,
                        product=product,
                        shop=shop,
                    )
            session.commit()
    finally:
        engine.dispose()
    print(
        f"入库完成：platform={platform} date={run_date} 商品 {len(snapshots)} 条，"
        f"丢弃 {dropped} 条；run_record={'有' if record else '无'}"
    )
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    """对已存在于 product_snapshots 的原始行重跑判定（采集模块直接写表时用）。"""

    settings = _settings(args)
    run_date = date.fromisoformat(args.date) if args.date else date.today()
    engine, factory = _session(settings)
    scored = 0
    try:
        with session_scope(factory) as session:
            query = (
                select(ProductSnapshot)
                .join(Product, Product.id == ProductSnapshot.product_id)
                .where(ProductSnapshot.snapshot_date == run_date)
            )
            if args.platform:
                query = query.where(Product.platform == args.platform)
            snapshots = list(session.scalars(query).all())
            seen: set[int] = set()
            for snapshot in snapshots:
                if snapshot.id in seen:
                    continue
                seen.add(snapshot.id)
                product = session.get(Product, snapshot.product_id)
                shop = (
                    session.scalar(
                        select(Shop).where(
                            Shop.platform == product.platform,
                            Shop.shop_id == product.shop_id,
                        )
                    )
                    if product and product.shop_id
                    else None
                )
                if product is None:
                    continue
                score_snapshot(
                    session,
                    settings=settings,
                    snapshot=snapshot,
                    product=product,
                    shop=shop,
                )
                scored += 1
            session.commit()
    finally:
        engine.dispose()
    print(f"判定完成：date={run_date} 快照 {scored} 条")
    return 0


def cmd_run_entry(args: argparse.Namespace) -> int:
    from pipeline.run_entry import main as run_entry_main

    argv: list[str] = []
    for name in ("ip", "keywords", "whitelist_dir", "crawler_config", "output_dir", "date"):
        value = getattr(args, name.replace("_", "-").replace("-", "_"), None)
        if value:
            argv.extend(["--" + name.replace("_", "-"), str(value)])
    if args.config:
        argv.extend(["--config", str(args.config)])
    for platform in args.platform or []:
        argv.extend(["--platform", str(platform)])
    for flag in ("dry_run", "no_alert", "interactive", "no_editor", "prepare_only"):
        if getattr(args, flag):
            argv.append("--" + flag.replace("_", "-"))
    return run_entry_main(argv)


def cmd_db_upgrade(args: argparse.Namespace) -> int:
    settings = _settings(args)
    ensure_schema(settings)
    print(f"数据库已升级到 head：{settings.database_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pipeline", description="电商侵权线索后端")
    parser.add_argument("--config", help="rules.yaml 路径（默认 config/rules.yaml）")
    parser.add_argument("--version", action="version", version="ip-lead-pipeline 0.1.0")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="一键运行：采集→入库→判定→聚合→报警")
    run.add_argument("--platform", action="append", help="平台，逗号分隔或重复，如 taobao,jd")
    run.add_argument("--date", help="快照日期 YYYY-MM-DD，默认今天")
    run.add_argument("--dry-run", action="store_true", help="临时库演练，不写真实库、不发邮件")
    run.add_argument("--input-dir", help="不调用采集，直接读取 out/<date> 目录")
    run.add_argument("--no-alert", action="store_true", help="本次不发报警邮件")
    run.set_defaults(func=cmd_run)

    ingest = sub.add_parser("ingest", help="把采集 JSONL 写入 products/product_snapshots")
    ingest.add_argument("--platform", required=True, choices=["taobao", "tmall", "jd", "1688"])
    ingest.add_argument("--jsonl", help="单个 JSONL 文件")
    ingest.add_argument("--run-json", help="对应 run.json")
    ingest.add_argument("--input-dir", help="目录（含 <platform>.jsonl）")
    ingest.add_argument("--date", help="快照日期，默认今天")
    ingest.add_argument("--run-id", help="覆盖 run_id")
    ingest.set_defaults(func=cmd_ingest)

    score = sub.add_parser("score", help="对已有快照重跑判定（采集直接写表后使用）")
    score.add_argument("--date", help="快照日期，默认今天")
    score.add_argument("--platform", choices=["taobao", "tmall", "jd", "1688"])
    score.set_defaults(func=cmd_score)

    review = sub.add_parser("review", help="复核回写")
    review_sub = review.add_subparsers(dest="review_command", required=True)
    review_import = review_sub.add_parser("import", help="从 Excel「线索汇总」导入复核结论")
    review_import.add_argument("excel")
    review_import.set_defaults(func=cmd_review_import)
    review_saas = review_sub.add_parser("mark-saas", help="标记已录入SaaS")
    review_saas.add_argument("lead_no", nargs="+")
    review_saas.add_argument("--operator", default="review.bat")
    review_saas.set_defaults(func=cmd_review_mark_saas)

    smtp = sub.add_parser("setup-smtp", help="把 SMTP 授权码写入 Windows 凭据管理器")
    smtp.add_argument("--email", help="发件邮箱，默认读配置")
    smtp.set_defaults(func=cmd_setup_smtp)

    test_smtp = sub.add_parser("test-smtp", help="按配置发送一封 SMTP 测试邮件")
    test_smtp.set_defaults(func=cmd_test_smtp)

    run_entry = sub.add_parser("run-entry", help="人工录入 IP/白名单后运行（run.bat）")
    run_entry.add_argument("--config", help="基础后端 rules.yaml")
    run_entry.add_argument("--platform", action="append", help="平台，逗号分隔或重复")
    run_entry.add_argument("--ip", help="IP 名")
    run_entry.add_argument("--keywords", help="关键词，逗号分隔；留空默认 IP 名")
    run_entry.add_argument("--whitelist-dir", help="白名单目录")
    run_entry.add_argument("--crawler-config", help="crawler 配置输出路径")
    run_entry.add_argument("--output-dir", help="当次配置输出目录")
    run_entry.add_argument("--date", help="快照日期")
    run_entry.add_argument("--dry-run", action="store_true")
    run_entry.add_argument("--no-alert", action="store_true")
    run_entry.add_argument("--interactive", action="store_true")
    run_entry.add_argument("--no-editor", action="store_true")
    run_entry.add_argument("--prepare-only", action="store_true")
    run_entry.set_defaults(func=cmd_run_entry)

    db = sub.add_parser("db", help="数据库维护")
    db_sub = db.add_subparsers(dest="db_command", required=True)
    upgrade = db_sub.add_parser("upgrade", help="执行 alembic upgrade head")
    upgrade.set_defaults(func=cmd_db_upgrade)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        settings = _settings(args)
        logging.basicConfig(level=logging.INFO, format="%(message)s")
        return int(args.func(args))
    except (ConfigError, ValueError) as exc:
        print(f"配置/参数错误：{exc}")
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
