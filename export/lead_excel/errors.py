"""导出模块异常类型。

异常信息会进入主流程的 run_logs.error_summary，因此保持中文、可操作、不吞异常。
"""

from __future__ import annotations


class ExportError(Exception):
    """导出模块所有可预期异常的基类。"""


class ExportConfigError(ExportError):
    """配置错误：日期、输出目录、文件名、环境变量等。"""


class ExportDatabaseError(ExportError):
    """数据库 / 视图 / 列结构错误。"""


class ExportWriteError(ExportError):
    """写 xlsx 失败（目录权限、文件被 Excel 占用等）。"""


class ExportDataError(ExportError):
    """视图数据无法安全写入 Excel。"""
