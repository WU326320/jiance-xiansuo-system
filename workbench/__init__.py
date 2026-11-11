"""内网网页工作台 v1。

工作台只做 Web 层、权限、任务编排和展示，采集/判定/导出继续复用
``backend/pipeline`` 与 ``export/lead_excel`` 的既有实现。
"""

from __future__ import annotations

__version__ = "1.0.0"

__all__ = ["__version__"]
