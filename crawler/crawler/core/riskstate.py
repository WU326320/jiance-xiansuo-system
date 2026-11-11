"""按平台 + 日期的风控熔断标记。

命中验证码/滑块/访问受限后写 ``state/risk/{platform}-{date}.json``；
同一天再次运行直接跳过该平台，不发请求。次日文件按日期自然失效。
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path


def risk_marker_path(state_root: str | Path, platform: str, date: str) -> Path:
    return Path(state_root) / "risk" / f"{platform}-{date}.json"


def read_risk_marker(state_root: str | Path, platform: str, date: str) -> dict | None:
    path = risk_marker_path(state_root, platform, date)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def mark_risk_blocked(
    state_root: str | Path,
    platform: str,
    date: str,
    *,
    reason: str,
    screenshot_path: str | None = None,
) -> Path:
    path = risk_marker_path(state_root, platform, date)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "platform": platform,
        "date": date,
        "reason": reason,
        "screenshot_path": screenshot_path,
        "blocked_at": datetime.now().astimezone().isoformat(),
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
