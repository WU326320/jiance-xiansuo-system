"""backend 调 crawler 子进程时优先使用 crawler/.venv 的 Python（11/6 R1 补字段 c）。"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from pipeline.crawler_python import crawler_python

DAY = date(2026, 11, 6)


def test_unexpected_timeout_after_deadline_preserves_rows_and_reports_failure(
    settings, monkeypatch
):
    from datetime import datetime, timedelta
    from pipeline.crawler_adapter import SubprocessCrawlerAdapter

    settings.raw["run"]["ends_at"] = (datetime.now() + timedelta(seconds=5)).isoformat()

    def fake_run(command, **kwargs):
        assert kwargs["timeout"] is None
        assert kwargs["env"]["CRAWLER_ENDS_AT"]
        root = Path(kwargs["env"]["CRAWLER_OUTPUT_DIR"])
        _write_crawl_output(
            root, "1688", [{"platform": "1688", "product_id": "first"}], {}
        )
        (root / DAY.isoformat() / "1688.partial.json").write_text(
            json.dumps(
                [
                    {"platform": "1688", "product_id": "second", "shop_name": "已补齐"},
                ]
            ),
            encoding="utf-8",
        )
        settings.raw["run"]["ends_at"] = "2000-01-01 00:00:00"
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr("pipeline.crawler_adapter.subprocess.run", fake_run)
    data = SubprocessCrawlerAdapter(settings).run("1688", DAY)
    assert data.status == "timeout" and data.login_ok
    assert {row["product_id"] for row in data.items} == {"first", "second"}
    assert "补采未正常收尾" in data.run_record["errors"][0]
    assert data.items[1]["shop_name"] == "已补齐"
    # 已到期时再次调用不能再启动采集进程。
    assert SubprocessCrawlerAdapter(settings).run("jd", DAY).status == "deadline"


def test_real_crawler_process_finishes_details_after_deadline(settings):
    import time
    from datetime import datetime, timedelta
    from pipeline.crawler_adapter import SubprocessCrawlerAdapter

    settings.crawler_dir.mkdir(parents=True, exist_ok=True)
    (settings.crawler_dir / "crawler.py").write_text(
        "import json, os, pathlib, sys, time\n"
        "from datetime import datetime\n"
        "day = sys.argv[sys.argv.index('--date') + 1]\n"
        "directory = pathlib.Path(os.environ['CRAWLER_OUTPUT_DIR']) / day\n"
        "directory.mkdir(parents=True, exist_ok=True)\n"
        "deadline = datetime.fromisoformat(os.environ['CRAWLER_ENDS_AT'])\n"
        "time.sleep(max(0, (deadline - datetime.now()).total_seconds()) + 0.2)\n"
        "row = {'platform':'1688','product_id':'saved','shop_name':'after deadline'}\n"
        "(directory / '1688.jsonl').write_text(json.dumps(row) + '\\n', encoding='utf-8')\n"
        "print(json.dumps({'platform':'1688','status':'deadline','login_ok':True,'items_ok':1}))\n",
        encoding="utf-8",
    )
    settings.raw["run"]["ends_at"] = (datetime.now() + timedelta(seconds=2)).isoformat()
    started = time.monotonic()
    data = SubprocessCrawlerAdapter(settings).run("1688", DAY)
    assert 2 <= time.monotonic() - started < 10
    assert data.status == "deadline"
    assert [row["product_id"] for row in data.items] == ["saved"]
    assert data.items[0]["shop_name"] == "after deadline"


@pytest.mark.parametrize("checkpoint_ip", ["first-ip", "second-ip"])
def test_deadline_recovery_preserves_completed_fields_and_ip_matches(
    settings, monkeypatch, checkpoint_ip
):
    from datetime import datetime, timedelta
    from pipeline.crawler_adapter import SubprocessCrawlerAdapter

    settings.raw["run"]["ends_at"] = (datetime.now() + timedelta(seconds=5)).isoformat()
    completed_row = {
        "platform": "1688",
        "product_id": "same",
        "ip_code": "first-ip",
        "shop_name": "已补齐店名",
        "image_path": "images/saved.jpg",
    }

    def fake_run(command, **kwargs):
        root = Path(kwargs["env"]["CRAWLER_OUTPUT_DIR"])
        _write_crawl_output(root, "1688", [completed_row], {})
        (root / DAY.isoformat() / "1688.partial.json").write_text(
            json.dumps(
                [
                    {**completed_row, "ip_code": checkpoint_ip, "image_path": None},
                ]
            ),
            encoding="utf-8",
        )
        settings.raw["run"]["ends_at"] = "2000-01-01 00:00:00"
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr("pipeline.crawler_adapter.subprocess.run", fake_run)
    data = SubprocessCrawlerAdapter(settings).run("1688", DAY)
    assert data.status == "timeout"
    assert data.items[0]["image_path"] == "images/saved.jpg"
    assert {row["ip_code"] for row in data.items} == {"first-ip", checkpoint_ip}
    assert len(data.items) == (1 if checkpoint_ip == "first-ip" else 2)


@pytest.mark.parametrize("platform", ["jd", "taobao"])
@pytest.mark.parametrize("outcome", ["success", "timeout", "partial"])
def test_detail_after_deadline_has_no_batch_timeout_and_preserves_current_patches(
    settings, monkeypatch, platform, outcome
):
    from pipeline.jd_detail import (
        CrawlerJdDetailFetcher,
        DetailFetchError,
        JdDetailTarget,
    )
    from pipeline.taobao_detail import CrawlerTaobaoDetailFetcher, TaobaoDetailTarget

    settings.crawler_dir.mkdir(parents=True, exist_ok=True)
    settings.raw["run"]["ends_at"] = "2000-01-01 00:00:00"
    monkeypatch.setenv("CRAWLER_ENDS_AT", "2000-01-01 00:00:00")
    directory = settings.crawler_output_dir / DAY.isoformat()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{platform}_detail.jsonl"
    path.write_text(
        json.dumps({"platform": platform, "product_id": "123", "comments": 999}) + "\n",
        encoding="utf-8",
    )

    def fake_run(command, **kwargs):
        assert kwargs["timeout"] is None
        assert "CRAWLER_ENDS_AT" not in kwargs["env"]
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps({"platform": platform, "product_id": "123", "comments": 7})
                + "\n"
            )
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return _completed(
            {
                "platform": platform,
                "status": "partial" if outcome == "partial" else "ok",
                "login_ok": True,
                "errors": ["重试耗尽"] if outcome == "partial" else [],
            }
        )

    monkeypatch.setattr("subprocess.run", fake_run)
    if platform == "jd":
        fetcher = CrawlerJdDetailFetcher(settings)
        target = JdDetailTarget("s", Decimal("1"), ("123",))
    else:
        fetcher = CrawlerTaobaoDetailFetcher(settings)
        target = [TaobaoDetailTarget("taobao", "123")]
    if outcome in ("timeout", "partial"):
        message = "独立超时" if outcome == "timeout" else "重试耗尽"
        with pytest.raises(DetailFetchError, match=message) as caught:
            fetcher(target, DAY)
        assert [row["comments"] for row in caught.value.patches] == [7]
        if outcome == "timeout":
            assert isinstance(caught.value.__cause__, subprocess.TimeoutExpired)
    else:
        assert [row["comments"] for row in fetcher(target, DAY)] == [7]


def test_deadline_pipeline_keeps_leads_and_exports_without_more_requests(
    settings, monkeypatch
):
    from datetime import datetime, timedelta
    from pipeline.runner import PipelineRunner
    from tests.conftest import make_item, platform_data

    settings.raw["run"]["ends_at"] = (datetime.now() + timedelta(minutes=1)).isoformat()
    settings.raw["export"].update(enabled=True, run_id="deadline-test")
    calls = []

    class Adapter:
        def run(self, platform, run_date):
            calls.append(platform)
            settings.raw["run"]["ends_at"] = "2000-01-01 00:00:00"
            return platform_data(
                "taobao", [make_item(price=600, comments=None)], status="deadline"
            )

    def forbidden(*args):
        pytest.fail("截止后不应补抓详情")

    summary = PipelineRunner(
        settings,
        adapter=Adapter(),
        taobao_detail_fetcher=forbidden,
        jd_detail_fetcher=forbidden,
    ).run(
        platforms=["taobao", "jd", "1688"],
        run_date=DAY,
        run_id="deadline-test",
        allow_alert=False,
    )
    assert calls == ["taobao"]
    assert summary.deadline_reached
    assert not summary.failed  # 评论缺失不影响已有销量金额。
    assert summary.platforms["taobao"].items == 1
    assert summary.platforms["taobao"].amount_missing == 0
    assert summary.platforms["jd"].status == "deadline"
    assert summary.leads == 1
    assert summary.excel and Path(summary.excel).is_file()


def _completed(stdout: dict) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        [], 0, stdout=json.dumps(stdout, ensure_ascii=False), stderr=""
    )


@pytest.mark.parametrize("kind", ["search", "jd_detail", "taobao_detail"])
def test_collection_has_no_batch_time_limit(settings, monkeypatch, kind):
    from pipeline.crawler_adapter import SubprocessCrawlerAdapter
    from pipeline.jd_detail import CrawlerJdDetailFetcher, JdDetailTarget
    from pipeline.taobao_detail import CrawlerTaobaoDetailFetcher, TaobaoDetailTarget

    settings.crawler_dir.mkdir(parents=True, exist_ok=True)
    settings.raw["run"].pop("ends_at", None)
    settings.raw["run"]["crawl_timeout_seconds"] = 1
    settings.raw["run"]["jd_detail_timeout_seconds"] = 1
    settings.raw["run"]["taobao_detail_timeout_seconds"] = 1

    def fake_run(command, **kwargs):
        assert kwargs["timeout"] is None
        assert "CRAWLER_ENDS_AT" not in kwargs["env"]
        platform = command[command.index("--platform") + 1]
        return _completed({"platform": platform, "status": "ok", "login_ok": True})

    monkeypatch.setattr("subprocess.run", fake_run)
    if kind == "search":
        SubprocessCrawlerAdapter(settings).run("1688", DAY)
    elif kind == "jd_detail":
        CrawlerJdDetailFetcher(settings)(
            JdDetailTarget("shop", Decimal("0"), ("123",)), DAY
        )
    else:
        CrawlerTaobaoDetailFetcher(settings)([TaobaoDetailTarget("taobao", "123")], DAY)


def _make_crawler_venv(settings):
    python = settings.crawler_dir / ".venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text("", encoding="utf-8")
    return python


def test_crawler_python_prefers_crawler_venv_then_current(settings):
    expected = _make_crawler_venv(settings)
    assert crawler_python(settings) == str(expected)

    expected.unlink()
    # POSIX 的 .venv/bin/python 也是候选；删掉后必须回退当前解释器。
    assert crawler_python(settings) == sys.executable


def test_subprocess_crawler_adapter_uses_crawler_venv(settings, monkeypatch):
    from pipeline.crawler_adapter import SubprocessCrawlerAdapter

    python = _make_crawler_venv(settings)
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        return _completed(
            {"platform": "taobao", "status": "ok", "login_ok": True, "items_ok": 0}
        )

    monkeypatch.setattr("pipeline.crawler_adapter.subprocess.run", fake_run)
    SubprocessCrawlerAdapter(settings).run("taobao", DAY)
    assert captured["command"][0] == str(python)
    assert captured["command"][1:4] == ["-m", "crawler", "run"]


def _write_crawl_output(root, platform, rows, record):
    directory = root / DAY.isoformat()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{platform}.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    (directory / f"{platform}.run.json").write_text(
        json.dumps(record, ensure_ascii=False), encoding="utf-8"
    )


@pytest.mark.parametrize("platform", ["taobao", "jd", "1688"])
def test_subprocess_crawl_does_not_import_previous_ip(settings, monkeypatch, platform):
    from pipeline.crawler_adapter import SubprocessCrawlerAdapter

    stale = {"platform": platform, "product_id": "old", "ip_code": "葫芦兄弟"}
    fresh = {"platform": platform, "product_id": "new", "ip_code": "星星人"}
    old_record = {
        "platform": platform,
        "login_ok": True,
        "status": "ok",
        "run_id": "old",
    }
    new_record = {**old_record, "run_id": "new", "items_ok": 1}
    _write_crawl_output(settings.crawler_output_dir, platform, [stale], old_record)

    def fake_run(command, **kwargs):
        root = Path(kwargs["env"]["CRAWLER_OUTPUT_DIR"])
        directory = root / DAY.isoformat()
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / f"{platform}.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(fresh, ensure_ascii=False) + "\n")
        return _completed(new_record)

    monkeypatch.setattr("pipeline.crawler_adapter.subprocess.run", fake_run)
    data = SubprocessCrawlerAdapter(settings).run(platform, DAY)
    assert [row["product_id"] for row in data.items] == ["new"]
    assert data.run_record["run_id"] == "new"
    # 历史原始文件保留，不能靠清空当天文件实现隔离。
    old_rows = (
        settings.crawler_output_dir / DAY.isoformat() / f"{platform}.jsonl"
    ).read_text(encoding="utf-8")
    assert json.loads(old_rows)["ip_code"] == "葫芦兄弟"


def test_subprocess_crawl_same_day_repeat_has_fresh_output(settings, monkeypatch):
    from pipeline.crawler_adapter import SubprocessCrawlerAdapter

    roots = []

    def fake_run(command, **kwargs):
        root = Path(kwargs["env"]["CRAWLER_OUTPUT_DIR"])
        roots.append(root)
        directory = root / DAY.isoformat()
        # 模拟采集器跨次运行按 product_id 去重：旧文件存在就不再写。
        if not (directory / "taobao.jsonl").exists():
            row = {"product_id": "same", "ip_code": "星星人", "price": len(roots)}
            _write_crawl_output(root, "taobao", [row], {})
        return _completed({"platform": "taobao", "status": "ok", "login_ok": True})

    monkeypatch.setattr("pipeline.crawler_adapter.subprocess.run", fake_run)
    adapter = SubprocessCrawlerAdapter(settings)
    first = adapter.run("taobao", DAY)
    second = adapter.run("taobao", DAY)
    assert first.items[0]["price"] == 1
    assert second.items[0]["price"] == 2
    assert roots[0] != roots[1]
    assert settings.crawler_output_dir not in roots


def test_crawler_progress_scope_is_task_scoped_or_fresh(settings, monkeypatch):
    from pipeline.crawler_adapter import SubprocessCrawlerAdapter

    scopes = []
    monkeypatch.setenv("CRAWLER_PROGRESS_SCOPE", "stale-parent")

    def fake_run(command, **kwargs):
        scopes.append(kwargs["env"].get("CRAWLER_PROGRESS_SCOPE"))
        return _completed({"platform": "1688", "status": "ok", "login_ok": True})

    monkeypatch.setattr("pipeline.crawler_adapter.subprocess.run", fake_run)
    settings.raw["run"]["progress_scope"] = "new-workbench-task"
    adapter = SubprocessCrawlerAdapter(settings)
    adapter.run("1688", DAY)
    adapter.run("jd", DAY)
    assert scopes == ["new-workbench-task", "new-workbench-task"]
    settings.raw["run"].pop("progress_scope")
    adapter.run("1688", DAY)
    adapter.run("1688", DAY)
    assert scopes[2] and scopes[3] and scopes[2] != scopes[3]
    assert "stale-parent" not in scopes


@pytest.mark.parametrize("returncode", [0, 1])
def test_subprocess_without_new_output_never_uses_old_files(
    settings, monkeypatch, returncode
):
    from pipeline.crawler_adapter import SubprocessCrawlerAdapter

    _write_crawl_output(
        settings.crawler_output_dir,
        "jd",
        [{"product_id": "stale"}],
        {"platform": "jd", "status": "ok", "login_ok": True},
    )
    monkeypatch.setattr(
        "pipeline.crawler_adapter.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            [], returncode, stdout="", stderr="no record"
        ),
    )
    data = SubprocessCrawlerAdapter(settings).run("jd", DAY)
    assert data.items == []
    assert data.status == "error"
    assert not data.login_ok


def test_jd_detail_fetcher_uses_crawler_venv(settings, monkeypatch):
    from pipeline.jd_detail import CrawlerJdDetailFetcher, JdDetailTarget

    python = _make_crawler_venv(settings)
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        return _completed(
            {"platform": "jd", "status": "ok", "login_ok": True, "errors": []}
        )

    monkeypatch.setattr("pipeline.jd_detail.subprocess.run", fake_run)
    target = JdDetailTarget(
        shop_id="JD-C",
        total_amount=Decimal("400000.00"),
        product_ids=("1001",),
    )
    CrawlerJdDetailFetcher(settings)(target, DAY)
    assert captured["command"][0] == str(python)
    assert captured["command"][1:4] == ["-m", "crawler", "detail"]


def test_taobao_detail_fetcher_uses_crawler_venv(settings, monkeypatch):
    from pipeline.taobao_detail import (
        CrawlerTaobaoDetailFetcher,
        TaobaoDetailTarget,
    )

    python = _make_crawler_venv(settings)
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        return _completed(
            {"platform": "taobao", "status": "ok", "login_ok": True, "errors": []}
        )

    monkeypatch.setattr("pipeline.taobao_detail.subprocess.run", fake_run)
    CrawlerTaobaoDetailFetcher(settings)(
        [TaobaoDetailTarget(platform="tmall", product_id="759606896896")], DAY
    )
    assert captured["command"][0] == str(python)
    assert captured["command"][1:4] == ["-m", "crawler", "detail"]


@pytest.mark.parametrize(
    "status,login_ok,returncode,raises",
    [
        ("skipped", False, 0, False),
        ("disabled", False, 0, False),
        ("login_failed", False, 1, True),
        ("risk_blocked", True, 1, True),
        ("ok", False, 0, True),
        ("skipped", False, 1, True),
    ],
)
def test_detail_skip_is_distinct_from_login_failure(
    settings, monkeypatch, status, login_ok, returncode, raises
):
    from pipeline.taobao_detail import CrawlerTaobaoDetailFetcher, TaobaoDetailTarget

    settings.crawler_dir.mkdir(parents=True, exist_ok=True)

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            returncode,
            stderr="",
            stdout=json.dumps(
                {
                    "platform": "taobao",
                    "status": status,
                    "login_ok": login_ok,
                    "errors": [],
                }
            ),
        )

    monkeypatch.setattr("pipeline.taobao_detail.subprocess.run", fake_run)
    fetcher = CrawlerTaobaoDetailFetcher(settings)
    if raises:
        with pytest.raises(RuntimeError):
            fetcher([TaobaoDetailTarget("taobao", "123")], DAY)
    else:
        assert fetcher([TaobaoDetailTarget("taobao", "123")], DAY) == []
