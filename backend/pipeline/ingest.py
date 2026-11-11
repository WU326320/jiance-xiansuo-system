"""JSONL / run.json 适配层。

采集模块当前输出 ``out/<date>/<platform>.jsonl`` 与 ``<platform>.run.json``。
本模块只做读取和轻量规整；真正的写库在 :mod:`pipeline.store`。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

from pipeline.store import parse_datetime


@dataclass
class PlatformData:
    platform: str
    items: list[dict[str, Any]] = field(default_factory=list)
    run_record: dict[str, Any] = field(default_factory=dict)

    @property
    def login_ok(self) -> bool:
        value = self.run_record.get("login_ok")
        return True if value is None else bool(value)

    @property
    def status(self) -> str:
        return str(self.run_record.get("status") or "ok")

    @property
    def error_summary(self) -> str:
        errors = self.run_record.get("errors") or []
        if isinstance(errors, str):
            errors = [errors]
        warnings = self.run_record.get("warnings") or []
        parts = [str(item) for item in [*errors, *warnings] if item]
        return "；".join(parts)[:2000]

    @property
    def items_count(self) -> int:
        value = self.run_record.get("items_ok")
        return len(self.items) if value is None else int(value)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    items: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                value = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                value.setdefault("_line_number", line_number)
                items.append(value)
    return items


def read_run_record(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def load_platform_dir(directory: Path, platform: str) -> PlatformData:
    return PlatformData(
        platform=platform,
        items=read_jsonl(directory / f"{platform}.jsonl"),
        run_record=read_run_record(directory / f"{platform}.run.json"),
    )


def snapshot_date_from_run(run_record: dict[str, Any], fallback: date) -> date:
    value = run_record.get("started_at") or run_record.get("ended_at")
    if value:
        try:
            return parse_datetime(value).date()
        except (TypeError, ValueError):
            pass
    return fallback


def iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    yield from read_jsonl(path)


__all__ = [
    "PlatformData",
    "iter_jsonl",
    "load_platform_dir",
    "read_jsonl",
    "read_run_record",
    "snapshot_date_from_run",
]
