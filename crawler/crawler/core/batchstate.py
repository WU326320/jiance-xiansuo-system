"""淘宝关键词分批进度（按平台 + 日期持久化）。

文件：``state/batches/taobao-{date}.json``，权限 0600。只记录批次进度，
不含 Cookie/页面内容；当天跑完第 N 批，下次可用 ``--batch next`` 继续。
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path


def batch_state_path(state_root: str | Path, platform: str, date: str) -> Path:
    return Path(state_root) / "batches" / f"{platform}-{date}.json"


def read_batch_state(state_root: str | Path, platform: str, date: str) -> dict | None:
    path = batch_state_path(state_root, platform, date)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_batch_state(
    state_root: str | Path,
    platform: str,
    date: str,
    *,
    batch_size: int,
    batch_total: int,
    last_completed_batch: int,
    next_batch: int,
    status: str,
    run_id: str | None = None,
    keywords: list[str] | None = None,
) -> Path:
    path = batch_state_path(state_root, platform, date)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "platform": platform,
        "date": date,
        "batch_size": int(batch_size),
        "batch_total": int(batch_total),
        "last_completed_batch": int(last_completed_batch),
        "next_batch": int(next_batch),
        "status": status,
        "run_id": run_id,
        "keywords": list(keywords or []),
        "updated_at": datetime.now().astimezone().isoformat(),
    }
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return path


__all__ = ["batch_state_path", "read_batch_state", "write_batch_state"]
