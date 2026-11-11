"""运行前数据库备份与 14 份保留策略。"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from pipeline.config import Settings


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def backup_database(settings: Settings) -> Path | None:
    from pipeline.db import database_url

    if not database_url(settings).startswith("sqlite"):
        return None
    source_path = settings.database_path
    if not source_path.exists():
        return None
    settings.backups_dir.mkdir(parents=True, exist_ok=True)
    stem = f"leads_{_timestamp()}"
    destination = settings.backups_dir / f"{stem}.db"
    counter = 1
    while destination.exists():
        destination = settings.backups_dir / f"{stem}_{counter}.db"
        counter += 1
    source = sqlite3.connect(str(source_path))
    target = sqlite3.connect(str(destination))
    try:
        source.backup(target)
        target.commit()
    finally:
        target.close()
        source.close()
    prune_backups(settings.backups_dir, settings.backup_keep)
    return destination


def backup_files(directory: Path) -> list[Path]:
    if not directory.exists():
        return []
    return sorted(
        (path for path in directory.glob("leads_*.db") if path.is_file()),
        key=lambda path: (path.stat().st_mtime, path.name),
        reverse=True,
    )


def prune_backups(directory: Path, keep: int) -> list[Path]:
    keep = max(1, int(keep))
    removed: list[Path] = []
    for path in backup_files(directory)[keep:]:
        try:
            path.unlink()
            removed.append(path)
        except FileNotFoundError:
            pass
    return removed


__all__ = ["backup_database", "backup_files", "prune_backups"]
