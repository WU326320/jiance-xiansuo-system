from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from crawler.config import ConfigError, load_config, _pair
from crawler.tests.conftest import assert_private_mode


def test_load_config_and_defaults(make_config):
    config = make_config()
    assert config.config_version
    assert config.caliber_version == "v1.0"
    assert config.enabled_platforms() == ["taobao", "jd", "1688"]
    assert config.crawl.max_pages == 0
    assert config.crawl.daily_max_pages == 0
    assert config.crawl.rate_limit.page_interval_seconds == (0.0, 0.0)
    assert config.config_sha256 and len(config.config_sha256) == 64


def test_profile_dirs_are_private(make_config):
    config = make_config()
    for platform in ("taobao", "jd", "1688"):
        path = config.profile_dir(platform)
        assert path.name == platform
        assert_private_mode(path, 0o700)


def test_profile_env_override(make_config, monkeypatch, tmp_path):
    config = make_config()
    base = tmp_path / "custom-profiles"
    monkeypatch.setenv("CRAWLER_PROFILES_DIR", str(base))
    assert config.profile_dir("jd") == base / "jd"


def test_config_version_falls_back_to_hash(tmp_path):
    data = {"caliber_version": "v1.0", "platforms": {"taobao": {"enabled": True}}}
    path = tmp_path / "mini.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    config = load_config(path)
    assert config.config_version == config.config_sha256[:12]
    assert config.crawl.max_pages == 0  # 默认不限量


def test_missing_and_invalid_config(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("crawl:\n  max_pages: -1\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(bad)


def test_interval_validation():
    assert _pair(3) == (3.0, 3.0)
    assert _pair([1, 2]) == (1.0, 2.0)
    with pytest.raises(ValueError):
        _pair([3, 2])
    with pytest.raises(ValueError):
        _pair([1])


def test_keyword_specs_accept_strings(tmp_path):
    data = {
        "crawl": {"keywords": ["葫芦兄弟", {"keyword": "叶罗丽", "ip_code": "yeluoli"}]}
    }
    path = tmp_path / "kw.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    config = load_config(path)
    assert [k.keyword for k in config.crawl.keywords] == ["葫芦兄弟", "叶罗丽"]
    assert config.crawl.keywords[1].ip_code == "yeluoli"


def test_detail_switch_defaults_to_1688_disabled(make_config):
    """11/6 轮：1688 详情默认停用，但列表采集开关不受影响。"""
    config = make_config()
    assert config.crawl.detail.enabled is True
    assert config.crawl.detail.platform_enabled("taobao") is True
    assert config.crawl.detail.platform_enabled("jd") is True
    assert config.crawl.detail.platform_enabled("1688") is False
    assert config.platform_enabled("1688") is True


def test_detail_switch_can_be_overridden(make_config):
    config = make_config(crawl={"detail": {"platforms": {"1688": True}}})
    assert config.crawl.detail.platform_enabled("1688") is True
