"""命令行入口：手工验证 / 后端联调。

用法::

    python -m lead_excel --db ..\\data\\leads.db --date 2026-11-05 --config ..\\rules.yaml
    python -m lead_excel --date 20261105 --config ..\\rules.yaml  # 读 storage.db_path
    python -m lead_excel --date 20261105 --out-dir D:\\tmp          # 默认 data/leads.db
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from .api import export_leads_excel
from .errors import ExportError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导出侵权线索 Excel")
    parser.add_argument(
        "--db",
        help="SQLite 数据库路径；省略时读配置 storage.db_path（默认 data/leads.db）",
    )
    parser.add_argument(
        "--date",
        required=True,
        help="快照日期，YYYY-MM-DD 或 YYYYMMDD",
    )
    parser.add_argument("--config", help="rules.yaml 路径（含 export 段）")
    parser.add_argument("--out-dir", help="覆盖 export.dir，便于临时验证")
    args = parser.parse_args(argv)

    config: dict = {}
    if args.config:
        with Path(args.config).open("r", encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle) or {}
        if not isinstance(loaded, dict):
            parser.error("--config 必须是 YAML 映射（rules.yaml）")
        config = loaded
    if args.out_dir:
        config.setdefault("export", {})
        if not isinstance(config["export"], dict):
            config["export"] = {}
        config["export"]["dir"] = args.out_dir

    try:
        path = export_leads_excel(args.db, args.date, config)
    except ExportError as exc:
        print(f"导出失败：{exc}", file=sys.stderr)
        return 2
    print(path)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
