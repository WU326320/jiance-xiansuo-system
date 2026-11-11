"""文件日志：logs/run_YYYYMMDD.log，保留 30 天；每步记录开始/结束/条数/耗时。"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path

from pipeline.config import Settings


def cleanup_old_logs(settings: Settings) -> list[Path]:
    logs_dir = settings.logs_dir
    if not logs_dir.exists():
        return []
    cutoff = time.time() - settings.log_keep_days * 86400
    removed: list[Path] = []
    for path in logs_dir.glob("run_*.log"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed.append(path)
        except FileNotFoundError:
            pass
    return removed


def setup_run_logger(settings: Settings, run_id: str) -> logging.Logger:
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    cleanup_old_logs(settings)
    logger = logging.getLogger("pipeline")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # noqa: BLE001
            pass
    log_path = settings.logs_dir / f"run_{datetime.now():%Y%m%d}.log"
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(console)
    logger.info("run_id=%s 日志文件=%s", run_id, log_path)
    return logger


class StepLogger:
    """上下文管理器：开始/结束/条数/耗时。"""

    def __init__(self, logger: logging.Logger, step: str, **fields: object):
        self.logger = logger
        self.step = step
        self.fields = fields
        self.count: int | None = None
        self.started = 0.0

    def __enter__(self) -> "StepLogger":
        self.started = time.perf_counter()
        detail = " ".join(f"{key}={value}" for key, value in self.fields.items())
        self.logger.info("开始 step=%s %s", self.step, detail)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:  # noqa: ANN001
        elapsed = time.perf_counter() - self.started
        if exc_type is not None:
            self.logger.error(
                "失败 step=%s 耗时=%.3fs 错误=%s: %s",
                self.step,
                elapsed,
                exc_type.__name__,
                exc,
            )
            return False
        count = f" 条数={self.count}" if self.count is not None else ""
        self.logger.info("结束 step=%s%s 耗时=%.3fs", self.step, count, elapsed)
        return False


__all__ = ["StepLogger", "cleanup_old_logs", "setup_run_logger"]
