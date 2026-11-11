"""通过现有工作台服务执行一次星星人三平台、前三页任务。"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from workbench.config import WorkbenchConfig
from workbench.db import Database
from workbench.tasks import TaskService
from workbench import backend_bridge as bridge


class TrialTaskService(TaskService):
    def _prepare_run(self, task, ip):
        prepared = super()._prepare_run(task, ip)
        run_dir = prepared["run_dir"]
        # 用户只停用本轮白名单，不改动已保存的 IP/公司白名单。
        prepared["whitelist_path"].write_text("", encoding="utf-8")
        prepared["entries"] = []
        crawler_path = prepared["crawler_config"]
        raw = yaml.safe_load(crawler_path.read_text(encoding="utf-8-sig"))
        crawl = raw.setdefault("crawl", {})
        crawl["max_pages"] = 3
        crawl.setdefault("taobao", {})["max_pages"] = 3
        crawl["keywords"] = [{"keyword": "星星人", "ip_code": task["ip_norm"]}]
        # 使用原有登录 profile、节流、风控状态；仅隔离本次原始输出。
        crawl["output_dir"] = str(run_dir / "capture")
        # 小批量试采保留主图链接，不等待逐张图片下载。
        crawl.setdefault("image", {})["enabled"] = False
        crawler_path.write_text(
            yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        backend_path = prepared["backend_config"]
        settings = prepared["settings"]
        settings.raw.setdefault("paths", {})["crawler_output_dir"] = str(
            run_dir / "capture"
        )
        backend_path.write_text(
            yaml.safe_dump(settings.raw, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        bridge.ensure_backend_paths()
        from pipeline.config import load_config

        prepared["settings"] = load_config(backend_path)
        (run_dir / "trial_request.json").write_text(
            json.dumps(
                {
                    "ip_name": "星星人",
                    "platforms": task["platforms"],
                    "keywords": ["星星人"],
                    "max_pages": 3,
                    "whitelist": [],
                    "include_company": False,
                    "architecture": "workbench/PipelineRunner/crawler/export",
                    "amount_policy": "沿用现有系统输出，单商品筛选口径仍待确认，不能视为新口径验收",
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return prepared


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--start", action="store_true")
    action.add_argument("--execute", metavar="TASK_NO")
    parser.add_argument(
        "--platform",
        nargs="+",
        choices=["taobao", "jd", "1688"],
        default=["taobao", "jd", "1688"],
    )
    args = parser.parse_args()
    config = WorkbenchConfig.load()
    # 基础 rules 中路径相对 backend；明确传入原配置，避免按项目根误解析后退化。
    config.crawler_config_path = ROOT / "crawler" / "config" / "windows.yaml"
    db = Database(config.db_path)
    service = TrialTaskService(config, db)
    if args.start:
        task = service.create_task(
            {
                "ip_name": "星星人",
                "keywords": ["星星人"],
                "platforms": args.platform,
                "mode": "formal",
                "whitelist": [],
                "whitelist_provided": False,
                "include_company": False,
                "notify_email": "",
            },
            actor={"id": None, "username": "Crow5用户委托运行"},
            run_now=False,
        )
        run_dir = config.runs_dir / task["task_no"]
        run_dir.mkdir(parents=True, exist_ok=False)
        env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        with (run_dir / "execution.log").open("x", encoding="utf-8") as log:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-X",
                    "utf8",
                    str(Path(__file__).resolve()),
                    "--execute",
                    task["task_no"],
                ],
                cwd=ROOT,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
        print(
            json.dumps(
                {
                    "task_no": task["task_no"],
                    "pid": process.pid,
                    "run_dir": str(run_dir),
                },
                ensure_ascii=False,
            )
        )
        return 0
    task = service.get_task(args.execute)
    if task["ip_name"] != "星星人" or task["keywords"] != ["星星人"]:
        raise ValueError("任务不属于本轮星星人采集")
    result = service.execute(args.execute)
    output = config.runs_dir / args.execute / "trial_result.json"
    with output.open("x", encoding="utf-8") as target:
        json.dump(result, target, ensure_ascii=False, indent=2, default=str)
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
