"""配置解析：storage.db_path / export.dir / filename / 缩略图 / 口径版本。

目标环境是 Windows 10 + Python 3.13；路径统一用 pathlib.Path。为便于在 Linux
夹具环境自测，环境变量展开同时支持 ``%USERPROFILE%``、``$USERPROFILE`` 和
``${USERPROFILE}``。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping

from .columns import DEFAULT_THUMBNAIL_MAX_HEIGHT, DEFAULT_THUMBNAIL_WIDTH
from .errors import ExportConfigError

DEFAULT_FILENAME = "线索_{YYYYMMDD}.xlsx"
DEFAULT_THRESHOLD = 500_000.0
DEFAULT_COMMENT_MULTIPLIER = 10.0
# 后端方案 §6 / Excel 方案 v0.1.1 §3：数据库路径配置键 storage.db_path。
DEFAULT_DB_PATH = "data/leads.db"

_VAR_RE = re.compile(
    r"%([^%]+)%"                       # %USERPROFILE%
    r"|\$\{([^}]+)\}"                  # ${USERPROFILE}
    r"|\$([A-Za-z_][A-Za-z0-9_]*)"     # $USERPROFILE
)
_INVALID_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


@dataclass(frozen=True)
class ExportSettings:
    output_dir: Path
    filename: str
    image_base_dirs: tuple[Path, ...] = field(default_factory=tuple)
    thumbnail_width: int = DEFAULT_THUMBNAIL_WIDTH
    thumbnail_max_height: int = DEFAULT_THUMBNAIL_MAX_HEIGHT
    threshold: float = DEFAULT_THRESHOLD
    comment_multiplier: float = DEFAULT_COMMENT_MULTIPLIER
    caliber_version: str = ""
    config_version: str = ""

    def output_path(self, snapshot: date) -> Path:
        ymd = snapshot.strftime("%Y%m%d")
        name = _render_filename(self.filename, ymd)
        return self.output_dir / name


def expand_env(value: str) -> str:
    """展开 %VAR%、$VAR、${VAR}；USERPROFILE 缺失时回落到用户主目录。"""

    def repl(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2) or match.group(3) or ""
        resolved = os.environ.get(name)
        if resolved is None and name.upper() == "USERPROFILE":
            # 夹具/Linux 环境没有 USERPROFILE 时，用 home 作为等价目录，
            # 保证同一份 rules.yaml 能跨平台跑测试。
            resolved = str(Path.home())
        if resolved is None:
            raise ExportConfigError(
                f"配置中的环境变量 %{name}% 未设置，无法解析导出路径。"
            )
        return resolved

    return _VAR_RE.sub(repl, value)


def _as_path(value: str | os.PathLike[str]) -> Path:
    expanded = expand_env(os.fspath(value))
    path = Path(expanded).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def default_export_dir() -> Path:
    """默认 %USERPROFILE%\\Documents\\侵权线索（非 Windows 用 home 等价）。"""

    user_profile = os.environ.get("USERPROFILE")
    base = Path(user_profile) if user_profile else Path.home()
    return base / "Documents" / "侵权线索"


def _get_export_section(config: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """兼容传入完整 rules.yaml 或只传 export 段两种调用方式。"""

    if not config:
        return {}
    export_section = config.get("export")
    if isinstance(export_section, Mapping):
        return export_section
    return config


def _get_storage_section(config: Mapping[str, Any]) -> Mapping[str, Any]:
    storage = config.get("storage")
    return storage if isinstance(storage, Mapping) else {}


def _configured_db_path(config: Mapping[str, Any]) -> str | None:
    """按 v0.1.1 读取 ``storage.db_path``，并兼容旧配置键。

    首选且只把 ``storage.db_path`` 当作正式配置键；``paths.database`` 与
    顶层 ``db_path`` 仅用于兼容上一轮后端配置和手工调用，不改变优先级。
    """

    storage = _get_storage_section(config)
    for source, key in (
        (storage, "db_path"),
        (config, "db_path"),
        (config.get("paths") if isinstance(config.get("paths"), Mapping) else {}, "database"),
    ):
        value = source.get(key) if isinstance(source, Mapping) else None
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def resolve_db_path(
    db_path: str | os.PathLike[str] | None,
    config: Mapping[str, Any] | None = None,
) -> Path:
    """解析数据库路径并校验文件存在。

    优先级：显式 ``db_path`` 参数 → ``storage.db_path`` → 顶层 ``db_path``
    → 旧 ``paths.database`` → 默认 ``data/leads.db``。

    相对路径默认按当前工作目录解析；如需固定基准，可在配置里提供
    ``storage.base_dir``（或顶层 ``base_dir``）。默认路径因此也能在
    backend 根目录下直接运行 ``python -m lead_excel``。
    """

    config = config if isinstance(config, Mapping) else {}
    raw = db_path
    if raw is None or not str(raw).strip():
        raw = _configured_db_path(config) or DEFAULT_DB_PATH

    expanded = expand_env(str(raw))
    path = Path(expanded).expanduser()
    if not path.is_absolute():
        storage = _get_storage_section(config)
        base_raw = storage.get("base_dir") or config.get("base_dir")
        base = _as_path(base_raw) if base_raw else Path.cwd()
        path = base / path
    path = path.resolve()
    if not path.exists():
        raise ExportConfigError(
            f"SQLite 数据库不存在：{path}（配置项 storage.db_path，默认 {DEFAULT_DB_PATH}）。"
        )
    if not path.is_file():
        raise ExportConfigError(f"storage.db_path 不是文件：{path}")
    return path


def _get_number(
    config: Mapping[str, Any],
    dotted_key: str,
    default: float,
    *,
    minimum: float | None = None,
) -> float:
    node: Any = config
    for part in dotted_key.split("."):
        if not isinstance(node, Mapping) or part not in node:
            node = None
            break
        node = node[part]
    if node is None or node == "":
        return default
    try:
        number = float(node)
    except (TypeError, ValueError) as exc:
        raise ExportConfigError(
            f"配置项 {dotted_key} 必须是数字，当前为 {node!r}。"
        ) from exc
    if minimum is not None and number < minimum:
        raise ExportConfigError(
            f"配置项 {dotted_key} 不能小于 {minimum:g}，当前为 {number:g}。"
        )
    return number


def _get_int(
    export_section: Mapping[str, Any],
    key: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    raw = export_section.get(key, default)
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ExportConfigError(f"配置项 export.{key} 必须是整数，当前为 {raw!r}。") from exc
    if not minimum <= value <= maximum:
        raise ExportConfigError(
            f"配置项 export.{key} 必须在 {minimum}..{maximum} 之间，当前为 {value}。"
        )
    return value


def _get_version(config: Mapping[str, Any], export_section: Mapping[str, Any], key: str) -> str:
    for source in (config, export_section):
        value = source.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _render_filename(template: str, ymd: str) -> str:
    """替换日期占位符并清洗 Windows 非法文件名字符。"""

    name = str(template or DEFAULT_FILENAME).replace("{YYYYMMDD}", ymd)
    name = name.replace("{yyyyMMdd}", ymd)
    name = _INVALID_FILENAME_RE.sub("_", name).strip()
    if not name:
        name = DEFAULT_FILENAME.replace("{YYYYMMDD}", ymd)
    # 配置漏写日期占位符时补上，避免跨日重跑覆盖历史文件。
    if ymd not in name:
        suffix = Path(name).suffix or ".xlsx"
        stem = name[: -len(suffix)] if name.lower().endswith(suffix.lower()) else name
        name = f"{stem}_{ymd}{suffix}"
    if not name.lower().endswith(".xlsx"):
        name = f"{name}.xlsx"
    return name


def parse_snapshot_date(value: str | date | datetime | None) -> date:
    """接受 YYYY-MM-DD / YYYYMMDD / YYYY/MM/DD，返回 date。"""

    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    text = str(value or "").strip()
    if not text:
        raise ExportConfigError("snapshot_date 不能为空，应为 YYYY-MM-DD 或 YYYYMMDD。")
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ExportConfigError(
        f"snapshot_date={value!r} 不是合法日期，应为 YYYY-MM-DD 或 YYYYMMDD。"
    )


def resolve_settings(config: Mapping[str, Any] | None) -> ExportSettings:
    if config is None:
        config = {}
    if not isinstance(config, Mapping):
        raise ExportConfigError(
            f"config 必须是 dict（完整 rules.yaml 或 export 段），当前为 {type(config).__name__}。"
        )
    export_section = _get_export_section(config)

    raw_dir = export_section.get("dir")
    if raw_dir is None:
        raw_dir = config.get("export_dir")
    if raw_dir is not None and not str(raw_dir).strip():
        raw_dir = None
    output_dir = _as_path(raw_dir) if raw_dir else default_export_dir()

    raw_filename = export_section.get("filename") or DEFAULT_FILENAME
    if not isinstance(raw_filename, str):
        raise ExportConfigError(f"配置项 export.filename 必须是字符串，当前为 {raw_filename!r}。")

    image_base_dirs: list[Path] = []
    raw_image_base = export_section.get("image_base_dir") or config.get("base_dir")
    if raw_image_base:
        if isinstance(raw_image_base, (list, tuple)):
            image_base_dirs = [_as_path(item) for item in raw_image_base if item]
        else:
            image_base_dirs = [_as_path(raw_image_base)]

    thumbnail_width = _get_int(
        export_section,
        "thumbnail_width",
        DEFAULT_THUMBNAIL_WIDTH,
        minimum=40,
        maximum=200,
    )
    thumbnail_max_height = _get_int(
        export_section,
        "thumbnail_max_height",
        DEFAULT_THUMBNAIL_MAX_HEIGHT,
        minimum=40,
        maximum=400,
    )

    return ExportSettings(
        output_dir=output_dir,
        filename=raw_filename,
        image_base_dirs=tuple(image_base_dirs),
        thumbnail_width=thumbnail_width,
        thumbnail_max_height=thumbnail_max_height,
        threshold=_get_number(config, "aggregate.shop_threshold", DEFAULT_THRESHOLD, minimum=0.0),
        comment_multiplier=_get_number(
            config, "amount.comment_multiplier", DEFAULT_COMMENT_MULTIPLIER, minimum=0.0
        ),
        caliber_version=_get_version(config, export_section, "caliber_version"),
        config_version=_get_version(config, export_section, "config_version"),
    )
