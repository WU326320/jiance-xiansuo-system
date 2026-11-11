"""账号、会话、邀请与三级权限。

角色只有三级：
- supervisor（主管，唯一）：可授予/收回管理员权限，管理所有账号与邀请；
- admin（管理员）：可改系统设置、更新/回滚版本、生成邀请、停用普通用户；
- user（普通用户）：只能使用任务与复核。
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from workbench.config import WorkbenchConfig
from workbench.db import Database, now_text
from workbench.security import (
    PasswordError,
    hash_password,
    hash_token,
    new_invite_code,
    new_token,
    normalize_username,
    validate_password,
    verify_password,
)

ROLE_SUPERVISOR = "supervisor"
ROLE_ADMIN = "admin"
ROLE_USER = "user"
ROLES = (ROLE_SUPERVISOR, ROLE_ADMIN, ROLE_USER)
ROLE_LABELS = {ROLE_SUPERVISOR: "主管", ROLE_ADMIN: "管理员", ROLE_USER: "普通用户"}


class AuthError(RuntimeError):
    def __init__(self, message: str, *, code: str = "auth_error", status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


class PermissionError_(AuthError):
    def __init__(self, message: str = "无权限"):
        super().__init__(message, code="forbidden", status=403)


def _now() -> datetime:
    return datetime.now()


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _fmt(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _check_password(password: str, min_length: int) -> None:
    try:
        validate_password(password, min_length=min_length)
    except PasswordError as exc:
        raise AuthError(str(exc), code="weak_password") from exc


def public_user(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "username": row["username"],
        "role": row["role"],
        "role_label": ROLE_LABELS.get(row["role"], row["role"]),
        "status": row["status"],
        "must_change_password": bool(row.get("must_change_password")),
        "created_at": row.get("created_at"),
        "last_login_at": row.get("last_login_at"),
    }


@dataclass
class Session:
    token: str
    csrf_token: str
    user: dict[str, Any]


class AuthService:
    def __init__(self, config: WorkbenchConfig, db: Database):
        self.config = config
        self.db = db

    # ------------------------------------------------------------------
    def has_supervisor(self) -> bool:
        return self.db.has_supervisor()

    def create_supervisor(
        self, username: str, password: str, *, source_ip: str = "127.0.0.1"
    ) -> dict[str, Any]:
        name = str(username or "").strip() or "supervisor"
        norm = normalize_username(name)
        if not norm:
            raise AuthError("账号不能为空")
        _check_password(password, self.config.min_password_length)
        now = now_text()
        with self.db.transaction() as connection:
            exists = connection.execute(
                "SELECT COUNT(*) FROM users WHERE role='supervisor'"
            ).fetchone()[0]
            if exists:
                raise AuthError("主管已存在，不能重复初始化", code="already_initialized")
            try:
                cursor = connection.execute(
                    """INSERT INTO users(username, username_norm, role, password_hash,
                        must_change_password, status, created_at, updated_at)
                       VALUES(?,?,?,?,0,'active',?,?)""",
                    (name, norm, ROLE_SUPERVISOR, hash_password(password), now, now),
                )
            except Exception as exc:  # sqlite IntegrityError
                raise AuthError(f"账号已存在或初始化失败：{exc}", code="duplicate") from exc
            user_id = int(cursor.lastrowid)
            connection.execute(
                """INSERT INTO audit_log(actor_id, actor_name, source_ip, action,
                    object_type, object_id, before_json, after_json, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    user_id,
                    name,
                    source_ip,
                    "supervisor.initialize",
                    "user",
                    str(user_id),
                    "{}",
                    '{"role":"supervisor"}',
                    now,
                ),
            )
        return self.get_user(user_id)

    # ------------------------------------------------------------------
    def get_user(self, user_id: int) -> dict[str, Any]:
        row = self.db.fetchone("SELECT * FROM users WHERE id=?", (int(user_id),))
        if row is None:
            raise AuthError("账号不存在", code="not_found", status=404)
        return row

    def list_users(self, actor: dict[str, Any]) -> list[dict[str, Any]]:
        self.require(actor, ROLE_SUPERVISOR, ROLE_ADMIN)
        rows = self.db.fetchall("SELECT * FROM users ORDER BY id")
        return [public_user(row) for row in rows]

    def require(self, actor: dict[str, Any] | None, *roles: str) -> dict[str, Any]:
        if not actor:
            raise AuthError("未登录", code="unauthenticated", status=401)
        if actor.get("status") != "active":
            raise AuthError("账号已停用", code="disabled", status=403)
        if roles and actor.get("role") not in roles:
            raise PermissionError_("当前账号无此权限")
        return actor

    # ------------------------------------------------------------------
    def login(
        self, username: str, password: str, *, source_ip: str = ""
    ) -> Session:
        norm = normalize_username(username)
        now = _now()
        attempt = self.db.fetchone(
            "SELECT * FROM login_attempts WHERE username_norm=?", (norm,)
        )
        if attempt and attempt.get("locked_until"):
            locked = _parse(attempt["locked_until"])
            if locked and locked > now:
                self.db.audit(
                    actor_id=None,
                    actor_name=norm,
                    source_ip=source_ip,
                    action="login.locked",
                )
                raise AuthError(
                    f"连续输错次数过多，请到 {locked:%H:%M} 后再试",
                    code="locked",
                    status=429,
                )
        row = self.db.fetchone("SELECT * FROM users WHERE username_norm=?", (norm,))
        if row is None or row["status"] != "active" or not verify_password(password, row["password_hash"]):
            self._record_failure(norm, source_ip)
            raise AuthError("账号或密码错误", code="bad_credentials", status=401)
        self.db.execute("DELETE FROM login_attempts WHERE username_norm=?", (norm,))
        token = new_token()
        csrf = new_token(24)
        expires = now + timedelta(hours=max(1, self.config.session_idle_hours))
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO sessions(user_id, token_hash, csrf_token, created_at,
                    last_seen_at, expires_at, revoked) VALUES(?,?,?,?,?,?,0)""",
                (row["id"], hash_token(token), csrf, _fmt(now), _fmt(now), _fmt(expires)),
            )
            connection.execute(
                "UPDATE users SET last_login_at=?, updated_at=? WHERE id=?",
                (_fmt(now), _fmt(now), row["id"]),
            )
            connection.execute(
                """INSERT INTO audit_log(actor_id, actor_name, source_ip, action,
                    object_type, object_id, before_json, after_json, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (row["id"], row["username"], source_ip, "login.success", "user", str(row["id"]), "{}", "{}", _fmt(now)),
            )
        row["last_login_at"] = _fmt(now)
        return Session(token=token, csrf_token=csrf, user=public_user(row))

    def _record_failure(self, norm: str, source_ip: str) -> None:
        now = _now()
        attempt = self.db.fetchone(
            "SELECT * FROM login_attempts WHERE username_norm=?", (norm,)
        )
        failures = int(attempt["failures"]) + 1 if attempt else 1
        locked_until = None
        if failures >= self.config.login_lock_threshold:
            locked_until = _fmt(now + timedelta(minutes=max(1, self.config.login_lock_minutes)))
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO login_attempts(username_norm, failures, locked_until, updated_at)
                   VALUES(?,?,?,?)
                   ON CONFLICT(username_norm) DO UPDATE SET failures=excluded.failures,
                       locked_until=excluded.locked_until, updated_at=excluded.updated_at""",
                (norm, failures, locked_until, _fmt(now)),
            )
            connection.execute(
                """INSERT INTO audit_log(actor_id, actor_name, source_ip, action,
                    object_type, object_id, before_json, after_json, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (None, norm, source_ip, "login.failure", "user", "", "{}", '{"reason":"bad_credentials"}', _fmt(now)),
            )

    def authenticate(self, token: str | None) -> dict[str, Any] | None:
        if not token:
            return None
        row = self.db.fetchone(
            """SELECT s.id AS session_id, s.csrf_token, s.last_seen_at, s.expires_at,
                      s.revoked, u.*
               FROM sessions s JOIN users u ON u.id=s.user_id
               WHERE s.token_hash=?""",
            (hash_token(token),),
        )
        if row is None or row["revoked"] or row["status"] != "active":
            return None
        now = _now()
        idle_deadline = (_parse(row["last_seen_at"]) or now) + timedelta(
            hours=max(1, self.config.session_idle_hours)
        )
        if idle_deadline < now or (_parse(row["expires_at"]) or now) < now:
            self.db.execute("UPDATE sessions SET revoked=1 WHERE id=?", (row["session_id"],))
            return None
        deadline = now + timedelta(hours=max(1, self.config.session_idle_hours))
        self.db.execute(
            "UPDATE sessions SET last_seen_at=?, expires_at=? WHERE id=?",
            (_fmt(now), _fmt(deadline), row["session_id"]),
        )
        user = public_user(row)
        user["csrf_token"] = row["csrf_token"]
        user["session_token"] = token
        return user

    def logout(self, token: str | None, *, source_ip: str = "") -> None:
        if not token:
            return
        self.db.execute(
            "UPDATE sessions SET revoked=1 WHERE token_hash=?", (hash_token(token),)
        )
        row = self.db.fetchone(
            "SELECT u.id, u.username FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=?",
            (hash_token(token),),
        )
        if row:
            self.db.audit(
                actor_id=row["id"],
                actor_name=row["username"],
                source_ip=source_ip,
                action="logout",
            )

    def _revoke_sessions(self, connection, user_id: int) -> None:
        connection.execute("UPDATE sessions SET revoked=1 WHERE user_id=?", (user_id,))

    # ------------------------------------------------------------------
    def create_invite(
        self,
        actor: dict[str, Any],
        *,
        role: str = ROLE_USER,
        expires_minutes: int = 1440,
        max_uses: int = 1,
        source_ip: str = "",
    ) -> dict[str, Any]:
        self.require(actor, ROLE_SUPERVISOR, ROLE_ADMIN)
        role = str(role or ROLE_USER)
        if role not in (ROLE_ADMIN, ROLE_USER):
            raise AuthError("邀请角色只能是管理员或普通用户")
        if role == ROLE_ADMIN and actor["role"] != ROLE_SUPERVISOR:
            raise PermissionError_("只有主管可以邀请管理员")
        minutes = max(5, min(int(expires_minutes or 1440), 60 * 24 * 30))
        uses = max(1, min(int(max_uses or 1), 1000))
        code = new_invite_code()
        now = _now()
        expires = now + timedelta(minutes=minutes)
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO invites(code_hash, role, max_uses, used_count, expires_at,
                    created_by, created_at, active) VALUES(?,?,?,0,?,?,?,1)""",
                (hash_token(code), role, uses, _fmt(expires), actor["id"], _fmt(now)),
            )
            connection.execute(
                """INSERT INTO audit_log(actor_id, actor_name, source_ip, action,
                    object_type, object_id, before_json, after_json, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    actor["id"], actor["username"], source_ip, "invite.create", "invite", "",
                    "{}", f'{{"role":"{role}","max_uses":{uses},"expires_at":"{_fmt(expires)}"}}', _fmt(now),
                ),
            )
        return {
            "code": code,
            "role": role,
            "role_label": ROLE_LABELS.get(role, role),
            "max_uses": uses,
            "expires_at": _fmt(expires),
            "register_url": f"/login?invite={code}",
        }

    def list_invites(self, actor: dict[str, Any]) -> list[dict[str, Any]]:
        self.require(actor, ROLE_SUPERVISOR, ROLE_ADMIN)
        rows = self.db.fetchall(
            "SELECT id, role, max_uses, used_count, expires_at, created_at, active FROM invites ORDER BY id DESC"
        )
        for row in rows:
            row["role_label"] = ROLE_LABELS.get(row["role"], row["role"])
            row["expired"] = (_parse(row["expires_at"]) or _now()) < _now()
        return rows

    def revoke_invite(self, actor: dict[str, Any], invite_id: int, *, source_ip: str = "") -> None:
        self.require(actor, ROLE_SUPERVISOR, ROLE_ADMIN)
        with self.db.transaction() as connection:
            cursor = connection.execute("UPDATE invites SET active=0 WHERE id=?", (int(invite_id),))
            if cursor.rowcount == 0:
                raise AuthError("邀请不存在", code="not_found", status=404)
            connection.execute(
                """INSERT INTO audit_log(actor_id, actor_name, source_ip, action,
                    object_type, object_id, before_json, after_json, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (actor["id"], actor["username"], source_ip, "invite.revoke", "invite", str(invite_id), "{}", "{}", now_text()),
            )

    def register(
        self, code: str, username: str, password: str, *, source_ip: str = ""
    ) -> dict[str, Any]:
        norm = normalize_username(username)
        if len(norm) < 2 or len(norm) > 32:
            raise AuthError("账号长度需为 2–32 个字符")
        _check_password(password, self.config.min_password_length)
        now = _now()
        with self.db.transaction() as connection:
            invite = connection.execute(
                "SELECT * FROM invites WHERE code_hash=?", (hash_token(code),)
            ).fetchone()
            if invite is None or not invite["active"]:
                raise AuthError("邀请码无效", code="invalid_invite", status=400)
            if (_parse(invite["expires_at"]) or now) < now:
                raise AuthError("邀请码已过期", code="invite_expired", status=400)
            if int(invite["used_count"]) >= int(invite["max_uses"]):
                raise AuthError("邀请码使用次数已用完", code="invite_used", status=400)
            exists = connection.execute(
                "SELECT 1 FROM users WHERE username_norm=?", (norm,)
            ).fetchone()
            if exists:
                raise AuthError("账号已存在", code="duplicate", status=409)
            cursor = connection.execute(
                """INSERT INTO users(username, username_norm, role, password_hash,
                    must_change_password, status, created_at, updated_at)
                   VALUES(?,?,?,?,0,'active',?,?)""",
                (str(username).strip(), norm, invite["role"], hash_password(password), _fmt(now), _fmt(now)),
            )
            user_id = int(cursor.lastrowid)
            connection.execute(
                "UPDATE invites SET used_count=used_count+1 WHERE id=?", (invite["id"],)
            )
            connection.execute(
                """INSERT INTO audit_log(actor_id, actor_name, source_ip, action,
                    object_type, object_id, before_json, after_json, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (user_id, norm, source_ip, "user.register", "user", str(user_id), "{}", f'{{"role":"{invite["role"]}"}}', _fmt(now)),
            )
        return self.get_user(user_id)

    # ------------------------------------------------------------------
    def disable_user(
        self, actor: dict[str, Any], user_id: int, *, source_ip: str = ""
    ) -> None:
        self.require(actor, ROLE_SUPERVISOR, ROLE_ADMIN)
        target = self.get_user(int(user_id))
        if target["role"] == ROLE_SUPERVISOR:
            raise PermissionError_("不能停用主管")
        if actor["role"] == ROLE_ADMIN and target["role"] == ROLE_ADMIN:
            raise PermissionError_("管理员不能停用其他管理员")
        if target["id"] == actor["id"]:
            raise AuthError("不能停用自己", code="self_disable")
        if target["role"] == ROLE_ADMIN:
            count = self.db.fetchone(
                "SELECT COUNT(*) AS n FROM users WHERE role='admin' AND status='active'"
            )
            if int(count["n"]) <= 1:
                raise AuthError("不能停用最后一个管理员", code="last_admin")
        now = now_text()
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE users SET status='disabled', updated_at=? WHERE id=?",
                (now, target["id"]),
            )
            self._revoke_sessions(connection, target["id"])
            connection.execute(
                """INSERT INTO audit_log(actor_id, actor_name, source_ip, action,
                    object_type, object_id, before_json, after_json, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    actor["id"], actor["username"], source_ip, "user.disable", "user", str(target["id"]),
                    f'{{"status":"{target["status"]}"}}', '{"status":"disabled"}', now,
                ),
            )

    def set_role(
        self, actor: dict[str, Any], user_id: int, role: str, *, source_ip: str = ""
    ) -> None:
        self.require(actor, ROLE_SUPERVISOR)
        if role not in (ROLE_ADMIN, ROLE_USER):
            raise AuthError("角色只能是管理员或普通用户")
        target = self.get_user(int(user_id))
        if target["role"] == ROLE_SUPERVISOR:
            raise PermissionError_("不能修改主管角色")
        if target["role"] == role:
            return
        if target["role"] == ROLE_ADMIN and role == ROLE_USER:
            count = self.db.fetchone(
                "SELECT COUNT(*) AS n FROM users WHERE role='admin' AND status='active'"
            )
            if int(count["n"]) <= 1:
                raise AuthError("不能降级最后一个管理员", code="last_admin")
        now = now_text()
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE users SET role=?, updated_at=? WHERE id=?", (role, now, target["id"])
            )
            connection.execute(
                """INSERT INTO audit_log(actor_id, actor_name, source_ip, action,
                    object_type, object_id, before_json, after_json, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    actor["id"], actor["username"], source_ip, "user.role_change", "user", str(target["id"]),
                    f'{{"role":"{target["role"]}"}}', f'{{"role":"{role}"}}', now,
                ),
            )

    def reset_password(
        self,
        actor: dict[str, Any],
        user_id: int,
        *,
        new_password: str | None = None,
        source_ip: str = "",
    ) -> str:
        self.require(actor, ROLE_SUPERVISOR, ROLE_ADMIN)
        target = self.get_user(int(user_id))
        if target["role"] == ROLE_SUPERVISOR and actor["role"] != ROLE_SUPERVISOR:
            raise PermissionError_("只有主管可以重置主管密码")
        if actor["role"] == ROLE_ADMIN and target["role"] == ROLE_ADMIN and target["id"] != actor["id"]:
            raise PermissionError_("管理员不能重置其他管理员密码")
        password = new_password or ("Tmp" + secrets.token_urlsafe(9) + "9")
        _check_password(password, self.config.min_password_length)
        now = now_text()
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE users SET password_hash=?, must_change_password=1, updated_at=? WHERE id=?",
                (hash_password(password), now, target["id"]),
            )
            self._revoke_sessions(connection, target["id"])
            connection.execute(
                """INSERT INTO audit_log(actor_id, actor_name, source_ip, action,
                    object_type, object_id, before_json, after_json, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (actor["id"], actor["username"], source_ip, "user.reset_password", "user", str(target["id"]), "{}", "{}", now),
            )
        return password

    def change_password(
        self,
        actor: dict[str, Any],
        old_password: str,
        new_password: str,
        *,
        source_ip: str = "",
    ) -> None:
        user = self.get_user(int(actor["id"]))
        if not verify_password(old_password, user["password_hash"]):
            raise AuthError("原密码错误", code="bad_credentials")
        _check_password(new_password, self.config.min_password_length)
        now = now_text()
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE users SET password_hash=?, must_change_password=0, updated_at=? WHERE id=?",
                (hash_password(new_password), now, user["id"]),
            )
            connection.execute(
                """INSERT INTO audit_log(actor_id, actor_name, source_ip, action,
                    object_type, object_id, before_json, after_json, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (user["id"], user["username"], source_ip, "user.change_password", "user", str(user["id"]), "{}", "{}", now),
            )


__all__ = [
    "AuthError",
    "AuthService",
    "PermissionError_",
    "ROLE_ADMIN",
    "ROLE_LABELS",
    "ROLE_SUPERVISOR",
    "ROLE_USER",
    "ROLES",
    "Session",
    "public_user",
]
