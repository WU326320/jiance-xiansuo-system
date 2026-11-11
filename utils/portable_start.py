"""便携交付入口：固定使用本包数据路径，默认通过 Edge 打开。"""

from __future__ import annotations

import os
from pathlib import Path
import socket
import subprocess
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]


def main():
    os.chdir(ROOT)
    for key in (
        "IP_LEAD_DB_URL",
        "WORKBENCH_CONFIG",
        "WORKBENCH_HOST",
        "WORKBENCH_PORT",
    ):
        os.environ.pop(key, None)
    os.environ["WORKBENCH_DATA_DIR"] = str(ROOT / "workbench/data")
    os.environ["IP_LEAD_DB_PATH"] = str(ROOT / "backend/data/leads.db")
    args = sys.argv[1:] or ["serve"]
    if args == ["--version"]:
        from workbench.cli import main as cli

        return cli(args)
    from workbench.config import WorkbenchConfig

    config = WorkbenchConfig.load()
    if args[0] == "serve":
        # Explicit --port remains available for an isolated acceptance instance.
        port = int(args[args.index("--port") + 1]) if "--port" in args else config.port
        with socket.socket() as sock:
            if sock.connect_ex((config.host, port)) == 0:
                print(
                    f"端口 {port} 已占用，请先关闭原工作台，或使用 --port 指定独立端口。"
                )
                return 2
        # Finish first-run migration before concurrent HTTP requests are accepted.
        from workbench.backend_bridge import load_backend_settings, ensure_leads_schema

        ensure_leads_schema(load_backend_settings(config))
        args = [arg for arg in args if arg != "--open"]
        if not os.environ.get("WORKBENCH_NO_BROWSER"):
            candidates = [
                Path(os.environ.get(key, "")) / "Microsoft/Edge/Application/msedge.exe"
                for key in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA")
            ]
            edge = next((path for path in candidates if path.is_file()), None)
            if edge:
                timer = threading.Timer(
                    1.5,
                    lambda: subprocess.Popen(
                        [str(edge), f"http://{config.host}:{port}/"]
                    ),
                )
                timer.daemon = True
                timer.start()
        print(
            f"便携版源码：{ROOT}\n数据：{config.data_dir}\n默认访问：http://{config.host}:{port}/",
            flush=True,
        )
    from workbench.launcher import main as launch

    return launch(args)


if __name__ == "__main__":
    raise SystemExit(main())
