"""运行日志：控制台 + 每平台一个文件 ``logs/{date}_{platform}.log``。

每个请求至少记录时间、URL、耗时、结果、条数（由 Browser/平台模块写入）。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path


def setup_logging(verbose: bool = False) -> None:
    root = logging.getLogger()
    if root.handlers:
        root.setLevel(logging.DEBUG if verbose else logging.INFO)
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)


def platform_logger(platform: str, date: str, log_dir: str | Path) -> tuple[logging.Logger, Path]:
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{date}_{platform}.log"
    logger = logging.getLogger(f"crawler.{platform}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        if getattr(handler, "baseFilename", None) == str(path.resolve()):
            return logger, path
        logger.removeHandler(handler)
        handler.close()
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger, path
