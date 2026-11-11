#!/usr/bin/env python3
"""以 ``python -m pipeline run --dry-run --no-alert`` 的同一入口运行后端，
但把 dry-run 的临时 sqlite 保留在指定目录，供打分统计复核。

为什么需要它
------------
``PipelineRunner.run(dry_run=True)`` 会把 ``IP_LEAD_DB_PATH`` 指向
``tempfile.mkdtemp()`` 建的目录，并在结束时 ``shutil.rmtree``。真库不会被写，
这点很好；但临时库也随之消失，无法统计 text_score 分布。

本包装器只做两件事，不改业务代码：
  1. 让 ``tempfile.mkdtemp`` 在 ``WU_KEEP_DRYRUN_DIR`` 下建目录；
  2. 让 ``shutil.rmtree`` 跳过删除 ``WU_KEEP_DRYRUN_DIR`` 内的目录。

真实 ``backend/data/leads.db`` 仍然完全不会打开/写入，邮件仍由 ``--no-alert``
关闭。运行结束后 ``<WU_KEEP_DRYRUN_DIR>/ip-lead-dry-run-*/leads.db`` 保留，
供 ``wu_tools/ultraman_report.py`` 只读统计。
"""

from __future__ import annotations

import itertools
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

_keep_raw = os.environ.get("WU_KEEP_DRYRUN_DIR")
if not _keep_raw:
    print("WU_KEEP_DRYRUN_DIR 未设置", file=sys.stderr)
    raise SystemExit(2)
KEEP = Path(_keep_raw).expanduser().resolve()
KEEP.mkdir(parents=True, exist_ok=True)

_counter = itertools.count(1)
_original_mkdtemp = tempfile.mkdtemp
_original_rmtree = shutil.rmtree


def _mkdtemp(*args, **kwargs):  # noqa: ANN002, ANN003 - 保持原签名
    path = KEEP / f"ip-lead-dry-run-{next(_counter)}"
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def _inside_keep(path: object) -> bool:
    try:
        resolved = Path(path).resolve()
    except (OSError, TypeError, ValueError):
        return False
    return resolved == KEEP or KEEP in resolved.parents


def _rmtree(path, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
    if _inside_keep(path):
        # 保留 dry-run 临时库；它只含本次验证数据，且位于 logs 目录下。
        return
    return _original_rmtree(path, *args, **kwargs)


tempfile.mkdtemp = _mkdtemp  # type: ignore[assignment]
shutil.rmtree = _rmtree  # type: ignore[assignment]

from pipeline.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
