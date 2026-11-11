from __future__ import annotations

import os
import sys
from pathlib import Path as _Path

_BACKEND_ROOT = _Path(__file__).resolve().parent.parent
_DEPS = _BACKEND_ROOT / ".deps"
if os.name != "nt" and _DEPS.is_dir() and str(_DEPS) not in sys.path:
    sys.path.insert(0, str(_DEPS))

from datetime import date
from pathlib import Path
from typing import Any

import pytest

from pipeline.config import Settings, load_config
from pipeline.db import create_engine_for, ensure_schema, make_session_factory
from pipeline.ingest import PlatformData


def deep_update(target: dict, updates: dict) -> dict:
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            deep_update(target[key], value)
        else:
            target[key] = value
    return target


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    settings = load_config()
    settings.raw["storage"] = {"db_path": str(tmp_path / "data" / "leads.db")}
    settings.raw["paths"] = {
        "backups": str(tmp_path / "backups"),
        "logs": str(tmp_path / "logs"),
        "images": str(tmp_path / "data" / "images"),
        "crawler_dir": str(tmp_path / "crawler"),
        "crawler_config": str(tmp_path / "crawler" / "config" / "rules.yaml"),
        "crawler_output_dir": str(tmp_path / "crawler" / "out"),
    }
    settings.raw["run"] = {
        "lock_file": str(tmp_path / "data" / "run.lock"),
        "backup_keep": 14,
        "log_keep_days": 30,
        "crawl_timeout_seconds": 30,
    }
    settings.raw["alert"] = {
        "enabled": False,
        "smtp_host": "smtp.example.test",
        "smtp_port": 465,
        "smtp_ssl": True,
        "from": "sender@example.test",
        "to": ["receiver@example.test"],
        "min_items_per_platform": 1,
        "keyring_service": "ip-lead-pipeline-smtp-test",
        "subject_prefix": "[TEST]",
        "timeout_seconds": 3,
    }
    settings.raw["platforms"] = {
        "taobao": {"enabled": True},
        "jd": {"enabled": True},
        "1688": {"enabled": True},
    }
    settings.raw.setdefault("aggregate", {})["shop_threshold"] = 500000
    # 大多数用例只测后端主链路，不写真实 Excel；导出专项用例再显式打开。
    settings.raw["export"] = {
        "enabled": False,
        "dir": str(tmp_path / "excel"),
        "filename": "线索_{YYYYMMDD}.xlsx",
    }
    return settings


@pytest.fixture
def engine(settings: Settings):
    ensure_schema(settings)
    engine = create_engine_for(settings)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def session_factory(engine):
    return make_session_factory(engine)


def make_item(**overrides: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "platform": "taobao",
        "product_id": "10001",
        "url": "https://item.example/10001",
        "title": "葫芦娃 印花 童装 批发",
        "shop_id": "S1",
        "shop_name": "测试旗舰店",
        "shop_label_raw": "天猫",
        "shop_type_raw_hint": "天猫",
        "price": 300.0,
        "price_raw": "¥300",
        "sales_raw": "1000+",
        "sales": 1000,
        "comments_raw": "100+",
        "comments": 100,
        "main_image_url": "https://img.example/1.jpg",
        "image_path": None,
        "category": "童装",
        "ip_code": "huluxiongdi",
        "search_keyword": "葫芦娃",
        "crawled_at": "2026-11-05T10:00:00+08:00",
        "listed": True,
        "flags": [],
    }
    item.update(overrides)
    for field in ("price", "sales", "comments"):
        if field in overrides and field + "_raw" not in overrides:
            value = overrides[field]
            item[field + "_raw"] = None if value is None else str(value)
    return item


def platform_data(platform: str, items: list[dict], **record: Any) -> PlatformData:
    base = {
        "platform": platform,
        "login_ok": True,
        "status": "ok",
        "items_ok": len(items),
        "errors": [],
        "warnings": [],
    }
    base.update(record)
    return PlatformData(platform=platform, items=items, run_record=base)


class FakeAdapter:
    def __init__(self, mapping: dict[str, PlatformData | Exception]):
        self.mapping = mapping
        self.calls: list[tuple[str, date]] = []

    def run(self, platform: str, run_date: date) -> PlatformData:
        self.calls.append((platform, run_date))
        value = self.mapping[platform]
        if isinstance(value, Exception):
            raise value
        return value


class FakeSMTP:
    instances: list["FakeSMTP"] = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.logged_in = False
        self.messages: list[Any] = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        self.logged_in = True
        self.user = user
        self.password = password

    def send_message(self, message, to_addrs=None):
        self.messages.append((message, to_addrs))


@pytest.fixture
def fake_smtp(monkeypatch):
    FakeSMTP.instances = []
    monkeypatch.setattr("smtplib.SMTP_SSL", FakeSMTP)
    monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
    return FakeSMTP
