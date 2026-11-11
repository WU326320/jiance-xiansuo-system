"""真实服务入口与正式数据副本的接口、浏览器下载验收。"""

from pathlib import Path
from contextlib import closing
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from threading import Thread
from wsgiref.simple_server import make_server, WSGIRequestHandler

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from playwright.sync_api import sync_playwright, expect
    from workbench.app import WorkbenchApp
    from workbench.config import WorkbenchConfig
    from utils.finalize_history_recovery import backup, RUN

    class QuietHandler(WSGIRequestHandler):
        def log_message(self, *args):
            pass

    with tempfile.TemporaryDirectory(prefix="history-ready-") as temporary:
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
        user = app.auth.create_supervisor("recovery-check", "RecoveryCheck1234")
        with closing(
            sqlite3.connect(
                (ROOT / "workbench/data/workbench.db").as_uri() + "?mode=ro", uri=True
            )
        ) as conn:
            conn.row_factory = sqlite3.Row
            task = dict(
                conn.execute("SELECT * FROM tasks WHERE task_no=?", (RUN,)).fetchone()
            )
        original_excel = Path(task["excel_path"])
        local_excel = config.exports_dir / original_excel.name
        shutil.copyfile(original_excel, local_excel)
        task.pop("id")
        task["ip_id"] = None
        task["created_by"] = user["id"]
        task["excel_path"] = str(local_excel)
        with closing(sqlite3.connect(config.db_path)) as conn, conn:
            conn.execute(
                f"INSERT INTO tasks ({','.join(task)}) VALUES ({','.join('?' for _ in task)})",
                tuple(task.values()),
            )
        server = make_server("127.0.0.1", 0, app, handler_class=QuietHandler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(channel="msedge", headless=True)
                try:
                    page = browser.new_page(
                        accept_downloads=True, viewport={"width": 1440, "height": 1000}
                    )
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    live = page.goto("http://127.0.0.1:8765/login")
                    assert live.status == 200
                    expect(
                        page.locator('#login-form input[name="password"]')
                    ).to_be_visible()
                    assert (
                        page.request.get("http://127.0.0.1:8765/api/tasks").status
                        == 401
                    )
                    page.screenshot(
                        path=str(ROOT / "doc/真实测试入口.png"), full_page=True
                    )
                    base = f"http://127.0.0.1:{server.server_port}"
                    page.goto(base + "/login")
                    page.locator('#login-form input[name="username"]').fill(
                        "recovery-check"
                    )
                    page.locator('#login-form input[name="password"]').fill(
                        "RecoveryCheck1234"
                    )
                    page.locator('#login-form button[type="submit"]').click()
                    expect(page.locator('[data-nav="leads"]')).to_be_visible()
                    response = page.request.get(
                        base + "/api/leads?status=all&page_size=200"
                    )
                    assert response.status == 200
                    listing = response.json()
                    assert listing["total"] == 84
                    assert all(item["shop_name"] for item in listing["items"])
                    detail = page.request.get(
                        base + "/api/leads/" + listing["items"][0]["lead_no"]
                    )
                    assert detail.status == 200 and detail.json()["links"]
                    response = page.request.get(base + f"/api/tasks/{RUN}")
                    assert (
                        response.status == 200
                        and response.json()["task"]["status"] == "partial"
                    )
                    page.locator('[data-nav="leads"]').click()
                    expect(page.locator("#lead-body")).not_to_be_empty()
                    page.screenshot(
                        path=str(ROOT / "doc/历史恢复_线索页面.png"), full_page=True
                    )
                    page.locator('[data-nav="tasks"]').click()
                    page.locator("#task-body [data-task]").first.click()
                    with page.expect_download() as downloaded:
                        page.get_by_role(
                            "link", name="下载本次 Excel", exact=False
                        ).click()
                    download = downloaded.value
                    assert download.failure() is None
                    output = folder / "download.xlsx"
                    download.save_as(output)
                    assert (
                        hashlib.sha256(output.read_bytes()).digest()
                        == hashlib.sha256(original_excel.read_bytes()).digest()
                    )
                    page.screenshot(
                        path=str(ROOT / "doc/历史恢复_任务下载.png"), full_page=True
                    )
                    assert not errors, errors
                    report = {
                        "live_login_http": 200,
                        "unauthenticated_api": 401,
                        "isolated_real_data_leads": listing["total"],
                        "task_status": "partial",
                        "download_sha256_match": True,
                        "page_errors": errors,
                        "excel": str(original_excel),
                    }
                    (ROOT / "doc/真实测试准备验收.json").write_text(
                        json.dumps(report, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    print(json.dumps(report, ensure_ascii=False, indent=2))
                finally:
                    browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    main()
