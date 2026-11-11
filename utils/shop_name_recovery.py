"""盘点真实业务库缺失店名及采集证据；默认只读。"""

from pathlib import Path
import sqlite3
import json
import argparse
import sys
import subprocess
from datetime import datetime

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "crawler"), str(ROOT / "backend")]
EVIDENCE = ROOT / "doc/shop-name-evidence.jsonl"


def inventory(*, verbose=True):
    with sqlite3.connect(
        (ROOT / "backend/data/leads.db").as_uri() + "?mode=ro", uri=True
    ) as conn:
        conn.row_factory = sqlite3.Row
        missing = [
            dict(row)
            for row in conn.execute(
                "SELECT platform, product_id, shop_id, title, url FROM products WHERE shop_name IS NULL OR trim(shop_name)=''"
            )
        ]
        counts = [
            dict(row)
            for row in conn.execute(
                "SELECT p.platform, s.run_id, s.snapshot_date, count(*) AS rows, sum(p.shop_name IS NULL OR trim(p.shop_name)='') AS missing_names FROM product_snapshots s JOIN products p ON p.id=s.product_id GROUP BY p.platform,s.run_id,s.snapshot_date ORDER BY s.snapshot_date DESC"
            )
        ]
    wanted = {(r["platform"], r["product_id"]) for r in missing}
    evidence = {}
    shop_evidence = {}
    for path in (ROOT / "crawler/out").rglob("*.jsonl"):
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            key = (row.get("platform"), row.get("product_id"))
            if row.get("shop_id") and str(row.get("shop_name") or "").strip():
                shop_evidence.setdefault(
                    (row.get("platform"), row["shop_id"]), []
                ).append(
                    {"name": row["shop_name"], "source": str(path.relative_to(ROOT))}
                )
            if key in wanted and str(row.get("shop_name") or "").strip():
                evidence.setdefault(key, []).append(
                    {"name": row["shop_name"], "source": str(path.relative_to(ROOT))}
                )
    if EVIDENCE.exists():
        for line in EVIDENCE.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if row.get("name"):
                shop_evidence.setdefault((row["platform"], row["shop_id"]), []).append(
                    row
                )
    for row in missing:
        key = (row["platform"], row["product_id"])
        if key not in evidence and row["shop_id"]:
            evidence[key] = shop_evidence.get((row["platform"], row["shop_id"]), [])
    recoverable = {
        key: rows
        for key, rows in evidence.items()
        if rows and len({r["name"] for r in rows}) == 1
    }
    if verbose:
        print(
            json.dumps(
                {
                    "runs": counts,
                    "missing_count": len(missing),
                    "recoverable_count": len(recoverable),
                    "recoverable": [
                        {"platform": k[0], "product_id": k[1], "evidence": v}
                        for k, v in recoverable.items()
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    return missing, recoverable


def collect(limit):
    from crawler.config import load_config
    from crawler.core.browser import ScraplingBrowser
    from crawler.models import Item
    from crawler.platforms import create_platform

    missing, evidence = inventory()
    config = load_config(ROOT / "crawler/config/windows.yaml")
    done = set()
    browser = ScraplingBrowser(config, "1688", headless=False)
    browser.start()
    try:
        platform = create_platform("1688", config, browser)
        if not platform.check_login():
            raise RuntimeError("1688登录不可用")
        for row in missing:
            key = (row["platform"], row["product_id"])
            if row["platform"] != "1688" or key in evidence or row["shop_id"] in done:
                continue
            if len(done) >= limit:
                break
            done.add(row["shop_id"])
            item = Item(**row, search_keyword="店名补采", crawled_at=datetime.now())
            name = platform._fetch_shop_name(item)
            record = {
                "platform": "1688",
                "product_id": row["product_id"],
                "shop_id": row["shop_id"],
                "name": name,
                "source": row["url"],
                "collected_at": datetime.now().isoformat(),
            }
            with EVIDENCE.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            print(json.dumps(record, ensure_ascii=False), flush=True)
            if not name:
                break
    finally:
        browser.close()


def writeback(platform=None, product_id=None):
    if bool(platform) != bool(product_id):
        raise ValueError("单商品恢复必须同时指定平台与商品ID")
    missing, evidence = inventory(verbose=False)
    if product_id:
        evidence = {
            key: rows for key, rows in evidence.items() if key == (platform, product_id)
        }
        if not evidence:
            raise ValueError("目标商品没有可写回的一致店名证据")
    db = ROOT / "backend/data/leads.db"
    backup = (
        ROOT
        / "database"
        / ("leads-before-shop-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".db")
    )
    with sqlite3.connect(db) as source, sqlite3.connect(backup) as target:
        source.backup(target)
    changes = []
    with sqlite3.connect(db) as conn:
        for (platform, pid), rows in evidence.items():
            name = rows[0]["name"]
            updated = conn.execute(
                "UPDATE products SET shop_name=? WHERE platform=? AND product_id=? AND (shop_name IS NULL OR trim(shop_name)='')",
                (name, platform, pid),
            )
            if updated.rowcount:
                conn.execute(
                    "UPDATE shops SET shop_name=? WHERE platform=? AND shop_id=(SELECT shop_id FROM products WHERE platform=? AND product_id=?) AND (shop_name IS NULL OR trim(shop_name)='')",
                    (name, platform, platform, pid),
                )
                for sid, flags in conn.execute(
                    "SELECT s.id,s.missing_flags FROM product_snapshots s JOIN products p ON p.id=s.product_id WHERE p.platform=? AND p.product_id=?",
                    (platform, pid),
                ).fetchall():
                    conn.execute(
                        "UPDATE product_snapshots SET missing_flags=? WHERE id=?",
                        (
                            json.dumps(
                                [
                                    f
                                    for f in json.loads(flags or "[]")
                                    if f != "店铺名缺失"
                                ],
                                ensure_ascii=False,
                            ),
                            sid,
                        ),
                    )
                changes.append(
                    {
                        "platform": platform,
                        "product_id": pid,
                        "name": name,
                        "evidence": rows,
                    }
                )
    report = {"backup": str(backup), "updated": len(changes), "changes": changes}
    (
        ROOT
        / "doc"
        / ("店名写回-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".json")
    ).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps({"updated": len(changes), "backup": str(backup)}, ensure_ascii=False)
    )
    return report


def inspect_page(pid):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp("http://127.0.0.1:9224")
        page = browser.contexts[0].new_page()
        try:
            page.goto(
                f"https://detail.1688.com/offer/{pid}.html",
                wait_until="domcontentloaded",
            )
            page.wait_for_timeout(3500)
            result = page.locator("a[href*='1688.com']").evaluate_all(
                "els => els.map(e=>({text:e.innerText,href:e.href})).filter(e=>e.text.trim() && !e.href.includes('offer/')).slice(0,100)"
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            (ROOT / "doc" / f"shop-inspect-{pid}.html").write_text(
                page.content(), encoding="utf-8"
            )
            page.screenshot(path=str(ROOT / "doc" / f"shop-inspect-{pid}.png"))
        finally:
            page.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--collect", type=int)
    parser.add_argument("--writeback", action="store_true")
    parser.add_argument("--start-collect", action="store_true")
    parser.add_argument("--inspect")
    parser.add_argument("--platform")
    parser.add_argument("--product-id")
    args = parser.parse_args()
    if args.inspect:
        inspect_page(args.inspect)
    elif args.start_collect:
        with (ROOT / "doc/shop-name-collection.log").open("w", encoding="utf-8") as log:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-B",
                    "-u",
                    "-X",
                    "utf8",
                    str(Path(__file__).resolve()),
                    "--collect",
                    "30",
                ],
                cwd=ROOT,
                stdout=log,
                stderr=log,
            )
        print(json.dumps({"pid": process.pid, "log": "doc/shop-name-collection.log"}))
    elif args.collect:
        collect(args.collect)
    elif args.writeback:
        writeback(args.platform, args.product_id)
    else:
        inventory()
