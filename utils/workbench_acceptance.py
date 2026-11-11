"""执行一轮有界的真实工作台验收，保留正式任务和数据库记录。"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description="真实工作台有界验收")
    parser.add_argument(
        "--platforms", nargs="+", choices=["taobao", "jd", "1688"], default=["taobao"]
    )
    parser.add_argument("--ip", default="葫芦兄弟", help="本次采集的 IP 名和搜索关键词")
    parser.add_argument(
        "--ends-at", required=True, help="共同截止时间 YYYY-MM-DD HH:MM:SS（本机时间）"
    )
    parser.add_argument(
        "--background", action="store_true", help="后台执行并保留运行日志"
    )
    args = parser.parse_args()
    if not args.ip.strip():
        parser.error("IP 名不能为空")
    args.ip = args.ip.strip()
    from workbench.tasks import parse_datetime

    deadline = parse_datetime(args.ends_at)
    if deadline <= datetime.now():
        parser.error("截止时间必须晚于当前时间")
    args.ends_at = deadline.strftime("%Y-%m-%d %H:%M:%S")
    import yaml
    from workbench.config import WorkbenchConfig
    from workbench.db import Database
    from workbench.tasks import TaskService

    config = WorkbenchConfig.load()
    config.ensure_dirs()
    db = Database(config.db_path)
    db.init_schema()
    active = db.fetchone(
        "SELECT task_no FROM tasks WHERE status IN ('running','queued') LIMIT 1"
    )
    if active:
        raise RuntimeError(f"已有活动任务 {active['task_no']}，不并发使用采集浏览器")
    if args.background:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        log_path = config.logs_dir / f"acceptance_{stamp}.log"
        command = [
            sys.executable,
            "-X",
            "utf8",
            str(Path(__file__).resolve()),
            "--ip",
            args.ip,
            "--ends-at",
            args.ends_at,
            "--platforms",
            *args.platforms,
        ]
        with log_path.open("x", encoding="utf-8") as log:
            process = subprocess.Popen(
                command,
                cwd=ROOT,
                env=dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8"),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "DETACHED_PROCESS", 0),
            )
        print(
            json.dumps({"pid": process.pid, "log": str(log_path)}, ensure_ascii=False),
            flush=True,
        )
        return 0
    actor = db.fetchone(
        "SELECT id,username,role,status FROM users WHERE role='supervisor' AND status='active' LIMIT 1"
    )
    if not actor:
        raise RuntimeError("需要已有的有效主管账号")
    source = ROOT / "crawler/config/windows.yaml"
    raw = yaml.safe_load(source.read_text(encoding="utf-8-sig"))
    raw["crawl"]["max_pages"] = 1
    raw["crawl"]["taobao"]["max_pages"] = 1
    raw["crawl"]["taobao"]["max_keywords_per_run"] = 1
    # 不变更页面/详情间隔、请求预算、登录及风险停止条件。
    trial_config = config.runs_dir / "acceptance_crawler.yaml"
    trial_config.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    service = TaskService(replace(config, crawler_config_path=trial_config), db)
    task = service.create_task(
        {
            "ip_name": args.ip,
            "platforms": list(dict.fromkeys(args.platforms)),
            "mode": "formal",
            "keywords": [args.ip],
            "notify_email": "",
            "ends_at": args.ends_at,
        },
        actor=actor,
        source_ip="127.0.0.1",
        run_now=False,
    )
    print(
        json.dumps(
            {
                "task_no": task["task_no"],
                "status": task["status"],
                "ends_at": task["ends_at"],
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    result = service.execute(task["task_no"])
    report = config.runs_dir / task["task_no"] / "acceptance_result.json"
    report.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    exit_code = main()
    print(json.dumps({"execution_exit_code": exit_code}), flush=True)
    raise SystemExit(exit_code)
