"""采集模块适配器。

正式运行：逐个平台调用 ``python -m crawler run --platform ...``，单平台
子进程失败不抛到外层；测试/补跑：用 ``FileCrawlerAdapter`` 直接读
``out/<date>/*.jsonl``。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import uuid
from datetime import date
from pathlib import Path
from typing import Protocol

from pipeline.config import Settings
from pipeline.crawler_python import crawler_python
from pipeline.ingest import PlatformData, load_platform_dir


class CrawlerAdapter(Protocol):
    def run(self, platform: str, run_date: date) -> PlatformData: ...


class FileCrawlerAdapter:
    """从现成目录读取采集结果（试运行/回归测试/补跑）。"""

    def __init__(self, base_dir: str | Path, *, logger: logging.Logger | None = None):
        self.base_dir = Path(base_dir).expanduser().resolve()
        self.logger = logger or logging.getLogger("pipeline")

    def run(self, platform: str, run_date: date) -> PlatformData:
        dated = self.base_dir / run_date.isoformat()
        directory = dated if dated.is_dir() else self.base_dir
        data = load_platform_dir(directory, platform)
        self.logger.info(
            "读取采集文件 platform=%s dir=%s items=%d",
            platform,
            directory,
            len(data.items),
        )
        return data


class SubprocessCrawlerAdapter:
    """调用采集模块 CLI；每个平台独立子进程，失败只影响该平台。"""

    def __init__(self, settings: Settings, *, logger: logging.Logger | None = None):
        self.settings = settings
        self.logger = logger or logging.getLogger("pipeline")

    def run(self, platform: str, run_date: date) -> PlatformData:
        if self.settings.deadline_reached:
            return PlatformData(
                platform=platform,
                items=[],
                run_record={
                    "platform": platform,
                    "login_ok": False,
                    "status": "deadline",
                    "errors": [],
                    "items_ok": 0,
                },
            )
        command = [
            crawler_python(self.settings),
            "-m",
            "crawler",
            "run",
            "--platform",
            platform,
            "--date",
            run_date.isoformat(),
            "--config",
            str(self.settings.crawler_config),
        ]
        env = dict(os.environ)
        paths = [str(self.settings.crawler_dir), str(self.settings.root)]
        env["PYTHONPATH"] = os.pathsep.join([*paths, env.get("PYTHONPATH", "")]).strip(
            os.pathsep
        )
        # Windows 管道默认用 cp936；强制子进程输出 UTF-8，JSON 才能稳定解析。
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        # JSONL 会追加写入并跨运行去重。每次调用独立输出，避免把同日旧 IP
        # 或旧价格重新归入当前任务，也保证再次查询同一商品能写入新结果。
        output_root = self.settings.crawler_output_dir / "runs" / uuid.uuid4().hex
        env["CRAWLER_OUTPUT_DIR"] = str(output_root)
        env["CRAWLER_AMOUNT_DETAIL"] = "1"
        env["CRAWLER_PROGRESS_SCOPE"] = str(
            self.settings.get("run.progress_scope") or output_root.name
        )
        if self.settings.get("run.ends_at"):
            env["CRAWLER_ENDS_AT"] = str(self.settings.get("run.ends_at"))
        else:
            env.pop("CRAWLER_ENDS_AT", None)
        # 整批采集没有人为时限；仅采集端处理明确截止及单次请求异常。
        timeout = None
        self.logger.info(
            "启动采集 platform=%s cwd=%s timeout=%ss",
            platform,
            self.settings.crawler_dir,
            timeout,
        )
        try:
            completed = subprocess.run(
                command,
                cwd=str(self.settings.crawler_dir),
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            return PlatformData(
                platform=platform,
                items=[],
                run_record={
                    "platform": platform,
                    "login_ok": False,
                    "status": "error",
                    "errors": [f"采集模块不可用: {exc}"],
                    "items_ok": 0,
                },
            )
        except subprocess.TimeoutExpired:
            directory = output_root / run_date.isoformat()
            data = load_platform_dir(directory, platform)
            checkpoint = directory / f"{platform}.partial.json"
            if checkpoint.exists():
                rows = json.loads(checkpoint.read_text(encoding="utf-8"))
                merged = {
                    (
                        row.get("platform"),
                        row.get("product_id"),
                        row.get("ip_code"),
                    ): row
                    for row in data.items
                }
                # 正式结果写入晚于检查点，包含图片等后处理字段；只补未落盘的命中。
                for row in rows:
                    key = (
                        row.get("platform"),
                        row.get("product_id"),
                        row.get("ip_code"),
                    )
                    merged.setdefault(key, row)
                data.items = list(merged.values())
            data.run_record = {
                **(data.run_record or {}),
                "platform": platform,
                "login_ok": bool(data.items)
                or bool(data.run_record.get("login_ok", False)),
                "status": "timeout",
                "errors": [
                    f"采集超过 {timeout:g}s"
                    if timeout is not None
                    else "采集进程异常超时，补采未正常收尾"
                ],
                "items_ok": len(data.items),
            }
            return data

        record: dict = {}
        for line in reversed((completed.stdout or "").splitlines()):
            text = line.strip()
            if text.startswith("{") and text.endswith("}"):
                try:
                    value = json.loads(text)
                    if isinstance(value, dict) and value.get("platform"):
                        record = value
                        break
                except json.JSONDecodeError:
                    continue
        if not record:
            record = {
                "platform": platform,
                "login_ok": False,
                "status": "error",
                "errors": [
                    f"采集进程退出码 {completed.returncode}，未找到 run 记录",
                    (completed.stderr or "")[-1000:],
                ],
                "items_ok": 0,
            }
        elif completed.returncode != 0:
            errors = record.get("errors") or []
            if isinstance(errors, str):
                errors = [errors]
            errors = list(errors)
            errors.append(f"采集进程退出码 {completed.returncode}")
            record["errors"] = errors

        directory = output_root / run_date.isoformat()
        data = load_platform_dir(directory, platform)
        if not data.run_record:
            data.run_record = record
        else:
            data.run_record = {**data.run_record, **record}
        if self.logger:
            self.logger.info(
                "采集结束 platform=%s status=%s login_ok=%s items=%d dir=%s",
                platform,
                data.status,
                data.login_ok,
                len(data.items),
                directory,
            )
        return data


__all__ = ["CrawlerAdapter", "FileCrawlerAdapter", "SubprocessCrawlerAdapter"]
