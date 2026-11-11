"""Excel 导出模块接入（/workspace/ip-monitor/export）。

后端主流程只调用导出模块的公开接口
``lead_excel.export_leads_excel(db_path, snapshot_date, config)``，不自行写 xlsx。
数据库路径统一来自配置 ``storage.db_path``（由 :class:`pipeline.config.Settings`
解析，默认 ``data/leads.db``）。

导出模块与后端是两个并列目录。运行 ``python -m pipeline`` 时项目根目录不一定在
``sys.path``，因此这里优先直接 import；失败时把同级的 ``export/`` 加入 ``sys.path``
后重试。导出模块自身抛出的 ``ExportError`` 子类不在本模块吞掉，交由 runner 记
``run_logs`` 并走第 7 节报警。
"""

from __future__ import annotations

import importlib
import sys
from datetime import date
from pathlib import Path

from pipeline.config import BACKEND_ROOT, Settings

EXPORT_DIR = BACKEND_ROOT.parent / "export"


def _import_export_function():
    try:
        module = importlib.import_module("lead_excel")
    except ModuleNotFoundError:
        export_dir = str(EXPORT_DIR)
        if export_dir not in sys.path:
            sys.path.insert(0, export_dir)
        importlib.invalidate_caches()
        module = importlib.import_module("lead_excel")
    function = getattr(module, "export_leads_excel", None)
    if function is None:  # pragma: no cover - 仅在导出模块损坏时出现
        raise ImportError(f"导出模块 {EXPORT_DIR} 未提供 export_leads_excel()")
    return function


def export_leads_for_run(session, settings: Settings, run_date: date) -> str:
    """调用导出模块；``session`` 仅用于保持 runner exporter 钩子签名一致。"""

    config = dict(settings.raw)
    # 视图与 Excel 口径说明使用与 leads 记录相同的版本号。
    config["config_version"] = settings.config_version
    config["caliber_version"] = settings.caliber_version
    # 导出模块也读取 storage.db_path；这里显式传解析后的绝对路径，避免 cwd 漂移。
    storage = config.get("storage")
    storage = dict(storage) if isinstance(storage, dict) else {}
    storage["db_path"] = str(settings.database_path)
    config["storage"] = storage

    export_leads_excel = _import_export_function()
    return export_leads_excel(
        db_path=str(settings.database_path),
        snapshot_date=run_date.isoformat(),
        config=config,
    )


__all__ = ["EXPORT_DIR", "export_leads_for_run"]
