"""工作台 SQLite 数据库（与 leads.db 分离）。

工作台自己的账号、邀请、IP 档案、任务、邮件日志、审计日志放在
``workbench/data/workbench.db``；线索和复核仍写 backend 的 ``leads.db``。
这样更新/回滚工作台版本时不会碰业务数据。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    username_norm TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL CHECK (role IN ('supervisor','admin','user')),
    password_hash TEXT NOT NULL,
    must_change_password INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_login_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_users_single_supervisor
    ON users(role) WHERE role = 'supervisor';

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    csrf_token TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS login_attempts (
    username_norm TEXT PRIMARY KEY,
    failures INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code_hash TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL CHECK (role IN ('admin','user')),
    max_uses INTEGER NOT NULL DEFAULT 1,
    used_count INTEGER NOT NULL DEFAULT 0,
    expires_at TEXT NOT NULL,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS ip_assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    name_norm TEXT NOT NULL UNIQUE,
    company_code TEXT NOT NULL DEFAULT '',
    company_name TEXT NOT NULL DEFAULT '',
    keywords_json TEXT NOT NULL DEFAULT '[]',
    ref_images_json TEXT NOT NULL DEFAULT '[]',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    created_by INTEGER REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS whitelist_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ip_id INTEGER REFERENCES ip_assets(id) ON DELETE SET NULL,
    scope_type TEXT NOT NULL DEFAULT 'ip' CHECK (scope_type IN ('ip','company')),
    company_code TEXT NOT NULL DEFAULT '',
    platform_group TEXT NOT NULL CHECK (platform_group IN ('taobao','jd','1688')),
    shop_name TEXT NOT NULL,
    shop_name_norm TEXT NOT NULL,
    shop_id TEXT NOT NULL DEFAULT '',
    note TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    deleted_at TEXT,
    created_by INTEGER REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS ix_whitelist_ip ON whitelist_entries(ip_id, active);
CREATE INDEX IF NOT EXISTS ix_whitelist_company ON whitelist_entries(company_code, active);
CREATE INDEX IF NOT EXISTS ix_whitelist_key ON whitelist_entries(platform_group, shop_name_norm);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_no TEXT NOT NULL UNIQUE,
    ip_id INTEGER REFERENCES ip_assets(id) ON DELETE SET NULL,
    ip_name TEXT NOT NULL,
    ip_norm TEXT NOT NULL,
    company_code TEXT NOT NULL DEFAULT '',
    platforms_json TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'formal' CHECK (mode IN ('formal','dry_run')),
    keywords_json TEXT NOT NULL DEFAULT '[]',
    notify_email TEXT NOT NULL DEFAULT '',
    include_company INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued','scheduled','running','success','partial','failed','cancelled')),
    payload_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT NOT NULL DEFAULT '{}',
    run_id TEXT,
    excel_path TEXT,
    error TEXT,
    mail_status TEXT NOT NULL DEFAULT 'pending',
    mail_error TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_tasks_status ON tasks(status, created_at);

CREATE TABLE IF NOT EXISTS scheduled_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_no TEXT NOT NULL UNIQUE REFERENCES tasks(task_no) ON DELETE CASCADE,
    run_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'scheduled'
        CHECK (status IN ('scheduled','running','done','cancelled','failed')),
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS ix_scheduled_due ON scheduled_runs(status, run_at);

CREATE TABLE IF NOT EXISTS mail_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_no TEXT,
    recipients TEXT NOT NULL,
    subject TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('sent','failed','skipped')),
    error TEXT,
    sent_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id INTEGER,
    actor_name TEXT NOT NULL DEFAULT '',
    source_ip TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,
    object_type TEXT NOT NULL DEFAULT '',
    object_id TEXT NOT NULL DEFAULT '',
    before_json TEXT NOT NULL DEFAULT '{}',
    after_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_audit_time ON audit_log(created_at);
CREATE INDEX IF NOT EXISTS ix_audit_actor ON audit_log(actor_name);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by INTEGER
);

CREATE TABLE IF NOT EXISTS versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version TEXT NOT NULL,
    path TEXT NOT NULL,
    digest TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    note TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.path), timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def init_schema(self) -> None:
        connection = self.connect()
        try:
            connection.executescript(SCHEMA)
            connection.commit()
        finally:
            connection.close()

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        with self.transaction() as connection:
            cursor = connection.execute(sql, params)
            return int(cursor.lastrowid or 0)

    def execute_rowcount(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        with self.transaction() as connection:
            cursor = connection.execute(sql, params)
            return int(cursor.rowcount or 0)

    def fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        connection = self.connect()
        try:
            row = connection.execute(sql, params).fetchone()
            return dict(row) if row is not None else None
        finally:
            connection.close()

    def fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        connection = self.connect()
        try:
            return [dict(row) for row in connection.execute(sql, params).fetchall()]
        finally:
            connection.close()

    # ---- 常用封装 ----
    def has_supervisor(self) -> bool:
        row = self.fetchone("SELECT COUNT(*) AS n FROM users WHERE role='supervisor'")
        return bool(row and row["n"])

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self.fetchone("SELECT value FROM settings WHERE key=?", (key,))
        return row["value"] if row else default

    def set_setting(self, key: str, value: str, updated_by: int | None = None) -> None:
        self.execute(
            """
            INSERT INTO settings(key, value, updated_at, updated_by)
            VALUES(?,?,?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                updated_at=excluded.updated_at, updated_by=excluded.updated_by
            """,
            (key, str(value), now_text(), updated_by),
        )

    def audit(
        self,
        *,
        actor_id: int | None,
        actor_name: str,
        source_ip: str,
        action: str,
        object_type: str = "",
        object_id: str = "",
        before: Any = None,
        after: Any = None,
    ) -> None:
        self.execute(
            """
            INSERT INTO audit_log(actor_id, actor_name, source_ip, action,
                object_type, object_id, before_json, after_json, created_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                actor_id,
                actor_name or "",
                source_ip or "",
                action,
                object_type or "",
                str(object_id or ""),
                json.dumps(before or {}, ensure_ascii=False, default=str),
                json.dumps(after or {}, ensure_ascii=False, default=str),
                now_text(),
            ),
        )


__all__ = ["Database", "SCHEMA", "now_text"]
