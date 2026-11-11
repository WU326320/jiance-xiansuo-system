"""启动器：如果管理员安装过版本包，优先运行 current_version.json 指向的版本。

版本包约定为完整项目树（至少含 ``workbench/`` 包和 ``backend/`` 配置）；
数据目录始终指回原项目的 ``workbench/data``，更新/回滚不碰账号、任务和线索。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    return Path(
        os.environ.get("WORKBENCH_DATA_DIR") or (PROJECT_ROOT / "workbench" / "data")
    ).expanduser().resolve()


def resolve_release(project_root: Path | None = None) -> Path:
    root = (project_root or PROJECT_ROOT).resolve()
    state_path = data_dir() / "current_version.json"
    if not state_path.exists():
        return root
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return root
    candidate = Path(str(state.get("path") or ""))
    if not candidate.is_absolute():
        candidate = (root / candidate).resolve()
    if (candidate / "workbench" / "__main__.py").exists():
        return candidate
    return root


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    release = resolve_release()
    if release != PROJECT_ROOT:
        env = dict(os.environ)
        env["WORKBENCH_DATA_DIR"] = str(data_dir())
        os.chdir(release)
        os.execve(sys.executable, [sys.executable, "-m", "workbench", *args], env)
        return 0  # pragma: no cover
    from workbench.cli import main as cli_main  # noqa: PLC0415

    return cli_main(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
