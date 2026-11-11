"""不限量回归：保留请求节奏、详情筛选与风控，不按旧配额截断。"""

from __future__ import annotations

import json
import re
from urllib.parse import quote_plus

import pytest

from crawler.config import KeywordSpec, load_config
from crawler.core.batchstate import read_batch_state
from crawler.core.browser import (
    CapturedResponse,
    PageResult,
    PaginationState,
    StaticBrowser,
)
from crawler.core.progress import SearchProgress
from crawler.core.jsonl import read_jsonl
from crawler.platforms import create_platform
from crawler.runner import build_keyword_matrix, run_detail, run_platform, run_platforms
from crawler.tests.conftest import ROOT, fixture_text


def _login_page() -> PageResult:
    return PageResult(
        url="https://i.taobao.com/my_taobao.htm",
        html="<html><body><h1>我的淘宝</h1><p>账号正常</p></body></html>",
    )


def _search_page(
    keyword: str, *, empty: bool = False, page: int = 1, last: bool = True
) -> PageResult:
    url = f"https://s.taobao.com/search?q={quote_plus(keyword)}&page={page}"
    if empty:
        return PageResult(
            url=url, html="<html><body><p>没有找到相关商品</p></body></html>"
        )
    return PageResult(
        url=url,
        html="",
        pagination=PaginationState(current_page=page, has_next=not last),
        captured=[
            CapturedResponse(
                url="https://h5api.m.taobao.com/h5/mtop.taobao.wsearch.pcsearch/1.0/",
                text=fixture_text("taobao_search.json")
                if page == 1
                else re.sub(
                    r"\b\d{4,}\b",
                    lambda m: str(page) + m.group(),
                    fixture_text("taobao_search.json"),
                ),
                content_type="application/json",
                status=200,
            )
        ],
    )


def _detail_page(product_id: str, *, tmall: bool = False) -> PageResult:
    url = (
        f"https://detail.tmall.com/item.htm?id={product_id}"
        if tmall
        else f"https://item.taobao.com/item.htm?id={product_id}"
    )
    return PageResult(url=url, html=fixture_text("taobao_detail.html"))


def _factory(browser):
    return lambda config, platform, headless: browser


def _set_keywords(config, *keywords: str) -> None:
    config.crawl.keywords = [
        KeywordSpec(keyword=word, ip_code="huluxiongdi") for word in keywords
    ]


def test_taobao_defaults_are_unlimited_and_keep_request_intervals():
    # 直接读真实配置文件，不能读单测里把节流归零的临时配置。
    config = load_config(ROOT / "config" / "rules.yaml")
    taobao = config.crawl.taobao
    assert taobao.max_keywords_per_run == 0
    assert taobao.max_pages == config.crawl.max_pages == 0
    assert config.crawl.max_items_per_keyword == config.crawl.daily_max_pages == 0
    assert (
        taobao.rate_limit.keyword_interval_seconds[0]
        > config.crawl.rate_limit.keyword_interval_seconds[0]
    )
    assert (
        taobao.rate_limit.page_interval_seconds[0]
        > config.crawl.rate_limit.page_interval_seconds[0]
    )
    assert (
        taobao.rate_limit.detail_interval_seconds[0]
        > config.crawl.rate_limit.detail_interval_seconds[0]
    )
    assert (
        taobao.rate_limit.requests_per_minute
        < config.crawl.rate_limit.requests_per_minute
    )
    assert taobao.detail.detail_min_text_score == 60
    assert taobao.detail.max_per_run == 0
    assert taobao.batch.size == 4
    assert taobao.batch.rest_seconds == 1800
    assert taobao.risk.empty_result_streak == 2
    # 其他平台沿用全局请求节奏，页数同样不限量。
    assert config.crawl.rate_limit_for("jd") is config.crawl.rate_limit
    assert config.crawl.max_pages_for("jd") == config.crawl.max_pages


def test_taobao_and_jd_continue_past_old_page_caps(make_config):
    config = make_config()
    config.crawl.max_pages = config.crawl.taobao.max_pages = 1
    taobao = create_platform(
        "taobao",
        config,
        StaticBrowser(
            [_search_page("葫芦兄弟", page=page, last=page == 3) for page in (1, 2, 3)]
        ),
    )
    items = taobao.search("葫芦兄弟", max_pages=1)
    assert len(items) == 9
    assert taobao.stats.pages_fetched == 3
    assert taobao.search_stop_reason == "last_page"

    jd_pages = [
        PageResult(
            url=f"https://search.jd.com/Search?keyword=x&enc=utf-8&page={n}",
            html=re.sub(
                r"\b\d{4,}\b",
                lambda m: str(n) + m.group(),
                fixture_text("jd_search.html"),
            ),
            pagination=PaginationState(has_next=n < 5),
        )
        for n in (1, 3, 5)
    ]
    jd = create_platform("jd", config, StaticBrowser(jd_pages))
    jd.search("x", max_pages=1)
    assert jd.stats.pages_fetched == 3
    assert jd.search_stop_reason == "last_page"


def test_taobao_run_ignores_keyword_cap(make_config):
    config = make_config()
    config.crawl.taobao.max_keywords_per_run = 1
    _set_keywords(config, "葫芦兄弟", "葫芦娃", "叶罗丽", "黑猫警长", "吾皇", "巴扎黑")
    browser = StaticBrowser(
        [
            _login_page(),
            *[
                _search_page(word)
                for word in (
                    "葫芦兄弟",
                    "葫芦娃",
                    "叶罗丽",
                    "黑猫警长",
                    "吾皇",
                    "巴扎黑",
                )
            ],
        ]
    )
    record = run_platform(
        "taobao",
        config,
        date="2026-11-06",
        max_pages=1,
        limit_keywords=1,
        browser_factory=_factory(browser),
    )
    assert record.status == "ok"
    assert record.keywords_done == 6
    assert len(browser.calls) == 7
    assert record.items_ok == 3  # 六个关键词返回同一批 fixture，runner 按 ID 去重。
    assert record.items_duplicate == 15


@pytest.mark.parametrize("batch", [1, 2, "next", "all"])
def test_taobao_batch_compatibility_keeps_all_keywords(make_config, tmp_path, batch):
    config = make_config()
    config.crawl.taobao.batch.size = 2
    _set_keywords(config, "葫芦兄弟", "葫芦娃", "叶罗丽", "黑猫警长", "吾皇", "巴扎黑")

    browser = StaticBrowser(
        [
            _login_page(),
            *[
                _search_page(str(entry["keyword"]))
                for entry in build_keyword_matrix(config)
            ],
        ]
    )
    record = run_platform(
        "taobao",
        config,
        date="2026-11-06",
        max_pages=1,
        batch=batch,
        browser_factory=_factory(browser),
    )
    assert record.status == "ok" and record.keywords_done == 6
    assert len(browser.calls) == 7
    assert (record.batch_index, record.batch_total, record.batch_next) == (
        None,
        None,
        None,
    )
    assert read_batch_state(tmp_path / "state", "taobao", "2026-11-06") is None


def test_taobao_batch_risk_does_not_advance_progress(make_config, tmp_path):
    config = make_config()
    config.crawl.taobao.batch.size = 2
    _set_keywords(config, "葫芦兄弟", "葫芦娃", "叶罗丽")
    browser = StaticBrowser(
        [
            _login_page(),
            PageResult(
                url=create_platform(
                    "taobao", config, StaticBrowser([])
                ).build_search_url("葫芦兄弟", 1),
                html=fixture_text("risk_taobao.html"),
                dom_hints=["#nc_1_wrapper"],
            ),
        ]
    )
    record = run_platform(
        "taobao",
        config,
        date="2026-11-06",
        max_pages=1,
        batch=2,
        browser_factory=_factory(browser),
    )
    assert record.status == "risk_blocked"
    assert record.keywords == ["葫芦兄弟", "葫芦娃", "叶罗丽"]
    assert record.keywords_done == 0 and len(browser.calls) == 2
    assert read_batch_state(tmp_path / "state", "taobao", "2026-11-06") is None
    progress = SearchProgress(config.state_root, "taobao", build_keyword_matrix(config))
    assert progress.state["current"]["page"] == 1
    assert not progress.position("葫芦兄弟", "huluxiongdi").get("done")


def test_taobao_unconfirmed_empty_result_keeps_keyword_unfinished(
    make_config, tmp_path
):
    config = make_config()
    _set_keywords(config, "葫芦兄弟", "葫芦娃")
    browser = StaticBrowser(
        [
            _login_page(),
            _search_page("葫芦兄弟", empty=True),
            _search_page("葫芦娃", empty=True),
        ]
    )
    record = run_platform(
        "taobao",
        config,
        date="2026-11-06",
        max_pages=1,
        browser_factory=_factory(browser),
    )
    assert record.status == "error"
    assert record.risk_blocked is False
    assert record.keywords_done == 0
    assert record.pages_fetched == 2  # 登录 + 首个空页，不跳到下一关键词。
    assert len(browser.calls) == 2
    assert any("解析为空且没有无结果证据" in error for error in record.errors)
    log_text = (tmp_path / "logs" / "2026-11-06_taobao.log").read_text(encoding="utf-8")
    assert "疑似频控" in log_text and "不重试" in log_text
    from crawler.core.progress import SearchProgress
    from crawler.runner import build_keyword_matrix

    progress = SearchProgress(config.state_root, "taobao", build_keyword_matrix(config))
    assert not progress.position("葫芦兄弟", "huluxiongdi")["done"]


def _write_list_rows(config, date: str, rows: list[dict]) -> None:
    out_dir = config.output_root / date
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "taobao.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )


def test_taobao_detail_keeps_score_filter_but_ignores_cap(make_config, tmp_path):
    config = make_config()
    config.crawl.taobao.detail.max_per_run = 2
    rows = [
        {
            "platform": "taobao",
            "product_id": "1111111111",
            "title": "葫芦兄弟 印花卫衣 童装",
            "shop_name": "葫芦童装店",
            "ip_code": "huluxiongdi",
        },
        {
            "platform": "taobao",
            "product_id": "2222222222",
            "title": "普通白牌 贴纸",
            "shop_name": "白牌文具商行",
            "ip_code": "huluxiongdi",
        },
        {
            "platform": "taobao",
            "product_id": "3333333333",
            "title": "葫芦娃 公仔 玩偶 挂件",
            "shop_name": "葫芦玩具店",
            "ip_code": "huluxiongdi",
        },
        {
            "platform": "tmall",
            "product_id": "4444444444",
            "title": "葫芦兄弟 公仔 玩偶",
            "shop_name": "葫芦玩具店",
            "ip_code": "huluxiongdi",
        },
    ]
    for row in rows:
        row.update(price=20, sales=100, comments=7)
    _write_list_rows(config, "2026-11-06", rows)
    browser = StaticBrowser(
        [
            _login_page(),
            _detail_page("1111111111"),
            _detail_page("3333333333"),
            _detail_page("4444444444", tmall=True),
        ]
    )
    record = run_detail(
        "taobao",
        config,
        ids=["1111111111", "2222222222", "3333333333", "tmall:4444444444"],
        date="2026-11-06",
        browser_factory=_factory(browser),
    )
    assert record.status == "ok"
    assert record.items_ok == 3  # 低分且字段完整的222跳过，其余全部补采。
    assert len(browser.calls) == 4
    detail_rows = list(
        read_jsonl(tmp_path / "out" / "2026-11-06" / "taobao_detail.jsonl")
    )
    assert {row["product_id"] for row in detail_rows} == {
        "1111111111",
        "3333333333",
        "4444444444",
    }
    assert any("低于阈值跳过" in warning for warning in record.warnings)
    assert not any("上限" in warning for warning in record.warnings)


def test_taobao_detail_keeps_all_unscored_explicit_ids(make_config, tmp_path):
    config = make_config()
    config.crawl.taobao.detail.max_per_run = 1
    browser = StaticBrowser(
        [_login_page(), _detail_page("5555555555"), _detail_page("6666666666")]
    )
    record = run_detail(
        "taobao",
        config,
        ids=["5555555555", "6666666666"],
        date="2026-11-07",
        browser_factory=_factory(browser),
    )
    # 当天没有列表 JSONL（人工/后端显式 ID）：全部逐条补采。
    assert record.status == "ok"
    assert record.items_ok == 2
    assert len(browser.calls) == 3


def test_taobao_detail_all_low_score_returns_skipped(make_config):
    config = make_config()
    _write_list_rows(
        config,
        "2026-11-06",
        [
            {
                "platform": "taobao",
                "product_id": "2222222222",
                "title": "普通白牌 贴纸",
                "shop_name": "白牌文具商行",
                "ip_code": "huluxiongdi",
                "price": 20,
                "sales": 100,
                "comments": 7,
            }
        ],
    )
    calls = []

    def factory(config, platform, headless):
        calls.append(platform)
        raise AssertionError("全部低分时不应启动浏览器")

    record = run_detail(
        "taobao",
        config,
        ids=["2222222222"],
        date="2026-11-06",
        browser_factory=factory,
    )
    assert record.status == "skipped"
    assert record.detail_status == "skipped"
    assert record.login_ok is False  # 未检查登录，不能伪报登录成功。
    assert record.items_ok == 0
    assert calls == []
    assert any("详情候选为空" in warning for warning in record.warnings)


@pytest.mark.parametrize("missing_field", ["price", "sales", "shop_name"])
def test_taobao_detail_low_score_keeps_missing_fields(make_config, missing_field):
    config = make_config()
    row = {
        "platform": "taobao",
        "product_id": "2222222222",
        "title": "普通白牌 贴纸",
        "shop_name": "白牌文具商行",
        "ip_code": "huluxiongdi",
        "price": 20,
        "sales": 100,
        "comments": 7,
    }
    row.pop(missing_field)
    _write_list_rows(config, "2026-11-06", [row])
    browser = StaticBrowser([_login_page(), _detail_page("2222222222")])

    record = run_detail(
        "taobao",
        config,
        ids=["2222222222"],
        date="2026-11-06",
        browser_factory=_factory(browser),
    )

    assert record.status == "ok"
    assert record.items_ok == 1
    assert len(browser.calls) == 2  # 登录 + 详情；低分不阻止缺失字段补采。


def test_run_platforms_batch_all_does_not_split_keywords(make_config, monkeypatch):
    config = make_config()
    config.crawl.taobao.batch.rest_seconds = 7
    sleeps: list[float] = []
    calls: list[object] = []

    from crawler.models import RunRecord
    from datetime import datetime, timezone

    now = datetime(2026, 11, 6, tzinfo=timezone.utc)

    def fake_run_platform(*args, **kwargs):
        calls.append(kwargs.get("batch"))
        return RunRecord(
            platform="taobao",
            started_at=now,
            ended_at=now,
            status="ok",
        )

    monkeypatch.setattr("crawler.runner.run_platform", fake_run_platform)
    records = run_platforms(["taobao"], config, batch="all", batch_sleep=sleeps.append)
    assert len(records) == 1 and records[0].status == "ok"
    assert calls == ["all"]
    assert sleeps == []


def test_cli_batch_requires_taobao_and_parses_number(
    make_config, tmp_path, monkeypatch
):
    from crawler.cli import build_parser, main

    parser = build_parser()
    assert (
        parser.parse_args(["run", "--platform", "taobao", "--batch", "2"]).batch == "2"
    )
    make_config()
    captured = {}

    def fake_run_platforms(*args, **kwargs):
        captured.update(kwargs)
        from crawler.models import RunRecord
        from datetime import datetime, timezone

        now = datetime(2026, 11, 6, tzinfo=timezone.utc)
        return [RunRecord(platform="taobao", started_at=now, ended_at=now, status="ok")]

    monkeypatch.setattr("crawler.cli.run_platforms", fake_run_platforms)
    code = main(
        [
            "run",
            "--platform",
            "taobao",
            "--config",
            str(tmp_path / "rules.yaml"),
            "--batch",
            "2",
            "--date",
            "2026-11-06",
        ]
    )
    assert code == 0
    assert captured["batch"] == 2

    code = main(
        [
            "run",
            "--platform",
            "jd",
            "--config",
            str(tmp_path / "rules.yaml"),
            "--batch",
            "1",
            "--date",
            "2026-11-06",
        ]
    )
    assert code == 2  # 不允许用 --batch 改京东节奏
