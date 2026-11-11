from __future__ import annotations

import importlib.util
import json
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[2] / "tools" / "trial_metrics.py"
SPEC = importlib.util.spec_from_file_location("trial_metrics", MODULE_PATH)
assert SPEC and SPEC.loader
trial_metrics = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(trial_metrics)


def _write(path: Path, rows) -> None:
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )


def test_collect_metrics_dedupes_merges_details_and_refreshes_flags(tmp_path):
    _write(
        tmp_path / "taobao.jsonl",
        [
            {
                "platform": "taobao",
                "product_id": "1111111111",
                "title": "奥特曼 卫衣",
                "search_keyword": "奥特曼",
                "shop_id": None,
                "shop_type_raw_hint": "淘宝C店",
                "price": 59.9,
                "sales_raw": "1万+",
                "sales": 10000,
                "comments_raw": None,
                "comments": None,
                "main_image_url": "https://img.alicdn.com/a.jpg",
                "flags": ["店铺ID缺失", "评论缺失", "按下限估算"],
            },
            {
                "platform": "taobao",
                "product_id": "1111111111",
                "title": "奥特曼 卫衣（重复）",
                "search_keyword": "奥特曼",
                "shop_id": "2001",
                "price": None,
                "main_image_url": None,
                "flags": ["店铺ID缺失", "评论缺失"],
            },
            {
                "platform": "taobao",
                "product_id": "2222222222",
                "title": "黑猫警长 贴纸",
                "search_keyword": "奥特曼",
                "shop_id": None,
                "shop_type_raw_hint": None,
                "price": None,
                "sales_raw": None,
                "sales": None,
                "comments_raw": None,
                "comments": None,
                "main_image_url": None,
                "flags": [],
            },
        ],
    )
    _write(
        tmp_path / "taobao_detail.jsonl",
        [
            {
                "platform": "taobao",
                "product_id": "1111111111",
                "comments_raw": "2.3万+",
                "comments": 23000,
                "shop_id": "2001",
                "flags": [],
            }
        ],
    )
    _write(
        tmp_path / "jd.jsonl",
        [
            {
                "platform": "jd",
                "product_id": "1001",
                "title": "奥特曼 手办",
                "search_keyword": "奥特曼",
                "shop_id": "3001",
                "shop_type_raw_hint": "京东自营",
                "price": 99,
                "sales_raw": "1000+",
                "sales": 1000,
                "comments_raw": "500+条评价",
                "comments": 500,
                "main_image_url": "https://img.jd.com/a.jpg",
                "flags": ["店铺ID缺失", "销量缺失", "主图缺失", "评论缺失"],
            }
        ],
    )
    _write(
        tmp_path / "run_logs.jsonl",
        [{"platform": "taobao", "status": "ok", "items_ok": 2}],
    )

    metrics = trial_metrics.collect_metrics(tmp_path)

    assert metrics["total_unique"] == 3
    taobao = metrics["platforms"]["taobao"]
    assert taobao["count"] == 2
    assert taobao["detail_count"] == 1
    assert taobao["field_non_empty"]["shop_id"]["non_empty"] == 1
    assert taobao["field_non_empty"]["comments_raw"]["non_empty"] == 1
    assert taobao["field_non_empty"]["price"]["non_empty"] == 1
    assert taobao["fuzzy_parse"] == {
        "total": 2,
        "success": 2,
        "rate": 1.0,
        "rows_with_fuzzy": 1,
        "rows_parsed": 1,
    }
    assert taobao["title_keyword_hit"] == {"hits": 1, "total": 2, "rate": 0.5}
    # 详情补齐 shop_id/comments 后，A 行不再保留旧缺失 flag；B 行确实缺失。
    assert taobao["flags"] == {
        "按下限估算": 1,
        "价格缺失": 1,
        "主图缺失": 1,
        "店铺名缺失": 2,
        "销量缺失": 1,
        "评论缺失": 1,
    }
    assert len(taobao["samples"]) == 2

    jd = metrics["platforms"]["jd"]
    assert jd["count"] == 1
    assert all(
        flag not in jd["flags"]
        for flag in ("店铺ID缺失", "销量缺失", "主图缺失", "评论缺失")
    )
    assert jd["flags"] == {"按下限估算": 1, "店铺名缺失": 1}


def test_write_outputs_creates_json_and_markdown(tmp_path):
    metrics = trial_metrics.collect_metrics(tmp_path)
    json_path, md_path = trial_metrics.write_outputs(
        metrics,
        json_path=tmp_path / "trial_metrics.json",
        md_path=tmp_path / "trial_metrics.md",
    )
    assert json.loads(json_path.read_text(encoding="utf-8"))["total_unique"] == 0
    text = md_path.read_text(encoding="utf-8")
    assert "# 试采指标" in text
    assert "没有可统计的 JSONL 记录" in text


def test_main_writes_default_outputs(tmp_path, monkeypatch):
    day = tmp_path / "out" / "2026-11-05"
    day.mkdir(parents=True)
    _write(
        day / "1688.jsonl",
        [
            {
                "platform": "1688",
                "product_id": "6001",
                "title": "奥特曼 玩具",
                "search_keyword": "奥特曼",
                "shop_id": "b2b-1",
                "price": 3.5,
                "sales_raw": "1000+",
                "sales": 1000,
                "comments_raw": None,
                "comments": None,
                "main_image_url": "https://img.1688.com/a.jpg",
                "flags": [],
            }
        ],
    )
    monkeypatch.chdir(tmp_path)
    assert trial_metrics.main(["--date", "2026-11-05"]) == 0
    assert (day / "trial_metrics.json").exists()
    assert (day / "trial_metrics.md").exists()
    assert "1688" in (day / "trial_metrics.md").read_text(encoding="utf-8")


def test_platform_scope_dedupe_and_detail_success_metrics(tmp_path):
    """按 platform+product_id 去重：同 ID 跨平台不合并；详情成功单列统计。

    同时锁死“绝不用京东评论补淘宝/1688”：淘宝同关键词行没有评论数时，
    即使同目录京东行有评论数，淘宝行也必须保持 comments=None + 评论缺失。
    """
    _write(
        tmp_path / "taobao.jsonl",
        [
            {
                "platform": "taobao",
                "product_id": "777777",
                "title": "奥特曼 玩具",
                "search_keyword": "奥特曼",
                "shop_id": "2001",
                "price": 89,
                "sales_raw": "1万+",
                "sales": 10000,
                "comments_raw": None,
                "comments": None,
                "main_image_url": "https://img.alicdn.com/a.jpg",
                "flags": [],
            }
        ],
    )
    _write(
        tmp_path / "jd.jsonl",
        [
            {
                "platform": "jd",
                "product_id": "777777",
                "title": "奥特曼 玩具 京东",
                "search_keyword": "奥特曼",
                "shop_id": "3001",
                "price": 99,
                "sales_raw": "500+",
                "sales": 500,
                "comments_raw": "5000+条评价",
                "comments": 5000,
                "main_image_url": "https://img.jd.com/a.jpg",
                "flags": [],
            }
        ],
    )
    _write(
        tmp_path / "taobao_detail.jsonl",
        [
            {
                "platform": "taobao",
                "product_id": "777777",
                "comments_raw": "320+条评价",
                "comments": 320,
                "listed": True,
                "flags": [],
            },
            {
                "platform": "taobao",
                "product_id": "888888",
                "listed": False,
                "title": None,
                "shop_id": None,
                "price": None,
                "comments": None,
                "sales": None,
                "main_image_url": None,
                "flags": ["商品下架"],
            },
        ],
    )

    metrics = trial_metrics.collect_metrics(tmp_path)
    assert metrics["total_unique"] == 3  # 淘宝 2 条 + 京东 1 条（同 ID 不跨平台合并）
    taobao = metrics["platforms"]["taobao"]
    jd = metrics["platforms"]["jd"]
    assert taobao["count"] == 2 and jd["count"] == 1
    assert taobao["list_count"] == 2 and jd["list_count"] == 1
    assert taobao["detail_count"] == 2
    assert taobao["detail_success"] == 1  # 888888 下架且无字段，不算成功
    assert taobao["comments_actual"] == 1  # 777777 有；888888 下架无字段
    assert taobao["shop_id_actual"] == 1
    assert taobao["comments_rate"] == 0.5
    assert jd["comments_actual"] == 1
    assert jd["detail_count"] == 0
    assert jd["detail_success"] == 0

    # 淘宝详情补到 320；京东的 5000 绝不能串到淘宝行。888888 下架缺评论，
    # 只允许它自己带 1 个 评论缺失，777777 不得被标缺失。
    assert taobao["field_non_empty"]["comments_raw"]["non_empty"] == 1
    assert taobao["flags"].get("评论缺失") == 1


def test_markdown_lists_requested_detail_and_field_metrics(tmp_path):
    _write(
        tmp_path / "1688.jsonl",
        [
            {
                "platform": "1688",
                "product_id": "6001",
                "title": "奥特曼 批发",
                "search_keyword": "奥特曼",
                "shop_id": None,
                "price": 3.5,
                "sales_raw": "1000+",
                "sales": 1000,
                "comments_raw": None,
                "comments": None,
                "main_image_url": "https://img.1688.com/a.jpg",
                "flags": [],
            }
        ],
    )
    metrics = trial_metrics.collect_metrics(tmp_path)
    text = trial_metrics.render_markdown(metrics)
    for label in (
        "列表条数",
        "进入详情页条数",
        "详情页成功打开数",
        "评论数实际取到数",
        "shop_id 实际取到数",
        "店铺名实际取到数",
        "评论数非空率",
        "店铺名非空率",
        "模糊值解析成功率",
        "### 样本（3 条）",
    ):
        assert label in text
    assert "评论缺失" in text  # 1688 缺评论数照常标记，不补京东评论


def test_shop_id_consistency_across_runs_by_shop_name(tmp_path):
    run_b = tmp_path / "out_b" / "2026-11-06"
    run_c = tmp_path / "out_c" / "2026-11-06"
    run_b.mkdir(parents=True)
    run_c.mkdir(parents=True)
    _write(
        run_b / "taobao.jsonl",
        [
            {"platform": "taobao", "product_id": "1", "shop_name": "国工树脂实业", "shop_id": "3878627212"},
            {"platform": "taobao", "product_id": "2", "shop_name": "知叶玩具专营店", "shop_id": "916148540"},
            {"platform": "taobao", "product_id": "3", "shop_name": "帕龙母婴旗舰店", "shop_id": "0"},
            {"platform": "taobao", "product_id": "4", "shop_name": "仅 b 店", "shop_id": "111111"},
        ],
    )
    _write(
        run_c / "taobao.jsonl",
        [
            {"platform": "taobao", "product_id": "1", "shop_name": "国工树脂实业", "shop_id": "3878627212"},
            {"platform": "taobao", "product_id": "2", "shop_name": "知叶玩具专营店", "shop_id": "999999999"},
            {"platform": "taobao", "product_id": "3", "shop_name": "帕龙母婴旗舰店", "shop_id": "2455999666"},
            {"platform": "taobao", "product_id": "5", "shop_name": "仅 c 店", "shop_id": "222222"},
        ],
    )

    result = trial_metrics.collect_shop_consistency(run_b, run_c)
    assert result["common"] == 3
    assert result["consistent"] == 1
    assert result["inconsistent"] == 2
    assert result["only_b"] == 1
    assert result["only_c"] == 1
    assert result["both_missing"] == 0
    by_name = {row["shop_name"]: row for row in result["rows"]}
    assert by_name["国工树脂实业"]["status"] == "一致"
    assert by_name["知叶玩具专营店"]["status"] == "不一致"
    assert by_name["帕龙母婴旗舰店"]["shop_id_b"] is None  # "0" 归一为缺失
    assert by_name["仅 b 店"]["status"] == "仅 b"
    assert by_name["仅 c 店"]["status"] == "仅 c"

    text = trial_metrics.render_shop_consistency(result)
    assert "同店跨运行 shop_id 一致性" in text
    assert "| 店铺名 | b shop_id | c shop_id | 结果 |" in text
    assert "国工树脂实业" in text and "不一致" in text


def test_main_compare_dirs_writes_consistency_files(tmp_path, monkeypatch):
    run_b = tmp_path / "b" / "2026-11-06"
    run_c = tmp_path / "c" / "2026-11-06"
    run_b.mkdir(parents=True)
    run_c.mkdir(parents=True)
    _write(run_b / "taobao.jsonl", [{"platform": "taobao", "product_id": "1", "shop_name": "同店", "shop_id": "10"}])
    _write(run_c / "taobao.jsonl", [{"platform": "taobao", "product_id": "1", "shop_name": "同店", "shop_id": "10"}])
    md_path = tmp_path / "logs" / "consistency.md"
    json_path = tmp_path / "logs" / "consistency.json"
    monkeypatch.chdir(tmp_path)
    code = trial_metrics.main(
        [
            "--input-dir",
            str(run_c),
            "--compare-dir",
            str(run_b),
            "--compare-dir",
            str(run_c),
            "--consistency-md",
            str(md_path),
            "--consistency-json",
            str(json_path),
        ]
    )
    assert code == 0
    assert md_path.exists() and "同店" in md_path.read_text(encoding="utf-8")
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["consistent"] == 1 and data["inconsistent"] == 0


def test_metrics_treat_zero_shop_id_as_missing(tmp_path):
    _write(
        tmp_path / "taobao.jsonl",
        [
            {
                "platform": "taobao",
                "product_id": "1",
                "title": "奥特曼 玩具",
                "search_keyword": "奥特曼",
                "shop_id": "0",
                "price": 9.9,
                "main_image_url": "https://img.alicdn.com/a.jpg",
                "flags": [],
            }
        ],
    )
    metrics = trial_metrics.collect_metrics(tmp_path)
    taobao = metrics["platforms"]["taobao"]
    assert taobao["shop_id_actual"] == 0
    assert taobao["field_non_empty"]["shop_id"]["non_empty"] == 0
    assert taobao["shop_name_actual"] == 0
    assert taobao["flags"].get("店铺名缺失") == 1


def test_shop_name_rate_uses_decoded_1688_name(tmp_path):
    _write(
        tmp_path / "1688.jsonl",
        [
            {
                "platform": "1688",
                "product_id": "1",
                "title": "奥特曼 玩具",
                "search_keyword": "奥特曼",
                "shop_id": "S1",
                "shop_name": "%E7%88%86%E4%B9%90%E5%88%9B%E6%84%8F%E7%8E%A9%E5%85%B7%E5%BA%97",
                "price": 9.9,
                "main_image_url": "https://img.1688.com/a.jpg",
            },
            {
                "platform": "1688",
                "product_id": "2",
                "title": "奥特曼 玩具 2",
                "search_keyword": "奥特曼",
                "shop_id": "S2",
                "shop_name": "",
                "price": 9.9,
                "main_image_url": "https://img.1688.com/b.jpg",
            },
        ],
    )
    metrics = trial_metrics.collect_metrics(tmp_path)
    platform = metrics["platforms"]["1688"]
    assert platform["shop_name_actual"] == 1
    assert platform["shop_name_rate"] == 0.5


def test_run_logs_are_counted_separately_not_as_zero_product_rows(tmp_path):
    _write(
        tmp_path / "run_logs.jsonl",
        [
            {
                "platform": "taobao",
                "status": "ok",
                "login_ok": True,
                "keywords": ["奥特曼"],
                "pages_fetched": 2,
                "items_ok": 10,
                "risk_blocked": False,
                "started_at": "2026-11-06T10:00:00+08:00",
                "ended_at": "2026-11-06T10:01:00+08:00",
            }
        ],
    )
    metrics = trial_metrics.collect_metrics(tmp_path)
    assert metrics["run_log_count"] == 1
    assert metrics["run_logs"][0]["keywords"] == ["奥特曼"]
    assert metrics["files"][0]["kind"] == "run_logs"
    assert metrics["files"][0]["rows"] == 1
