"""允许 python -m lead_excel 调用 CLI。"""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
