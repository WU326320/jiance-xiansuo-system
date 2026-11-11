"""隔离验收环境：临时数据库、测试账号、模拟执行器，不连接正式采集或邮件。

启动：backend/.venv/Scripts/python.exe utils/workbench_preview.py
访问：http://127.0.0.1:2876，账号 preview，密码 Preview1234
退出后临时数据自动清理。
"""

from __future__ import annotations

import os
import argparse
import sys
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from wsgiref.simple_server import make_server

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def serve(app, config) -> None:
    app.scheduler.start()
    try:
        with make_server(config.host, config.port, app) as server:
            print(f"Preview ready: http://{config.host}:{config.port}", flush=True)
            server.serve_forever()
    finally:
        app.scheduler.stop()


def main(port=2876, empty=False) -> None:
    from workbench.app import WorkbenchApp
    from workbench.config import WorkbenchConfig
    from workbench.tests.conftest import make_lead

    with TemporaryDirectory(prefix="workbench-preview-") as temporary:
        data = Path(temporary)
        os.environ["IP_LEAD_DB_PATH"] = str(data / "leads.db")
        os.environ.pop("IP_LEAD_DB_URL", None)
        config = WorkbenchConfig(
            data_dir=data,
            db_path=data / "workbench.db",
            leads_db_path=data / "leads.db",
            host="127.0.0.1",
            port=port,
            allowed_networks=["127.0.0.1/32"],
            smtp={"enabled": False},
        )
        app = WorkbenchApp(config)
        if empty:
            serve(app, config)
            return
        actor = app.auth.create_supervisor("preview", "Preview1234")
        for username, role in (("member", "user"), ("manager", "admin")):
            invite = app.auth.create_invite(actor, role=role)
            app.auth.register(invite["code"], username, "Preview1234")
        app.leads.ensure_ready()
        for index, (shop, status, amount) in enumerate(
            [
                ("星河动漫旗舰店", "待复核", 780000),
                ("青禾玩具专营店", "待复核", 532000),
                ("小熊文创生活馆", "待复核", 420000),
                ("云间数码配件店", "已确认", 916000),
                ("晨光礼品企业店", "已录入SaaS", 656000),
                ("雨林周边工作室", "驳回", 280000),
            ]
        ):
            make_lead(
                config,
                lead_no=f"PREVIEW-{index + 1:03}",
                shop_name=shop,
                status=status,
                amount=amount,
            )
        for index in range(22):
            make_lead(
                config,
                lead_no=f"PAGE-{index:03}",
                shop_name=f"分页验收店铺 {index + 1:02}",
                amount=[
                    0,
                    49999.99,
                    50000,
                    99999.99,
                    100000,
                    199999.99,
                    200000,
                    399999.99,
                    400000,
                    499999.99,
                    500000,
                ][index % 11],
            )
        ip = app.ips.get_or_create(
            "葫芦兄弟",
            company_code="demo",
            company_name="演示授权公司",
            keywords=["葫芦娃", "葫芦兄弟"],
            actor=actor,
        )
        app.ips.save_task_whitelist(
            ip["id"],
            [
                {
                    "scope_type": "ip",
                    "platform_group": "taobao",
                    "shop_name": "正版授权示例店",
                    "shop_id": "DEMO-001",
                }
            ],
            actor=actor,
        )
        app.ips.add_company_entries(
            actor,
            "demo",
            [
                {
                    "platform_group": "taobao",
                    "shop_name": "公司共享授权店",
                    "shop_id": "COMPANY-001",
                }
            ],
        )
        export_path = Path(app.leads.export("2026-11-06")["path"])
        complete_result = {
            "status": "success",
            "candidates": 3,
            "run_id": "PREVIEW",
            "platforms": {
                "taobao": {
                    "label": "淘宝/天猫",
                    "items": 24,
                    "leads": 3,
                    "status": "ok",
                }
            },
            "risks": {"达标": 2, "待核验": 0, "临界": 1},
            "excel_path": str(export_path),
        }
        incomplete_result = {
            "status": "success",
            "candidates": 0,
            "run_id": "PREVIEW-AMOUNT",
            "stop_reason": "deadline",
            "platforms": {
                "1688": {
                    "label": "1688",
                    "items": 3,
                    "leads": 0,
                    "status": "deadline",
                    "amount_complete": 1,
                    "amount_missing": 2,
                    "missing_fields": {"price": 1, "sales": 1, "comments": 2},
                }
            },
            "excel_path": str(export_path),
        }
        app.tasks.pipeline_executor = lambda _service, task, _prepared: (
            incomplete_result if task["ip_name"] == "金额缺失验收" else complete_result
        )
        app.tasks.create_task(
            {"ip_name": "葫芦兄弟", "platforms": ["taobao"], "mode": "dry_run"},
            actor=actor,
        )
        app.tasks.create_task(
            {
                "ip_name": "金额缺失验收",
                "platforms": ["1688"],
                "mode": "dry_run",
                "_sync": True,
            },
            actor=actor,
        )
        app.tasks.create_task(
            {
                "ip_name": "历史预约验收",
                "platforms": ["taobao"],
                "mode": "dry_run",
                "scheduled_at": "2099-01-01T10:00",
            },
            actor=actor,
        )
        serve(app, config)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="隔离工作台浏览验收环境")
    parser.add_argument("--port", type=int, default=2876)
    parser.add_argument(
        "--empty", action="store_true", help="空数据库，用于首次初始化验收"
    )
    args = parser.parse_args()
    main(port=args.port, empty=args.empty)
