"""工作台配置。

默认值全部指向项目内路径；生产环境优先读 ``workbench/config/workbench.yaml``，
也支持用环境变量覆盖少量启动参数。SMTP 密码不落配置：只允许从 keyring 或
环境变量读取。
"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

WORKBENCH_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = WORKBENCH_ROOT.parent
DEFAULT_DATA_DIR = WORKBENCH_ROOT / "data"
DEFAULT_DB_PATH = DEFAULT_DATA_DIR / "workbench.db"
DEFAULT_CONFIG_PATH = WORKBENCH_ROOT / "config" / "workbench.yaml"
BACKEND_RULES = PROJECT_ROOT / "backend" / "config" / "rules.yaml"


class ConfigError(RuntimeError):
    """配置非法或缺少必要项。"""


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        import yaml
    except ModuleNotFoundError as exc:  # pragma: no cover - 目标机有 PyYAML
        raise ConfigError("缺少 PyYAML，无法读取 workbench.yaml") from exc
    data = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    if not isinstance(data, dict):
        raise ConfigError("workbench.yaml 顶层必须是映射")
    return data


def _as_path(value: Any, base: Path) -> Path:
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = (base / path).resolve()
    return path


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on", "是"}


@dataclass
class WorkbenchConfig:
    project_root: Path = PROJECT_ROOT
    data_dir: Path = DEFAULT_DATA_DIR
    db_path: Path = DEFAULT_DB_PATH
    host: str = "127.0.0.1"
    port: int = 8765
    open_browser: bool = False
    allowed_networks: list[str] = field(
        default_factory=lambda: [
            "127.0.0.1/32",
            "::1/128",
            "10.0.0.0/8",
            "172.16.0.0/12",
            "192.168.0.0/16",
        ]
    )
    session_idle_hours: int = 8
    login_lock_threshold: int = 5
    login_lock_minutes: int = 15
    min_password_length: int = 8
    rules_path: Path = BACKEND_RULES
    crawler_config_path: Path | None = None
    leads_db_path: Path | None = None
    smtp: dict[str, Any] = field(default_factory=dict)
    version_package_dir: Path | None = None
    max_update_bytes: int = 64 * 1024 * 1024

    # ---- 派生路径 ----
    @property
    def versions_dir(self) -> Path:
        return self.data_dir / "versions"

    @property
    def version_backups_dir(self) -> Path:
        return self.data_dir / "version_backups"

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def versions_state_path(self) -> Path:
        return self.data_dir / "current_version.json"

    def ensure_dirs(self) -> None:
        for path in (
            self.data_dir,
            self.versions_dir,
            self.version_backups_dir,
            self.runs_dir,
            self.exports_dir,
            self.logs_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def validate_host(self) -> str:
        host = str(self.host or "").strip()
        if host in {"0.0.0.0", "::", "*"}:
            raise ConfigError("不允许绑定 0.0.0.0/::；请填写 127.0.0.1 或本机内网地址")
        try:
            address = ipaddress.ip_address(host)
        except ValueError as exc:
            raise ConfigError(f"监听地址必须是 IP 地址：{host}") from exc
        if not (address.is_loopback or address.is_private):
            raise ConfigError("只允许监听 127.0.0.1 或本机内网地址，不能绑定公网 IP")
        return host

    @classmethod
    def load(cls, path: str | Path | None = None) -> "WorkbenchConfig":
        config_path = Path(
            path or os.environ.get("WORKBENCH_CONFIG") or DEFAULT_CONFIG_PATH
        ).expanduser()
        raw = _load_yaml(config_path)
        base = config_path.parent
        data_dir = _as_path(
            os.environ.get("WORKBENCH_DATA_DIR") or raw.get("data_dir") or DEFAULT_DATA_DIR,
            PROJECT_ROOT,
        )
        server = raw.get("server") or {}
        security = raw.get("security") or {}
        paths = raw.get("paths") or {}
        db_path = _as_path(raw.get("db_path") or (data_dir / "workbench.db"), PROJECT_ROOT)
        rules_path = _as_path(paths.get("rules") or BACKEND_RULES, PROJECT_ROOT)
        crawler_raw = paths.get("crawler_config")
        leads_raw = paths.get("leads_db")
        smtp_raw = dict(raw.get("smtp") or {})
        # 允许用后端 rules.yaml 的 alert 节作为 SMTP 默认值，密码仍只从 keyring/环境读。
        try:
            import yaml

            rules = yaml.safe_load(rules_path.read_text(encoding="utf-8-sig")) or {}
            alert = rules.get("alert") or {}
            for key, value in alert.items():
                smtp_raw.setdefault(key, value)
        except (OSError, ModuleNotFoundError, Exception):  # noqa: BLE001
            pass
        config = cls(
            project_root=PROJECT_ROOT,
            data_dir=data_dir,
            db_path=db_path,
            host=str(server.get("host") or os.environ.get("WORKBENCH_HOST") or "127.0.0.1"),
            port=_as_int(server.get("port") or os.environ.get("WORKBENCH_PORT"), 8765),
            open_browser=_as_bool(server.get("open_browser"), False),
            allowed_networks=list(
                security.get("allowed_networks") or cls.__dataclass_fields__["allowed_networks"].default_factory()
            ),
            session_idle_hours=_as_int(security.get("session_idle_hours"), 8),
            login_lock_threshold=_as_int(security.get("login_lock_threshold"), 5),
            login_lock_minutes=_as_int(security.get("login_lock_minutes"), 15),
            min_password_length=_as_int(security.get("min_password_length"), 8),
            rules_path=rules_path,
            crawler_config_path=_as_path(crawler_raw, PROJECT_ROOT) if crawler_raw else None,
            leads_db_path=_as_path(leads_raw, PROJECT_ROOT) if leads_raw else None,
            smtp=smtp_raw,
        )
        return config


__all__ = [
    "BACKEND_RULES",
    "ConfigError",
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_DATA_DIR",
    "DEFAULT_DB_PATH",
    "PROJECT_ROOT",
    "WORKBENCH_ROOT",
    "WorkbenchConfig",
]
