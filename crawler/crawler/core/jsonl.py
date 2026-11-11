"""统一 JSONL 输出与运行记录。

- 一行一个 JSON，UTF-8，字段顺序固定。
- 缺失字段输出 null，不输出 0。
- 同一输出文件内按 (platform, product_id) 去重（跨多次运行也生效）。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable

from crawler.models import (
    Item,
    RunRecord,
    item_json_dict,
    patch_json_dict,
    run_record_json_dict,
)


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def json_line(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)


class JsonlWriter:
    def __init__(
        self,
        path: str | Path,
        *,
        append: bool = True,
        dedupe_keys: tuple[str, ...] = ("platform", "product_id"),
    ):
        self.path = Path(path)
        _ensure_parent(self.path)
        self.dedupe_keys = dedupe_keys
        self._seen: set[tuple] = set()
        if append and self.path.exists():
            for row in read_jsonl(self.path):
                key = tuple(str(row.get(k)) for k in dedupe_keys)
                if all(key):
                    self._seen.add(key)
        self._fh = open(
            self.path, "a" if append else "w", encoding="utf-8", newline="\n"
        )

    def _key(self, data: dict[str, Any]) -> tuple:
        return tuple(str(data.get(k)) for k in self.dedupe_keys)

    def write(self, item: Item) -> bool:
        data = item_json_dict(item)
        key = self._key(data)
        if all(key) and key in self._seen:
            return False
        if all(key):
            self._seen.add(key)
        self._fh.write(json_line(data) + "\n")
        self._fh.flush()
        return True

    def write_patch(self, patch) -> bool:
        data = patch_json_dict(patch)
        key = self._key(data)
        if all(key) and key in self._seen:
            return False
        if all(key):
            self._seen.add(key)
        self._fh.write(json_line(data) + "\n")
        self._fh.flush()
        return True

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.flush()
            self._fh.close()

    def sync_rows(self, rows) -> None:
        """保留已有记录，原子更新检查点中的最新商品字段。"""
        self._fh.flush()
        merged = {self._key(row): row for row in read_jsonl(self.path)}
        merged.update({self._key(row): row for row in rows})
        fd, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=self.path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                for row in merged.values():
                    handle.write(json_line(row) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            self.close()
            for attempt in range(5):
                try:
                    os.replace(temporary, self.path)
                    break
                except OSError as exc:
                    # Windows 的短暂访问/共享冲突可恢复；永久权限问题仍抛出。
                    # 不删除目标或改用截断写入，保证失败时旧数据完整。
                    if (
                        getattr(exc, "winerror", None) not in (5, 32, 33)
                        or attempt == 4
                    ):
                        raise
                    delay = 0.1 * (2**attempt)
                    logging.getLogger(__name__).warning(
                        "JSONL replacement delayed path=%s winerror=%s attempt=%s/5 delay=%.1fs",
                        self.path,
                        exc.winerror,
                        attempt + 1,
                        delay,
                    )
                    time.sleep(delay)
            self._seen = set(merged)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
            if self._fh.closed:
                self._fh = open(self.path, "a", encoding="utf-8", newline="\n")

    def __enter__(self) -> "JsonlWriter":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def write_json_atomic(path: str | Path, data: Any, *, mode: int = 0o644) -> Path:
    path = Path(path)
    _ensure_parent(path)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, default=str)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return path


def write_run_record(output_dir: str | Path, record: RunRecord) -> Path:
    output_dir = Path(output_dir)
    path = output_dir / f"{record.platform}.run.json"
    write_json_atomic(path, run_record_json_dict(record))
    log_line = json_line(run_record_json_dict(record)) + "\n"
    with open(
        output_dir / "run_logs.jsonl", "a", encoding="utf-8", newline="\n"
    ) as handle:
        handle.write(log_line)
        handle.flush()
    return path


def read_jsonl(path: str | Path) -> Iterable[dict[str, Any]]:
    path = Path(path)
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                rows.append(value)
    return rows


def relative_or_absolute(path: str | Path, base: str | Path | None = None) -> str:
    path = Path(path)
    base = Path(base or Path.cwd())
    try:
        return str(path.resolve().relative_to(base.resolve()))
    except ValueError:
        return str(path.resolve())
