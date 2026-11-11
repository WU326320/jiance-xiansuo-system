#!/usr/bin/env python3
"""离线生成三平台样例 JSONL / 运行记录 / 日志（不访问任何电商网站）。

用法（项目根目录）：
    .venv/bin/python tools/make_samples.py
输出：
    samples/2026-11-05/{taobao,jd,1688}.jsonl   各 20 行
    samples/2026-11-05/*.run.json / run_logs.jsonl
    samples/logs/2026-11-05_*.log
"""

from __future__ import annotations

import html
import json
import shutil
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(ROOT))

from crawler.config import load_config
from crawler.core.browser import CapturedResponse, PageResult, StaticBrowser
from crawler.runner import run_platform

DATE = "2026-11-05"


def taobao_payload() -> dict:
    auctions = []
    for i in range(20):
        item_id = f"8800{i:08d}"
        tmall = i % 4 == 0
        record = {
            "nid": item_id,
            "raw_title": f"葫芦兄弟 印花卫衣 童装 第{i + 1}款",
            "view_price": f"{19.9 + i:.2f}",
            "nick": f"葫芦童装{'旗舰店' if tmall else '商行'}{i}",
            "view_sales": f"{i + 1}万+" if i % 3 == 0 else f"{100 + i}+",
            "comment_count": 1200 + i * 10 if i % 7 else None,
            "pic_url": f"//img.alicdn.com/imgextra/taobao_{i}.jpg" if i != 13 else None,
            "detail_url": f"//{'detail.tmall.com/item.htm' if tmall else 'item.taobao.com/item.htm'}?id={item_id}",
            "isTmall": tmall,
            "category": "童装",
        }
        if i not in (5, 14):
            record["sellerId"] = str(2000 + i)
        if i == 11:
            record["view_price"] = None
        auctions.append(record)
    return {"data": {"auctions": auctions}}


def jd_pages() -> tuple[str, list[dict]]:
    cards = []
    prices = []
    for i in range(20):
        sku = str(900000 + i)
        price = f"{29.9 + i:.2f}"
        prices.append({"id": f"J_{sku}", "p": price, "m": f"{49.9 + i:.2f}"})
        shop = "" if i in (6, 17) else f'<div class="p-shop"><span><a href="//mall.jd.com/index-{4000 + i}.html">京东店铺{i}</a></span></div>'
        comments = "" if i == 9 else f'<div class="p-commit"><strong><a href="#">{i + 1}万+条评价</a></strong></div>'
        price_html = '<div class="p-price"></div>' if i == 3 else f'<div class="p-price"><strong><i>{price}</i></strong></div>'
        cards.append(
            f'''<li class="gl-item" data-sku="{sku}">
  <div class="p-img"><a href="//item.jd.com/{sku}.html"><img data-lazy-img="//img14.360buyimg.com/n7/jd_{i}.jpg"></a></div>
  {price_html}
  <div class="p-name"><a href="//item.jd.com/{sku}.html"><em>葫芦兄弟 儿童卫衣 京东第{i + 1}款</em></a></div>
  {comments}
  {shop}
  <div class="p-icons"><i>{'自营' if i % 3 == 0 else 'POP'}</i></div>
</li>'''
        )
    page = f'<!doctype html><html><head><meta charset="utf-8"></head><body><div id="J_goodsList"><ul>{"".join(cards)}</ul></div></body></html>'
    return page, prices


def ali1688_payload() -> dict:
    offers = []
    for i in range(20):
        offer_id = f"7000{i:08d}"
        record = {
            "offerId": offer_id,
            "subject": f"葫芦兄弟 公仔 批发 第{i + 1}款",
            "price": f"{3.5 + i / 10:.2f}-{5.2 + i / 10:.2f}",
            "companyName": f"义乌市葫芦玩具厂{i}",
            "saleQuantity": f"{i + 1}万+" if i % 3 == 0 else f"{100 + i}+",
            "commentCount": 100 + i if i % 6 else None,
            "picUrl": f"//cbu01.alicdn.com/img/1688_{i}.jpg" if i != 15 else None,
            "offerUrl": f"//detail.1688.com/offer/{offer_id}.html",
            "tags": ["实力商家"] if i % 2 == 0 else [],
            "tpYear": 3 + i % 4,
            "category": "毛绒公仔",
        }
        if i not in (8, 18):
            record["memberId"] = str(6000 + i)
            record["shopUrl"] = f"//shop{6000 + i}.1688.com"
        offers.append(record)
    return {"data": {"offerList": offers}}


def _normal_page(url: str) -> PageResult:
    return PageResult(url=url, html="<html><body><h1>账号页</h1><p>登录态正常</p></body></html>")


def main() -> None:
    samples = ROOT / "samples"
    if samples.exists():
        shutil.rmtree(samples)
    (samples / "logs").mkdir(parents=True, exist_ok=True)

    data = yaml.safe_load((ROOT / "config" / "rules.yaml").read_text(encoding="utf-8"))
    data["crawl"]["output_dir"] = str(samples)
    data["crawl"]["log_dir"] = str(samples / "logs")
    data["crawl"]["state_dir"] = str(samples / "state")
    data["crawl"]["rate_limit"] = {
        "requests_per_minute": 600,
        "page_interval_seconds": [0, 0],
        "keyword_interval_seconds": [0, 0],
        "detail_interval_seconds": [0, 0],
    }
    data["crawl"]["retry"] = {"attempts": 1, "backoff_seconds": [0], "jitter_seconds": 0}
    data["crawl"]["image"]["enabled"] = False
    data["crawl"]["max_pages"] = 1
    data["crawl"]["keywords"] = [{"keyword": "葫芦兄弟", "ip_code": "huluxiongdi"}]
    data["security"]["profile_dir"] = str(samples / "profiles" / "{platform}")
    config_path = samples / "rules.sample.yaml"
    config_path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    config = load_config(config_path)

    from crawler.platforms.ali1688 import Ali1688Platform
    from crawler.platforms.jd import JdPlatform
    from crawler.platforms.taobao import TaobaoPlatform

    taobao = TaobaoPlatform(config, StaticBrowser([]))
    jd = JdPlatform(config, StaticBrowser([]))
    ali = Ali1688Platform(config, StaticBrowser([]))

    taobao_page = PageResult(
        url=taobao.build_search_url("葫芦兄弟", 1),
        html="",
        captured=[
            CapturedResponse(
                url="https://h5api.m.taobao.com/h5/mtop.taobao.search/1.0/",
                text=json.dumps(taobao_payload(), ensure_ascii=False),
                content_type="application/json",
            )
        ],
    )
    jd_html, jd_prices = jd_pages()
    jd_page = PageResult(
        url=jd.build_search_url("葫芦兄弟", 1),
        html=jd_html,
        captured=[
            CapturedResponse(
                url="https://p.3.cn/prices/mgets?skuIds=J_900000",
                text=json.dumps(jd_prices, ensure_ascii=False),
                content_type="application/json",
            )
        ],
    )
    ali_page = PageResult(
        url=ali.build_search_url("葫芦兄弟", 1),
        html="",
        captured=[
            CapturedResponse(
                url="https://s.1688.com/selloffer/offer_search.htm",
                text=json.dumps(ali1688_payload(), ensure_ascii=False),
                content_type="application/json",
            )
        ],
    )

    cases = [
        ("taobao", taobao.login_check_url, taobao_page),
        ("jd", jd.login_check_url, jd_page),
        ("1688", ali.login_check_url, ali_page),
    ]
    for platform, check_url, search_page in cases:
        browser = StaticBrowser([_normal_page(check_url), search_page])
        record = run_platform(
            platform,
            config,
            date=DATE,
            max_pages=1,
            download_images=False,
            browser_factory=lambda _config, _platform, _headless, browser=browser: browser,
        )
        print(
            f"{platform}: status={record.status} items_ok={record.items_ok} "
            f"output={record.output_path} log={record.log_path}"
        )
    print(f"samples -> {samples}")


if __name__ == "__main__":
    main()
