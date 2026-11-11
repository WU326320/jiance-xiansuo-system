"""验证独立工作台原型；file://运行，无服务、数据库或业务API。

运行：python utils/prototype_acceptance.py
完成后可双击 prototype/workbench-redesign.html 查看。
"""

from __future__ import annotations

import argparse
import json
from io import BytesIO
from datetime import datetime, timedelta
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
URL = (ROOT / "prototype/workbench-redesign.html").as_uri()
PAGES = {
    "leads": "线索复核",
    "tasks": "运行任务",
    "ips": "IP / 白名单",
    "accounts": "账号 / 邀请",
    "settings": "系统设置",
    "update": "版本更新",
    "logs": "审计 / 邮件",
    "account": "个人账户",
    "login": "登录",
    "register": "邀请注册",
    "setup": "首次初始化",
}


def main(variant="original"):
    global URL
    entry = {
        "original": "workbench-redesign.html",
        "ios": "workbench-ios.html",
        "glass": "workbench-glass.html",
    }[variant]
    URL = (ROOT / "prototype" / entry).as_uri()
    prefix = {"original": "redesign", "ios": "ios", "glass": "glass"}[variant]
    for directory in (
        "doc",
        "prototype",
        "project/frontend",
        "project/backend",
        "database",
        "utils",
    ):
        assert (ROOT / directory).is_dir(), directory
    results = []
    errors = []
    requests = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(
            viewport={"width": 1440, "height": 1000}, accept_downloads=True
        )
        page.set_default_timeout(8000)
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on(
            "request", lambda request: requests.append((request.method, request.url))
        )

        def act(name):
            scope = (
                page.locator("dialog[open]")
                if page.locator("dialog[open]").count()
                else page
            )
            scope.locator(f'[data-act="{name}"]:visible').first.click()

        def go(key):
            page.goto(URL + "#" + key)
            expect(page.locator("main")).to_be_visible()
            expect(page).to_have_title(PAGES[key] + " · 工作台设计预览")
            page.locator("#toasts").evaluate("e=>e.replaceChildren()")

        def check(name):
            results.append(name)
            print("PASS:", name)

        def reset():
            act("reset")

        def no_overflow():
            return page.evaluate(
                "document.documentElement.scrollWidth <= innerWidth + 1"
            )

        for width in (1440, 768, 390, 320):
            page.set_viewport_size({"width": width, "height": 1000})
            for key in PAGES:
                go(key)
                assert no_overflow(), (width, key, "page horizontal overflow")
            check(f"{width}px 全部11个视图加载与页面宽度")

        page.set_viewport_size({"width": 1440, "height": 1000})
        go("leads")
        page.screenshot(
            path=str(ROOT / f"prototype/{prefix}-leads-1440.png"), full_page=True
        )
        expect(page.locator("[data-select]")).to_have_count(5)
        act("next")
        expect(page.locator("[data-select]")).to_have_count(1)
        act("prev")
        page.get_by_label("搜索店铺、线索编号或 IP").fill("不存在的店铺")
        expect(page.get_by_text("没有符合条件的线索", exact=True)).to_be_visible()
        act("clear-filters")
        page.get_by_label("平台", exact=True).select_option("京东")
        expect(page.locator("[data-select]")).to_have_count(2)
        act("clear-filters")
        page.locator(".extra summary").click()
        page.locator('[name="filter-from"]').fill("2026-11-09")
        page.locator('[name="filter-to"]').fill("2026-11-08")
        expect(page.locator("#filter-error")).to_contain_text("开始日期不能晚于")
        page.locator('[name="filter-to"]').fill("2026-11-10")
        expect(page.locator("#filter-error")).to_be_empty()
        act("clear-filters")
        check("筛选、无结果、日期边界与分页")

        act("review")
        expect(page.locator("dialog")).to_be_visible()
        page.locator("dialog").evaluate(
            "async e=>{await Promise.all(e.getAnimations({subtree:true}).map(a=>a.finished.catch(()=>{})))}"
        )
        page.screenshot(
            path=str(ROOT / f"prototype/{prefix}-review-1440.png"), full_page=True
        )
        page.get_by_label("确认侵权", exact=True).check()
        page.get_by_role("button", name="保存复核结果", exact=True).click()
        expect(page.locator("#modal-title")).to_have_text("青禾玩具专营店")
        act("close")
        expect(
            page.locator('[data-act="lead-tab"][data-value="待复核"]')
        ).to_contain_text("5")
        page.locator('[data-act="lead-tab"][data-value="已录入 SaaS"]').click()
        act("review")
        expect(page.locator("#review-form")).to_have_count(0)
        expect(
            page.get_by_text(
                "已录入 SaaS，复核结果受保护，可继续查看证据。", exact=True
            )
        ).to_be_visible()
        page.keyboard.press("Escape")
        expect(page.locator("dialog")).not_to_be_visible()
        check("连续复核、结果计数、已录入保护、Esc关闭")

        reset()
        page.locator("#select-all").check()
        page.locator('[data-act="batch"][data-value="误报"]').click()
        page.get_by_role("button", name="确认复核", exact=True).click()
        expect(page.locator("#batch-form")).to_be_visible()
        page.locator('[name="confirm"]').fill("确认删除")
        page.get_by_role("button", name="确认复核", exact=True).click()
        expect(
            page.locator('[data-act="lead-tab"][data-value="待复核"]')
        ).to_contain_text("1")
        check("当前页批量复核与误报确认校验")

        go("tasks")
        act("create-task")
        page.locator('[name="ip"]').fill("葫芦兄弟")
        act("load-ip")
        expect(page.locator("#loaded-hint")).to_contain_text("已加载")
        expect(page.locator("#task-form")).to_be_visible()
        page.locator("#task-advanced summary").click()
        expect(page.locator('#whitelist-rows [name="wl-name"]').first).to_have_value(
            "葫芦兄弟官方旗舰店"
        )
        act("add-wl")
        expect(page.locator(".wl-row")).to_have_count(2)
        act("remove-wl")
        expect(page.locator(".wl-row")).to_have_count(1)
        page.locator('[name="ends"]').fill(
            (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")
        )
        page.get_by_role("button", name="检查并运行", exact=True).click()
        expect(page.locator("#task-error")).to_contain_text("已有任务在运行")
        page.keyboard.press("Escape")
        act("create-task")
        expect(page.locator('[name="ip"]')).to_have_value("葫芦兄弟")
        act("save-draft")
        page.locator('[data-act="task-detail"][data-id="DEMO-T-1108-03"]').click()
        act("finish-task")
        act("close")
        act("create-task")
        page.locator('[name="ip"]').fill("罗小黑")
        expect(page.locator('[name="companyCode"]')).to_have_value("")
        expect(page.locator("#loaded-hint")).to_contain_text("IP 已更改")
        act("load-ip")
        page.get_by_role("button", name="检查并运行", exact=True).click()
        expect(page.locator("#modal-title")).to_have_text("确认本次运行")
        act("create-task")
        expect(page.locator('[name="ip"]')).to_have_value("罗小黑")
        page.get_by_role("button", name="检查并运行", exact=True).click()
        act("run-task")
        expect(page.locator("tbody tr")).to_have_count(5)
        check("任务档案加载、白名单增删、单任务限制、Esc草稿、IP切换、运行确认")

        page.locator('[data-act="task-detail"][data-id="DEMO-T-1108-02"]').click()
        with page.expect_download() as download:
            act("download")
        assert download.value.suggested_filename.endswith(".csv")
        act("close")
        page.locator('[data-act="task-detail"][data-id="DEMO-T-1106-01"]').click()
        act("cancel-task")
        act("confirmed")
        expect(page.locator("tbody")).to_contain_text("已取消")
        check("演示文件下载、未开始任务取消")

        go("ips")
        act("new-ip")
        page.locator('[name="name"]').fill("原型验收IP")
        page.get_by_role("button", name="创建档案", exact=True).click()
        expect(page.locator("tbody")).to_contain_text("原型验收IP")
        page.locator('[data-act="edit-ip"][data-id="1"]').click()
        page.locator('[name="keywords"]').fill("验收关键词")
        page.get_by_role("button", name="保存档案", exact=True).click()
        expect(page.locator("tbody")).to_contain_text("验收关键词")
        page.locator('[data-act="edit-ip"][data-id="1"]').click()
        page.get_by_text("公司共享白名单 · 1 条", exact=True).click()
        act("company-wl")
        page.locator('[name="entries"]').fill("invalid,测试店铺,1")
        page.get_by_role("button", name="添加共享条目", exact=True).click()
        expect(page.locator("#company-error")).to_contain_text("支持的平台")
        page.locator('[name="entries"]').fill("jd,原型验收授权店,DEMO-3001")
        page.get_by_role("button", name="添加共享条目", exact=True).click()
        expect(page.locator("dialog")).to_contain_text("原型验收授权店")
        act("close")
        check("IP新增编辑、公司白名单格式校验与添加")

        go("accounts")
        act("invite")
        page.get_by_role("button", name="生成邀请码", exact=True).click()
        expect(page.locator("#invite-code")).to_have_value(
            __import__("re").compile("DEMO-INVITE-")
        )
        code = page.locator("#invite-code").input_value()
        assert code
        act("close")
        page.locator("#role").select_option("admin")
        act("invite")
        expect(page.locator('#invite-form [name="role"] option')).to_have_count(1)
        act("close")
        page.locator("#role").select_option("user")
        expect(page.locator("#nav .nav-item")).to_have_count(2)
        page.evaluate("location.hash='settings'")
        expect(page).to_have_title(PAGES["leads"] + " · 工作台设计预览")
        page.locator("#role").select_option("supervisor")
        check("一次性邀请码保留、管理员邀请限制、普通用户导航限制")

        go("settings")
        page.locator('[name="smtp_host"]').fill("prototype.example.com")
        page.locator('[data-act="settings-tab"][data-value="安全与访问"]').click()
        page.locator('[data-act="settings-tab"][data-value="邮件通知"]').click()
        expect(page.locator('[name="smtp_host"]')).to_have_value(
            "prototype.example.com"
        )
        page.get_by_role("button", name="保存邮件通知设置", exact=True).click()
        go("account")
        page.locator('[name="old"]').fill("OldPass123")
        page.locator('[name="password"]').fill("NewPass123")
        page.locator('[name="confirm"]').fill("OtherPass123")
        page.get_by_role("button", name="保存新密码", exact=True).click()
        expect(page.locator("#password-error")).to_contain_text("不一致")
        page.locator('[name="confirm"]').fill("NewPass123")
        page.get_by_role("button", name="保存新密码", exact=True).click()
        expect(page.locator('[name="password"]')).to_have_value("")
        check("设置分区编辑保留、密码不一致与成功反馈")

        go("update")
        page.locator('[name="package"]').set_input_files(
            {
                "name": "prototype.zip",
                "mimeType": "application/zip",
                "buffer": b"demo, not installed",
            }
        )
        page.get_by_role("button", name="检查版本包", exact=True).click()
        expect(page.locator("#modal-title")).to_have_text("确认安装版本包")
        act("install-demo")
        expect(page.locator("tbody")).to_contain_text("等待重启（演示）")
        act("rollback")
        act("confirmed")
        expect(page.locator(".version")).to_have_text("v1.0.0")
        go("logs")
        act("log-detail")
        expect(page.locator("dialog")).to_contain_text("来源 IP")
        act("close")
        page.locator('[data-act="logs-tab"][data-value="邮件日志"]').click()
        expect(page.locator("tbody")).to_contain_text("review@example.com")
        check("版本文件选择、演示安装回滚、审计详情与邮件切换")

        for auth in ("login", "register", "setup"):
            go(auth)
            page.locator('[name="username"]').fill("demo")
            page.locator('[name="password"]').fill("DemoPass123")
            if auth != "login":
                page.locator('[name="confirm"]').fill("DemoPass123")
            if auth == "register":
                page.locator('[name="invite"]').fill("DEMO-INVITE")
            page.locator('#auth-form [type="submit"]').click()
            expect(page).to_have_title(PAGES["leads"] + " · 工作台设计预览")
        check("登录、邀请注册与初始化演示流程")

        reset()
        for width in (1440, 768, 390):
            page.set_viewport_size({"width": width, "height": 1000})
            for key in ("leads", "tasks"):
                go(key)
                page.screenshot(
                    path=str(ROOT / f"prototype/{prefix}-{key}-{width}.png"),
                    full_page=True,
                )
            go("leads")
            act("review")
            assert page.locator("dialog").evaluate(
                "e=>e.scrollWidth <= e.clientWidth+1"
            ), width
            page.locator("dialog").evaluate(
                "async e=>{await Promise.all(e.getAnimations({subtree:true}).map(a=>a.finished.catch(()=>{})))}"
            )
            page.screenshot(
                path=str(ROOT / f"prototype/{prefix}-review-{width}.png"),
                full_page=True,
            )
            page.keyboard.press("Escape")
        page.set_viewport_size({"width": 390, "height": 844})
        go("leads")
        act("menu")
        expect(page.locator("#sidebar")).to_be_visible()
        page.locator('#nav a[href="#tasks"]').click()
        expect(page.locator("#sidebar")).not_to_be_visible()
        check("桌面平板手机截图、详情无横向溢出、移动导航可操作")

        if variant == "glass":
            page.set_viewport_size({"width": 1440, "height": 1000})
            go("leads")
            from PIL import Image

            aside_box = page.locator("aside").bounding_box()
            assert aside_box is not None
            sample = (
                int(aside_box["x"] + aside_box["width"] - 12),
                int(aside_box["y"] + 170),
            )
            transmitted = []
            for backdrop in ("#b7d7ef", "#e4ccdc"):
                page.locator("body").evaluate(
                    '(e,c)=>{e.style.backgroundImage="none";e.style.backgroundColor=c}',
                    backdrop,
                )
                with Image.open(BytesIO(page.screenshot())) as shot:
                    transmitted.append(shot.convert("RGB").getpixel(sample))
            assert sum(abs(a - b) for a, b in zip(*transmitted)) >= 20, transmitted
            page.locator("body").evaluate(
                'e=>{e.style.backgroundImage="";e.style.backgroundColor=""}'
            )
            assert (
                page.locator("aside")
                .evaluate("e=>getComputedStyle(e).backdropFilter")
                .startswith("blur(14px)")
            )
            page.locator("#reduce-transparency").check()
            for selector in ("aside", "header", ".panel"):
                assert (
                    page.locator(selector).first.evaluate(
                        "e=>getComputedStyle(e).backdropFilter"
                    )
                    == "none"
                ), selector
            page.locator("#reduce-transparency").uncheck()
            check("玻璃透色实拍响应、适度模糊与内容面板实色回退")
            expect(page.locator(".glass-selection")).to_have_count(1)
            page.locator('[data-act="lead-tab"][data-value="待复核"]').focus()
            page.keyboard.press("ArrowRight")
            expect(page.locator(".tabs .tab.active")).to_have_attribute(
                "data-value", "已确认"
            )
            expect(page.locator(".tabs .tab.active")).to_be_focused()
            page.keyboard.press("End")
            expect(page.locator(".tabs .tab.active")).to_have_attribute(
                "data-value", "全部线索"
            )
            page.keyboard.press("Home")
            expect(page.locator(".tabs .tab.active")).to_have_attribute(
                "data-value", "待复核"
            )
            page.locator("#reduce-transparency").check()
            expect(page.locator("body")).to_have_attribute("data-material", "solid")
            assert (
                page.locator("aside").evaluate("e=>getComputedStyle(e).backdropFilter")
                == "none"
            )
            page.screenshot(
                path=str(ROOT / "prototype/glass-solid-1440.png"), full_page=True
            )
            page.locator("#reduce-transparency").uncheck()
            page.emulate_media(reduced_motion="reduce")
            page.locator('[data-act="lead-tab"][data-value="已确认"]').click()
            assert (
                page.locator(".glass-selection").evaluate("e=>e.getAnimations().length")
                == 0
            )
            act("review")
            assert (
                page.locator("dialog").evaluate("e=>getComputedStyle(e).animationName")
                == "none"
            )
            act("close")
            page.emulate_media(reduced_motion="no-preference")
            check("玻璃选中块、键盘切换与焦点、降低透明度、减少动态效果")

            go("leads")
            table_box = page.locator("table").bounding_box()
            desktop_row_box = page.locator("tbody tr").first.bounding_box()
            assert table_box is not None and desktop_row_box is not None
            assert table_box["y"] < 520
            desktop_row_height = desktop_row_box["height"]
            assert 80 <= desktop_row_height <= 100
            icon_colors = page.locator(".nav-item:not(.active) .nav-icon").evaluate_all(
                "es=>es.map(e=>getComputedStyle(e).backgroundColor)"
            )
            assert len(set(icon_colors)) == 1, icon_colors
            page.set_viewport_size({"width": 390, "height": 844})
            go("leads")
            mobile_row_box = page.locator("tbody tr").first.bounding_box()
            assert mobile_row_box is not None
            mobile_row_height = mobile_row_box["height"]
            assert 210 <= mobile_row_height <= 270
            page.locator("[data-select]").first.check()
            expect(page.locator("#batch")).to_contain_text("当前页已选")
            page.goto((ROOT / "prototype/glass-comparison.html").as_uri())
            for label, image in (
                ("手机对比 · 390px", "glass-leads-390.png"),
                ("桌面对比 · 1440px", "glass-leads-1440.png"),
                ("本轮材质对比", "glass-leads-1440.png"),
            ):
                page.get_by_role("button", name=label, exact=True).click()
                expect(page.locator("#after")).to_have_attribute("src", image)
                expect(page.locator("#after")).to_be_visible()
                page.wait_for_function(
                    "Array.from(document.images).every(e=>e.complete&&e.naturalWidth>0)"
                )
                assert no_overflow()
            check("精修密度、统一图标、手机勾选、前后对比切换与图片加载")

        assert not errors, errors
        assert not [r for r in requests if r[0] != "GET" or "/api/" in r[1]], requests
        check("无页面脚本错误、无业务API与写入请求")
        report = {
            "status": "passed",
            "variant": variant,
            "checks": results,
            "page_errors": errors,
            "viewports": [1440, 768, 390, 320],
            "pages": list(PAGES),
            "business_api_requests": 0,
            "scope": "独立原型；不代表正式业务接口回归",
        }
        report_name = {
            "original": "工作台原型验收结果.json",
            "ios": "工作台iOS原型验收结果.json",
            "glass": "工作台玻璃原型验收结果.json",
        }[variant]
        (ROOT / "doc" / report_name).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        browser.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--variant", choices=["original", "ios", "glass"], default="original"
    )
    main(parser.parse_args().variant)
