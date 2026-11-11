"""任务编排：手动运行、单次定时、完成通知。

采集与判定仍走 backend 的 ``PipelineRunner``；本模块只负责任务记录、
生成当次配置/白名单、在 try/finally 里保证每个任务最多发一封完成邮件。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from workbench import backend_bridge as bridge
from workbench.auth import AuthError, PermissionError_, ROLE_ADMIN, ROLE_SUPERVISOR
from workbench.config import WorkbenchConfig
from workbench.db import Database, now_text
from workbench.mailer import MailResult, Mailer
from workbench.whitelist import IPService, build_whitelist_text, normalize_entry

logger = logging.getLogger("workbench.tasks")

PLATFORMS = ("taobao", "jd", "1688")
PLATFORM_LABELS = {"taobao": "淘宝/天猫", "jd": "京东", "1688": "1688"}
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_DATETIME_FORMATS = (
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d %H:%M:%S",
)

Executor = Callable[["TaskService", dict[str, Any], dict[str, Any]], dict[str, Any]]


@dataclass
class TaskOutcome:
    status: str = "success"
    platforms: dict[str, dict[str, Any]] = field(default_factory=dict)
    candidates: int = 0
    risks: dict[str, int] = field(default_factory=dict)
    excel_path: str | None = None
    errors: list[str] = field(default_factory=list)
    run_id: str | None = None
    stop_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "platforms": self.platforms,
            "candidates": int(self.candidates),
            "risks": dict(self.risks),
            "excel_path": self.excel_path,
            "errors": list(self.errors),
            "run_id": self.run_id,
            "stop_reason": self.stop_reason,
        }


def parse_datetime(value: Any) -> datetime:
    text = str(value or "").strip()
    for fmt in _DATETIME_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise AuthError("时间格式应为 YYYY-MM-DD HH:MM", code="bad_datetime")


def validate_email(value: str | None) -> str:
    text = str(value or "").strip()
    if text and not _EMAIL_RE.match(text):
        raise AuthError("提醒邮箱格式不正确", code="bad_email")
    return text


def validate_platforms(values: Any) -> list[str]:
    if isinstance(values, str):
        values = [item.strip() for item in values.split(",") if item.strip()]
    result: list[str] = []
    for item in values or []:
        name = str(item).strip().lower()
        if name == "tmall":
            name = "taobao"
        if name not in PLATFORMS:
            raise AuthError(f"不支持的平台：{item}")
        if name not in result:
            result.append(name)
    if not result:
        raise AuthError("至少选择一个平台")
    return result


def task_status_label(status: str) -> str:
    return {
        "queued": "排队中",
        "scheduled": "已预约",
        "running": "运行中",
        "success": "成功",
        "partial": "部分失败",
        "failed": "失败",
        "cancelled": "已取消",
    }.get(status, status)


class TaskService:
    def __init__(
        self,
        config: WorkbenchConfig,
        db: Database,
        *,
        mailer: Mailer | None = None,
        pipeline_executor: Executor | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.config = config
        self.db = db
        self.mailer = mailer or Mailer(config, db)
        self.pipeline_executor = pipeline_executor or _default_pipeline_executor
        self.clock = clock or datetime.now
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    def preview_ip(self, name: str) -> dict[str, Any]:
        service = IPService(self.config, self.db)
        return service.preview(name) or {
            "ip": {
                "name": str(name or "").strip(),
                "name_norm": bridge.normalize_ip(name),
                "company_code": "",
                "keywords": [],
            },
            "whitelist": [],
            "new": True,
        }

    def create_task(
        self,
        payload: dict[str, Any],
        *,
        actor: dict[str, Any],
        source_ip: str = "",
        run_now: bool = True,
    ) -> dict[str, Any]:
        ip_name = str(payload.get("ip_name") or "").strip()
        if not ip_name:
            raise AuthError("IP 名不能为空")
        platforms = validate_platforms(payload.get("platforms"))
        mode = str(payload.get("mode") or "formal").strip()
        if mode not in ("formal", "dry_run"):
            raise AuthError("运行模式只能是 formal 或 dry_run")
        notify_email = validate_email(payload.get("notify_email"))
        ip_service = IPService(self.config, self.db)
        existing = ip_service.find(ip_name) or {}
        supplied_keywords = (
            payload.get("keywords")
            if "keywords" in payload
            else json.loads(existing.get("keywords_json") or "[]")
        )
        keywords = [
            str(item).strip() for item in (supplied_keywords or []) if str(item).strip()
        ] or [ip_name]
        include_company = bool(payload.get("include_company", True))
        company_code = (
            str(payload.get("company_code", existing.get("company_code")) or "")
            .strip()
            .lower()
        )
        company_name = str(
            payload.get("company_name", existing.get("company_name")) or ""
        ).strip()
        whitelist = payload.get("whitelist") or []
        if not isinstance(whitelist, list):
            raise AuthError("白名单必须是列表")

        scheduled_at = str(payload.get("scheduled_at") or "").strip()
        ends_at = str(payload.get("ends_at") or "").strip()
        if ends_at:
            deadline = parse_datetime(ends_at)
            if deadline <= self.clock():
                raise AuthError("常规采集截止时间必须晚于当前时间", code="bad_datetime")
            ends_at = deadline.strftime("%Y-%m-%d %H:%M:%S")
            # 新工作台任务立即开始；旧客户端的预约参数仅在无结束时间时兼容。
            scheduled_at = ""
        run_at: datetime | None = None
        if scheduled_at:
            run_at = parse_datetime(scheduled_at)
            if run_at <= self.clock():
                raise AuthError("预约时间必须晚于当前时间", code="bad_datetime")
        if run_at is None:
            running = self.db.fetchone(
                "SELECT task_no FROM tasks WHERE status='running' LIMIT 1"
            )
            if running is not None:
                raise AuthError(
                    f"已有任务在运行（{running['task_no']}），请等它结束后再启动",
                    code="already_running",
                )
        for entry in whitelist:
            if not isinstance(entry, dict):
                raise AuthError("白名单条目必须是对象")
            normalize_entry(entry, default_company=company_code)

        ip = ip_service.get_or_create(
            ip_name,
            company_code=company_code,
            company_name=company_name,
            keywords=keywords,
            actor=actor,
            source_ip=source_ip,
        )
        whitelist_provided = bool(
            payload.get("whitelist_provided", "whitelist" in payload)
        )
        if whitelist_provided:
            ip_service.save_task_whitelist(
                ip["id"], whitelist, actor=actor, source_ip=source_ip
            )
            company_entries = [
                item
                for item in whitelist
                if str(item.get("scope_type") or item.get("scope") or "ip").lower()
                == "company"
            ]
            if company_entries and actor.get("role") in (ROLE_SUPERVISOR, ROLE_ADMIN):
                ip_service.add_company_entries(
                    actor,
                    company_code or ip.get("company_code") or "",
                    company_entries,
                    source_ip=source_ip,
                )
        task_no = f"WB{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:4].upper()}"
        now = now_text()
        status = "scheduled" if run_at else "queued"
        record = {
            "task_no": task_no,
            "ip_id": ip["id"],
            "ip_name": ip["name"],
            "ip_norm": ip["name_norm"],
            "company_code": ip.get("company_code") or "",
            "platforms": platforms,
            "mode": mode,
            "keywords": keywords,
            "notify_email": notify_email,
            "include_company": include_company,
            "whitelist": whitelist,
            "scheduled_at": scheduled_at,
            "ends_at": ends_at,
        }
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO tasks(task_no, ip_id, ip_name, ip_norm, company_code,
                    platforms_json, mode, keywords_json, notify_email, include_company,
                    status, payload_json, created_by, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    task_no,
                    ip["id"],
                    ip["name"],
                    ip["name_norm"],
                    ip.get("company_code") or "",
                    json.dumps(platforms, ensure_ascii=False),
                    mode,
                    json.dumps(keywords, ensure_ascii=False),
                    notify_email,
                    1 if include_company else 0,
                    status,
                    json.dumps(record, ensure_ascii=False),
                    actor.get("id"),
                    now,
                ),
            )
            if run_at is not None:
                connection.execute(
                    """INSERT INTO scheduled_runs(task_no, run_at, status, created_by, created_at)
                       VALUES(?,?,'scheduled',?,?)""",
                    (
                        task_no,
                        run_at.strftime("%Y-%m-%d %H:%M:%S"),
                        actor.get("id"),
                        now,
                    ),
                )
            connection.execute(
                """INSERT INTO audit_log(actor_id, actor_name, source_ip, action,
                    object_type, object_id, before_json, after_json, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    actor.get("id"),
                    actor.get("username", ""),
                    source_ip,
                    "task.create",
                    "task",
                    task_no,
                    "{}",
                    json.dumps(record, ensure_ascii=False),
                    now,
                ),
            )
        if run_now and run_at is None:
            self.execute(task_no)
        return self.get_task(task_no)

    def execute(self, task_no: str) -> dict[str, Any]:
        now = now_text()
        running = self.db.fetchone(
            "SELECT task_no FROM tasks WHERE status='running' AND task_no<>? LIMIT 1",
            (task_no,),
        )
        if running is not None:
            raise AuthError(
                f"已有任务在运行（{running['task_no']}），同一时间只允许一个任务",
                code="already_running",
            )
        with self.db.transaction() as connection:
            cursor = connection.execute(
                """UPDATE tasks SET status='running', started_at=?
                   WHERE task_no=? AND status IN ('queued','scheduled')""",
                (now, task_no),
            )
            if cursor.rowcount == 0:
                raise AuthError(
                    "任务不可运行（可能已在运行或已结束）", code="task_not_runnable"
                )
            connection.execute(
                "UPDATE scheduled_runs SET status='running', started_at=? WHERE task_no=? AND status='scheduled'",
                (now, task_no),
            )
        task = self.get_task(task_no)
        ip_service = IPService(self.config, self.db)
        ip = ip_service.get(int(task["ip_id"])) if task.get("ip_id") else None
        error: str | None = None
        outcome = TaskOutcome()
        try:
            prepared = self._prepare_run(task, ip)
            raw = self.pipeline_executor(self, task, prepared)
            outcome = _coerce_outcome(raw)
        except BaseException as exc:  # noqa: BLE001 - 中断也必须发一封通知
            error = f"{type(exc).__name__}: {exc}"
            outcome = TaskOutcome(status="failed", errors=[error])
            logger.exception("任务 %s 执行失败", task_no)
        finally:
            # executor 返回前已等待各平台在途请求、补采及有限重试收尾。
            # 先提交终态和报表，再发送邮件，收件人打开工作台即能看到最终结果。
            ended = now_text()
            result = outcome.as_dict()
            if error:
                result["error"] = error
            status = outcome.status
            if error:
                status = "failed"
            elif status not in ("success", "partial", "failed"):
                status = "success"
            with self.db.transaction() as connection:
                connection.execute(
                    """UPDATE tasks SET status=?, ended_at=?, result_json=?, run_id=?,
                        excel_path=?, error=?, mail_status=?, mail_error=?
                       WHERE task_no=?""",
                    (
                        status,
                        ended,
                        json.dumps(result, ensure_ascii=False),
                        outcome.run_id or task_no,
                        outcome.excel_path,
                        error,
                        "pending",
                        None,
                        task_no,
                    ),
                )
                connection.execute(
                    """UPDATE scheduled_runs SET status=?, ended_at=?, error=?
                       WHERE task_no=? AND status IN ('scheduled','running')""",
                    (
                        "done" if status in ("success", "partial") else "failed",
                        ended,
                        error,
                        task_no,
                    ),
                )
            # execute 的 queued/scheduled -> running 条件更新只允许一个调用者
            # 进入这里；邮件失败不会重新执行任务，也不会触发第二封完成通知。
            try:
                mail_result = self._notify(self.get_task(task_no), outcome, error)
            except Exception as exc:  # noqa: BLE001
                logger.exception("任务 %s 完成通知失败", task_no)
                mail_result = MailResult(False, reason=f"{type(exc).__name__}: {exc}")
            self.db.execute(
                "UPDATE tasks SET mail_status=?, mail_error=? WHERE task_no=?",
                (
                    "sent"
                    if mail_result.sent
                    else ("skipped" if mail_result.skipped else "failed"),
                    mail_result.reason,
                    task_no,
                ),
            )
        return self.get_task(task_no)

    def _prepare_run(
        self, task: dict[str, Any], ip: dict[str, Any] | None
    ) -> dict[str, Any]:
        if ip is None:
            raise AuthError("IP 档案不存在", code="not_found")
        ip_service = IPService(self.config, self.db)
        entries = ip_service.entries(
            ip["id"], include_company=bool(task["include_company"])
        )
        run_dir = self.config.runs_dir / task["task_no"]
        run_dir.mkdir(parents=True, exist_ok=True)
        whitelist_path = run_dir / "whitelist.txt"
        whitelist_path.write_text(build_whitelist_text(entries), encoding="utf-8")
        base = bridge.load_backend_settings(self.config)
        output_dir = run_dir
        crawler_source = Path(
            self.config.crawler_config_path
            or base.get("paths.crawler_config")
            or (base.crawler_dir / "config" / "windows.yaml")
        ).expanduser()
        if not crawler_source.is_absolute():
            # 工作台显式配置相对项目根；后端 paths.* 相对 backend 根。
            source_root = (
                self.config.project_root
                if self.config.crawler_config_path
                else base.root
            )
            crawler_source = (source_root / crawler_source).resolve()
        if not crawler_source.exists():
            # 测试/精简部署没有 crawler 配置时，生成最小配置；不修改任何降频参数。
            crawler_source = run_dir / "crawler_source.yaml"
            import yaml

            crawler_source.write_text(
                yaml.safe_dump(
                    {
                        "ips": [
                            {
                                "code": ip["name_norm"],
                                "name": ip["name"],
                                "aliases": [],
                                "variants": [],
                            }
                        ],
                        "crawl": {
                            "keywords": [
                                {"keyword": word, "ip_code": ip["name_norm"]}
                                for word in task["keywords"]
                            ]
                        },
                    },
                    allow_unicode=True,
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
        # 让 build_run_configs 读取上面确认存在的 crawler 源配置。
        base.raw.setdefault("paths", {})["crawler_config"] = str(crawler_source)
        backend_path, crawler_path = bridge.build_run_configs(
            base,
            ip_name=ip["name"],
            ip_code=ip["name_norm"],
            keywords=list(task["keywords"]),
            whitelist_path=whitelist_path,
            output_dir=output_dir,
            crawler_config_path=run_dir / "crawler_run.yaml",
        )
        settings = bridge.load_backend_settings(
            self.config, db_path=self.config.leads_db_path
        )
        from pipeline.config import load_config  # noqa: PLC0415

        settings = load_config(backend_path)
        if self.config.leads_db_path is not None:
            settings.raw["storage"] = {
                **dict(settings.raw.get("storage") or {}),
                "db_path": str(Path(self.config.leads_db_path).expanduser().resolve()),
            }
        settings.raw["export"] = {
            **dict(settings.raw.get("export") or {}),
            "dir": str(self.config.exports_dir),
            "run_id": task["task_no"],
            "filename": f"线索_{task['task_no']}.xlsx",
        }
        settings.raw.setdefault("run", {})["progress_scope"] = task["task_no"]
        # 留空即不限时，不能从模板继承其他任务的截止时间。
        settings.raw.setdefault("run", {})["ends_at"] = task.get("ends_at") or ""
        return {
            "settings": settings,
            "backend_config": backend_path,
            "crawler_config": crawler_path,
            "run_dir": run_dir,
            "whitelist_path": whitelist_path,
            "entries": entries,
            "ip": ip,
        }

    # ------------------------------------------------------------------
    def _notify(
        self, task: dict[str, Any], outcome: TaskOutcome, error: str | None
    ) -> Any:
        email = str(task.get("notify_email") or "").strip()
        if not email:
            return self.mailer.send(
                subject="任务完成通知（未填写收件人）",
                body="",
                recipients=[],
                task_no=task["task_no"],
            )
        status = "failed" if error else outcome.status
        label = task_status_label(status)
        prefix = str(self.config.smtp.get("subject_prefix") or "[侵权线索]")
        subject = f"{prefix} 工作台任务{label} {task['task_no']}（{task['ip_name']}）"
        body = build_completion_body(task, outcome, error)
        return self.mailer.send(
            subject=subject,
            body=body,
            recipients=email,
            task_no=task["task_no"],
        )

    # ------------------------------------------------------------------
    def get_task(self, task_no: str) -> dict[str, Any]:
        row = self.db.fetchone("SELECT * FROM tasks WHERE task_no=?", (str(task_no),))
        if row is None:
            raise AuthError("任务不存在", code="not_found", status=404)
        return _task_to_dict(row)

    def list_tasks(self, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.db.fetchall(
            "SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (max(1, min(limit, 200)),)
        )
        return [_task_to_dict(row) for row in rows]

    def cancel(
        self, task_no: str, *, actor: dict[str, Any], source_ip: str = ""
    ) -> dict[str, Any]:
        task = self.get_task(task_no)
        if task["status"] not in ("queued", "scheduled"):
            raise AuthError("只能取消未开始的任务")
        if actor.get("role") not in (ROLE_SUPERVISOR, ROLE_ADMIN) and task.get(
            "created_by"
        ) != actor.get("id"):
            raise PermissionError_("只能取消自己创建的任务")
        now = now_text()
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE tasks SET status='cancelled', ended_at=? WHERE task_no=? AND status IN ('queued','scheduled')",
                (now, task_no),
            )
            connection.execute(
                "UPDATE scheduled_runs SET status='cancelled', ended_at=? WHERE task_no=? AND status='scheduled'",
                (now, task_no),
            )
            connection.execute(
                """INSERT INTO audit_log(actor_id, actor_name, source_ip, action,
                    object_type, object_id, before_json, after_json, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    actor.get("id"),
                    actor.get("username", ""),
                    source_ip,
                    "task.cancel",
                    "task",
                    task_no,
                    "{}",
                    "{}",
                    now,
                ),
            )
        return self.get_task(task_no)

    def scheduled(self) -> list[dict[str, Any]]:
        return self.db.fetchall(
            """SELECT s.*, t.ip_name, t.platforms_json, t.notify_email, t.status AS task_status
               FROM scheduled_runs s JOIN tasks t ON t.task_no=s.task_no
               ORDER BY s.run_at"""
        )

    def tick(self, now: datetime | None = None) -> list[str]:
        """进程内调度：把到点的单次任务各执行一次。"""

        current = (now or self.clock()).strftime("%Y-%m-%d %H:%M:%S")
        due = self.db.fetchall(
            "SELECT * FROM scheduled_runs WHERE status='scheduled' AND run_at<=? ORDER BY run_at",
            (current,),
        )
        executed: list[str] = []
        for row in due:
            with self.db.transaction() as connection:
                cursor = connection.execute(
                    "UPDATE scheduled_runs SET status='running', started_at=? WHERE id=? AND status='scheduled'",
                    (now_text(), row["id"]),
                )
                if cursor.rowcount == 0:
                    continue
            try:
                self.execute(row["task_no"])
                executed.append(row["task_no"])
            except Exception:  # noqa: BLE001
                logger.exception("定时任务 %s 执行失败", row["task_no"])
                self.db.execute(
                    "UPDATE scheduled_runs SET status='failed', ended_at=?, error=? WHERE id=?",
                    (now_text(), "执行失败", row["id"]),
                )
        return executed


class Scheduler:
    """进程内单次任务调度线程；服务退出即停止。"""

    def __init__(self, tasks: TaskService, *, interval_seconds: float = 2.0):
        self.tasks = tasks
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="workbench-scheduler", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                self.tasks.tick()
            except Exception:  # noqa: BLE001
                logger.exception("调度线程异常")


def _default_pipeline_executor(
    service: TaskService, task: dict[str, Any], prepared: dict[str, Any]
) -> dict[str, Any]:
    settings = prepared["settings"]
    run_dir: Path = prepared["run_dir"]
    platforms = list(task["platforms"])
    dry_run = task["mode"] == "dry_run"
    env = {
        "IP_LEAD_CRAWLER_CONFIG": str(prepared["crawler_config"]),
        "IP_LEAD_CRAWLER_DIR": str(settings.crawler_dir),
    }
    if service.config.leads_db_path is not None:
        env["IP_LEAD_DB_PATH"] = str(
            Path(service.config.leads_db_path).expanduser().resolve()
        )
    with bridge.with_env(env):
        runner = bridge.pipeline_runner(
            settings,
            notify_email=task.get("notify_email") or None,
            ip_name=task["ip_name"],
        )
        summary = runner.run(
            platforms=platforms,
            run_date=date.today(),
            dry_run=dry_run,
            allow_alert=False,
            run_id=task["task_no"],
        )
    platforms_result: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for name, item in getattr(summary, "platforms", {}).items():
        platforms_result[name] = {
            "label": PLATFORM_LABELS.get(name, name),
            "items": int(getattr(item, "items", 0) or 0),
            "leads": int(getattr(item, "leads", 0) or 0),
            "status": getattr(item, "status", ""),
            "login_ok": bool(getattr(item, "login_ok", False)),
            "error": getattr(item, "error", "") or "",
            "amount_complete": int(getattr(item, "amount_complete", 0)),
            "amount_missing": int(getattr(item, "amount_missing", 0)),
            "missing_fields": dict(getattr(item, "missing_fields", {})),
            "detail_failures": dict(getattr(item, "detail_failures", {})),
        }
        if getattr(item, "status", "") not in ("ok", "disabled", "deadline"):
            errors.append(
                f"{PLATFORM_LABELS.get(name, name)}：{getattr(item, 'error', '') or getattr(item, 'status', '')}"
            )
        elif getattr(item, "amount_missing", 0):
            errors.append(
                f"{PLATFORM_LABELS.get(name, name)}：金额数据不完整，{item.amount_missing} 条待核验"
            )
        if getattr(item, "missing_fields", {}).get("shop_name", 0):
            errors.append(
                f"{PLATFORM_LABELS.get(name, name)}：店名待补齐 {item.missing_fields['shop_name']} 条"
            )
    excel_error = getattr(summary, "excel_error", None)
    if excel_error:
        errors.append(str(excel_error))
    risk_counts = {"达标": 0, "待核验": 0, "临界": 0}
    if not dry_run:
        try:
            risk_counts = service._leads_service().risk_counts(task["task_no"])
        except Exception:  # noqa: BLE001
            logger.exception("统计风险等级失败")
    status = "partial" if errors or getattr(summary, "failed", False) else "success"
    return {
        "status": status,
        "platforms": platforms_result,
        "candidates": int(getattr(summary, "leads", 0) or 0),
        "risks": risk_counts,
        "excel_path": getattr(summary, "excel", None),
        "errors": errors,
        "run_id": task["task_no"],
        "stop_reason": "deadline"
        if getattr(summary, "deadline_reached", False)
        else None,
    }


def _coerce_outcome(raw: Any) -> TaskOutcome:
    if isinstance(raw, TaskOutcome):
        return raw
    if not isinstance(raw, dict):
        return TaskOutcome(status="success")
    status = str(raw.get("status") or "success")
    if status == "success" and any(
        p.get("amount_missing", 0)
        or p.get("missing_fields", {}).get("shop_name", 0)
        or p.get("missing_fields", {}).get("title", 0)
        or p.get("detail_failures")
        or p.get("status") in ("partial", "error", "timeout")
        for p in (raw.get("platforms") or {}).values()
    ):
        status = "partial"
    return TaskOutcome(
        status=status,
        platforms=dict(raw.get("platforms") or {}),
        candidates=int(raw.get("candidates") or 0),
        risks={str(key): int(value) for key, value in (raw.get("risks") or {}).items()},
        excel_path=raw.get("excel_path"),
        errors=[str(item) for item in (raw.get("errors") or [])],
        run_id=raw.get("run_id"),
        stop_reason=raw.get("stop_reason"),
    )


def build_completion_body(
    task: dict[str, Any], outcome: TaskOutcome, error: str | None = None
) -> str:
    status = "failed" if error else outcome.status
    lines = [
        f"IP 名：{task.get('ip_name', '')}",
        f"任务编号：{task.get('task_no', '')}",
        f"模式：{'dry-run（演练）' if task.get('mode') == 'dry_run' else '正式'}",
        f"结果：{task_status_label(status)}",
        f"平台：{'、'.join(PLATFORM_LABELS.get(item, item) for item in task.get('platforms', []))}",
        "",
        "各平台采集数：",
    ]
    if outcome.platforms:
        for name, item in outcome.platforms.items():
            label = item.get("label") or PLATFORM_LABELS.get(name, name)
            lines.append(
                f"- {label}：采集 {item.get('items', 0)} 条，候选 {item.get('leads', 0)} 条，"
                f"状态 {item.get('status', '')}"
            )
            if "amount_missing" in item:
                lines.append(
                    f"  金额：可计算 {item.get('amount_complete', 0)} 条，待核验 {item['amount_missing']} 条"
                )
    else:
        lines.append("- 无平台结果")
    lines.extend(
        [
            "",
            f"候选数（线索）：{outcome.candidates}",
            "风险等级："
            + " / ".join(
                f"{key} {outcome.risks.get(key, 0)}"
                for key in ("达标", "待核验", "临界")
            ),
            f"Excel：{outcome.excel_path or '未生成'}",
            "异常摘要：" + ("；".join(outcome.errors) if outcome.errors else "无"),
        ]
    )
    if error:
        lines.append(f"失败/中断原因：{error}")
    if task.get("ends_at"):
        lines.append(f"常规采集截止时间：{task['ends_at']}")
    if outcome.stop_reason == "deadline":
        lines.append("常规采集已按时停止；在途请求和补采已收尾，未补齐项见异常摘要。")
    lines.append("")
    lines.append("（本邮件由工作台自动发送；每个任务只发一封。）")
    return "\n".join(lines)


def _task_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    result = row.get("result_json")
    try:
        parsed = json.loads(result) if result else {}
    except ValueError:
        parsed = {}
    return {
        "task_no": row["task_no"],
        "ip_id": row.get("ip_id"),
        "ip_name": row["ip_name"],
        "ip_norm": row["ip_norm"],
        "company_code": row.get("company_code") or "",
        "platforms": json.loads(row["platforms_json"] or "[]"),
        "platform_labels": [
            PLATFORM_LABELS.get(item, item)
            for item in json.loads(row["platforms_json"] or "[]")
        ],
        "mode": row["mode"],
        "keywords": json.loads(row["keywords_json"] or "[]"),
        "notify_email": row.get("notify_email") or "",
        "include_company": bool(row.get("include_company")),
        "status": row["status"],
        "status_label": (
            "已完成（常规采集已截止）"
            if row["status"] == "success" and parsed.get("stop_reason") == "deadline"
            else "部分完成（金额待核验）"
            if row["status"] == "partial"
            and any(
                p.get("amount_missing", 0) for p in parsed.get("platforms", {}).values()
            )
            else "部分完成（店名待补齐）"
            if row["status"] == "partial"
            and any(
                p.get("missing_fields", {}).get("shop_name", 0)
                for p in parsed.get("platforms", {}).values()
            )
            else "部分完成（标题待补齐）"
            if row["status"] == "partial"
            and any(
                p.get("missing_fields", {}).get("title", 0)
                for p in parsed.get("platforms", {}).values()
            )
            else task_status_label(row["status"])
        ),
        "created_at": row.get("created_at"),
        "started_at": row.get("started_at"),
        "ended_at": row.get("ended_at"),
        "ends_at": json.loads(row.get("payload_json") or "{}").get("ends_at") or "",
        "run_id": row.get("run_id"),
        "excel_path": row.get("excel_path"),
        "error": row.get("error"),
        "mail_status": row.get("mail_status"),
        "mail_error": row.get("mail_error"),
        "result": parsed,
        "created_by": row.get("created_by"),
    }


def _leads_service(self: TaskService):
    from workbench.leads import LeadsService  # noqa: PLC0415

    return LeadsService(self.config, self.db)


TaskService._leads_service = _leads_service  # type: ignore[attr-defined]


__all__ = [
    "PLATFORM_LABELS",
    "PLATFORMS",
    "Scheduler",
    "TaskOutcome",
    "TaskService",
    "build_completion_body",
    "parse_datetime",
    "task_status_label",
    "validate_email",
    "validate_platforms",
]
