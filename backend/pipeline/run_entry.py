"""run.bat 的交互/非交互入口：人工录入 IP、白名单，再跑 pipeline。

- 交互模式：run.bat 先用 ``set /p`` 读 IP 和关键词；本模块负责建/打开
  ``whitelist/<规范化IP>.txt``、备份到 ``whitelist/history/``，生成当次
  后端与 crawler 配置后调用 :class:`pipeline.runner.PipelineRunner`。
- 非交互模式：``--ip 奥特曼 --keywords 奥特曼 --whitelist-dir <目录>``，
  不打开编辑器，供 wu_validate 无人值守调用；目录可指向 logs 下的临时目录。
"""

from __future__ import annotations

import argparse
import copy
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

import yaml

from pipeline.config import BACKEND_ROOT, Settings, load_config
from pipeline.runner import PipelineRunner
from pipeline.shopkey import decode_shop_name, normalize_ip_name
from pipeline.whitelist import WHITELIST_TEMPLATE, parse_whitelist_text

_KEYWORD_SPLIT = re.compile(r"[,，;；]+")


@dataclass
class PreparedRun:
    ip_name: str
    ip_code: str
    keywords: list[str]
    whitelist_dir: Path
    whitelist_path: Path
    backup_path: Path | None
    backend_config_path: Path
    crawler_config_path: Path
    entries: list


def split_keywords(value: str | Iterable[str] | None, default: str) -> list[str]:
    if value is None:
        return [default]
    if isinstance(value, str):
        parts = _KEYWORD_SPLIT.split(value)
    else:
        parts = [part for item in value for part in _KEYWORD_SPLIT.split(str(item))]
    result = [part.strip() for part in parts if part and part.strip()]
    return list(dict.fromkeys(result)) or [default]


def ensure_whitelist_file(path: Path) -> bool:
    """文件不存在时创建带注释模板；返回是否新建。"""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(WHITELIST_TEMPLATE, encoding="utf-8")
    return True


def backup_whitelist(
    path: Path, history_dir: Path, ip_code: str, *, now: datetime | None = None
) -> Path:
    history_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    target = history_dir / f"{ip_code}_{stamp}.txt"
    suffix = 1
    while target.exists():
        target = history_dir / f"{ip_code}_{stamp}_{suffix}.txt"
        suffix += 1
    shutil.copy2(path, target)
    return target


def _read_entries(path: Path) -> list:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return []
    return parse_whitelist_text(text)


def build_run_configs(
    base: Settings,
    *,
    ip_name: str,
    ip_code: str,
    keywords: list[str],
    whitelist_path: Path,
    output_dir: Path,
    crawler_config_path: Path | None = None,
) -> tuple[Path, Path]:
    """从当前 rules.yaml / windows.yaml 生成当次配置。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    aliases = [
        word
        for word in keywords
        if normalize_ip_name(word) != normalize_ip_name(ip_name)
    ]

    backend_raw = copy.deepcopy(base.raw)
    backend_raw["ips"] = [
        {
            "code": ip_code,
            "name": ip_name,
            "aliases": aliases,
            "variants": [],
            "reference_images": [],
        }
    ]
    backend_raw["whitelist_file"] = str(whitelist_path)
    backend_raw.setdefault("run", {})["ip_code"] = ip_code
    backend_path = output_dir / f"run_{stamp}_{ip_code}.yaml"
    backend_path.write_text(
        yaml.safe_dump(backend_raw, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    crawler_source = Path(
        os.environ.get("IP_LEAD_CRAWLER_CONFIG")
        or base.get("paths.crawler_config")
        or (base.crawler_dir / "config" / "windows.yaml")
    ).expanduser()
    if not crawler_source.is_absolute():
        crawler_source = (base.root / crawler_source).resolve()
    if not crawler_source.exists():
        crawler_source = base.crawler_dir / "config" / "windows.yaml"
    crawler_raw = yaml.safe_load(crawler_source.read_text(encoding="utf-8-sig")) or {}
    for section in ("text_score", "exclude"):
        if section not in crawler_raw and section in base.raw:
            crawler_raw[section] = copy.deepcopy(base.raw[section])
    crawler_raw["ips"] = [
        {
            "code": ip_code,
            "name": ip_name,
            "aliases": aliases,
            "variants": [],
        }
    ]
    crawl = crawler_raw.setdefault("crawl", {})
    crawl["keywords"] = [{"keyword": word, "ip_code": ip_code} for word in keywords]
    crawler_path = (
        Path(crawler_config_path)
        if crawler_config_path is not None
        else output_dir / f"crawler_{stamp}_{ip_code}.yaml"
    )
    crawler_path.parent.mkdir(parents=True, exist_ok=True)
    crawler_path.write_text(
        yaml.safe_dump(crawler_raw, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return backend_path, crawler_path


def prepare_run(
    *,
    ip_name: str | None = None,
    keywords: str | Iterable[str] | None = None,
    whitelist_dir: str | Path | None = None,
    config_path: str | Path | None = None,
    interactive: bool = False,
    open_editor: Callable[[Path], None] | None = None,
    crawler_config_path: str | Path | None = None,
    output_dir: str | Path | None = None,
) -> PreparedRun:
    base = load_config(config_path)
    if not ip_name and interactive:
        ip_name = input("请输入本次 IP 名（必填）：").strip()
    ip_name = (ip_name or "").strip()
    if not ip_name:
        raise ValueError("IP 名不能为空")
    ip_code = normalize_ip_name(ip_name)
    if not ip_code:
        raise ValueError("IP 名规范化后为空")
    keyword_list = split_keywords(keywords, ip_name)

    root_dir = (
        Path(whitelist_dir).expanduser()
        if whitelist_dir is not None
        else (base.root / "whitelist")
    )
    if not root_dir.is_absolute():
        root_dir = (base.root / root_dir).resolve()
    whitelist_path = root_dir / f"{ip_code}.txt"
    ensure_whitelist_file(whitelist_path)

    if interactive:
        editor = open_editor or _default_editor
        editor(whitelist_path)

    entries = _read_entries(whitelist_path)
    backup_path = backup_whitelist(whitelist_path, root_dir / "history", ip_code)
    config_output = (
        Path(output_dir).expanduser() if output_dir is not None else base.logs_dir
    )
    if not config_output.is_absolute():
        config_output = (base.root / config_output).resolve()
    backend_path, crawler_path = build_run_configs(
        base,
        ip_name=ip_name,
        ip_code=ip_code,
        keywords=keyword_list,
        whitelist_path=whitelist_path,
        output_dir=config_output,
        crawler_config_path=Path(crawler_config_path) if crawler_config_path else None,
    )
    return PreparedRun(
        ip_name=ip_name,
        ip_code=ip_code,
        keywords=keyword_list,
        whitelist_dir=root_dir,
        whitelist_path=whitelist_path,
        backup_path=backup_path,
        backend_config_path=backend_path,
        crawler_config_path=crawler_path,
        entries=entries,
    )


def _default_editor(path: Path) -> None:
    if os.name == "nt":
        subprocess.run(["notepad.exe", "/wait", str(path)], check=False)
    else:
        print(f"请编辑白名单文件：{path}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="录入 IP/白名单并运行 pipeline")
    parser.add_argument("--ip", help="IP 名（非交互时必填）")
    parser.add_argument("--keywords", help="关键词，逗号分隔；留空默认用 IP 名")
    parser.add_argument("--whitelist-dir", help="白名单目录，默认 backend/whitelist")
    parser.add_argument("--config", help="基础后端 rules.yaml")
    parser.add_argument("--crawler-config", help="输出/基础 crawler windows.yaml")
    parser.add_argument("--output-dir", help="当次配置输出目录，默认 base.logs_dir")
    parser.add_argument("--platform", action="append", help="平台，逗号分隔或重复")
    parser.add_argument("--date", help="快照日期 YYYY-MM-DD")
    parser.add_argument("--dry-run", action="store_true", help="临时库演练")
    parser.add_argument("--no-alert", action="store_true", help="不发报警邮件")
    parser.add_argument(
        "--interactive", action="store_true", help="打开白名单编辑器（run.bat）"
    )
    parser.add_argument(
        "--no-editor", action="store_true", help="不打开编辑器（无人值守）"
    )
    parser.add_argument(
        "--prepare-only", action="store_true", help="只生成配置，不运行 pipeline"
    )
    return parser


def _parse_platforms(values: list[str] | None) -> list[str] | None:
    if not values:
        return None
    result: list[str] = []
    for value in values:
        result.extend(part.strip() for part in str(value).split(",") if part.strip())
    return result or None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    interactive = bool(args.interactive) or (not args.ip and not args.no_editor)
    try:
        prepared = prepare_run(
            ip_name=args.ip,
            keywords=args.keywords,
            whitelist_dir=args.whitelist_dir,
            config_path=args.config,
            interactive=interactive,
            crawler_config_path=args.crawler_config,
            output_dir=args.output_dir,
        )
    except (ValueError, OSError, yaml.YAMLError) as exc:
        print(f"准备运行失败：{exc}")
        return 2
    print(f"IP：{prepared.ip_name}（{prepared.ip_code}）")
    print(f"关键词：{'、'.join(prepared.keywords)}")
    print(f"白名单：{prepared.whitelist_path}（{len(prepared.entries)} 条）")
    if prepared.backup_path:
        print(f"白名单备份：{prepared.backup_path}")
    print(f"当次后端配置：{prepared.backend_config_path}")
    print(f"当次 crawler 配置：{prepared.crawler_config_path}")
    if args.prepare_only:
        return 0

    settings = load_config(prepared.backend_config_path)
    old_crawler_env = os.environ.get("IP_LEAD_CRAWLER_CONFIG")
    os.environ["IP_LEAD_CRAWLER_CONFIG"] = str(prepared.crawler_config_path)
    try:
        runner = PipelineRunner(settings)
        summary = runner.run(
            platforms=_parse_platforms(args.platform),
            run_date=args.date,
            dry_run=args.dry_run,
            allow_alert=not args.no_alert,
        )
    finally:
        if old_crawler_env is None:
            os.environ.pop("IP_LEAD_CRAWLER_CONFIG", None)
        else:
            os.environ["IP_LEAD_CRAWLER_CONFIG"] = old_crawler_env
    for line in summary.lines():
        print(line)
    return 1 if summary.failed else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
