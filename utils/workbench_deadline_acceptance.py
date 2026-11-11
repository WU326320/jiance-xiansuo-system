"""不限时、显式截止与补采验收：真实 HTTP/页面，临时数据库及受控执行器。"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from wsgiref.simple_server import WSGIRequestHandler, make_server

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
REPORT = ROOT / "doc/采集截止补采浏览器验收.json"


class QuietHandler(WSGIRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


def main():
    for directory in (
        "doc",
        "prototype",
        "project/frontend",
        "project/backend",
        "database",
        "utils",
    ):
        assert (ROOT / directory).is_dir(), directory
    report = {"passed": False, "checks": [], "page_errors": [], "scenarios": []}
    old_env = {
        key: os.environ.get(key) for key in ("IP_LEAD_DB_PATH", "IP_LEAD_DB_URL")
    }
    try:
        with TemporaryDirectory(prefix="workbench-deadline-") as temporary:
            data = Path(temporary)
            os.environ["IP_LEAD_DB_PATH"] = str(data / "leads.db")
            os.environ.pop("IP_LEAD_DB_URL", None)
            from workbench.app import WorkbenchApp
            from workbench.auth import AuthError
            from workbench.config import WorkbenchConfig
            from workbench.mailer import Mailer
            from workbench.tasks import parse_datetime

            config = WorkbenchConfig(
                project_root=ROOT,
                data_dir=data,
                db_path=data / "workbench.db",
                leads_db_path=data / "leads.db",
                host="127.0.0.1",
                allowed_networks=["127.0.0.1/32"],
                smtp={"enabled": False},
            )
            app = WorkbenchApp(config)
            app.auth.create_supervisor("preview", "Preview1234")
            app.leads.ensure_ready()
            entered, release = Event(), Event()
            mailbox, active = [], []
            clock = [datetime.now()]
            app.tasks.clock = lambda: clock[0]

            def transport(message, recipients):
                task = app.tasks.get_task(active[-1])
                assert task["status"] in ("success", "partial")
                assert task["ended_at"] and task["mail_status"] == "pending"
                mailbox.append(message)

            app.mailer = Mailer(config, app.db, transport=transport)
            app.tasks.mailer = app.mailer

            def executor(service, task, prepared):
                deadline = parse_datetime(task["ends_at"]) if task["ends_at"] else None
                clock[0] = (
                    deadline + timedelta(seconds=1)
                    if deadline
                    else clock[0] + timedelta(days=2)
                )
                active.append(task["task_no"])
                entered.set()
                assert release.wait(30), "浏览器验收未释放补采"
                partial = task["ip_name"].endswith("partial")
                return {
                    "status": "partial" if partial else "success",
                    "stop_reason": "deadline" if deadline else "last_page",
                    "errors": ["1688:222 补采重试耗尽"] if partial else [],
                    "platforms": {
                        "1688": {
                            "items": 3,
                            "status": "partial"
                            if partial
                            else "deadline"
                            if deadline
                            else "ok",
                            "detail_failures": {"1688:222": "补采重试耗尽"}
                            if partial
                            else {},
                        }
                    },
                }

            app.tasks.pipeline_executor = executor
            with make_server("127.0.0.1", 0, app, handler_class=QuietHandler) as server:
                base = f"http://127.0.0.1:{server.server_port}"
                thread = Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    with sync_playwright() as p:
                        browser = p.chromium.launch(channel="msedge", headless=False)
                        page = browser.new_page(
                            viewport={"width": 1440, "height": 1000}
                        )
                        page.set_default_timeout(10000)
                        page.on(
                            "pageerror",
                            lambda err: report["page_errors"].append(str(err)),
                        )
                        try:
                            page.goto(base + "/login")
                            page.locator('#login-form [name="username"]').fill(
                                "preview"
                            )
                            page.locator('#login-form [name="password"]').fill(
                                "Preview1234"
                            )
                            page.locator('#login-form button[type="submit"]').click()
                            expect(page.locator('[data-nav="tasks"]')).to_be_visible()
                            page.locator('[data-nav="tasks"]').click()
                            expect(page.locator("#create-task")).to_be_visible()

                            scenarios = ("untimed", "success", "partial", "no-email")
                            for scenario in scenarios:
                                clock[0] = datetime.now()
                                entered.clear()
                                release.clear()
                                before = len(mailbox)
                                page.locator("#create-task").click()
                                expect(page.locator("#task-form")).to_contain_text(
                                    "常规采集截止时间"
                                )
                                expect(page.locator("#task-form")).to_contain_text(
                                    "留空不限时，逐页采集到末页"
                                )
                                page.locator("#t-ip").fill("截止收尾验收-" + scenario)
                                page.locator('.t-platform[value="taobao"]').uncheck()
                                page.locator('.t-platform[value="jd"]').uncheck()
                                deadline = (
                                    ""
                                    if scenario == "untimed"
                                    else (clock[0] + timedelta(minutes=10)).strftime(
                                        "%Y-%m-%dT%H:%M"
                                    )
                                )
                                page.locator("#t-ends-at").fill(deadline)
                                if scenario == "untimed":
                                    expect(
                                        page.locator("#t-ends-at")
                                    ).to_have_js_property("required", False)
                                    expect(
                                        page.locator("#task-summary")
                                    ).to_contain_text("不限时，逐页采集到末页")
                                page.locator("#t-email").fill(
                                    ""
                                    if scenario == "no-email"
                                    else "owner@example.test"
                                )
                                if scenario == "untimed":
                                    for width in (1440, 768, 390):
                                        page.set_viewport_size(
                                            {"width": width, "height": 1000}
                                        )
                                        assert page.locator("#modal-overlay").evaluate(
                                            "e=>e.scrollWidth<=e.clientWidth+1"
                                        )
                                        page.screenshot(
                                            path=str(
                                                ROOT
                                                / f"prototype/deadline-form-{width}.png"
                                            )
                                        )
                                    page.set_viewport_size(
                                        {"width": 1440, "height": 1000}
                                    )
                                    report["checks"].append(
                                        "桌面、平板、手机表单说明可见且无横向溢出"
                                    )
                                page.locator("#submit-task").click()
                                expect(
                                    page.locator("#task-confirmation")
                                ).to_contain_text("常规采集截止时间")
                                if scenario == "untimed":
                                    expect(
                                        page.locator("#task-confirmation")
                                    ).to_contain_text("不限时，逐页采集到末页")
                                page.locator("#confirm-run").evaluate(
                                    "el=>{el.click();el.click()}"
                                )
                                expect(page.locator("#modal-overlay")).to_have_count(0)
                                assert entered.wait(10)
                                task_no = active[-1]
                                if deadline:
                                    assert clock[0] > parse_datetime(deadline)
                                record = page.request.get(
                                    base + f"/api/tasks/{task_no}"
                                ).json()["task"]
                                assert (
                                    record["status"] == "running"
                                    and record["ended_at"] is None
                                )
                                assert bool(record["ends_at"]) == bool(deadline)
                                assert len(mailbox) == before
                                row = page.locator("#task-body tr").filter(
                                    has_text=task_no
                                )
                                page.locator("#refresh-tasks").click()
                                expect(row).to_contain_text("运行中")
                                row.locator("[data-task]").click()
                                expect(page.locator("#modal-overlay")).to_contain_text(
                                    "尚未完成"
                                )
                                if scenario == "untimed":
                                    expect(
                                        page.locator("#modal-overlay")
                                    ).to_contain_text("不限时，逐页采集到末页")
                                page.screenshot(
                                    path=str(
                                        ROOT
                                        / f"prototype/deadline-waiting-{scenario}.png"
                                    )
                                )
                                page.keyboard.press("Escape")
                                release.set()
                                page.wait_for_function(
                                    "async no => { const r=await fetch('/api/tasks/'+no); const t=(await r.json()).task; return ['sent','skipped'].includes(t.mail_status); }",
                                    arg=task_no,
                                    polling=200,
                                )
                                final = page.request.get(
                                    base + f"/api/tasks/{task_no}"
                                ).json()["task"]
                                assert final["status"] == (
                                    "partial" if scenario == "partial" else "success"
                                )
                                assert final["ended_at"]
                                assert final["result"]["stop_reason"] == (
                                    "deadline" if deadline else "last_page"
                                )
                                page.locator("#refresh-tasks").click()
                                expect(row.locator(".pill")).to_have_text(
                                    {
                                        "untimed": "成功",
                                        "partial": "部分失败",
                                        "success": "已完成（常规采集已截止）",
                                        "no-email": "已完成（常规采集已截止）",
                                    }[scenario]
                                )
                                row.locator("[data-task]").click()
                                expect(page.locator("#modal-overlay")).to_contain_text(
                                    "未发送" if scenario == "no-email" else "已发送"
                                )
                                if scenario == "partial":
                                    expect(
                                        page.locator("#modal-overlay")
                                    ).to_contain_text("补采重试耗尽")
                                    assert "补采重试耗尽" in mailbox[-1].get_content()
                                if scenario == "untimed":
                                    expect(
                                        page.locator("#modal-overlay")
                                    ).to_contain_text("不限时，逐页采集到末页")
                                    assert not final["ends_at"]
                                page.screenshot(
                                    path=str(
                                        ROOT
                                        / f"prototype/deadline-finished-{scenario}.png"
                                    )
                                )
                                page.keyboard.press("Escape")
                                assert len(mailbox) - before == int(
                                    scenario != "no-email"
                                )
                                try:
                                    app.tasks.execute(task_no)
                                except AuthError as exc:
                                    assert exc.code == "task_not_runnable"
                                else:
                                    raise AssertionError("已完成任务不应再次执行")
                                assert len(mailbox) - before == int(
                                    scenario != "no-email"
                                )
                                report["scenarios"].append(
                                    {
                                        "scenario": scenario,
                                        "mail_count": len(mailbox) - before,
                                        "passed": True,
                                    }
                                )
                                print(
                                    f"通过：{scenario} 执行时间语义、收尾通知及重复执行保护",
                                    flush=True,
                                )
                            assert len(app.tasks.list_tasks()) == len(scenarios)
                            assert not report["page_errors"], report["page_errors"]
                            report["checks"].extend(
                                [
                                    "空截止时间可创建任务，时钟推进两天仍等待执行器收尾，详情显示不限时",
                                    "截止后补采等待期间仍为运行中且无完成邮件",
                                    "成功及最终失败均在收尾后提交终态并按配置发送一次邮件",
                                    "未填写邮箱不发送通知",
                                    "任务创建双击及已完成任务重复执行保护",
                                    "Edge页面脚本无异常",
                                ]
                            )
                            report["passed"] = True
                        finally:
                            release.set()
                            browser.close()
                finally:
                    server.shutdown()
                    thread.join(timeout=5)
    finally:
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        report["completed_at"] = datetime.now().isoformat(timespec="seconds")
        report["isolation"] = (
            "临时数据库；受控时钟；模拟采集及补采执行器；内存邮件，不连接外部采集及SMTP"
        )
        REPORT.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
