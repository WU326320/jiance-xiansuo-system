"""部署后自检：环境、数据库、页面、后端模块与配置。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from workbench.config import WorkbenchConfig
from workbench.db import Database
from workbench.updater import VersionManager


def _check(name: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "detail": detail}


def run_selfcheck(config: WorkbenchConfig) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    try:
        host = config.validate_host()
        checks.append(_check("监听地址", True, f"{host}:{config.port}（不会绑定 0.0.0.0）"))
    except Exception as exc:  # noqa: BLE001
        checks.append(_check("监听地址", False, str(exc)))

    try:
        config.ensure_dirs()
        checks.append(_check("数据目录", True, str(config.data_dir)))
    except Exception as exc:  # noqa: BLE001
        checks.append(_check("数据目录", False, str(exc)))

    db = Database(config.db_path)
    try:
        db.init_schema()
        checks.append(_check("工作台数据库", True, str(config.db_path)))
    except Exception as exc:  # noqa: BLE001
        checks.append(_check("工作台数据库", False, str(exc)))

    try:
        VersionManager(config, db)
        checks.append(_check("版本目录", True, str(config.versions_dir)))
    except Exception as exc:  # noqa: BLE001
        checks.append(_check("版本目录", False, str(exc)))

    web = Path(__file__).resolve().parent / "web"
    missing = [name for name in ("index.html", "login.html", "setup.html") if not (web / name).exists()]
    checks.append(_check("页面资源", not missing, "缺少：" + ", ".join(missing) if missing else str(web)))

    try:
        from workbench import backend_bridge as bridge  # noqa: PLC0415

        bridge.ensure_backend_paths()
        import pipeline.config  # noqa: F401,PLC0415
        import pipeline.runner  # noqa: F401,PLC0415
        import lead_excel  # noqa: F401,PLC0415

        checks.append(_check("backend/export 模块", True, "pipeline、lead_excel 可导入"))
    except Exception as exc:  # noqa: BLE001
        checks.append(_check("backend/export 模块", False, f"{type(exc).__name__}: {exc}"))

    try:
        from workbench.app import WorkbenchApp  # noqa: PLC0415

        app = WorkbenchApp(config)
        checks.append(
            _check(
                "应用装配",
                True,
                f"主管已初始化={app.auth.has_supervisor()}，版本={app.versions.current().get('version')}",
            )
        )
    except Exception as exc:  # noqa: BLE001
        checks.append(_check("应用装配", False, f"{type(exc).__name__}: {exc}"))

    try:
        from workbench.mailer import Mailer  # noqa: PLC0415

        status = Mailer(config, db).status()
        detail = f"SMTP {'已配置' if status['configured'] else '未配置'}（{status['host'] or '无主机'}）"
        checks.append(_check("邮件配置", True, detail))
    except Exception as exc:  # noqa: BLE001
        checks.append(_check("邮件配置", False, str(exc)))

    return {"ok": all(item["ok"] for item in checks), "checks": checks, "version": "1.0.0"}


__all__ = ["run_selfcheck"]
