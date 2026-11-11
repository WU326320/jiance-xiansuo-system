"""核验京东店名恢复；实际任务只读，登录和下载使用临时工作台副本。"""

from contextlib import closing
from datetime import datetime
from pathlib import Path
import argparse
import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import sys
import tempfile
from threading import Thread
from wsgiref.simple_server import make_server, WSGIRequestHandler
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "crawler")]


def read_tasks(path):
    with closing(sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        return {r["task_no"]: dict(r) for r in conn.execute("SELECT * FROM tasks")}


def verify_excel(original, corrected, expected):
    from openpyxl import load_workbook

    before = load_workbook(original)
    after = load_workbook(corrected)
    try:
        assert before.sheetnames == after.sheetnames == ["链接明细"]
        a, b = before["链接明细"], after["链接明细"]
        assert (a.max_row, a.max_column) == (b.max_row, b.max_column)
        assert b.max_column == 10
        filled = set()
        links = 0
        for old, new in zip(a.iter_rows(), b.iter_rows()):
            url = old[5].value
            if url in expected:
                assert not str(old[2].value or "").strip()
                assert new[2].value == expected[url]
                filled.add(url)
            if isinstance(url, str) and url.startswith("https://item.jd.com/"):
                links += 1
                assert str(new[2].value or "").strip()
            for index, (old_cell, new_cell) in enumerate(zip(old, new)):
                if not (index == 2 and url in expected):
                    assert old_cell.value == new_cell.value
                assert old_cell._style == new_cell._style
                assert (old_cell.comment.text if old_cell.comment else None) == (
                    new_cell.comment.text if new_cell.comment else None
                )
                assert (old_cell.hyperlink.target if old_cell.hyperlink else None) == (
                    new_cell.hyperlink.target if new_cell.hyperlink else None
                )
        assert filled == set(expected) and len(filled) == 8 and links == 27
        assert a.freeze_panes == b.freeze_panes
    finally:
        before.close()
        after.close()
    with ZipFile(original) as a, ZipFile(corrected) as b:
        media = lambda z: {
            n: hashlib.sha256(z.read(n)).hexdigest()
            for n in z.namelist()
            if n.startswith("xl/media/")
        }
        assert media(a) == media(b)
    return {
        "excel_rows": links,
        "names_filled": len(filled),
        "other_cells_and_images_preserved": True,
    }


def main(report_path):
    from playwright.sync_api import sync_playwright, expect
    from workbench.app import WorkbenchApp
    from workbench.config import WorkbenchConfig
    from utils.finalize_history_recovery import backup
    from utils.repair_jd_task_shops import (
        TASK,
        SOURCE,
        DETAILS,
        PROGRESS,
        read_rows,
        evidence_patches,
    )

    for folder in (
        "doc",
        "prototype",
        "project/frontend",
        "project/backend",
        "database",
        "utils",
    ):
        assert (ROOT / folder).is_dir(), folder
    recovery = json.loads(Path(report_path).read_text(encoding="utf-8"))
    assert recovery["applied"] and recovery["task_no"] == TASK
    original_tasks = read_tasks(recovery["backup"])
    current_tasks = read_tasks(ROOT / "workbench/data/workbench.db")
    task = current_tasks[TASK]
    old_task = original_tasks[TASK]
    assert task["excel_path"] == recovery["excel"]
    assert task["mode"] == "dry_run" and task["status"] == "partial"
    assert {
        k: v for k, v in task.items() if k not in {"result_json", "excel_path"}
    } == {k: v for k, v in old_task.items() if k not in {"result_json", "excel_path"}}
    assert {k: v for k, v in current_tasks.items() if k != TASK} == {
        k: v for k, v in original_tasks.items() if k != TASK
    }
    result = json.loads(task["result_json"])
    old_result = json.loads(old_task["result_json"])
    for field in ("candidates", "risks", "stop_reason", "status"):
        assert result.get(field) == old_result.get(field)
    jd = result["platforms"]["jd"]
    assert jd["missing_fields"]["shop_name"] == 0
    assert (jd["items"], jd["amount_complete"], jd["amount_missing"]) == (60, 53, 7)
    rows = read_rows(SOURCE)
    patches = evidence_patches(rows, read_rows(DETAILS))
    corrected = read_rows(result["shop_name_recovery"]["corrected_jsonl"])
    assert len(rows) == len(corrected) == 60 and len(patches) == 8
    for old, new in zip(rows, corrected):
        assert {
            k: v for k, v in old.items() if k not in {"shop_name", "shop_id", "flags"}
        } == {
            k: v for k, v in new.items() if k not in {"shop_name", "shop_id", "flags"}
        }
        assert new["shop_name"] == (
            patches[old["product_id"]]["shop_name"]
            if old["product_id"] in patches
            else old["shop_name"]
        )
    state = json.loads(PROGRESS.read_text(encoding="utf-8"))
    old_state = json.loads(
        Path(recovery["progress_backup"]).read_text(encoding="utf-8")
    )
    for key in ("scope", "keywords", "status", "active", "detail_attempted"):
        assert state.get(key) == old_state.get(key)
    assert len(state["detail_pending"]) == 7
    assert all(r["shop_name"] for r in state["rows"].values())
    for pid, patch in patches.items():
        assert state["detail_patches"][f"jd:{pid}"]["shop_name"] == patch["shop_name"]
    expected = {
        r["url"]: patches[r["product_id"]]["shop_name"]
        for r in rows
        if r["product_id"] in patches
    }
    excel = Path(task["excel_path"])
    original_excel = Path(old_task["excel_path"])
    assert (
        hashlib.sha256(original_excel.read_bytes()).hexdigest()
        == recovery["original_excel_sha256"]
    )
    report = {
        "task_no": TASK,
        "verified_at": datetime.now().isoformat(),
        **verify_excel(original_excel, excel, expected),
    }

    class QuietHandler(WSGIRequestHandler):
        def log_message(self, format, *args):
            pass

    with tempfile.TemporaryDirectory(prefix="jd-shop-acceptance-") as temporary:
        folder = Path(temporary)
        leads = folder / "leads.db"
        backup(ROOT / "backend/data/leads.db", leads)
        os.environ["IP_LEAD_DB_PATH"] = str(leads)
        os.environ.pop("IP_LEAD_DB_URL", None)
        config = WorkbenchConfig(
            data_dir=folder,
            db_path=folder / "workbench.db",
            leads_db_path=leads,
            smtp={"enabled": False},
        )
        config.ensure_dirs()
        app = WorkbenchApp(config)
        password = "JdCheck-" + secrets.token_urlsafe(18)
        user = app.auth.create_supervisor("jd-shop-check", password)
        local_excel = config.exports_dir / excel.name
        shutil.copyfile(excel, local_excel)
        copied = dict(task)
        copied.pop("id")
        copied.update(ip_id=None, created_by=user["id"], excel_path=str(local_excel))
        with app.db.transaction() as conn:
            conn.execute(
                f"INSERT INTO tasks ({','.join(copied)}) VALUES ({','.join('?' for _ in copied)})",
                tuple(copied.values()),
            )
        server = make_server("127.0.0.1", 0, app, handler_class=QuietHandler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel="msedge", headless=True)
                try:
                    page = browser.new_page(
                        accept_downloads=True, viewport={"width": 1440, "height": 1100}
                    )
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    live = page.goto("http://127.0.0.1:8765/login")
                    assert live is not None and live.status == 200
                    expect(
                        page.locator('#login-form input[name="password"]')
                    ).to_be_visible()
                    assert (
                        page.request.get("http://127.0.0.1:8765/api/tasks").status
                        == 401
                    )
                    base = f"http://127.0.0.1:{server.server_port}"
                    assert (
                        page.request.get(base + f"/api/tasks/{TASK}/export").status
                        == 401
                    )
                    page.goto(base + "/login")
                    page.locator('#login-form input[name="username"]').fill(
                        "jd-shop-check"
                    )
                    page.locator('#login-form input[name="password"]').fill(password)
                    page.locator('#login-form button[type="submit"]').click()
                    expect(page.locator('[data-nav="tasks"]')).to_be_visible()
                    response = page.request.get(base + f"/api/tasks/{TASK}")
                    assert response.status == 200
                    api_task = response.json()["task"]
                    assert (
                        api_task["result"] == result and api_task["status"] == "partial"
                    )
                    page.locator('[data-nav="tasks"]').click()
                    page.locator(f'#task-body [data-task="{TASK}"]').click()
                    expect(page.locator(".task-platform-results")).to_contain_text(
                        "店名待补齐 0 条"
                    )
                    expect(page.locator(".task-platform-results")).to_contain_text(
                        "待核验 7 条"
                    )
                    with page.expect_download() as downloaded:
                        page.get_by_role(
                            "link", name="下载本次 Excel", exact=False
                        ).click()
                    download = downloaded.value
                    assert download.failure() is None
                    output = folder / "download.xlsx"
                    download.save_as(output)
                    digest = hashlib.sha256(output.read_bytes()).hexdigest()
                    assert digest == hashlib.sha256(excel.read_bytes()).hexdigest()
                    page.locator(".task-platform-results").scroll_into_view_if_needed()
                    expect(
                        page.get_by_text("店名待补齐 0 条", exact=True)
                    ).to_be_in_viewport()
                    screenshot = ROOT / "doc/京东店名修复_任务下载.png"
                    page.screenshot(path=str(screenshot), full_page=True)
                    assert not errors, errors
                    report.update(
                        live_login_http=200,
                        task_api_http=200,
                        unauthenticated_export_http=401,
                        browser_data="当前真实演练任务的隔离副本",
                        task_status=api_task["status"],
                        source_items=60,
                        missing_shop_names=0,
                        pending_amount_items=7,
                        download_sha256=digest,
                        download_matches=True,
                        page_errors=errors,
                        excel=str(excel),
                        screenshot=str(screenshot),
                        recovery_report=str(report_path),
                    )
                finally:
                    browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
    target = ROOT / "doc/京东店名修复_页面验收.json"
    target.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path, help="--apply生成的恢复验收JSON")
    main(parser.parse_args().report)
