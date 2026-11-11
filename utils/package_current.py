"""封装当前源码与现有依赖，SQLite在线备份；不删除或覆盖历史内容。

使用 crawler/.venv/Scripts/python.exe -X utf8 utils/package_current.py prepare
随后在新目录验证，最后使用本脚本 finalize <新目录> 生成最终zip与校验清单。
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "交付文件/exports"
EXCLUDED = {
    ".venv",
    ".deps",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".git",
    "data",
    "logs",
    "out",
    "state",
    "profiles",
    "backups",
    "cleanup_recovery",
    "htmlcov",
}
TREES = (
    "workbench",
    "backend",
    "crawler",
    "export",
    "project",
    "prototype",
    "database",
    "doc",
    "utils",
    "wu_tools",
)


def sha(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def copy_tree(source, target, excluded=EXCLUDED):
    for parent, directories, files in os.walk(source):
        directories[:] = [
            name
            for name in directories
            if name not in excluded and not (Path(parent) / name).is_symlink()
        ]
        for name in files:
            path = Path(parent) / name
            if (
                path.is_symlink()
                or name.startswith(".env")
                or name.endswith(
                    (".pyc", ".pyo", ".bak", ".log", ".db", ".db-wal", ".db-shm")
                )
                or name == ".coverage"
            ):
                continue
            destination = target / path.relative_to(source)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)


def backup(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    with (
        sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as src,
        sqlite3.connect(destination) as dst,
    ):
        src.backup(dst)
        assert dst.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        counts = {
            name: dst.execute(
                'SELECT COUNT(*) FROM "' + name.replace('"', '""') + '"'
            ).fetchone()[0]
            for (name,) in dst.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    return {
        "path": destination.name,
        "bytes": destination.stat().st_size,
        "sha256": sha(destination),
        "integrity": "ok",
        "table_counts": counts,
    }


def prepare():
    for name in (
        "doc",
        "prototype",
        "project/frontend",
        "project/backend",
        "database",
        "utils",
    ):
        assert (ROOT / name).is_dir(), name
    assert DEST.is_dir()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    folder = DEST / ("当前可用版_" + stamp)
    folder.mkdir(exist_ok=False)
    for tree in TREES:
        copy_tree(ROOT / tree, folder / tree)
    for name in (
        "doc",
        "prototype",
        "project/frontend",
        "project/backend",
        "database",
        "utils",
    ):
        (folder / name).mkdir(parents=True, exist_ok=True)
    for name in (
        "config.json",
        "rules.yaml",
        "wu_validate.ps1",
        "需求补充记录.md",
        "工作台使用手册_v1.docx",
    ):
        if (ROOT / name).is_file():
            shutil.copy2(ROOT / name, folder / name)
    shutil.copy2(ROOT / "启动工作台.bat", folder / "doc/原启动工作台.bat")
    runtime = folder / "runtime"
    base = Path(sys.base_prefix)
    runtime.mkdir()
    for path in base.iterdir():
        if path.is_file() and (
            path.suffix.lower() in {".exe", ".dll"} or path.name.startswith("LICENSE")
        ):
            shutil.copy2(path, runtime / path.name)
    copy_tree(base / "DLLs", runtime / "DLLs", {"__pycache__"})
    copy_tree(
        base / "Lib",
        runtime / "Lib",
        {
            "site-packages",
            "test",
            "tests",
            "idlelib",
            "tkinter",
            "turtledemo",
            "ensurepip",
            "__pycache__",
        },
    )
    site = Path(sys.prefix) / "Lib/site-packages"
    assert site.is_dir() and sys.prefix != sys.base_prefix, (
        "请使用现有crawler虚拟环境执行"
    )
    copy_tree(site, runtime / "Lib/site-packages", {"__pycache__", ".pytest_cache"})
    (runtime / "Lib/site-packages/workbench-project.pth").write_text(
        "../../..\n../../../backend\n../../../crawler\n../../../export\n",
        encoding="utf-8",
    )
    dependencies = sorted(
        f"{d.metadata['Name']}=={d.version}"
        for d in importlib.metadata.distributions()
        if d.metadata["Name"]
    )
    (folder / "utils/requirements-snapshot.txt").write_text(
        "\n".join(dependencies) + "\n", encoding="utf-8"
    )
    (folder / "启动工作台.bat").write_bytes(
        (
            '@echo off\r\nsetlocal\r\nchcp 65001 >nul\r\npushd "%~dp0"\r\n"%~dp0runtime\\python.exe" -B -X utf8 "%~dp0utils\\portable_start.py" %*\r\nset "RC=%ERRORLEVEL%"\r\npopd\r\nif not "%RC%"=="0" pause\r\nexit /b %RC%\r\n'
        ).encode("utf-8")
    )
    (folder / "自检工作台.bat").write_bytes(
        (
            '@echo off\r\nsetlocal\r\nchcp 65001 >nul\r\npushd "%~dp0"\r\n"%~dp0runtime\\python.exe" -B -X utf8 "%~dp0utils\\portable_start.py" selfcheck\r\nset "RC=%ERRORLEVEL%"\r\npopd\r\npause\r\nexit /b %RC%\r\n'
        ).encode("utf-8")
    )
    # Keep source and business data separable: clean first launch, optional private recovery.
    private = DEST / ("现有业务数据备份_" + stamp)
    private.mkdir(exist_ok=False)
    databases = []
    for relative in ("workbench/data/workbench.db", "backend/data/leads.db"):
        source = ROOT / relative
        if source.is_file():
            entry = backup(source, private / relative)
            entry["restore_path"] = relative
            databases.append(entry)
    for relative in (
        "workbench/data/exports",
        "workbench/data/runs",
        "backend/data/images",
        "backend/whitelist",
    ):
        source = ROOT / relative
        if source.is_dir():
            copy_tree(source, private / relative, {"__pycache__"})
    (private / "备份清单.json").write_text(
        json.dumps(
            {
                "snapshot_at": datetime.now().isoformat(),
                "databases": databases,
                "note": "私有数据，仅本地备份；恢复前停止服务；不包含浏览器登录态或Windows凭据；历史文件字段可能保留原目录绝对路径。",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (folder / "README.md").write_text(
        f"""# 侵权线索工作台 · 当前可用版

打包时间：{stamp}。从当前工作区取源码，包含已完成的正式玻璃版前端。

## 开始使用

1. 将整个目录解压到一个可写的新目录，建议路径尽量短。
2. 双击 `启动工作台.bat`，默认通过已安装的 Microsoft Edge 打开。
3. 访问 `http://127.0.0.1:8765/`。首次使用在本机 `/setup` 设置主管密码。
4. 退出时在服务窗口按 Ctrl+C。不要同时启动原项目和本副本的8765端口。

本包自带 Windows x64 Python 3.13.13 及已安装依赖，启动不需重新安装Python或联网下载依赖。
已在本机 Windows / Edge 验证；系统仍需能运行Windows x64程序。真实采集需网络、Edge及本人登录。
新包是干净的数据环境，原账号、线索和任务另存于旁边的“现有业务数据备份”zip，不会覆盖原项目。
SQLite无独立数据库账号或密码；主管密码由首次初始化自行设置，无预置密码。

## 数据与恢复

- 新数据：`workbench/data/workbench.db` 与 `backend/data/leads.db`。
- 旧数据备份采用SQLite在线backup，包含完整性检查和各表条数，不是直接复制正在使用的db。
- 如需继续旧数据：关闭新旧服务，先备份新目录已有数据，再把独立备份中的`workbench/data`和`backend/data`复制到新目录对应位置。操作前确认没有进行中的任务。
- 旧报表、图片和任务配置按目录备份；数据库中已有绝对路径可能仍指向原项目，换机器后需重新定位历史文件。该数据恢复路径未做自动迁移。
- Windows邮件授权码及浏览器Cookies未复制，换电脑需重新配置并登录。业务数据备份不应公开分享。

## 代码和验证

正式前端在`workbench/web`，服务在`workbench`，判定后端在`backend/pipeline`，采集在`crawler`，Excel模块在`export`。
`project/backend`保留独立商品审核计算模块，`prototype`保留设计原型和截图。
运行`runtime\\python.exe -B -X utf8 utils\\portable_start.py selfcheck`检查本包。
运行`runtime\\python.exe -B -X utf8 utils\\workbench_glass_acceptance.py`进行隔离Edge浏览验收。
`doc/重新打包验证结果.json`记录本次实际验证结果，`文件校验清单.json`记录文件SHA-256。
打包标识用于区分此次交付，应用内部版本号继续沿用现有1.0.0。
原历史验收工具仍随源码保留，`wu_validate.ps1`是旧e轮部署验证流程，不作为本包启动命令。
不要把这个自带运行环境的大zip上传到页面“版本更新”（原更新接口有64MB限制），请完整解压启动。
""",
        encoding="utf-8",
    )
    info = {
        "folder": str(folder),
        "private": str(private),
        "stamp": stamp,
        "python": sys.version.split()[0],
        "dependencies": len(dependencies),
    }
    (folder / "doc/打包来源.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(info, ensure_ascii=False), flush=True)


def finalize(folder):
    folder = folder.resolve()
    assert folder.parent == DEST.resolve(), "只允许封装本次exports交付目录"
    report = json.loads(
        (folder / "doc/重新打包验证结果.json").read_text(encoding="utf-8")
    )
    assert report["ok"] is True, "必须先通过解压目录验证"
    records = []
    for path in sorted(folder.rglob("*")):
        if (
            path.is_file()
            and "__pycache__" not in path.parts
            and ".pytest_cache" not in path.parts
            and path.name != "文件校验清单.json"
            and path.name != ".coverage"
            and not path.name.startswith(".env")
            and path.relative_to(folder).parts[:2]
            not in {("workbench", "data"), ("backend", "data")}
            and path.relative_to(folder).parts[:2] != ("doc", "旧隔离路径配置.txt")
        ):
            records.append(
                {
                    "path": path.relative_to(folder).as_posix(),
                    "bytes": path.stat().st_size,
                    "sha256": sha(path),
                }
            )
    (folder / "文件校验清单.json").write_text(
        json.dumps(
            {"algorithm": "sha256", "files": records}, ensure_ascii=False, indent=2
        ),
        encoding="utf-8",
    )
    zip_path = folder.with_suffix(".zip")
    with zipfile.ZipFile(
        zip_path,
        "x",
        compression=zipfile.ZIP_STORED,
        allowZip64=True,
    ) as z:
        for relative in (
            "doc/",
            "prototype/",
            "project/frontend/",
            "project/backend/",
            "database/",
            "utils/",
        ):
            z.writestr(relative, b"")
        for record in records:
            z.write(folder / record["path"], record["path"])
        z.write(folder / "文件校验清单.json", "文件校验清单.json")
    with zipfile.ZipFile(zip_path) as z:
        assert z.testzip() is None
        for record in records:
            assert (
                hashlib.sha256(z.read(record["path"])).hexdigest() == record["sha256"]
            ), record["path"]
    info = json.loads((folder / "doc/打包来源.json").read_text(encoding="utf-8"))
    private = Path(info["private"])
    private_zip = private.with_suffix(".zip")
    with zipfile.ZipFile(
        private_zip, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=5
    ) as z:
        for path in sorted(private.rglob("*")):
            if path.is_file():
                z.write(path, path.relative_to(private).as_posix())
    with zipfile.ZipFile(private_zip) as z:
        assert z.testzip() is None
    result = {
        "code_zip": str(zip_path),
        "bytes": zip_path.stat().st_size,
        "sha256": sha(zip_path),
        "files": len(records) + 1,
        "private_zip": str(private_zip),
        "private_bytes": private_zip.stat().st_size,
        "private_sha256": sha(private_zip),
        "ok": True,
    }
    (zip_path.parent / (folder.name + "_校验结果.json")).write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["prepare", "finalize"])
    parser.add_argument("folder", nargs="?", type=Path)
    args = parser.parse_args()
    prepare() if args.action == "prepare" else finalize(args.folder)
