"""复用现有浏览器，只读采集页面与接口证据，不创建登录或修改业务数据。"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--url")
    parser.add_argument("--task")
    parser.add_argument("--task-view", action="store_true")
    args = parser.parse_args()
    for folder in (
        "doc",
        "prototype",
        "project/frontend",
        "project/backend",
        "database",
        "utils",
    ):
        assert (ROOT / folder).is_dir()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix = ROOT / "doc" / f"continuous_browser_{args.port}_{stamp}"
    report = {
        "observed_at": datetime.now().isoformat(),
        "port": args.port,
        "errors": [],
    }
    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(
                f"http://127.0.0.1:{args.port}", timeout=20000
            )
            context = browser.contexts[0]
            page = context.pages[0] if context.pages else context.new_page()
            page.on("pageerror", lambda error: report["errors"].append(str(error)))
            if args.url:
                page.goto(args.url, wait_until="domcontentloaded", timeout=25000)
            page.locator("body").first.wait_for(timeout=10000)
            if args.task_view:
                if args.port != 9225 or not args.task:
                    raise ValueError("任务详情验收需要 --port 9225 和 --task")
                page.goto("http://127.0.0.1:8765/#tasks", wait_until="domcontentloaded")
                page.locator(f'button[data-task="{args.task}"]').click(timeout=15000)
                page.get_by_role("heading", name="任务详情", exact=True).wait_for()
                report["task_detail_opened"] = True
            report.update(
                url=page.url,
                title=page.title(),
                text=page.locator("body").first.inner_text(timeout=5000)[:10000],
            )
            page.screenshot(path=str(prefix.with_suffix(".png")), timeout=10000)
            if args.port == 9225:
                report["api"] = {}
                paths = ["/api/health", "/api/tasks", "/api/leads?page_size=1"]
                if args.task:
                    paths.append(f"/api/tasks/{args.task}")
                for path in paths:
                    response = context.request.get(
                        "http://127.0.0.1:8765" + path, timeout=10000
                    )
                    report["api"][path] = {"status": response.status}
                    if response.status == 200:
                        data = response.json()
                        if path == "/api/tasks" and args.task:
                            data["items"] = [
                                item
                                for item in data.get("items", [])
                                if item.get("task_no") == args.task
                            ]
                        report["api"][path]["data"] = data
                report["authenticated"] = report["api"]["/api/health"]["status"] == 200
        except Exception as exc:
            report["errors"].append(f"{type(exc).__name__}: {exc}")
    prefix.with_suffix(".json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "report": str(prefix.with_suffix(".json")),
                "url": report.get("url"),
                "authenticated": report.get("authenticated"),
                "errors": report["errors"],
            },
            ensure_ascii=False,
        )
    )
    return 1 if report["errors"] or report.get("authenticated") is False else 0


if __name__ == "__main__":
    raise SystemExit(main())
