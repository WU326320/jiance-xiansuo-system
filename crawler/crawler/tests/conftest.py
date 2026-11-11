from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
import yaml

from crawler.config import AppConfig, load_config

FIXTURES = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).resolve().parents[2]


def assert_private_mode(path: str | Path, expected: int) -> None:
    """断言路径存在，并在 POSIX 上检查 0o600/0o700 权限位。

    Windows 的 os.chmod 只能控制只读位，st_mode 不反映 POSIX 权限；
    因此 Windows 上只检查文件/目录确实创建，权限语义由 ACL 负责。
    """
    path = Path(path)
    assert path.exists()
    if os.name == "posix":
        actual = stat.S_IMODE(path.stat().st_mode)
        assert actual == expected, f"{path} 权限为 {oct(actual)}，期望 {oct(expected)}"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def fixture_json(name: str):
    return json.loads(fixture_text(name))


@pytest.fixture
def make_config(tmp_path: Path):
    def _make(**overrides) -> AppConfig:
        data = yaml.safe_load((ROOT / "config" / "rules.yaml").read_text(encoding="utf-8"))
        data["crawl"]["output_dir"] = str(tmp_path / "out")
        data["crawl"]["log_dir"] = str(tmp_path / "logs")
        data["crawl"]["state_dir"] = str(tmp_path / "state")
        data["crawl"]["rate_limit"] = {
            "requests_per_minute": 600,
            "page_interval_seconds": [0, 0],
            "keyword_interval_seconds": [0, 0],
            "detail_interval_seconds": [0, 0],
        }
        # 淘宝降频配置在 rules.yaml 里是真实慢速值；单测必须归零，避免 sleep。
        data["crawl"].setdefault("taobao", {})
        data["crawl"]["taobao"]["rate_limit"] = {
            "requests_per_minute": 600,
            "page_interval_seconds": [0, 0],
            "keyword_interval_seconds": [0, 0],
            "detail_interval_seconds": [0, 0],
        }
        data["crawl"]["retry"] = {"attempts": 1, "backoff_seconds": [0], "jitter_seconds": 0}
        data["crawl"]["image"]["enabled"] = False
        data["crawl"]["keywords"] = [{"keyword": "葫芦兄弟", "ip_code": "huluxiongdi"}]
        data["security"]["profile_dir"] = str(tmp_path / "profiles" / "{platform}")
        for key, value in overrides.items():
            if key in data and isinstance(data[key], dict) and isinstance(value, dict):
                data[key].update(value)
            else:
                data[key] = value
        path = tmp_path / "rules.yaml"
        path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
        return load_config(path)

    return _make
