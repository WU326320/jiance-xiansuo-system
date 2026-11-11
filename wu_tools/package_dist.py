#!/usr/bin/env python3
"""打包 WU 验证包：从当前工作区最新代码生成 ``dist/code_20261106e.zip``。

包含：``backend/``、``export/``、``crawler/``、``workbench/``、``wu_tools/``、
``wu_validate.ps1``、``启动工作台.bat``。
排除：``*.log``、``.coverage``、``.venv``、``__pycache__``、数据库，以及
``profiles/``、``out/``、``logs/``、``state/`` 等运行产物。

打包后逐个校验包内**全部文件**的 MD5 与工作区一致（并单独打印关键文件），
避免把旧包文件混进来。脚本不冻结任何目录：所有文件都直接取当前工作区最新版本。

用法：
    python wu_tools/package_dist.py --root . --out dist/code_20261106e.zip
"""

from __future__ import annotations

import argparse
import hashlib
import zipfile
from pathlib import Path

ROOTS = ("backend", "export", "crawler", "workbench", "wu_tools")
SINGLE_FILES = ("wu_validate.ps1", "启动工作台.bat")
VERIFY_FILES = (
    "backend/pipeline/views.py",
    "export/lead_excel/workbook.py",
    "crawler/crawler/platforms/taobao.py",
    "crawler/crawler/platforms/ali1688.py",
    "crawler/tools/trial_metrics.py",
    "crawler/config/rules.yaml",
    "crawler/config/windows.yaml",
    "wu_tools/ultraman_report.py",
    "wu_tools/deploy_workbench.ps1",
    "workbench/app.py",
    "启动工作台.bat",
    "wu_validate.ps1",
)
EXCLUDE_DIR_NAMES = {
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".deps",
    ".git",
    "profiles",
    "logs",
    "out",
    "state",
    "htmlcov",
    ".mypy_cache",
    ".ruff_cache",
}
EXCLUDE_FILE_NAMES = {".coverage"}
EXCLUDE_SUFFIXES = (
    ".log",
    ".pyc",
    ".pyo",
    ".db",
    ".sqlite",
    ".sqlite3",
    ".db-wal",
    ".db-shm",
)


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _excluded(relative: Path) -> bool:
    if any(part in EXCLUDE_DIR_NAMES for part in relative.parts[:-1]):
        return True
    name = relative.name
    if name in EXCLUDE_FILE_NAMES:
        return True
    return any(name.endswith(suffix) for suffix in EXCLUDE_SUFFIXES)


def _collect(root: Path) -> list[tuple[str, Path]]:
    files: list[tuple[str, Path]] = []
    for base in ROOTS:
        directory = root / base
        if not directory.is_dir():
            raise SystemExit(f"缺少目录: {directory}")
        for path in sorted(directory.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            relative = path.relative_to(root)
            if _excluded(relative):
                continue
            files.append((relative.as_posix(), path))
    for name in SINGLE_FILES:
        path = root / name
        if not path.is_file():
            raise SystemExit(f"缺少文件: {path}")
        files.append((name, path))
    return files


def build(root: Path, out_zip: Path) -> dict[str, object]:
    root = root.resolve()
    out_zip = out_zip.resolve()
    files = _collect(root)
    if out_zip.exists():
        out_zip.unlink()
    out_zip.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, path in files:
            info = zipfile.ZipInfo(name)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())

    workspace_md5: dict[str, str] = {}
    archive_md5: dict[str, str] = {}
    all_workspace_md5: dict[str, str] = {}
    mismatches: list[str] = []
    with zipfile.ZipFile(out_zip) as archive:
        bad = archive.testzip()
        if bad:
            raise SystemExit(f"新包校验失败: {bad}")
        names = [name for name in archive.namelist() if not name.endswith("/")]
        for relative in names:
            source = root / relative
            if not source.is_file():
                mismatches.append(f"{relative}（工作区不存在）")
                continue
            digest = _md5(archive.read(relative))
            all_workspace_md5[relative] = _md5(source.read_bytes())
            archive_md5[relative] = digest
            if all_workspace_md5[relative] != digest:
                mismatches.append(
                    f"{relative} workspace={all_workspace_md5[relative]} zip={digest}"
                )
        for relative in VERIFY_FILES:
            if relative not in names:
                raise SystemExit(f"包内缺少校验文件: {relative}")
            workspace_md5[relative] = all_workspace_md5[relative]
        if mismatches:
            raise SystemExit("包内 MD5 与工作区不一致:\n  " + "\n  ".join(mismatches[:20]))

    print(f"wrote {out_zip} files={len(names)} size={out_zip.stat().st_size}")
    print(f"  MD5 全量校验：{len(names)}/{len(names)} 个文件与工作区一致")
    for relative in VERIFY_FILES:
        print(f"  MD5 {relative} = {workspace_md5[relative]} (workspace == zip)")
    return {
        "out": str(out_zip),
        "files": len(names),
        "workspace_md5": workspace_md5,
        "archive_md5": archive_md5,
        "all_md5_match": True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从当前工作区打包 WU 验证包")
    parser.add_argument("--root", default=".", help="项目根目录")
    parser.add_argument("--out", default="dist/code_20261106e.zip", help="输出 zip")
    args = parser.parse_args(argv)
    build(Path(args.root), Path(args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
