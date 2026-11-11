from __future__ import annotations

from datetime import datetime
from pathlib import Path

import yaml

from pipeline.run_entry import (
    backup_whitelist,
    ensure_whitelist_file,
    prepare_run,
    split_keywords,
)
from pipeline.shopkey import normalize_ip_name
from pipeline.whitelist import WHITELIST_TEMPLATE, parse_whitelist_text


def _temp_config(tmp_path: Path) -> Path:
    source = Path(__file__).resolve().parent.parent / "config" / "rules.yaml"
    raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    raw["paths"] = {
        "backups": str(tmp_path / "backups"),
        "logs": str(tmp_path / "logs"),
        "images": str(tmp_path / "images"),
    }
    raw["storage"] = {"db_path": str(tmp_path / "leads.db")}
    target = tmp_path / "rules.yaml"
    target.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return target


def test_normalize_ip_name_and_split_keywords():
    assert normalize_ip_name(" 奥 特 曼 ") == "奥特曼"
    assert normalize_ip_name("ＯＴＴＯ") == "otto"
    assert split_keywords("", "奥特曼") == ["奥特曼"]
    assert split_keywords("奥特曼，迪迦;赛罗", "奥特曼") == ["奥特曼", "迪迦", "赛罗"]


def test_parse_whitelist_txt_comments_decode_and_dedupe():
    text = (
        "# 注释\n"
        "\n"
        "taobao,  %E5%A5%A5%E7%89%B9%E6%9B%BC%E6%97%97%E8%88%B0%E5%BA%97 \n"
        "tmall,奥特曼旗舰店\n"
        "jd,京东自营店,1001\n"
        "bad,忽略我\n"
    )
    entries = parse_whitelist_text(text)
    assert [(e.platform_group, e.shop_name, e.shop_id) for e in entries] == [
        ("taobao", "奥特曼旗舰店", "name:奥特曼旗舰店"),
        ("jd", "京东自营店", "1001"),
    ]


def test_template_creation_and_backup(tmp_path):
    path = tmp_path / "whitelist" / "奥特曼.txt"
    assert ensure_whitelist_file(path) is True
    assert WHITELIST_TEMPLATE.splitlines()[0] in path.read_text(encoding="utf-8")
    assert ensure_whitelist_file(path) is False

    backup = backup_whitelist(
        path,
        tmp_path / "whitelist" / "history",
        "奥特曼",
        now=datetime(2026, 11, 6, 12, 0, 0),
    )
    assert backup.name == "奥特曼_20261106_120000.txt"
    assert backup.read_text(encoding="utf-8") == path.read_text(encoding="utf-8")


def test_prepare_run_non_interactive_uses_temp_whitelist_dir(tmp_path):
    config = _temp_config(tmp_path)
    whitelist_dir = tmp_path / "logs" / "validate_tmp" / "whitelist"
    prepared = prepare_run(
        ip_name="奥 特 曼",
        keywords="奥特曼，迪迦",
        whitelist_dir=whitelist_dir,
        config_path=config,
        interactive=False,
    )
    assert prepared.ip_code == "奥特曼"
    assert prepared.keywords == ["奥特曼", "迪迦"]
    assert prepared.whitelist_path == whitelist_dir / "奥特曼.txt"
    assert prepared.whitelist_path.exists()
    assert prepared.backup_path is not None and prepared.backup_path.exists()

    backend_raw = yaml.safe_load(
        prepared.backend_config_path.read_text(encoding="utf-8")
    )
    assert backend_raw["run"]["ip_code"] == "奥特曼"
    assert backend_raw["whitelist_file"] == str(prepared.whitelist_path)
    assert backend_raw["ips"][0]["name"] == "奥 特 曼"
    assert backend_raw["ips"][0]["aliases"] == ["迪迦"]

    crawler_raw = yaml.safe_load(
        prepared.crawler_config_path.read_text(encoding="utf-8")
    )
    assert crawler_raw["crawl"]["keywords"] == [
        {"keyword": "奥特曼", "ip_code": "奥特曼"},
        {"keyword": "迪迦", "ip_code": "奥特曼"},
    ]
    # 非交互模式不碰正式 backend/whitelist 目录。
    assert not (
        Path(__file__).resolve().parent.parent / "whitelist" / "奥特曼.txt"
    ).exists()


def test_prepare_run_interactive_opens_editor_even_when_ip_provided(tmp_path):
    config = _temp_config(tmp_path)
    whitelist_dir = tmp_path / "whitelist"
    calls = []

    def fake_editor(path):
        calls.append(path)
        path.write_text("taobao,现场加的店\n", encoding="utf-8")

    prepared = prepare_run(
        ip_name="奥特曼",
        keywords="",
        whitelist_dir=whitelist_dir,
        config_path=config,
        interactive=True,
        open_editor=fake_editor,
    )
    assert calls == [whitelist_dir / "奥特曼.txt"]
    assert [entry.shop_name for entry in prepared.entries] == ["现场加的店"]
    assert prepared.keywords == ["奥特曼"]
    assert prepared.backup_path.read_text(encoding="utf-8") == "taobao,现场加的店\n"


def test_generated_windows_config_inherits_missing_scoring_rules(tmp_path, settings):
    from pipeline.run_entry import build_run_configs

    source = tmp_path / "windows.yaml"
    original = {
        "browser": {"mode": "cdp"},
        "crawl": {
            "taobao": {
                "detail": {
                    "detail_min_text_score": 60,
                    "max_per_run": 5,
                }
            }
        },
    }
    source.write_text(yaml.safe_dump(original), encoding="utf-8")
    settings.raw["paths"]["crawler_config"] = str(source)
    _, crawler = build_run_configs(
        settings,
        ip_name="星星人",
        ip_code="星星人",
        keywords=["星星人"],
        whitelist_path=tmp_path / "whitelist.txt",
        output_dir=tmp_path / "run",
    )
    raw = yaml.safe_load(crawler.read_text(encoding="utf-8"))
    assert raw["text_score"] == settings.raw["text_score"]
    assert raw["exclude"] == settings.raw["exclude"]
    assert raw["crawl"]["taobao"] == original["crawl"]["taobao"]
    assert yaml.safe_load(source.read_text(encoding="utf-8")) == original
