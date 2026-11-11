"""复用 backend / export 既有实现。

工作台不复制判定、白名单、导出逻辑，只在 Web 层调用它们。导入前把
``backend/`` 与 ``backend/.deps``（Linux 开发容器的离线依赖目录）加入
``sys.path``；Windows 上使用 ``backend\\.venv`` 的已安装依赖。
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from workbench.config import PROJECT_ROOT, WorkbenchConfig

_BACKEND = PROJECT_ROOT / "backend"
_DEPS = _BACKEND / ".deps"
_EXPORT = PROJECT_ROOT / "export"


def ensure_backend_paths() -> None:
    for path in (_EXPORT, _BACKEND, _DEPS):
        if path.is_dir() and str(path) not in sys.path:
            sys.path.insert(0, str(path))


def load_backend_settings(
    config: WorkbenchConfig, *, db_path: Path | None = None
) -> Any:
    ensure_backend_paths()
    from pipeline.config import load_config  # noqa: PLC0415

    settings = load_config(config.rules_path)
    raw = settings.raw
    storage = raw.get("storage")
    storage = dict(storage) if isinstance(storage, dict) else {}
    target = db_path or config.leads_db_path
    if target is not None:
        storage["db_path"] = str(Path(target).expanduser().resolve())
        raw["storage"] = storage
    if config.crawler_config_path is not None:
        paths = raw.get("paths")
        paths = dict(paths) if isinstance(paths, dict) else {}
        paths["crawler_config"] = str(
            Path(config.crawler_config_path).expanduser().resolve()
        )
        raw["paths"] = paths
    return settings


@contextmanager
def leads_engine(settings: Any) -> Iterator[Any]:
    ensure_backend_paths()
    from pipeline.db import create_engine_for  # noqa: PLC0415

    engine = create_engine_for(settings)
    try:
        yield engine
    finally:
        engine.dispose()


@contextmanager
def leads_session(settings: Any) -> Iterator[Any]:
    ensure_backend_paths()
    from pipeline.db import create_engine_for, make_session_factory  # noqa: PLC0415

    engine = create_engine_for(settings)
    factory = make_session_factory(engine)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
        engine.dispose()


def ensure_leads_schema(settings: Any) -> None:
    ensure_backend_paths()
    from pipeline.db import ensure_schema  # noqa: PLC0415

    ensure_schema(settings)


def shopkey_module():
    ensure_backend_paths()
    import pipeline.shopkey as module  # noqa: PLC0415

    return module


def decode_shop_name(value: object | None) -> str | None:
    return shopkey_module().decode_shop_name(value)


def normalize_ip(value: object | None) -> str:
    return shopkey_module().normalize_ip_name(value)


def normalize_shop(value: object | None) -> str:
    return shopkey_module().normalize_shop_name(value)


def platform_group(value: str | None) -> str:
    return shopkey_module().platform_group(value)


def shop_key(platform: str | None, shop_name: object | None) -> str:
    return shopkey_module().shop_key(platform, shop_name)


def whitelist_sentinel(shop_name: object | None) -> str:
    return shopkey_module().whitelist_sentinel(shop_name)


def build_run_configs(
    settings: Any,
    *,
    ip_name: str,
    ip_code: str,
    keywords: list[str],
    whitelist_path: Path,
    output_dir: Path,
    crawler_config_path: Path | None = None,
) -> tuple[Path, Path]:
    ensure_backend_paths()
    from pipeline.run_entry import build_run_configs as builder  # noqa: PLC0415

    return builder(
        settings,
        ip_name=ip_name,
        ip_code=ip_code,
        keywords=keywords,
        whitelist_path=whitelist_path,
        output_dir=output_dir,
        crawler_config_path=crawler_config_path,
    )


def pipeline_runner(settings: Any, **kwargs: Any) -> Any:
    ensure_backend_paths()
    from pipeline.runner import PipelineRunner  # noqa: PLC0415

    return PipelineRunner(settings, **kwargs)


def export_leads(
    settings: Any, snapshot_date: str, *, output_dir: Path | None = None
) -> str:
    ensure_backend_paths()
    from lead_excel import export_leads_excel  # noqa: PLC0415

    raw = dict(settings.raw)
    storage = raw.get("storage")
    storage = dict(storage) if isinstance(storage, dict) else {}
    storage["db_path"] = str(settings.database_path)
    raw["storage"] = storage
    if output_dir is not None:
        raw["export"] = {**dict(raw.get("export") or {}), "dir": str(output_dir)}
    return export_leads_excel(
        db_path=str(settings.database_path),
        snapshot_date=str(snapshot_date),
        config=raw,
    )


def import_review_excel(
    session: Any, path: str | Path, *, operator: str = "Excel复核"
) -> Any:
    ensure_backend_paths()
    from pipeline.review import import_review_excel as importer  # noqa: PLC0415

    return importer(session, path, operator_fallback=operator)


@contextmanager
def with_env(updates: dict[str, str | None]) -> Iterator[None]:
    """临时修改环境变量，退出时恢复。"""

    old = {key: os.environ.get(key) for key in updates}
    try:
        for key, value in updates.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = str(value)
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


__all__ = [
    "build_run_configs",
    "decode_shop_name",
    "ensure_backend_paths",
    "ensure_leads_schema",
    "export_leads",
    "import_review_excel",
    "leads_engine",
    "leads_session",
    "load_backend_settings",
    "normalize_ip",
    "normalize_shop",
    "pipeline_runner",
    "platform_group",
    "shop_key",
    "shopkey_module",
    "whitelist_sentinel",
    "with_env",
]
