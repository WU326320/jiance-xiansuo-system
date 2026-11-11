"""淘宝 / 京东 / 1688 商品采集模块。

对外接口：每个平台模块实现 ``check_login()`` / ``search()`` / ``fetch_detail()``。
CLI 入口：``python -m crawler``。
"""

__version__ = "0.1.0"

import sys
from pathlib import Path

_project_root = str(Path(__file__).resolve().parents[2])
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
