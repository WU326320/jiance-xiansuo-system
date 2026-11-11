"""工作台 WSGI 应用（标准库，无额外 Web 框架依赖）。

页面与接口都要求登录；只有登录、首次初始化、注册和静态资源公开。
所有写接口校验 CSRF 令牌；权限在后端逐接口校验，不依赖前端隐藏按钮。
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import re
import traceback
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, quote, unquote, urlparse

from workbench.auth import (
    AuthError,
    AuthService,
    PermissionError_,
    ROLE_ADMIN,
    ROLE_LABELS,
    ROLE_SUPERVISOR,
    ROLE_USER,
    public_user,
)
from workbench.config import WorkbenchConfig
from workbench.db import Database
from workbench.leads import LeadsService, STATUS_SAAS, VALID_RESULTS
from workbench.mailer import Mailer
from workbench.security import ip_allowed, is_loopback
from workbench.tasks import Scheduler, TaskService, validate_email
from workbench.updater import UpdateError, VersionManager
from workbench.whitelist import IPService, entry_row_to_dict

logger = logging.getLogger("workbench.app")

COOKIE_NAME = "wb_session"
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_UPDATE_BYTES = 64 * 1024 * 1024

_SETTING_KEYS = {
    "notify_email_default",
    "company_whitelist_default",
    "allowed_networks",
    "smtp_host",
    "smtp_port",
    "smtp_ssl",
    "smtp_from",
    "smtp_to",
    "smtp_keyring_service",
    "session_idle_hours",
    "login_lock_threshold",
    "login_lock_minutes",
    "export_dir",
}


@dataclass
class Request:
    method: str
    path: str
    query: dict[str, list[str]]
    headers: dict[str, str]
    cookies: dict[str, str]
    body: bytes
    remote_addr: str
    user: dict[str, Any] | None = None

    def json(self) -> dict[str, Any]:
        if not self.body:
            return {}
        if len(self.body) > MAX_JSON_BYTES:
            raise AuthError("请求体过大", code="too_large", status=413)
        try:
            data = json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise AuthError("请求体不是合法 JSON", code="bad_json") from exc
        if not isinstance(data, dict):
            raise AuthError("请求体必须是 JSON 对象", code="bad_json")
        return data

    def q(self, name: str, default: str = "") -> str:
        values = self.query.get(name) or []
        return values[0] if values else default

    def header(self, name: str, default: str = "") -> str:
        return self.headers.get(name.lower(), default)


@dataclass
class Response:
    status: int = 200
    body: bytes = b""
    headers: list[tuple[str, str]] = field(default_factory=list)

    @classmethod
    def json(cls, data: Any, status: int = 200) -> "Response":
        payload = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        return cls(
            status, payload, [("Content-Type", "application/json; charset=utf-8")]
        )

    @classmethod
    def text(cls, text: str, status: int = 200) -> "Response":
        return cls(
            status,
            text.encode("utf-8"),
            [("Content-Type", "text/plain; charset=utf-8")],
        )

    @classmethod
    def html(cls, text: str, status: int = 200) -> "Response":
        return cls(
            status, text.encode("utf-8"), [("Content-Type", "text/html; charset=utf-8")]
        )

    @classmethod
    def redirect(cls, location: str, status: int = 302) -> "Response":
        return cls(status, b"", [("Location", location)])

    def cookie(
        self, token: str, *, max_age: int = 8 * 3600, clear: bool = False
    ) -> "Response":
        value = "" if clear else token
        age = 0 if clear else max_age
        self.headers.append(
            (
                "Set-Cookie",
                f"{COOKIE_NAME}={value}; Path=/; HttpOnly; SameSite=Lax; Max-Age={age}",
            )
        )
        return self


class WorkbenchApp:
    def __init__(
        self,
        config: WorkbenchConfig,
        *,
        db: Database | None = None,
        mailer: Mailer | None = None,
    ):
        config.ensure_dirs()
        self.config = config
        self.db = db or Database(config.db_path)
        self.db.init_schema()
        self.auth = AuthService(config, self.db)
        self.mailer = mailer or Mailer(config, self.db)
        self.ips = IPService(config, self.db)
        self.leads = LeadsService(config, self.db)
        self.tasks = TaskService(config, self.db, mailer=self.mailer)
        self.versions = VersionManager(config, self.db)
        self.scheduler = Scheduler(self.tasks)
        self.web_dir = Path(__file__).resolve().parent / "web"

    # ------------------------------------------------------------------
    def __call__(self, environ: dict[str, Any], start_response: Callable[..., Any]):
        try:
            response = self._handle(environ)
        except AuthError as exc:
            response = self._auth_error(exc)
        except Exception:  # noqa: BLE001
            logger.error("请求处理失败\n%s", traceback.format_exc())
            response = Response.json(
                {"error": "服务器内部错误", "code": "internal"}, 500
            )
        response.headers.append(("X-Content-Type-Options", "nosniff"))
        response.headers.append(("X-Frame-Options", "DENY"))
        response.headers.append(("Referrer-Policy", "same-origin"))
        response.headers.append(("Cache-Control", "no-store"))
        status_line = f"{response.status} {HTTPStatus(response.status).phrase}"
        start_response(status_line, response.headers)
        return [response.body]

    def _auth_error(self, exc: AuthError) -> Response:
        path = getattr(exc, "path", "")
        if exc.status == 401 and path and not path.startswith("/api/"):
            return Response.redirect("/login")
        return Response.json({"error": str(exc), "code": exc.code}, exc.status)

    # ------------------------------------------------------------------
    def _handle(self, environ: dict[str, Any]) -> Response:
        method = str(environ.get("REQUEST_METHOD") or "GET").upper()
        parsed = urlparse(str(environ.get("PATH_INFO") or "/"))
        path = parsed.path or "/"
        query = parse_qs(str(environ.get("QUERY_STRING") or ""))
        headers = {
            key[5:].replace("_", "-").lower(): value
            for key, value in environ.items()
            if key.startswith("HTTP_")
        }
        if environ.get("CONTENT_TYPE"):
            headers["content-type"] = str(environ["CONTENT_TYPE"])
        cookies = _parse_cookies(str(environ.get("HTTP_COOKIE") or ""))
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            length = 0
        body = environ["wsgi.input"].read(length) if length else b""
        remote = str(environ.get("REMOTE_ADDR") or "")
        request = Request(method, path, query, headers, cookies, body, remote)

        if not ip_allowed(remote, self.config.allowed_networks):
            self.db.audit(
                actor_id=None,
                actor_name="",
                source_ip=remote,
                action="access.denied",
                object_type="network",
                object_id=path,
            )
            return Response.json(
                {"error": "来源 IP 不在允许的内网网段", "code": "network_denied"}, 403
            )

        # 公开路由：登录/初始化/注册流程和静态资源。
        if path == "/api/bootstrap":
            return Response.json({"bootstrap_required": not self.auth.has_supervisor()})
        if path == "/login" and method == "GET":
            return self._page("login.html")
        if path == "/setup" and method == "GET":
            return self._setup_page(request)
        if path == "/api/setup" and method == "POST":
            return self._setup(request)
        if path == "/api/login" and method == "POST":
            return self._login(request)
        if path == "/api/register" and method == "POST":
            return self._register(request)
        if path.startswith("/static/"):
            return self._static(path)

        # 页面/接口登录校验
        token = request.cookies.get(COOKIE_NAME)
        user = self.auth.authenticate(token)
        if path == "/":
            if not self.auth.has_supervisor():
                return Response.redirect("/setup")
            if user is None:
                return Response.redirect("/login")
            return self._page("index.html")
        if user is None:
            raise _with_path(
                AuthError("未登录", code="unauthenticated", status=401), path
            )
        request.user = user
        if method in {"POST", "PUT", "PATCH", "DELETE"}:
            csrf = request.header("x-csrf-token")
            if csrf != user.get("csrf_token"):
                raise AuthError("CSRF 校验失败", code="csrf", status=403)
        return self._dispatch(request)

    # ------------------------------------------------------------------
    def _page(self, name: str) -> Response:
        path = self.web_dir / name
        if not path.exists():
            return Response.text(f"缺少页面 {name}", 500)
        return Response.html(path.read_text(encoding="utf-8"))

    def _static(self, path: str) -> Response:
        relative = path[len("/static/") :]
        target = (self.web_dir / relative).resolve()
        if (
            not str(target).startswith(str(self.web_dir.resolve()))
            or not target.is_file()
        ):
            return Response.text("Not Found", 404)
        content_type = (
            mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        )
        return Response(200, target.read_bytes(), [("Content-Type", content_type)])

    def _setup_page(self, request: Request) -> Response:
        if self.auth.has_supervisor():
            return Response.redirect("/login")
        if not is_loopback(request.remote_addr):
            return Response.html("<h1>首次初始化只能在本机 127.0.0.1 完成</h1>", 403)
        return self._page("setup.html")

    # ------------------------------------------------------------------
    def _setup(self, request: Request) -> Response:
        if self.auth.has_supervisor():
            raise AuthError("主管已存在", code="already_initialized")
        if not is_loopback(request.remote_addr):
            raise PermissionError_("首次初始化只能在本机 127.0.0.1 完成")
        data = request.json()
        if str(data.get("password") or "") != str(data.get("confirm") or ""):
            raise AuthError("两次密码不一致")
        user = self.auth.create_supervisor(
            str(data.get("username") or "supervisor"),
            str(data.get("password") or ""),
            source_ip=request.remote_addr,
        )
        session = self.auth.login(
            user["username"], str(data["password"]), source_ip=request.remote_addr
        )
        return self._session_response(session)

    def _login(self, request: Request) -> Response:
        data = request.json()
        session = self.auth.login(
            str(data.get("username") or ""),
            str(data.get("password") or ""),
            source_ip=request.remote_addr,
        )
        return self._session_response(session)

    def _register(self, request: Request) -> Response:
        data = request.json()
        if str(data.get("password") or "") != str(data.get("confirm") or ""):
            raise AuthError("两次密码不一致")
        user = self.auth.register(
            str(data.get("code") or ""),
            str(data.get("username") or ""),
            str(data.get("password") or ""),
            source_ip=request.remote_addr,
        )
        session = self.auth.login(
            user["username"], str(data["password"]), source_ip=request.remote_addr
        )
        return self._session_response(session)

    def _session_response(self, session: Any) -> Response:
        response = Response.json(
            {
                "user": session.user,
                "csrf_token": session.csrf_token,
                "bootstrap_required": False,
            }
        )
        response.cookie(
            session.token, max_age=max(1, self.config.session_idle_hours) * 3600
        )
        return response

    # ------------------------------------------------------------------
    def _dispatch(self, request: Request) -> Response:
        method, path, user = request.method, request.path, request.user
        assert user is not None
        if path == "/api/health" and method == "GET":
            return Response.json(
                {"ok": True, "version": self.versions.current().get("version")}
            )
        if path == "/api/me" and method == "GET":
            return Response.json(
                {
                    "user": user,
                    "csrf_token": user.get("csrf_token"),
                    "roles": ROLE_LABELS,
                    "bootstrap_required": False,
                    "version": self.versions.current().get("version"),
                }
            )
        if path == "/api/logout" and method == "POST":
            self.auth.logout(
                request.cookies.get(COOKIE_NAME), source_ip=request.remote_addr
            )
            return Response.json({"ok": True}).cookie("", clear=True)
        if path == "/api/leads" and method == "GET":
            return Response.json(self.leads.list_leads(_filters(request)))
        match = re.fullmatch(r"/api/leads/([^/]+)", path)
        if match and method == "GET":
            return Response.json(self.leads.get_lead(unquote(match.group(1))))
        if path == "/api/leads/review" and method == "POST":
            data = request.json()
            result = self.leads.review(
                data.get("lead_nos") or [],
                str(data.get("result") or ""),
                reviewer=str(data.get("reviewer") or user["username"]),
                actor=user,
                confirm_text=str(data.get("confirm_text") or ""),
                source_ip=request.remote_addr,
            )
            return Response.json(result)
        if path == "/api/export" and method == "POST":
            data = request.json()
            return Response.json(
                self.leads.export(str(data.get("snapshot_date") or "") or None)
            )

        # ---- 任务 ----
        if path == "/api/tasks" and method == "GET":
            return Response.json({"items": self.tasks.list_tasks()})
        if path == "/api/tasks/preview-ip" and method == "POST":
            data = request.json()
            return Response.json(self.tasks.preview_ip(str(data.get("name") or "")))
        if path == "/api/tasks" and method == "POST":
            data = request.json()
            sync = bool(data.pop("_sync", False))
            task = self.tasks.create_task(
                data, actor=user, source_ip=request.remote_addr, run_now=False
            )
            if task["status"] == "queued":
                if sync:
                    task = self.tasks.execute(task["task_no"])
                else:
                    import threading  # noqa: PLC0415

                    threading.Thread(
                        target=self._run_task_safely,
                        args=(task["task_no"],),
                        daemon=True,
                        name=f"task-{task['task_no']}",
                    ).start()
            return Response.json({"task": task}, 201)
        match = re.fullmatch(r"/api/tasks/([^/]+)/cancel", path)
        if match and method == "POST":
            return Response.json(
                {
                    "task": self.tasks.cancel(
                        unquote(match.group(1)),
                        actor=user,
                        source_ip=request.remote_addr,
                    )
                }
            )
        match = re.fullmatch(r"/api/tasks/([^/]+)/export", path)
        if match and method == "GET":
            task = self.tasks.get_task(unquote(match.group(1)))
            raw_path = str(task.get("excel_path") or "").strip()
            if not raw_path:
                raise AuthError(
                    "该任务尚未生成 Excel", code="export_not_ready", status=404
                )
            file_path = Path(raw_path).expanduser()
            if not file_path.is_absolute():
                file_path = (self.config.project_root / file_path).resolve()
            else:
                file_path = file_path.resolve()
            export_root = self.config.exports_dir.resolve()
            try:
                file_path.relative_to(export_root)
            except ValueError as exc:
                raise AuthError(
                    "任务导出文件路径无效", code="bad_export_path", status=404
                ) from exc
            if file_path.suffix.lower() != ".xlsx" or not file_path.is_file():
                raise AuthError(
                    "任务导出文件不存在", code="export_not_ready", status=404
                )
            return Response(
                200,
                file_path.read_bytes(),
                [
                    (
                        "Content-Type",
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    ),
                    (
                        "Content-Disposition",
                        f"attachment; filename=task-export.xlsx; filename*=UTF-8''{quote(file_path.name, safe='')}",
                    ),
                ],
            )
        match = re.fullmatch(r"/api/tasks/([^/]+)", path)
        if match and method == "GET":
            return Response.json({"task": self.tasks.get_task(unquote(match.group(1)))})
        if path == "/api/scheduled" and method == "GET":
            return Response.json({"items": self.tasks.scheduled()})

        # ---- IP / 白名单 ----
        if path == "/api/ips" and method == "GET":
            return Response.json({"items": self.ips.list_ips(include_inactive=True)})
        if path == "/api/ips" and method == "POST":
            self.auth.require(user, ROLE_SUPERVISOR, ROLE_ADMIN)
            data = request.json()
            ip = self.ips.get_or_create(
                str(data.get("name") or ""),
                company_code=str(data.get("company_code") or ""),
                company_name=str(data.get("company_name") or ""),
                keywords=data.get("keywords") or [],
                ref_images=data.get("ref_images") or [],
                actor=user,
                source_ip=request.remote_addr,
            )
            return Response.json({"ip": ip}, 201)
        match = re.fullmatch(r"/api/ips/(\d+)", path)
        if match and method == "PUT":
            data = request.json()
            ip = self.ips.update(
                user,
                int(match.group(1)),
                company_code=data.get("company_code"),
                company_name=data.get("company_name"),
                keywords=data.get("keywords"),
                ref_images=data.get("ref_images"),
                active=data.get("active"),
                source_ip=request.remote_addr,
            )
            return Response.json({"ip": ip})
        match = re.fullmatch(r"/api/ips/(\d+)/whitelist", path)
        if match and method == "POST":
            self.auth.require(user, ROLE_SUPERVISOR, ROLE_ADMIN)
            data = request.json()
            entries = self.ips.save_task_whitelist(
                int(match.group(1)),
                data.get("entries") or [],
                actor=user,
                source_ip=request.remote_addr,
            )
            return Response.json({"entries": entries})
        match = re.fullmatch(r"/api/ips/(\d+)/company-whitelist", path)
        if match and method == "POST":
            self.auth.require(user, ROLE_SUPERVISOR, ROLE_ADMIN)
            data = request.json()
            entries = self.ips.add_company_entries(
                user,
                str(data.get("company_code") or ""),
                data.get("entries") or [],
                source_ip=request.remote_addr,
            )
            return Response.json({"entries": [_row_to_entry(item) for item in entries]})
        match = re.fullmatch(r"/api/whitelist/(\d+)", path)
        if match and method == "DELETE":
            self.ips.remove_entry(
                user, int(match.group(1)), source_ip=request.remote_addr
            )
            return Response.json({"ok": True})

        # ---- 账号 / 邀请 ----
        if path == "/api/users" and method == "GET":
            return Response.json({"items": self.auth.list_users(user)})
        match = re.fullmatch(r"/api/users/(\d+)/disable", path)
        if match and method == "POST":
            self.auth.disable_user(
                user, int(match.group(1)), source_ip=request.remote_addr
            )
            return Response.json({"ok": True})
        match = re.fullmatch(r"/api/users/(\d+)/role", path)
        if match and method == "POST":
            data = request.json()
            self.auth.set_role(
                user,
                int(match.group(1)),
                str(data.get("role") or ""),
                source_ip=request.remote_addr,
            )
            return Response.json({"ok": True})
        match = re.fullmatch(r"/api/users/(\d+)/reset-password", path)
        if match and method == "POST":
            password = self.auth.reset_password(
                user, int(match.group(1)), source_ip=request.remote_addr
            )
            return Response.json({"temporary_password": password})
        if path == "/api/account/password" and method == "POST":
            data = request.json()
            if str(data.get("new_password") or "") != str(data.get("confirm") or ""):
                raise AuthError("两次密码不一致")
            self.auth.change_password(
                user,
                str(data.get("old_password") or ""),
                str(data.get("new_password") or ""),
                source_ip=request.remote_addr,
            )
            return Response.json({"ok": True})
        if path == "/api/invites" and method == "GET":
            return Response.json({"items": self.auth.list_invites(user)})
        if path == "/api/invites" and method == "POST":
            data = request.json()
            invite = self.auth.create_invite(
                user,
                role=str(data.get("role") or ROLE_USER),
                expires_minutes=int(data.get("expires_minutes") or 1440),
                max_uses=int(data.get("max_uses") or 1),
                source_ip=request.remote_addr,
            )
            return Response.json({"invite": invite}, 201)
        match = re.fullmatch(r"/api/invites/(\d+)/revoke", path)
        if match and method == "POST":
            self.auth.revoke_invite(
                user, int(match.group(1)), source_ip=request.remote_addr
            )
            return Response.json({"ok": True})

        # ---- 设置 ----
        if path == "/api/settings" and method == "GET":
            self.auth.require(user, ROLE_SUPERVISOR, ROLE_ADMIN)
            return Response.json(self._settings_view())
        if path == "/api/settings" and method == "PUT":
            self.auth.require(user, ROLE_SUPERVISOR, ROLE_ADMIN)
            data = request.json()
            return Response.json(self._save_settings(user, data, request.remote_addr))

        # ---- 版本 ----
        if path == "/api/admin/versions" and method == "GET":
            self.auth.require(user, ROLE_SUPERVISOR, ROLE_ADMIN)
            return Response.json(self.versions.list_versions())
        if path == "/api/admin/update" and method == "POST":
            self.auth.require(user, ROLE_SUPERVISOR, ROLE_ADMIN)
            data = request.json()
            raw = str(data.get("content_base64") or "")
            try:
                payload = base64.b64decode(raw, validate=True)
            except Exception as exc:  # noqa: BLE001
                raise AuthError("版本包 base64 无效", code="bad_package") from exc
            if len(payload) > min(self.config.max_update_bytes, MAX_UPDATE_BYTES):
                raise AuthError("版本包超过大小限制", code="too_large", status=413)
            state = self.versions.install(
                payload,
                filename=str(data.get("filename") or "version.zip"),
                actor=user,
                note=str(data.get("note") or ""),
            )
            return Response.json({"version": state})
        if path == "/api/admin/rollback" and method == "POST":
            self.auth.require(user, ROLE_SUPERVISOR, ROLE_ADMIN)
            state = self.versions.rollback(actor=user)
            return Response.json({"version": state})

        # ---- 日志 ----
        if path == "/api/audit" and method == "GET":
            self.auth.require(user, ROLE_SUPERVISOR, ROLE_ADMIN)
            clauses = []
            params: list[Any] = []
            for field, column in (
                ("actor", "actor_name"),
                ("source_ip", "source_ip"),
                ("action", "action"),
            ):
                value = request.q(field)
                if value:
                    clauses.append(f"{column} LIKE ?")
                    params.append(f"%{value}%")
            where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
            limit = max(1, min(_int(request.q("limit", "200") or 200, 200), 1000))
            rows = self.db.fetchall(
                f"SELECT * FROM audit_log {where} ORDER BY id DESC LIMIT ?",
                tuple(params + [limit]),
            )
            return Response.json({"items": rows})
        if path == "/api/mail-logs" and method == "GET":
            self.auth.require(user, ROLE_SUPERVISOR, ROLE_ADMIN)
            rows = self.db.fetchall(
                "SELECT * FROM mail_logs ORDER BY id DESC LIMIT 300"
            )
            return Response.json({"items": rows})

        raise AuthError("接口不存在", code="not_found", status=404)

    # ------------------------------------------------------------------
    def _run_task_safely(self, task_no: str) -> None:
        try:
            self.tasks.execute(task_no)
        except Exception:  # noqa: BLE001
            logger.exception("后台任务 %s 执行失败", task_no)

    def _settings_view(self) -> dict[str, Any]:
        stored = {
            row["key"]: row["value"]
            for row in self.db.fetchall("SELECT * FROM settings")
        }
        return {
            "settings": {
                "notify_email_default": stored.get("notify_email_default", ""),
                "company_whitelist_default": stored.get(
                    "company_whitelist_default", "1"
                ),
                "allowed_networks": stored.get(
                    "allowed_networks", ",".join(self.config.allowed_networks)
                ),
                "smtp_host": self.config.smtp.get("smtp_host")
                or self.config.smtp.get("host")
                or "",
                "smtp_port": self.config.smtp.get("smtp_port")
                or self.config.smtp.get("port")
                or 465,
                "smtp_ssl": "1" if self.config.smtp.get("smtp_ssl", True) else "0",
                "smtp_from": self.config.smtp.get("from")
                or self.config.smtp.get("sender")
                or "",
                "smtp_to": ",".join(
                    self.config.smtp.get("to") or []
                    if isinstance(self.config.smtp.get("to"), list)
                    else [self.config.smtp.get("to") or ""]
                ),
                "smtp_keyring_service": self.config.smtp.get("keyring_service") or "",
                "session_idle_hours": str(self.config.session_idle_hours),
                "login_lock_threshold": str(self.config.login_lock_threshold),
                "login_lock_minutes": str(self.config.login_lock_minutes),
                "export_dir": str(self.config.exports_dir),
            },
            "smtp_status": self.mailer.status(),
            "version": self.versions.current(),
        }

    def _save_settings(
        self, user: dict[str, Any], data: dict[str, Any], source_ip: str
    ) -> dict[str, Any]:
        unknown = set(data) - _SETTING_KEYS
        if unknown:
            raise AuthError(f"未知设置项：{', '.join(sorted(unknown))}")
        for numeric in (
            "smtp_port",
            "session_idle_hours",
            "login_lock_threshold",
            "login_lock_minutes",
        ):
            if numeric in data:
                try:
                    int(data[numeric])
                except (TypeError, ValueError) as exc:
                    raise AuthError(f"{numeric} 必须是整数") from exc
        for key, value in data.items():
            self.db.set_setting(key, str(value), user["id"])
            if key == "allowed_networks":
                self.config.allowed_networks = [
                    item.strip() for item in str(value).split(",") if item.strip()
                ]
            elif key == "smtp_host":
                self.config.smtp["smtp_host"] = str(value)
            elif key == "smtp_port":
                self.config.smtp["smtp_port"] = int(value)
            elif key == "smtp_ssl":
                self.config.smtp["smtp_ssl"] = str(value) not in (
                    "0",
                    "false",
                    "False",
                    "",
                )
            elif key == "smtp_from":
                self.config.smtp["from"] = str(value)
            elif key == "smtp_to":
                self.config.smtp["to"] = [
                    item.strip() for item in str(value).split(",") if item.strip()
                ]
            elif key == "smtp_keyring_service":
                self.config.smtp["keyring_service"] = str(value)
            elif key == "session_idle_hours":
                self.config.session_idle_hours = max(1, int(value))
            elif key == "login_lock_threshold":
                self.config.login_lock_threshold = max(1, int(value))
            elif key == "login_lock_minutes":
                self.config.login_lock_minutes = max(1, int(value))
        self.db.audit(
            actor_id=user["id"],
            actor_name=user["username"],
            source_ip=source_ip,
            action="settings.update",
            object_type="settings",
            object_id=",".join(sorted(data)),
            after=data,
        )
        return self._settings_view()


# ----------------------------------------------------------------------
def _with_path(exc: AuthError, path: str) -> AuthError:
    exc.path = path  # type: ignore[attr-defined]
    return exc


def _parse_cookies(value: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for part in value.split(";"):
        if "=" not in part:
            continue
        key, _, val = part.partition("=")
        result[key.strip()] = val.strip()
    return result


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _filters(request: Request) -> dict[str, Any]:
    return {
        "status": request.q("status", "待复核"),
        "platform_group": request.q("platform_group", "all"),
        "ip_code": request.q("ip_code"),
        "tier": request.q("tier", "all"),
        "min_amount": request.q("min_amount", "0"),
        "date_from": request.q("date_from"),
        "date_to": request.q("date_to"),
        "q": request.q("q"),
        "sort": request.q("sort", "date_desc"),
        "page": _int(request.q("page", "1") or 1, 1),
        "page_size": _int(request.q("page_size", "20") or 20, 20),
    }


def _row_to_entry(row: dict[str, Any]) -> dict[str, Any]:
    return entry_row_to_dict(row)


def create_app(config: WorkbenchConfig | None = None) -> WorkbenchApp:
    return WorkbenchApp(config or WorkbenchConfig.load())


__all__ = ["COOKIE_NAME", "Response", "WorkbenchApp", "create_app"]
