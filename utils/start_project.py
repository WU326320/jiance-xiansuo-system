"""Start the copied workbench using only paths inside this project."""

from __future__ import annotations

import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    os.chdir(ROOT)
    for name in (
        "IP_LEAD_DB_URL",
        "IP_LEAD_DB_PATH",
        "IP_LEAD_CRAWLER_DIR",
        "IP_LEAD_CRAWLER_CONFIG",
        "WORKBENCH_CONFIG",
        "WORKBENCH_DATA_DIR",
        "WORKBENCH_HOST",
        "WORKBENCH_PORT",
    ):
        os.environ.pop(name, None)

    from workbench.backend_bridge import ensure_leads_schema, load_backend_settings
    from workbench.cli import main as cli_main
    from workbench.config import WorkbenchConfig

    config = WorkbenchConfig.load()
    ensure_leads_schema(load_backend_settings(config))
    return cli_main(["serve", "--open", *sys.argv[1:]])


if __name__ == "__main__":
    raise SystemExit(main())
