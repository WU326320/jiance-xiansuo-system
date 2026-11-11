"""backend 调用 crawler 子进程时使用的 Python 解释器选择。

backend 自己的 venv 不一定装了 crawler 的依赖（典型：缺 pydantic）。调用
``python -m crawler ...`` 时优先使用 ``crawler/.venv`` 里的解释器；不存在
时回退到当前解释器，保持旧行为。
"""

from __future__ import annotations

import sys
from pathlib import Path

from pipeline.config import Settings


def crawler_python(settings: Settings) -> str:
    """返回调用 crawler 子进程应使用的解释器路径。"""
    crawler_dir = Path(settings.crawler_dir)
    candidates = (
        crawler_dir / ".venv" / "Scripts" / "python.exe",
        crawler_dir / ".venv" / "bin" / "python",
        crawler_dir / ".venv" / "bin" / "python3",
    )
    for candidate in candidates:
        try:
            if candidate.is_file():
                return str(candidate)
        except OSError:
            continue
    return sys.executable
