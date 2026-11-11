"""命令行入口：python -m workbench <command>。"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import sys
import threading
import webbrowser
from pathlib import Path
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer

from workbench import __version__
from workbench.app import WorkbenchApp
from workbench.auth import AuthError, AuthService
from workbench.config import ConfigError, WorkbenchConfig
from workbench.db import Database
from workbench.selfcheck import run_selfcheck
from workbench.updater import UpdateError, VersionManager


class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True
    allow_reuse_address = True


def _config(args: argparse.Namespace) -> WorkbenchConfig:
    return WorkbenchConfig.load(getattr(args, "config", None))


def cmd_init_db(args: argparse.Namespace) -> int:
    config = _config(args)
    config.ensure_dirs()
    db = Database(config.db_path)
    db.init_schema()
    VersionManager(config, db)
    print(f"工作台数据库已初始化：{config.db_path}")
    return 0


def cmd_set_supervisor(args: argparse.Namespace) -> int:
    config = _config(args)
    db = Database(config.db_path)
    db.init_schema()
    auth = AuthService(config, db)
    if auth.has_supervisor():
        print("主管已存在，不能重复初始化。如需重置请使用账号管理/数据库维护。")
        return 1
    username = args.username or input("主管账号（默认 supervisor）：").strip() or "supervisor"
    password = args.password
    if password is None:
        password = getpass.getpass("设置主管密码（至少 8 位，含字母和数字）：")
        confirm = getpass.getpass("再次输入密码：")
        if password != confirm:
            print("两次输入不一致")
            return 2
    try:
        user = auth.create_supervisor(username, password, source_ip="127.0.0.1")
    except (AuthError, ValueError) as exc:
        print(f"初始化失败：{exc}")
        return 1
    print(f"主管已创建：{user['username']}（密码只保存哈希）")
    return 0


def cmd_create_invite(args: argparse.Namespace) -> int:
    config = _config(args)
    db = Database(config.db_path)
    db.init_schema()
    auth = AuthService(config, db)
    row = db.fetchone("SELECT * FROM users WHERE role='supervisor' LIMIT 1")
    if row is None:
        print("尚未初始化主管，请先运行 python -m workbench set-supervisor")
        return 1
    actor = dict(row)
    try:
        invite = auth.create_invite(
            actor,
            role=args.role,
            expires_minutes=args.expires_minutes,
            max_uses=args.max_uses,
            source_ip="127.0.0.1",
        )
    except AuthError as exc:
        print(f"生成失败：{exc}")
        return 1
    print(f"邀请码：{invite['code']}")
    print(f"注册链接：http://{config.host}:{config.port}{invite['register_url']}")
    print(f"角色：{invite['role_label']}，次数：{invite['max_uses']}，到期：{invite['expires_at']}")
    return 0


def cmd_selfcheck(args: argparse.Namespace) -> int:
    config = _config(args)
    result = run_selfcheck(config)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for item in result["checks"]:
            mark = "OK" if item["ok"] else "FAIL"
            print(f"[{mark}] {item['name']}: {item['detail']}")
        print(f"自检结果：{'通过' if result['ok'] else '未通过'}")
    return 0 if result["ok"] else 1


def cmd_serve(args: argparse.Namespace) -> int:
    config = _config(args)
    if args.host:
        config.host = args.host
    if args.port:
        config.port = int(args.port)
    host = config.validate_host()
    config.ensure_dirs()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(config.logs_dir / "workbench.log", encoding="utf-8"),
        ],
    )
    app = WorkbenchApp(config)
    app.scheduler.start()
    server = ThreadingWSGIServer((host, config.port), WSGIRequestHandler)
    server.set_app(app)
    url_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    url = f"http://{url_host}:{config.port}/"
    print(f"侵权线索工作台 v{__version__} 已启动：{url}")
    print(f"监听：{host}:{config.port}；数据目录：{config.data_dir}")
    if not app.auth.has_supervisor():
        print("尚未初始化主管：请在本机打开 /setup 设置密码（只允许 127.0.0.1）。")
    if args.open or config.open_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("正在停止……")
    finally:
        app.scheduler.stop()
        server.server_close()
    return 0


def cmd_tick(args: argparse.Namespace) -> int:
    config = _config(args)
    app = WorkbenchApp(config)
    executed = app.tasks.tick()
    print(f"本次到点执行：{', '.join(executed) if executed else '无'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="workbench", description="侵权线索内网工作台 v1")
    parser.add_argument("--config", help="workbench.yaml 路径")
    parser.add_argument("--version", action="version", version=f"workbench {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init-db", help="初始化工作台数据库")
    init.set_defaults(func=cmd_init_db)

    supervisor = sub.add_parser("set-supervisor", help="首次启动设置唯一主管密码")
    supervisor.add_argument("--username", help="主管账号，默认 supervisor")
    supervisor.add_argument("--password", help="仅用于自动化/测试；建议交互输入")
    supervisor.set_defaults(func=cmd_set_supervisor)

    invite = sub.add_parser("create-invite", help="生成邀请码")
    invite.add_argument("--role", choices=["admin", "user"], default="user")
    invite.add_argument("--expires-minutes", type=int, default=1440)
    invite.add_argument("--max-uses", type=int, default=1)
    invite.set_defaults(func=cmd_create_invite)

    check = sub.add_parser("selfcheck", help="部署后自检")
    check.add_argument("--json", action="store_true")
    check.set_defaults(func=cmd_selfcheck)

    serve = sub.add_parser("serve", help="启动 Web 服务")
    serve.add_argument("--host", help="监听地址，默认 127.0.0.1")
    serve.add_argument("--port", type=int, help="监听端口，默认 8765")
    serve.add_argument("--open", action="store_true", help="启动后打开浏览器")
    serve.set_defaults(func=cmd_serve)

    tick = sub.add_parser("tick", help="手动执行一次到点任务（调试）")
    tick.set_defaults(func=cmd_tick)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ConfigError as exc:
        print(f"配置错误：{exc}")
        return 2
    except UpdateError as exc:
        print(f"版本操作失败：{exc}")
        return 1
    except AuthError as exc:
        print(f"权限/参数错误：{exc}")
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
