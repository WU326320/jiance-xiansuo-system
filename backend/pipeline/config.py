"""配置加载。

规则字段沿用 /workspace/requirements/rules.example.yaml；后端只额外读取
``app/paths/run/alert/export`` 节。所有路径都用 pathlib 解析，默认相对
后端根目录，便于 Windows 双击运行。

``config_version`` 一律取 YAML 文件原始字节 sha256 的前 12 位；这样即使
忘了手工递增配置里的版本号，C5（改 yaml 后新记录版本变化）也成立。
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

BACKEND_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_CONFIG_PATH = BACKEND_ROOT / "config" / "rules.yaml"
DEFAULT_CRAWLER_CONFIG = BACKEND_ROOT.parent / "crawler" / "config" / "rules.yaml"
DEFAULT_CRAWLER_DIR = BACKEND_ROOT.parent / "crawler"


class ConfigError(RuntimeError):
    """配置缺失或取值非法。"""


def _as_path(value: Any, base: Path) -> Path:
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = (base / path).resolve()
    return path


@dataclass(frozen=True)
class Settings:
    """一次运行使用的不可变配置快照。"""

    path: Path
    root: Path
    raw: dict[str, Any]
    yaml_text: str
    config_version: str

    # ---- 常用标量 ----
    @property
    def caliber_version(self) -> str:
        return str(self.raw.get("caliber_version") or "v1.0")

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def section(self, name: str) -> dict[str, Any]:
        value = self.raw.get(name)
        return value if isinstance(value, dict) else {}

    # ---- 路径 ----
    @property
    def database_path(self) -> Path:
        env = os.environ.get("IP_LEAD_DB_PATH")
        if env:
            return Path(env).expanduser().resolve()
        # 第 6 节最终配置键：storage.db_path（默认 data/leads.db）。
        # 兼容旧 rules.yaml 的 paths.database，避免存量配置升级后突然换库。
        configured = self.get("storage.db_path")
        if configured in (None, ""):
            configured = self.get("paths.database", "data/leads.db")
        return _as_path(configured, self.root)

    @property
    def backups_dir(self) -> Path:
        return _as_path(self.get("paths.backups", "backups"), self.root)

    @property
    def logs_dir(self) -> Path:
        return _as_path(self.get("paths.logs", "logs"), self.root)

    @property
    def images_dir(self) -> Path:
        return _as_path(self.get("paths.images", "data/images"), self.root)

    @property
    def lock_path(self) -> Path:
        return _as_path(self.get("run.lock_file", "data/run.lock"), self.root)

    @property
    def crawler_dir(self) -> Path:
        env = os.environ.get("IP_LEAD_CRAWLER_DIR")
        if env:
            return Path(env).expanduser().resolve()
        return _as_path(
            self.get("paths.crawler_dir", str(DEFAULT_CRAWLER_DIR)), self.root
        )

    @property
    def crawler_config(self) -> Path:
        env = os.environ.get("IP_LEAD_CRAWLER_CONFIG")
        if env:
            return Path(env).expanduser().resolve()
        return _as_path(
            self.get("paths.crawler_config", str(DEFAULT_CRAWLER_CONFIG)), self.root
        )

    @property
    def crawler_output_dir(self) -> Path:
        return _as_path(
            self.get("paths.crawler_output_dir", str(self.crawler_dir / "out")),
            self.root,
        )

    @property
    def export_dir(self) -> Path:
        default = (
            Path(os.environ.get("USERPROFILE", str(Path.home())))
            / "Documents"
            / "侵权线索"
        )
        raw = str(self.get("export.dir", "") or "").strip()
        return default if not raw else _as_path(raw, self.root)

    @property
    def export_enabled(self) -> bool:
        return bool(self.get("export.enabled", False))

    # ---- 运行 ----
    @property
    def remaining_seconds(self) -> float | None:
        value = self.get("run.ends_at")
        if not value:
            return None
        deadline = datetime.fromisoformat(str(value))
        current = datetime.now(deadline.tzinfo)
        return max(0.0, (deadline - current).total_seconds())

    @property
    def deadline_reached(self) -> bool:
        remaining = self.remaining_seconds
        return remaining is not None and remaining <= 0

    def crawl_timeout(self, seconds: float) -> float:
        """请求/补采超时独立于常规采集截止时间。"""
        return float(seconds)

    @property
    def backup_keep(self) -> int:
        return max(1, int(self.get("run.backup_keep", 14)))

    @property
    def log_keep_days(self) -> int:
        return max(1, int(self.get("run.log_keep_days", 30)))

    @property
    def min_items_per_platform(self) -> int:
        return int(self.get("alert.min_items_per_platform", 1))

    @property
    def smtp(self) -> dict[str, Any]:
        cfg = dict(self.section("alert"))
        cfg.setdefault("smtp_host", "smtp.qiye.aliyun.com")
        cfg.setdefault("smtp_port", 465)
        cfg.setdefault("smtp_ssl", True)
        cfg.setdefault("from", "wuhengxuan@ip-sky.cn")
        cfg.setdefault("to", ["wuhengxuan@ip-sky.cn"])
        cfg.setdefault("keyring_service", "ip-lead-pipeline-smtp")
        cfg.setdefault("subject_prefix", "[侵权线索]")
        cfg.setdefault("enabled", True)
        return cfg

    @property
    def platform_names(self) -> list[str]:
        configured = self.section("platforms")
        if not configured:
            return ["taobao", "jd", "1688"]
        return [
            name
            for name, value in configured.items()
            if (value or {}).get("enabled", True)
        ]

    def ip_config(self, code: str | None) -> dict[str, Any] | None:
        if not code:
            return None
        for entry in self.raw.get("ips") or []:
            if isinstance(entry, dict) and entry.get("code") == code:
                return entry
        return None

    def ip_names(self, code: str | None) -> list[str]:
        entry = self.ip_config(code)
        if not entry:
            return []
        names = [entry.get("name"), *(entry.get("aliases") or [])]
        return [str(item) for item in names if item]

    def ip_variants(self, code: str | None) -> list[str]:
        entry = self.ip_config(code)
        if not entry:
            return []
        return [str(item) for item in (entry.get("variants") or []) if item]

    def all_other_ip_names(self, code: str | None) -> list[str]:
        names: list[str] = []
        for entry in self.raw.get("ips") or []:
            if not isinstance(entry, dict) or entry.get("code") == code:
                continue
            for key in ("name", "aliases", "variants"):
                value = entry.get(key)
                if isinstance(value, list):
                    names.extend(str(item) for item in value if item)
                elif value:
                    names.append(str(value))
        return names


def load_config(path: str | Path | None = None) -> Settings:
    env_path = os.environ.get("IP_LEAD_CONFIG")
    candidate = Path(path or env_path or DEFAULT_CONFIG_PATH).expanduser()
    if not candidate.is_absolute():
        candidate = (BACKEND_ROOT / candidate).resolve()
    if not candidate.exists():
        raise ConfigError(f"配置文件不存在: {candidate}")
    raw_bytes = candidate.read_bytes()
    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:  # pragma: no cover - 仅在坏文件时触发
        raise ConfigError(f"配置文件不是 UTF-8: {candidate}") from exc
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML 解析失败: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("配置文件顶层必须是对象")
    version = hashlib.sha256(raw_bytes).hexdigest()[:12]
    return Settings(
        path=candidate,
        root=BACKEND_ROOT,
        raw=raw,
        yaml_text=text,
        config_version=version,
    )


__all__ = [
    "BACKEND_ROOT",
    "DEFAULT_CONFIG_PATH",
    "ConfigError",
    "Settings",
    "load_config",
]
