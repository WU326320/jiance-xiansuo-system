"""使用当前登录浏览器的独立标签页逐页验收到末页，不创建正式任务。"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict
from collections import Counter
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "crawler"))
from playwright.sync_api import sync_playwright
from crawler.config import load_config
from crawler.core.browser import (
    ScraplingBrowser,
    PageResult,
    SEARCH_CARD_SIGNATURE,
    _collect_dom_hints,
    search_pagination,
)
from crawler.core.progress import SearchProgress
from crawler.platforms import create_platform
from crawler.platforms.selectors import TAOBAO, JD, ALI1688, SEARCH_PAGINATION


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("platform", choices=["taobao", "jd", "1688"], nargs="?")
    parser.add_argument("--background", action="store_true")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--check", type=Path)
    args = parser.parse_args()
    if args.check:
        result = json.loads(args.check.read_text(encoding="utf-8"))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return result.get("exit_code", 2)
    if not args.platform:
        parser.error("需要指定平台")
    for directory in (
        "doc",
        "prototype",
        "project/frontend",
        "project/backend",
        "database",
        "utils",
    ):
        assert (ROOT / directory).is_dir()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = (
        args.report or ROOT / "doc" / f"pagination_live_{args.platform}_{stamp}.json"
    )
    if args.background:
        with target.with_suffix(".log").open("w", encoding="utf-8") as log:
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-B",
                    "-X",
                    "utf8",
                    str(Path(__file__).resolve()),
                    args.platform,
                    "--report",
                    str(target),
                ],
                stdout=log,
                stderr=subprocess.STDOUT,
                cwd=ROOT,
            )
        print(
            json.dumps(
                {"pid": child.pid, "report": str(target), "status": "started"},
                ensure_ascii=False,
            )
        )
        return 0
    report = {
        "platform": args.platform,
        "keyword": "葫芦兄弟",
        "pages": [],
        "error": None,
    }
    with (
        TemporaryDirectory(prefix="pagination-acceptance-") as temp,
        sync_playwright() as driver,
    ):
        config = load_config(ROOT / "crawler/config/windows.yaml")
        config.crawl.state_dir = temp + "/state"
        config.crawl.log_dir = temp + "/logs"
        remote = driver.chromium.connect_over_cdp(config.cdp_endpoint(args.platform))
        browser = ScraplingBrowser(config, args.platform)
        browser._session = SimpleNamespace(context=remote.contexts[0])
        platform = create_platform(args.platform, config, browser)
        platform.defer_after_search = True
        platform.search_progress = SearchProgress(
            config.state_root, args.platform, [{"keyword": "葫芦兄弟", "ip_code": None}]
        )
        platform.continuous = False
        seen_pages = set()

        def checkpoint(items):
            for number in sorted({item.page for item in items} - seen_pages):
                rows = [item for item in items if item.page == number]
                entry = {
                    "page": number,
                    "items": len(rows),
                    "product_ids": [item.product_id for item in rows],
                    "pagination": asdict(
                        search_pagination(browser._search_page, SEARCH_PAGINATION)
                    ),
                }
                report["pages"].append(entry)
                seen_pages.add(number)
                target.write_text(
                    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                browser._search_page.screenshot(
                    path=str(target.with_name(target.stem + f"_page{number}.png")),
                    timeout=10000,
                )

        platform.checkpoint_items = checkpoint
        try:
            platform.search("葫芦兄弟")
        except Exception as exc:
            report["error"] = f"{type(exc).__name__}: {exc}"
            page = browser._search_page
            if page is not None and not page.is_closed():
                state = platform._classify_result(
                    PageResult(
                        url=page.url,
                        html=page.content(),
                        dom_hints=_collect_dom_hints(page),
                    )
                )
                cards = ", ".join(
                    {"taobao": TAOBAO, "jd": JD, "1688": ALI1688}[args.platform]["card"]
                )
                report["failure_state"] = {
                    "classification": asdict(state),
                    "pagination": asdict(search_pagination(page, SEARCH_PAGINATION)),
                    "card_matches": page.locator(cards).count(),
                    "signature_count": len(
                        page.locator(cards)
                        .evaluate_all(SEARCH_CARD_SIGNATURE)
                        .splitlines()
                    ),
                }
                page.screenshot(
                    path=str(target.with_name(target.stem + "_failed.png")),
                    timeout=10000,
                )
        finally:
            report["stop_reason"] = platform.search_stop_reason
            report["checkpoint"] = platform.search_progress.position("葫芦兄弟", None)
            report["items_total"] = len(platform.search_progress.rows)
            report["page_counts"] = dict(
                Counter(row["page"] for row in platform.search_progress.rows)
            )
            report["remaining"] = (
                "当前关键词已确认真实末页或明确无结果"
                if platform.search_stop_reason in ("last_page", "no_results")
                else f"第{report['checkpoint'].get('next_page', 1)}页及以后未完成"
            )
            if browser._search_page is not None:
                browser._search_page.close()
    report["exit_code"] = (
        0
        if report["stop_reason"] in ("last_page", "no_results")
        and report["error"] is None
        else 1
    )
    target.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({"report": str(target), **report}, ensure_ascii=False, indent=2))
    return report["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
