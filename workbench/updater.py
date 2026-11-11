"""版本包上传、更新与回滚。

安全边界：
- 只接受 zip，拒绝绝对路径、``..``、符号链接和超大解压体积；
- 所有解压/备份/删除都在 ``workbench/data`` 下完成；
- 更新前把当前版本打成 zip 备份，状态文件原子替换；
- 回滚只切换版本指针，不动 leads.db / workbench.db。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from workbench.config import WorkbenchConfig
from workbench.db import Database, now_text

VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,63}$")


class UpdateError(RuntimeError):
    pass


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


class VersionManager:
    def __init__(self, config: WorkbenchConfig, db: Database):
        self.config = config
        self.db = db
        config.ensure_dirs()

    # ------------------------------------------------------------------
    def current(self) -> dict[str, Any]:
        path = self.config.versions_state_path
        if not path.exists():
            return {"version": "1.0.0", "path": "", "digest": "", "history": []}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"version": "1.0.0", "path": "", "digest": "", "history": []}
        data.setdefault("history", [])
        return data

    def list_versions(self) -> dict[str, Any]:
        return {
            "current": self.current(),
            "releases": self.db.fetchall(
                "SELECT id, version, path, digest, status, note, created_at FROM versions ORDER BY id DESC LIMIT 50"
            ),
            "backups": sorted(
                [item.name for item in self.config.version_backups_dir.glob("*.zip")],
                reverse=True,
            ),
        }

    # ------------------------------------------------------------------
    def install(
        self,
        data: bytes,
        *,
        filename: str = "version.zip",
        actor: dict[str, Any] | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        if not data:
            raise UpdateError("版本包为空")
        if len(data) > self.config.max_update_bytes:
            raise UpdateError("版本包超过大小限制")
        digest = _sha256(data)
        version, stage = self._extract(data)
        target = self.config.versions_dir / f"{version}-{digest[:8]}"
        if not _is_within(target, self.config.data_dir):
            self._safe_rmtree(stage)
            raise UpdateError("版本目录越界")
        previous = self.current()
        state_written = False
        target_existed = target.exists()
        try:
            self._backup_current(previous)
            if target_existed:
                self._safe_rmtree(stage)
            else:
                os.replace(stage, target)
            history = []
            if previous.get("path"):
                history.append(
                    {
                        "version": previous.get("version"),
                        "path": previous.get("path"),
                        "digest": previous.get("digest", ""),
                    }
                )
            history.extend(previous.get("history") or [])
            state = {
                "version": version,
                "path": str(target),
                "digest": digest,
                "installed_at": now_text(),
                "filename": Path(filename).name,
                "history": history[:10],
            }
            self._write_state(state)
            state_written = True
            with self.db.transaction() as connection:
                connection.execute("UPDATE versions SET status='superseded' WHERE status='active'")
                connection.execute(
                    """INSERT INTO versions(version, path, digest, status, note, created_by, created_at)
                       VALUES(?,?,?,'active',?,?,?)""",
                    (version, str(target), digest, note or Path(filename).name, (actor or {}).get("id"), now_text()),
                )
                connection.execute(
                    """INSERT INTO audit_log(actor_id, actor_name, source_ip, action,
                        object_type, object_id, before_json, after_json, created_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        (actor or {}).get("id"),
                        (actor or {}).get("username", ""),
                        "",
                        "version.install",
                        "version",
                        version,
                        json.dumps(previous, ensure_ascii=False),
                        json.dumps(state, ensure_ascii=False),
                        now_text(),
                    ),
                )
            return state
        except Exception:
            self._safe_rmtree(stage)
            if state_written:
                # 数据库记录失败时把版本指针恢复回安装前，保证「安装失败自动回滚」。
                self._write_state(previous)
                if not target_existed:
                    self._safe_rmtree(target)
            raise

    def rollback(
        self, *, actor: dict[str, Any] | None = None, note: str = ""
    ) -> dict[str, Any]:
        state = self.current()
        history = list(state.get("history") or [])
        if not history:
            raise UpdateError("没有可回滚的上一版")
        target = history.pop(0)
        target_path = Path(str(target.get("path") or ""))
        if not target_path.exists() or not _is_within(target_path, self.config.versions_dir):
            raise UpdateError("上一版文件不存在，无法回滚")
        self._backup_current(state)
        new_state = {
            "version": target.get("version") or "unknown",
            "path": str(target_path),
            "digest": target.get("digest") or "",
            "installed_at": now_text(),
            "filename": f"rollback-{target.get('version') or 'unknown'}",
            "history": history,
        }
        self._write_state(new_state)
        with self.db.transaction() as connection:
            connection.execute("UPDATE versions SET status='superseded' WHERE status='active'")
            connection.execute(
                """INSERT INTO versions(version, path, digest, status, note, created_by, created_at)
                   VALUES(?,?,?,'active',?,?,?)""",
                (
                    new_state["version"],
                    str(target_path),
                    new_state["digest"],
                    note or "rollback",
                    (actor or {}).get("id"),
                    now_text(),
                ),
            )
            connection.execute(
                """INSERT INTO audit_log(actor_id, actor_name, source_ip, action,
                    object_type, object_id, before_json, after_json, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    (actor or {}).get("id"),
                    (actor or {}).get("username", ""),
                    "",
                    "version.rollback",
                    "version",
                    new_state["version"],
                    json.dumps(state, ensure_ascii=False),
                    json.dumps(new_state, ensure_ascii=False),
                    now_text(),
                ),
            )
        return new_state

    # ------------------------------------------------------------------
    def _extract(self, data: bytes) -> tuple[str, Path]:
        import io

        try:
            archive = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as exc:
            raise UpdateError("不是有效的 zip 版本包") from exc
        with archive:
            bad = archive.testzip()
            if bad:
                raise UpdateError(f"版本包校验失败：{bad}")
            names = archive.namelist()
            if not names:
                raise UpdateError("版本包为空")
            version = None
            total = 0
            declared_version = None
            for info in archive.infolist():
                name = info.filename.replace("\\", "/")
                pure = PurePosixPath(name)
                if name.startswith("/") or ".." in pure.parts or re.match(r"^[A-Za-z]:", name):
                    raise UpdateError(f"版本包包含非法路径：{name}")
                if info.is_dir():
                    continue
                mode = (info.external_attr >> 16) & 0o170000
                if mode == 0o120000:
                    raise UpdateError(f"版本包不允许符号链接：{name}")
                total += int(info.file_size)
                if total > self.config.max_update_bytes * 8:
                    raise UpdateError("版本包解压后过大")
                if name in ("version.json", "workbench/version.json", "VERSION", "workbench/VERSION"):
                    payload = archive.read(info)
                    declared_version = self._parse_version(payload, name)
            if not declared_version:
                raise UpdateError("版本包缺少 version.json 或 VERSION")

            stage = Path(tempfile.mkdtemp(prefix=".stage-", dir=str(self.config.versions_dir)))
            try:
                for info in archive.infolist():
                    name = info.filename.replace("\\", "/")
                    pure = PurePosixPath(name)
                    if not name or name.endswith("/"):
                        continue
                    destination = stage / Path(*pure.parts)
                    if not _is_within(destination, stage):
                        raise UpdateError(f"版本包包含越界路径：{name}")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info) as source, destination.open("wb") as target:
                        shutil.copyfileobj(source, target)
                if not any(stage.iterdir()):
                    raise UpdateError("版本包没有文件")
                return declared_version, stage
            except Exception:
                self._safe_rmtree(stage)
                raise

    @staticmethod
    def _parse_version(payload: bytes, name: str) -> str:
        text = payload.decode("utf-8", errors="replace").strip()
        if not text:
            raise UpdateError(f"{name} 为空")
        if name.endswith(".json"):
            try:
                parsed = json.loads(text)
                text = str(parsed.get("version") or "")
            except ValueError as exc:
                raise UpdateError("version.json 不是合法 JSON") from exc
        if not VERSION_RE.match(text):
            raise UpdateError(f"版本号非法：{text}")
        return text

    def _backup_current(self, state: dict[str, Any]) -> Path | None:
        raw_path = state.get("path")
        if not raw_path:
            return None
        source = Path(str(raw_path))
        if not source.exists() or not _is_within(source, self.config.versions_dir):
            return None
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        version = re.sub(r"[^0-9A-Za-z._+-]", "_", str(state.get("version") or "unknown"))
        target = self.config.version_backups_dir / f"{version}_{stamp}.zip"
        suffix = 1
        while target.exists():
            target = self.config.version_backups_dir / f"{version}_{stamp}_{suffix}.zip"
            suffix += 1
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            for item in source.rglob("*"):
                if item.is_file() and _is_within(item, source):
                    archive.write(item, item.relative_to(source).as_posix())
        return target

    def _write_state(self, state: dict[str, Any]) -> None:
        path = self.config.versions_state_path
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, path)

    def _safe_rmtree(self, path: Path) -> None:
        if not _is_within(path, self.config.data_dir):
            raise UpdateError("拒绝删除工作台数据目录以外的文件")
        if path.resolve() == self.config.data_dir.resolve():
            raise UpdateError("拒绝删除工作台数据目录本身")
        if path.exists():
            shutil.rmtree(path)


__all__ = ["UpdateError", "VERSION_RE", "VersionManager"]
