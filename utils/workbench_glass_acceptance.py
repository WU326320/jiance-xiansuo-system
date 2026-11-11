"""正式玻璃版 Edge 验收；自动创建并关闭临时数据库服务，不操作正式数据。

运行：python -X utf8 utils/workbench_glass_acceptance.py
采集执行器为内存桩，页面、认证、数据库、复核、设置及文件接口均为真实实现。
"""

from __future__ import annotations

import json
import socket
import subprocess
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryFile
from urllib.request import urlopen
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "prototype"
REPORT = ROOT / "doc/工作台正式玻璃版验收结果.json"


@contextmanager
def service(*, empty=False):
    # Independent ephemeral listeners avoid Windows TIME_WAIT between fixtures.
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    command = [
        str(
            ROOT / "runtime/python.exe"
            if (ROOT / "runtime/python.exe").is_file()
            else ROOT / "backend/.venv/Scripts/python.exe"
        ),
        "-X",
        "utf8",
        str(ROOT / "utils/workbench_preview.py"),
        "--port",
        str(port),
    ]
    if empty:
        command.append("--empty")
    with TemporaryFile() as log:
        process = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    log.seek(0)
                    raise RuntimeError(log.read().decode("utf-8", "replace"))
                try:
                    with urlopen(base + "/api/bootstrap", timeout=1):
                        break
                except OSError:
                    time.sleep(0.15)
            else:
                raise RuntimeError("隔离服务启动超时")
            yield base
        finally:
            process.terminate()
            process.wait(timeout=10)


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
    checks, errors, api_responses = [], [], []
    started = datetime.now().isoformat(timespec="seconds")

    def check(name):
        checks.append(name)
        print("PASS:", name, flush=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 1000}, accept_downloads=True
        )
        page = context.new_page()
        page.set_default_timeout(10000)
        page.on("pageerror", lambda err: errors.append(str(err)))
        page.on(
            "response",
            lambda res: (
                api_responses.append(
                    {
                        "method": res.request.method,
                        "path": urlsplit(res.url).path
                        + (
                            "?" + urlsplit(res.url).query
                            if urlsplit(res.url).query
                            else ""
                        ),
                        "status": res.status,
                    }
                )
                if "/api/" in res.url
                else None
            ),
        )

        def shot(name):
            if name != "failure":
                page.locator(".toast").evaluate_all(
                    "nodes=>nodes.forEach(n=>n.remove())"
                )
            page.screenshot(
                path=str(OUTPUT / f"formal-glass-{name}.png"),
                full_page=False,
                animations="disabled",
            )

        def go(key):
            if page.locator("dialog[open]").count():
                page.keyboard.press("Escape")
            if page.locator("#menu-toggle").is_visible():
                page.locator("#menu-toggle").click()
            page.locator(f'[data-nav="{key}"]').click()
            expect(page.locator(".page-head h2")).to_be_visible()
            page.wait_for_function(
                "!document.querySelector('#main').textContent.includes('正在加载页面')"
            )

        def login(base, username="preview", password="Preview1234"):
            page.goto(base + "/login")
            page.locator('#login-form [name="username"]').fill(username)
            page.locator('#login-form [name="password"]').fill(password)
            page.locator('#login-form button[type="submit"]').click()
            expect(page.locator("#lead-body [data-open]").first).to_be_visible()

        try:
            with service(empty=True) as base:
                page.goto(base + "/setup")
                expect(page.locator("#setup-form")).to_be_visible()
                page.locator('[name="password"]').fill("Preview1234")
                page.locator('[name="confirm"]').fill("Different1234")
                page.locator('button[type="submit"]').click()
                expect(page.locator("#msg")).to_contain_text("两次密码不一致")
                page.locator('[name="confirm"]').fill("Preview1234")
                page.locator('button[type="submit"]').click()
                expect(page.locator(".page-head h2")).to_have_text("线索复核")
                check("首次初始化、密码确认校验及真实主管登录")
            context.clear_cookies()

            with service() as base:
                page.goto(base + "/login")
                shot("login-1440")
                page.locator('#login-form [name="username"]').fill("preview")
                page.locator('#login-form [name="password"]').fill("Wrong1234")
                page.locator('#login-form button[type="submit"]').click()
                expect(page.locator("#msg")).not_to_be_empty()
                expect(
                    page.locator('#login-form button[type="submit"]')
                ).to_be_enabled()
                login(base)
                check("真实登录、失败反馈及提交按钮恢复")
                shot("leads-1440")
                assert page.locator("#lead-body tr").count() == 20
                page.locator("#next-page").click()
                expect(page.locator("#page-info")).to_contain_text("第 2")
                page.locator("#prev-page").click()
                page.locator("#page-size").select_option("50")
                expect(page.locator("#lead-body tr")).to_have_count(25)
                page.locator("#f-sort").select_option("amount_desc")
                expect(page.locator("#lead-body tr").first).to_contain_text("星河动漫")
                page.locator("#f-q").fill("不存在的店铺")
                expect(page.locator("#lead-body")).to_contain_text("没有符合条件")
                page.locator("#reset-filters").click()
                page.locator(".filter-footer summary").click()
                page.locator("#f-date-from").fill("2026-11-07")
                page.locator("#f-date-to").fill("2026-11-06")
                page.locator("#f-date-to").blur()
                expect(page.locator("#filter-error")).to_contain_text(
                    "开始日期不能晚于"
                )
                page.locator("#reset-filters").click()
                expect(page.locator("#lead-body [data-open]").first).to_be_visible()
                check("真实分页、每页条数、排序、搜索空结果、日期边界与重置")

                expect(page.locator("#f-amount option")).to_have_text(
                    [
                        "不限制",
                        "5万元以上",
                        "10万元以上",
                        "20万元以上",
                        "40万元以上",
                        "50万元以上",
                    ]
                )
                baseline = page.request.get(
                    base + "/api/leads?page_size=200&status=待复核"
                ).json()["items"]
                for minimum in (0, 50000, 100000, 200000, 400000, 500000):
                    with page.expect_response(
                        lambda res: "/api/leads?" in res.url
                    ) as response:
                        page.locator("#f-amount").select_option(str(minimum))
                    assert response.value.status == 200
                    data = response.value.json()
                    expected = [
                        row for row in baseline if row["total_amount"] >= minimum
                    ]
                    assert data["total"] == data["stats"]["total"] == len(expected)
                    assert all(row["total_amount"] >= minimum for row in data["items"])
                    expect(page.locator("#lead-stats")).to_contain_text(
                        f"{len(expected)} 条线索"
                    )
                    expect(page.locator("#page-info")).to_contain_text("第 1")
                shot("sales-amount-filter-1440")
                page.locator("#reset-filters").click()
                expect(page.locator("#f-amount")).to_have_value("0")
                expect(page.locator("#lead-stats")).to_contain_text(
                    f"{len(baseline)} 条线索"
                )
                check("六档金额筛选真实接口、金额边界、统计同步、返回首页与重置")

                page.locator('[data-status="all"]').click()
                page.locator("#page-size").select_option("50")
                expect(page.locator("#lead-body input:disabled")).to_have_count(1)
                page.locator("#check-all").check()
                assert page.locator("#lead-body input:disabled:checked").count() == 0
                page.locator("#clear-selection").click()
                page.locator("#lead-body input:not(:disabled)").first.check()
                assert page.locator("#check-all").evaluate("e=>e.indeterminate")
                page.locator("#lead-body input:not(:disabled)").nth(1).check()
                page.locator('[data-batch="驳回"]').click()
                page.locator("#m-apply").click()
                expect(page.locator("#modal-overlay")).to_be_visible()
                expect(page.locator(".dialog-toasts")).to_contain_text("确认删除")
                page.locator("#batch-confirm").fill("确认删除")
                page.locator("#m-apply").click()
                expect(page.locator("#modal-overlay")).to_have_count(0)
                check("当前页全选与半选、已录入保护、批量误报确认及真实保存")

                page.locator('[data-status="待复核"]').click()
                expect(page.locator("#lead-body [data-open]").first).to_be_visible()
                page.locator("#lead-body [data-open]").first.click()
                expect(page.locator("#drawer-overlay[open]")).to_be_visible()
                shot("review-1440")
                old = page.locator("#lead-title").text_content()
                page.locator('[name="review-result"][value="已确认"]').check()
                page.locator("#submit-review").click()
                page.wait_for_function(
                    "old=>document.querySelector('#lead-title')?.textContent && document.querySelector('#lead-title').textContent!==old",
                    arg=old,
                )
                page.keyboard.press("Escape")
                page.locator('[data-status="已录入SaaS"]').click()
                page.locator("#lead-body [data-open]").first.click()
                expect(page.locator("#drawer-overlay .notice")).to_contain_text(
                    "受保护"
                )
                expect(page.locator("#submit-review")).to_have_count(0)
                page.keyboard.press("Escape")
                check("双栏详情、真实复核保存、连续下一条与已录入只读")

                go("tasks")
                expect(page.locator("#create-task")).to_be_visible()
                expect(page.locator("#task-form")).to_have_count(0)
                expect(page.locator("#task-body [data-task]").first).to_be_visible()
                shot("tasks-1440")
                amount_row = page.locator("#task-body tr").filter(
                    has_text="金额缺失验收"
                )
                expect(amount_row).to_contain_text("部分完成（金额待核验）")
                amount_row.locator("[data-task]").click()
                expect(page.locator(".task-platform-results")).to_contain_text(
                    "金额数据不完整：可计算 1 条 / 待核验 2 条"
                )
                expect(page.locator(".task-platform-results")).to_contain_text(
                    "缺价格 1 条 · 缺销量 1 条"
                )
                expect(page.locator(".task-platform-results")).to_contain_text(
                    "缺失值保留为空，未按 0 计算"
                )
                page.locator(".task-platform-results").scroll_into_view_if_needed()
                shot("amount-incomplete-1440")
                page.keyboard.press("Escape")
                check("金额缺失任务显示部分完成、可计算数量和缺失字段，缺失不伪填零")
                page.locator("#task-body [data-cancel]").first.click()
                page.locator("#m-apply").click()
                expect(page.locator("#task-body [data-cancel]")).to_have_count(0)
                page.locator("#create-task").click()
                page.locator("#t-ip").fill("葫芦兄弟")
                page.locator("#preview-ip").click()
                expect(page.locator("#preview-hint")).to_contain_text("已带出档案")
                page.locator("#task-advanced summary").click()
                expect(page.locator("#t-company")).to_have_value("demo")
                expect(
                    page.locator('[data-readonly="true"] .wl-name')
                ).to_have_attribute("readonly", "")
                expect(page.locator('[data-readonly="true"] .del')).to_be_disabled()
                page.locator("#t-keywords").fill("葫芦兄弟,正式前端验收")
                page.locator("#close-task").click()
                page.locator("#refresh-tasks").click()
                expect(page.locator("#task-refresh-state")).to_contain_text("已更新")
                go("leads")
                go("tasks")
                page.locator("#create-task").click()
                expect(page.locator("#t-keywords")).to_have_value(
                    "葫芦兄弟,正式前端验收"
                )
                page.locator("#t-ip").fill("其他 IP")
                expect(page.locator("#t-company")).to_have_value("")
                expect(page.locator(".wl-name").first).to_have_value("")
                page.locator("#t-ip").fill("葫芦兄弟")
                page.locator("#preview-ip").click()
                expect(page.locator("#preview-hint")).to_contain_text("已带出档案")
                page.locator("#t-mode").select_option("dry_run")
                page.locator("#t-ends-at").fill(
                    (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
                )
                shot("new-task-1440")
                page.locator("#submit-task").click()
                expect(page.locator("#task-confirmation")).to_contain_text(
                    "确认本次运行"
                )
                before = len(
                    [
                        r
                        for r in api_responses
                        if r["path"] == "/api/tasks" and r["method"] == "POST"
                    ]
                )
                page.locator("#confirm-run").evaluate("el=>{el.click();el.click()}")
                expect(page.locator("#modal-overlay")).to_have_count(0)
                page.wait_for_function(
                    "document.querySelector('#task-body').textContent.includes('成功')"
                )
                after = len(
                    [
                        r
                        for r in api_responses
                        if r["path"] == "/api/tasks" and r["method"] == "POST"
                    ]
                )
                assert after - before == 1
                check(
                    "任务记录优先、历史预约取消、共享只读、草稿跨页恢复、IP解绑及防重复创建"
                )

                page.locator("#task-body tr").filter(has_text="成功").first.locator(
                    "[data-task]"
                ).click()
                with page.expect_download() as download_info:
                    page.get_by_role("link", name="下载本次 Excel").click()
                download = download_info.value
                assert download.failure() is None
                assert Path(download.path()).read_bytes()[:2] == b"PK"
                page.keyboard.press("Escape")
                check("任务详情真实下载有效 XLSX 文件")

                go("ips")
                page.locator("#add-ip").click()
                page.locator("#new-ip-name").fill("玻璃版验收 IP")
                page.locator("#m-save").click()
                expect(page.locator("#ip-body")).to_contain_text("玻璃版验收 IP")
                row = page.locator("#ip-body tr").filter(has_text="玻璃版验收 IP")
                row.locator("[data-edit]").click()
                page.locator("#m-company").fill("glass-test")
                page.locator("#m-keywords").fill("测试,关键词")
                page.locator("#m-save").click()
                expect(page.locator("#ip-body")).to_contain_text("glass-test")
                row.locator("[data-edit]").click()
                page.locator("#manage-company").click()
                page.locator("#m-lines").fill("taobao,授权验收店,TEST-1")
                page.locator("#m-save").click()
                expect(page.locator("#modal-overlay")).to_have_count(0)
                check("IP档案真实创建、编辑及添加公司共享白名单")

                go("accounts")
                page.locator("#open-invite").click()
                page.locator("#create-invite").click()
                expect(page.locator("#invite-result")).to_be_visible()
                code = page.locator("#invite-result").input_value()
                register_url = page.locator("#register-link").input_value()
                assert code and register_url.startswith(base)
                expect(page.locator("#invite-body tr").first).to_be_attached()
                expect(page.locator("#invite-result")).to_have_value(code)
                shot("invite-result-1440")
                page.locator("#m-cancel").click()
                check("邀请码生成后列表刷新仍保留一次性结果")

                member = browser.new_context(viewport={"width": 390, "height": 900})
                joined = member.new_page()
                joined.on("pageerror", lambda err: errors.append(str(err)))
                joined.goto(register_url)
                expect(joined.locator("#register-form")).to_be_visible()
                expect(joined.locator("#invite-code")).to_have_value(code)
                joined.locator('#register-form [name="username"]').fill("glassmember")
                joined.locator('#register-form [name="password"]').fill("Member1234")
                joined.locator('#register-form [name="confirm"]').fill("Member1234")
                joined.locator('#register-form button[type="submit"]').click()
                expect(joined.locator(".page-head h2")).to_have_text("线索复核")
                assert joined.locator('[data-nav="settings"]').count() == 0
                assert joined.request.get(base + "/api/settings").status == 403
                member.close()
                check("真实邀请码注册、普通用户导航及后端权限拒绝")

                go("settings")
                snapshot = page.request.get(base + "/api/settings").json()["settings"]
                page.locator('[data-settings-tab="mail"]').click()
                page.locator("#s-smtp-host").fill("smtp.draft.example")
                page.locator('[data-settings-tab="tasks"]').click()
                page.locator("#s-email").fill("review@example.com")
                page.locator("#save-settings").click()
                expect(page.locator("#settings-hint")).to_contain_text("已保存")
                saved = page.request.get(base + "/api/settings").json()["settings"]
                assert saved["notify_email_default"] == "review@example.com"
                assert saved["smtp_host"] == snapshot["smtp_host"]
                assert saved["allowed_networks"] == snapshot["allowed_networks"]
                page.locator('[data-settings-tab="mail"]').click()
                expect(page.locator("#s-smtp-host")).to_have_value("smtp.draft.example")
                page.locator("#save-settings").click()
                expect(page.locator("#save-settings")).to_be_enabled()
                assert (
                    page.request.get(base + "/api/settings").json()["settings"][
                        "smtp_host"
                    ]
                    == "smtp.draft.example"
                )
                check("设置分区保存只更新当前分区、合并完整字段并保留其他草稿")

                go("update")
                page.locator("#u-upload").click()
                expect(page.locator("#toasts")).to_contain_text("请选择 zip")
                page.locator("#u-file").set_input_files(
                    {
                        "name": "invalid.zip",
                        "mimeType": "application/zip",
                        "buffer": b"not-a-valid-zip",
                    }
                )
                page.locator("#u-upload").click()
                expect(page.locator("#modal-title")).to_contain_text("确认安装")
                page.locator("#m-apply").click()
                expect(page.locator(".dialog-toasts")).not_to_be_empty()
                expect(page.locator("#m-apply")).to_be_enabled()
                page.keyboard.press("Escape")
                page.locator("#u-rollback").click()
                expect(page.locator("#modal-title")).to_contain_text("回滚")
                page.locator("#m-cancel").click()
                check("版本文件校验、安装确认、真实非法包拒绝及回滚取消")

                for width in (1440, 768, 390, 320):
                    page.set_viewport_size({"width": width, "height": 1000})
                    for key in (
                        "leads",
                        "tasks",
                        "ips",
                        "accounts",
                        "settings",
                        "update",
                        "logs",
                        "account",
                    ):
                        go(key)
                        page.wait_for_function(
                            "document.documentElement.scrollWidth<=innerWidth+1"
                        )
                    go("leads")
                    # Capture each view without transient messages from earlier checks.
                    page.locator(".toast").evaluate_all(
                        "nodes=>nodes.forEach(n=>n.remove())"
                    )
                    page.locator('[data-status="待复核"]').click()
                    expect(page.locator("#lead-body [data-open]").first).to_be_visible()
                    shot(f"leads-{width}")
                    page.locator("#lead-body [data-open]").first.click()
                    assert page.locator("#drawer-overlay").evaluate(
                        "e=>e.scrollWidth<=e.clientWidth+1"
                    )
                    if width in (768, 390, 320):
                        shot(f"review-{width}")
                    page.keyboard.press("Escape")
                    go("tasks")
                    page.locator("#create-task").click()
                    assert page.locator("#modal-overlay").evaluate(
                        "e=>e.scrollWidth<=e.clientWidth+1"
                    )
                    page.keyboard.press("Escape")
                    check(f"{width}px全部8个工作视图、详情及任务表单无横向溢出")

                page.set_viewport_size({"width": 1440, "height": 1000})
                go("leads")
                page.locator("#material-toggle").click()
                expect(page.locator("body")).to_have_attribute("data-material", "solid")
                assert (
                    page.locator(".sidebar").evaluate(
                        "e=>getComputedStyle(e).backdropFilter"
                    )
                    == "none"
                )
                page.reload()
                expect(page.locator("body")).to_have_attribute("data-material", "solid")
                page.locator("#material-toggle").click()
                page.emulate_media(reduced_motion="reduce")
                page.locator("#lead-body [data-open]").first.click()
                assert (
                    page.locator("#drawer-overlay").evaluate(
                        "e=>getComputedStyle(e).animationName"
                    )
                    == "none"
                )
                page.keyboard.press("Escape")
                check("降低透明度、偏好持久化与减少动效")
                assert errors == [], errors
                check("Edge 无页面脚本异常")
            report = {
                "started_at": started,
                "completed_at": datetime.now().isoformat(timespec="seconds"),
                "browser": "Microsoft Edge",
                "passed": len(checks),
                "checks": checks,
                "page_errors": errors,
                "api_responses": api_responses,
                "isolation": "临时数据库；采集模拟执行器；邮件关闭；未操作正式数据",
            }
            REPORT.write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(
                json.dumps(
                    {"passed": len(checks), "errors": errors, "report": str(REPORT)},
                    ensure_ascii=False,
                ),
                flush=True,
            )
        except Exception:
            shot("failure")
            print("失败页面：", page.url, "页面异常：", errors, flush=True)
            raise
        finally:
            browser.close()


if __name__ == "__main__":
    main()
