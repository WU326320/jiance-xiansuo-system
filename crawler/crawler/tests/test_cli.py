from __future__ import annotations

from datetime import datetime, timezone

import pytest

from crawler.cli import build_parser, main
from crawler.models import RunRecord
from crawler.runner import records_to_json


def test_parser_commands():
    parser = build_parser()
    args = parser.parse_args(["login", "--platform", "taobao"])
    assert args.command == "login"
    args_pos = parser.parse_args(["login", "taobao"])
    assert args_pos.platform_pos == "taobao"
    args_run = parser.parse_args(["run", "jd"])
    assert args_run.platform_pos == "jd"
    args = parser.parse_args(["check-login", "--platform", "all"])
    assert args.platform == "all"
    assert parser.parse_args(["check-login"]).platform_pos == "all"
    args = parser.parse_args(["run", "--platform", "jd", "--keyword", "葫芦兄弟", "--no-images"])
    assert args.keyword == ["葫芦兄弟"] and args.no_images
    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--platform", "pdd"])


def test_main_missing_config(tmp_path):
    code = main(["run", "--platform", "taobao", "--config", str(tmp_path / "missing.yaml")])
    assert code == 2


def test_main_missing_ids_file(make_config, tmp_path):
    config = make_config()
    code = main(
        [
            "detail",
            "--platform",
            "taobao",
            "--config",
            str(tmp_path / "rules.yaml"),
            "--ids-file",
            str(tmp_path / "ids.txt"),
        ]
    )
    assert code == 2


def test_records_to_json():
    now = datetime(2026, 11, 5, tzinfo=timezone.utc)
    record = RunRecord(platform="taobao", started_at=now, ended_at=now, login_ok=True)
    line = records_to_json([record])
    assert '"platform":"taobao"' in line.replace(" ", "")


def _record(status="ok"):
    now = datetime(2026, 11, 5, tzinfo=timezone.utc)
    return RunRecord(platform="taobao", started_at=now, ended_at=now, login_ok=True, status=status)


def test_main_login_dispatch(make_config, tmp_path, monkeypatch):
    make_config()
    calls = []
    monkeypatch.setattr("crawler.cli.login_platform", lambda *a, **k: calls.append((a, k)) or 0)
    code = main(
        [
            "login",
            "--platform",
            "taobao",
            "--config",
            str(tmp_path / "rules.yaml"),
            "--display",
            ":6",
            "--timeout",
            "10",
            "--poll",
            "0.1",
        ]
    )
    assert code == 0 and calls


def test_main_check_login_dispatch(make_config, tmp_path, monkeypatch):
    make_config()
    monkeypatch.setattr("crawler.cli.check_login_platform", lambda *a, **k: 0)
    code = main(["check-login", "--platform", "all", "--config", str(tmp_path / "rules.yaml")])
    assert code == 0


def test_main_run_dispatch_success(make_config, tmp_path, monkeypatch):
    make_config()
    monkeypatch.setattr("crawler.cli.run_platforms", lambda *a, **k: [_record()])
    code = main(
        [
            "run",
            "--platform",
            "taobao",
            "--config",
            str(tmp_path / "rules.yaml"),
            "--date",
            "2026-11-05",
            "--keyword",
            "葫芦兄弟",
            "--limit-keywords",
            "1",
            "--no-images",
        ]
    )
    assert code == 0


def test_main_run_dispatch_failure(make_config, tmp_path, monkeypatch):
    make_config()
    monkeypatch.setattr("crawler.cli.run_platforms", lambda *a, **k: [_record("login_failed")])
    code = main(["run", "--platform", "taobao", "--config", str(tmp_path / "rules.yaml")])
    assert code == 1


def test_main_detail_dispatch(make_config, tmp_path, monkeypatch):
    make_config()
    ids = tmp_path / "ids.txt"
    ids.write_text("1111111111\n", encoding="utf-8")
    monkeypatch.setattr("crawler.cli.run_detail", lambda *a, **k: _record())
    code = main(
        [
            "detail",
            "--platform",
            "taobao",
            "--config",
            str(tmp_path / "rules.yaml"),
            "--ids-file",
            str(ids),
            "--date",
            "2026-11-05",
        ]
    )
    assert code == 0


def test_main_detail_accepts_direct_ids(make_config, tmp_path, monkeypatch):
    make_config()
    captured = {}

    def fake_run_detail(platform, config, *, ids, date, headless=None):
        captured["platform"] = platform
        captured["ids"] = list(ids)
        return _record()

    monkeypatch.setattr("crawler.cli.run_detail", fake_run_detail)
    code = main(
        [
            "detail",
            "--platform",
            "jd",
            "--config",
            str(tmp_path / "rules.yaml"),
            "--ids",
            "1001,1002",
            "1003",
            "--date",
            "2026-11-05",
        ]
    )
    assert code == 0
    assert captured["platform"] == "jd"
    assert captured["ids"] == ["1001", "1002", "1003"]


def test_main_detail_requires_ids(make_config, tmp_path):
    make_config()
    code = main(
        [
            "detail",
            "--platform",
            "jd",
            "--config",
            str(tmp_path / "rules.yaml"),
        ]
    )
    assert code == 2


def test_main_invalid_date(make_config, tmp_path):
    make_config()
    code = main(["run", "--platform", "taobao", "--config", str(tmp_path / "rules.yaml"), "--date", "2026/11/05"])
    assert code == 2


def test_main_keyboard_interrupt(make_config, tmp_path, monkeypatch):
    make_config()
    monkeypatch.setattr("crawler.cli.run_platforms", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
    code = main(["run", "--platform", "taobao", "--config", str(tmp_path / "rules.yaml")])
    assert code == 130


def test_main_browser_unavailable(make_config, tmp_path, monkeypatch):
    from crawler.core.errors import BrowserUnavailable

    make_config()
    monkeypatch.setattr("crawler.cli.run_platforms", lambda *a, **k: (_ for _ in ()).throw(BrowserUnavailable("no")))
    code = main(["run", "--platform", "taobao", "--config", str(tmp_path / "rules.yaml")])
    assert code == 2
