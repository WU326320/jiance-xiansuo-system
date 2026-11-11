"""用隔离 Edge 页面验证首次登录等待；不访问真实平台或用户 profile。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "crawler"))

import yaml
from playwright.sync_api import sync_playwright
from crawler.config import load_config
from crawler.core.browser import PageResult
from crawler.login import check_task_login
from crawler.platforms import create_platform


def main():
    os.environ.pop("CRAWLER_ENDS_AT", None)
    output = ROOT / "doc" / "首次扫码等待验证.png"
    with tempfile.TemporaryDirectory(prefix="login-wait-") as directory:
        temporary = Path(directory)
        data = yaml.safe_load(
            (ROOT / "crawler/config/windows.yaml").read_text(encoding="utf-8")
        )
        for key in ("output_dir", "log_dir", "state_dir"):
            data["crawl"][key] = str(temporary / key)
        data["crawl"]["page_wait_ms_range"] = [0, 0]
        data["crawl"]["rate_limit"]["requests_per_minute"] = 600
        config_path = temporary / "rules.yaml"
        config_path.write_text(
            yaml.safe_dump(data, allow_unicode=True), encoding="utf-8"
        )
        config = load_config(config_path)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=False)
            try:
                context = browser.new_context(viewport={"width": 1100, "height": 760})
                page = context.new_page()
                calls = []

                def respond(route):
                    if "home.jd.com" in route.request.url:
                        route.fulfill(
                            content_type="text/html",
                            body="<html><body>账号正常</body></html>",
                        )
                    else:
                        route.fulfill(
                            content_type="text/html",
                            body="""<html lang='zh-CN'><meta charset='utf-8'><body style='font:24px sans-serif;padding:80px;background:#f3f7ff'><h1>等待扫码授权</h1><p>浏览器保持打开，授权完成后继续采集。</p><p>此页面用于隔离验证。</p><script>setTimeout(() => { document.cookie = 'pt_key=test-only; Domain=.jd.com; Path=/'; location.href = 'https://home.jd.com/'; }, 1500);</script></body></html>""",
                        )

                page.route("**/*", respond)

                class BrowserAdapter:
                    headless = False
                    mode = "playwright"

                    def page_html(self):
                        assert browser.is_connected() and not page.is_closed()
                        try:
                            html = page.content()
                        except Exception as exc:
                            if "navigat" not in str(exc).lower():
                                raise
                            return ""
                        if len(calls) == 1 and not output_saved[0]:
                            page.screenshot(path=str(output))
                            output_saved[0] = True
                        return html

                    def current_url(self):
                        return page.url

                    def cookie_values(self):
                        return {
                            cookie["name"]: cookie["value"]
                            for cookie in context.cookies()
                        }

                    def has_cookie(self, names):
                        return bool(set(names).intersection(self.cookie_values()))

                output_saved = [False]
                adapter = BrowserAdapter()
                crawler = create_platform("jd", config, adapter)
                crawler.check_login = lambda: False
                crawler.last_login_risk = False
                crawler.last_login_reason = "首次登录"
                page.goto("https://passport.jd.com/new/login.aspx")
                calls.append(page.url)
                import time

                started = time.monotonic()
                assert check_task_login(crawler), crawler.last_login_reason
                elapsed = time.monotonic() - started
                assert elapsed >= 1 and len(calls) == 1
                assert adapter.current_url().startswith("https://home.jd.com/")
                assert "pt_key" in adapter.cookie_values()
                assert browser.is_connected() and output.is_file()
                print(
                    json.dumps(
                        {
                            "passed": True,
                            "elapsed_seconds": round(elapsed, 1),
                            "navigation_count": len(calls),
                            "window_open_until_authorized": True,
                            "screenshot": str(output),
                        },
                        ensure_ascii=False,
                    )
                )
            finally:
                browser.close()


if __name__ == "__main__":
    main()
