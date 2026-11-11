"""在独立标签页检查既有关键词的分页 DOM，不改任务、登录态或采集数据。"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "crawler"))

from playwright.sync_api import sync_playwright
from crawler.config import load_config
from crawler.core.browser import (
    PageResult,
    SEARCH_CARD_SIGNATURE,
    StaticBrowser,
    _collect_dom_hints,
    search_pagination,
)
from crawler.platforms import create_platform
from crawler.platforms.selectors import (
    ALI1688,
    JD,
    TAOBAO,
    SEARCH_PAGINATION,
    SALES_SORT,
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("platform", choices=["taobao", "jd", "1688"])
    args = parser.parse_args()
    config = load_config(ROOT / "crawler/config/windows.yaml")
    platform = create_platform(args.platform, config, StaticBrowser([]))
    for directory in (
        "doc",
        "prototype",
        "project/frontend",
        "project/backend",
        "database",
        "utils",
    ):
        assert (ROOT / directory).is_dir()
    target = (
        ROOT
        / "doc"
        / f"pagination_inspect_{args.platform}_{datetime.now():%Y%m%d_%H%M%S}.json"
    )
    report = {
        "platform": args.platform,
        "keyword": "葫芦兄弟",
        "stages": [],
        "error": None,
    }
    cards = ", ".join(
        {"taobao": TAOBAO, "jd": JD, "1688": ALI1688}[args.platform]["card"]
    )
    with sync_playwright() as driver:
        browser = driver.chromium.connect_over_cdp(config.cdp_endpoint(args.platform))
        page = browser.contexts[0].new_page()
        page.set_default_timeout(5000)

        def snapshot(stage):
            result = PageResult(
                url=page.url, html=page.content(), dom_hints=_collect_dom_hints(page)
            )
            state = platform._classify_result(result)
            signature = page.locator(cards).evaluate_all(SEARCH_CARD_SIGNATURE)
            entry = {
                "stage": stage,
                "classification": asdict(state),
                "pagination": asdict(search_pagination(page, SEARCH_PAGINATION)),
                "card_matches": page.locator(cards).count(),
                "signature_count": len(signature.splitlines()),
                "sort_controls": page.locator(
                    SALES_SORT[args.platform]["control"]
                ).count(),
                "card_examples": page.locator(cards).evaluate_all(
                    "nodes => nodes.slice(0, 2).map(n => n.outerHTML.slice(0, 1800))"
                ),
                "offer_links": page.locator(
                    "a[href*='detail.1688.com/offer/']"
                ).evaluate_all(
                    "nodes => nodes.slice(0, 2).map(n => ({html: n.outerHTML.slice(0, 2200), parent: n.parentElement.tagName + '.' + n.parentElement.className}))"
                ),
                "parsed_items": len(platform.parse_search(result, "葫芦兄弟", 1, None))
                if state.state == "ok"
                else None,
            }
            report["stages"].append(entry)
            page.screenshot(
                path=str(target.with_name(target.stem + "_" + stage + ".png")),
                timeout=10000,
            )
            target.write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            return state

        try:
            page.goto(
                platform.build_search_url("葫芦兄弟", 1),
                wait_until="domcontentloaded",
                timeout=30000,
            )
            page.wait_for_timeout(5000)
            platform.page_action(page)
            state = snapshot("before_sort")
            if not state.is_risk and not state.is_login:
                platform._search_page_action(page)
                snapshot("after_sort")
            result = page.evaluate("""() => {
                const visible = n => !!n.getClientRects().length;
                const nodes = [...document.querySelectorAll('a,button,li,span,div')].filter(n => {
                    const text = n.textContent.trim();
                    return visible(n) && text.length < 60 && (/下一页|下页|加载更多|没有更多|共\\s*\\d+\\s*页/.test(text) ||
                        /pagination|paging|pager|page-next|next-page|nextArrow/i.test(n.className || ''));
                });
                return {url: location.origin + location.pathname,
                    controls: nodes.slice(-35).map(n => ({tag:n.tagName, text:n.textContent.trim(),
                        cls:n.className, disabled:n.getAttribute('aria-disabled'),
                        parent:n.parentElement.outerHTML.slice(0, 3000)})),
                    cards:document.querySelectorAll('[data-nid],[data-item-id],[data-sku],[data-offer-id],a.search-offer-item').length};
            }""")
            report["dom"] = result
        except Exception as exc:
            report["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            target.write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            page.close()
    print(
        json.dumps(
            {
                "report": str(target),
                "error": report["error"],
                "stages": [
                    {k: v for k, v in entry.items() if k not in ("card_examples",)}
                    for entry in report["stages"]
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if report["stages"] and not report["error"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
