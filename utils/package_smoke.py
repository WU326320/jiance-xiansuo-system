"""对指定便携目录进行真实启动和Edge验收，生成数据移至独立验收目录。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
from urllib.request import urlopen

from playwright.sync_api import sync_playwright, expect


def main(root):
    root = root.resolve()
    python = root / "runtime/python.exe"
    assert python.is_file()
    assert not (root / "workbench/data").exists(), "验收只允许使用无业务数据的新副本"
    env = dict(os.environ, WORKBENCH_NO_BROWSER="1", PYTHONDONTWRITEBYTECODE="1")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    checks = []
    artifacts = root.parent / (root.name + "_启动验收数据")
    if artifacts.exists():
        artifacts = artifacts.with_name(artifacts.name + "_" + time.strftime("%H%M%S"))
    artifacts.mkdir()
    with tempfile.TemporaryFile() as log:
        proc = subprocess.Popen(
            [
                str(python),
                "-B",
                "-X",
                "utf8",
                str(root / "utils/portable_start.py"),
                "serve",
                "--port",
                str(port),
            ],
            cwd=root,
            env=env,
            stdout=log,
            stderr=log,
        )
        try:
            for _ in range(200):
                if proc.poll() is not None:
                    log.seek(0)
                    raise AssertionError(log.read().decode("utf-8", "replace"))
                try:
                    with urlopen(base + "/api/bootstrap", timeout=1) as res:
                        assert json.load(res)["bootstrap_required"] is True
                    break
                except OSError:
                    time.sleep(0.1)
            else:
                raise AssertionError("服务启动超时")
            checks.append("便携入口真实启动及首次初始化状态")
            duplicate = subprocess.run(
                [
                    str(python),
                    "-B",
                    "-X",
                    "utf8",
                    str(root / "utils/portable_start.py"),
                    "serve",
                    "--port",
                    str(port),
                ],
                cwd=root,
                env=env,
                capture_output=True,
                timeout=20,
            )
            assert duplicate.returncode == 2
            checks.append("端口冲突拒绝启动且不连接未知服务")
            errors = []
            with sync_playwright() as p:
                browser = p.chromium.launch(channel="msedge", headless=True)
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                page.on("pageerror", lambda err: errors.append(str(err)))
                page.goto(base + "/setup")
                page.locator('[name="password"]').fill("PackageCheck1234")
                page.locator('[name="confirm"]').fill("PackageCheck1234")
                page.locator('button[type="submit"]').click()
                expect(page.locator(".page-head h2")).to_have_text("线索复核")
                expect(page.locator("#material-toggle")).to_be_visible()
                assert page.request.get(base + "/api/leads").ok
                for relative in ("app.js", "style.css"):
                    response = page.request.get(base + "/static/" + relative)
                    assert response.ok
                    assert (
                        hashlib.sha256(response.body()).digest()
                        == hashlib.sha256(
                            (root / "workbench/web" / relative).read_bytes()
                        ).digest()
                    )
                page.screenshot(path=str(artifacts / "真实启动.png"))
                assert not errors, errors
                browser.close()
            checks.append("Edge真实初始化登录、线索API及当前玻璃版静态资源哈希一致")
            checked = subprocess.run(
                [
                    str(python),
                    "-B",
                    "-X",
                    "utf8",
                    str(root / "utils/portable_start.py"),
                    "selfcheck",
                    "--json",
                ],
                cwd=root,
                env=env,
                capture_output=True,
                timeout=30,
            )
            assert checked.returncode == 0, checked.stdout.decode("utf-8", "replace")
            status = json.loads(checked.stdout.decode("utf-8"))
            assert status["ok"] is True
            checks.append("包内正式selfcheck全部通过")
        finally:
            proc.terminate()
            proc.wait(timeout=15)
            log.seek(0)
            (artifacts / "service.log").write_bytes(log.read())
            for relative in ("workbench/data", "backend/data"):
                if (root / relative).is_dir():
                    destination = artifacts / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(root / relative), str(destination))
    report = {
        "ok": True,
        "scope": "便携目录实际启动验收，不代表全量历史测试通过",
        "checks": checks,
        "root": str(root),
        "port": port,
        "page_errors": errors,
        "generated_data_moved_to": str(artifacts),
    }
    (artifacts / "结果.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("folder", type=Path)
    main(parser.parse_args().folder)
