from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from workbench.auth import AuthError
from workbench.mailer import Mailer
from workbench.tasks import TaskService, validate_email


def _payload(**overrides):
    payload = {
        "ip_name": "奥特曼",
        "platforms": ["taobao", "jd"],
        "mode": "formal",
        "notify_email": "owner@example.test",
        "keywords": ["奥特曼"],
        "whitelist": [{"platform": "taobao", "shop_name": "授权店"}],
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize("field,label", [("shop_name", "店名"), ("title", "标题")])
def test_shop_name_missing_marks_task_partial(
    tmp_config, db, mailer, supervisor, field, label
):
    service = _service(
        tmp_config,
        db,
        mailer,
        lambda *args: {
            "status": "success",
            "platforms": {
                "jd": {
                    "amount_missing": 0,
                    "amount_complete": 1,
                    "missing_fields": {field: 1},
                }
            },
        },
    )
    task = service.create_task(_payload(notify_email=""), actor=supervisor)
    assert task["status"] == "partial"
    assert task["status_label"] == f"部分完成（{label}待补齐）"


def _service(tmp_config, db, mailer, executor, clock=None):
    return TaskService(
        tmp_config, db, mailer=mailer, pipeline_executor=executor, clock=clock
    )


def test_task_with_end_time_starts_immediately(
    tmp_config, db, mailer, mailbox, supervisor
):
    calls = []
    deadline = (datetime.now() + timedelta(hours=1)).replace(microsecond=0)

    def executor(service, task, prepared):
        calls.append(task)
        assert prepared["settings"].get("run.ends_at") == deadline.strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        return {"status": "success", "stop_reason": "deadline", "candidates": 2}

    service = _service(tmp_config, db, mailer, executor)
    task = service.create_task(
        _payload(ends_at=deadline.isoformat(), scheduled_at="2099-01-01T10:00"),
        actor=supervisor,
    )
    assert len(calls) == 1
    assert task["status"] == "success"
    assert task["status_label"] == "已完成（常规采集已截止）"
    assert task["ends_at"] == deadline.strftime("%Y-%m-%d %H:%M:%S")
    assert task["result"]["candidates"] == 2
    assert service.scheduled() == []
    assert len(mailbox) == 1
    assert "在途请求和补采已收尾" in mailbox[0][0].get_content()


@pytest.mark.parametrize(
    "ends_at", ["invalid", "2020-01-01T10:00", "2026-11-06T10:00:00"]
)
def test_invalid_end_time_has_no_task_or_profile_side_effects(
    tmp_config, db, mailer, supervisor, ends_at
):
    service = _service(
        tmp_config,
        db,
        mailer,
        lambda *args: pytest.fail("不应执行"),
        clock=lambda: datetime(2026, 11, 6, 10),
    )
    with pytest.raises(AuthError):
        service.create_task(_payload(ends_at=ends_at), actor=supervisor)
    assert service.list_tasks() == []
    assert db.fetchall("SELECT * FROM ip_assets") == []


@pytest.mark.parametrize("ends_at", ["", None, "   "])
def test_empty_end_time_runs_without_inherited_deadline(
    tmp_config, db, mailer, supervisor, monkeypatch, ends_at
):
    from pipeline import config as pipeline_config

    original = pipeline_config.load_config

    def load_with_old_deadline(*args, **kwargs):
        settings = original(*args, **kwargs)
        settings.raw.setdefault("run", {})["ends_at"] = "2000-01-01 00:00:00"
        return settings

    monkeypatch.setattr(pipeline_config, "load_config", load_with_old_deadline)
    seen = []

    def executor(service, task, prepared):
        seen.append(prepared["settings"].get("run.ends_at"))
        return {"status": "success"}

    service = _service(tmp_config, db, mailer, executor)
    task = service.create_task(
        _payload(ends_at=ends_at, notify_email=""), actor=supervisor
    )
    assert seen == [""]
    assert task["ends_at"] == "" and task["status"] == "success"
    assert task["result"]["stop_reason"] is None


def test_end_time_does_not_prevent_early_completion(tmp_config, db, mailer, supervisor):
    service = _service(tmp_config, db, mailer, lambda *args: {"status": "success"})
    task = service.create_task(_payload(ends_at="2099-01-01T10:00"), actor=supervisor)
    assert task["status_label"] == "成功"
    assert task["ended_at"]
    assert task["result"]["stop_reason"] is None


def test_success_sends_exactly_one_completion_email(
    tmp_config, db, mailer, mailbox, supervisor
):
    calls = []

    def executor(service, task, prepared):
        calls.append(task["task_no"])
        return {
            "status": "success",
            "platforms": {
                "taobao": {
                    "items": 30,
                    "leads": 5,
                    "status": "ok",
                    "label": "淘宝/天猫",
                }
            },
            "candidates": 5,
            "risks": {"达标": 3, "待核验": 1, "临界": 1},
            "excel_path": "C:/out/线索.xlsx",
            "errors": [],
            "run_id": task["task_no"],
        }

    service = _service(tmp_config, db, mailer, executor)
    task = service.create_task(_payload(), actor=supervisor)
    assert task["status"] == "success"
    assert len(calls) == 1
    assert len(mailbox) == 1
    message, recipients = mailbox[0]
    body = message.get_content()
    assert recipients == ["owner@example.test"]
    assert "奥特曼" in body
    assert "候选数（线索）：5" in body
    assert "达标 3" in body and "临界 1" in body
    assert "C:/out/线索.xlsx" in body
    assert task["mail_status"] == "sent"


@pytest.mark.parametrize("outcome", ["success", "partial", "failed"])
def test_completion_is_committed_before_single_notification(
    tmp_config, db, mailer, mailbox, supervisor, outcome
):
    observed = []

    def executor(service, task, prepared):
        current = service.get_task(task["task_no"])
        assert current["status"] == "running"
        assert current["ended_at"] is None and mailbox == []
        return {
            "status": outcome,
            "errors": ["补采最终失败"] if outcome != "success" else [],
        }

    service = _service(tmp_config, db, mailer, executor)
    original_notify = service._notify

    def notify(task, result, error):
        observed.append(service.get_task(task["task_no"]))
        return original_notify(task, result, error)

    service._notify = notify
    task = service.create_task(_payload(), actor=supervisor)
    assert observed[0]["status"] == outcome
    assert observed[0]["ended_at"]
    assert task["mail_status"] == "sent"
    with pytest.raises(AuthError):
        service.execute(task["task_no"])
    assert len(mailbox) == 1
    if outcome != "success":
        assert "补采最终失败" in mailbox[0][0].get_content()


def test_zero_leads_still_sends_one_email(tmp_config, db, mailer, mailbox, supervisor):
    def executor(service, task, prepared):
        return {
            "status": "success",
            "platforms": {
                "jd": {"items": 0, "leads": 0, "status": "ok", "label": "京东"}
            },
            "candidates": 0,
            "risks": {"达标": 0, "待核验": 0, "临界": 0},
            "errors": [],
        }

    service = _service(tmp_config, db, mailer, executor)
    task = service.create_task(_payload(platforms=["jd"]), actor=supervisor)
    assert task["status"] == "success"
    assert len(mailbox) == 1
    assert "候选数（线索）：0" in mailbox[0][0].get_content()


def test_exception_still_sends_one_email_with_reason(
    tmp_config, db, mailer, mailbox, supervisor
):
    def executor(service, task, prepared):
        raise RuntimeError("浏览器登录态失效")

    service = _service(tmp_config, db, mailer, executor)
    task = service.create_task(_payload(), actor=supervisor)
    assert task["status"] == "failed"
    assert "浏览器登录态失效" in (task["error"] or "")
    assert len(mailbox) == 1
    body = mailbox[0][0].get_content()
    assert "失败" in body
    assert "失败/中断原因" in body
    assert "浏览器登录态失效" in body


def test_no_smtp_config_only_logs_and_does_not_raise(tmp_config, db, supervisor):
    no_smtp = Mailer(tmp_config, db)

    def executor(service, task, prepared):
        return {"status": "success", "platforms": {}, "candidates": 0, "errors": []}

    service = _service(tmp_config, db, no_smtp, executor)
    task = service.create_task(_payload(), actor=supervisor)
    assert task["status"] == "success"
    assert task["mail_status"] == "skipped"
    row = db.fetchone("SELECT * FROM mail_logs ORDER BY id DESC LIMIT 1")
    assert row["status"] == "skipped"
    assert "SMTP" in (row["error"] or "")


def test_single_scheduled_run_executes_once(
    tmp_config, db, mailer, mailbox, supervisor
):
    executions = []

    def executor(service, task, prepared):
        executions.append(task["task_no"])
        return {"status": "success", "platforms": {}, "candidates": 0, "errors": []}

    now = datetime(2026, 11, 6, 10, 0, 0)
    service = _service(tmp_config, db, mailer, executor, clock=lambda: now)
    run_at = now + timedelta(minutes=10)
    task = service.create_task(
        _payload(scheduled_at=run_at.strftime("%Y-%m-%d %H:%M")), actor=supervisor
    )
    assert task["status"] == "scheduled"
    assert service.tick(now=now) == []
    executed = service.tick(now=run_at + timedelta(seconds=1))
    assert executed == [task["task_no"]]
    assert service.tick(now=run_at + timedelta(minutes=5)) == []
    assert len(executions) == 1
    assert len(mailbox) == 1
    row = db.fetchone(
        "SELECT * FROM scheduled_runs WHERE task_no=?", (task["task_no"],)
    )
    assert row["status"] == "done"


def test_cancel_scheduled_task(tmp_config, db, mailer, supervisor):
    service = _service(tmp_config, db, mailer, lambda *args: {})
    run_at = datetime.now() + timedelta(hours=1)
    task = service.create_task(
        _payload(scheduled_at=run_at.strftime("%Y-%m-%d %H:%M")), actor=supervisor
    )
    cancelled = service.cancel(task["task_no"], actor=supervisor)
    assert cancelled["status"] == "cancelled"
    assert service.tick(now=run_at + timedelta(minutes=1)) == []


def test_email_validation_and_schedule_in_past(tmp_config, db, mailer, supervisor):
    with pytest.raises(AuthError):
        validate_email("not-an-email")
    service = _service(tmp_config, db, mailer, lambda *args: {})
    with pytest.raises(AuthError):
        service.create_task(_payload(scheduled_at="2020-01-01 10:00"), actor=supervisor)


def test_task_omitted_profile_fields_preserve_company_and_keywords(
    tmp_config, db, mailer, supervisor
):
    from workbench.whitelist import IPService

    ips = IPService(tmp_config, db)
    ip = ips.get_or_create(
        "已有IP",
        company_code="owner",
        company_name="授权方",
        keywords=["已有别名"],
        actor=supervisor,
    )
    ips.add_company_entries(
        supervisor, "owner", [{"platform_group": "taobao", "shop_name": "授权共享店"}]
    )
    service = _service(tmp_config, db, mailer, lambda *args: {})
    task = service.create_task(
        {"ip_name": "已有IP", "platforms": ["taobao"]}, actor=supervisor, run_now=False
    )
    assert ips.get(ip["id"])["company_code"] == "owner"
    assert ips.get(ip["id"])["company_name"] == "授权方"
    assert task["keywords"] == ["已有别名"]
    assert task["company_code"] == "owner"
    assert any(entry["shop_name"] == "授权共享店" for entry in ips.entries(ip["id"]))


def test_invalid_schedule_does_not_modify_profile_or_whitelist(
    tmp_config, db, mailer, supervisor
):
    from workbench.whitelist import IPService

    ips = IPService(tmp_config, db)
    ip = ips.get_or_create(
        "已有IP", company_code="owner", keywords=["已有别名"], actor=supervisor
    )
    ips.save_task_whitelist(
        ip["id"],
        [{"platform_group": "taobao", "shop_name": "授权店"}],
        actor=supervisor,
    )
    before = ips.get(ip["id"])
    entries = ips.entries(ip["id"])
    service = _service(tmp_config, db, mailer, lambda *args: {})
    with pytest.raises(AuthError):
        service.create_task(
            {
                "ip_name": "已有IP",
                "platforms": ["taobao"],
                "company_code": "changed",
                "keywords": ["changed"],
                "whitelist": [],
                "scheduled_at": "2020-01-01 10:00",
            },
            actor=supervisor,
            run_now=False,
        )
    assert ips.get(ip["id"]) == before
    assert ips.entries(ip["id"]) == entries


def test_task_exports_use_current_run_and_distinct_filenames(
    tmp_config, db, mailer, supervisor
):
    service = _service(tmp_config, db, mailer, lambda *args: {})
    first = service.create_task(
        _payload(notify_email=""), actor=supervisor, run_now=False
    )
    second = service.create_task(
        _payload(notify_email=""), actor=supervisor, run_now=False
    )
    configs = [
        service._prepare_run(task, service.preview_ip(task["ip_name"])["ip"])[
            "settings"
        ]
        for task in (first, second)
    ]
    for task, config in zip((first, second), configs):
        assert config.get("export.run_id") == task["task_no"]
        assert task["task_no"] in config.get("export.filename")
        assert config.get("run.progress_scope") == task["task_no"]
    assert configs[0].get("export.filename") != configs[1].get("export.filename")


def test_default_crawler_path_keeps_original_browser_and_throttle(
    tmp_config, db, mailer, supervisor
):
    import yaml

    tmp_config.crawler_config_path = None
    service = _service(tmp_config, db, mailer, lambda *args: {})
    task = service.create_task(
        _payload(notify_email=""), actor=supervisor, run_now=False
    )
    prepared = service._prepare_run(task, service.preview_ip(task["ip_name"])["ip"])
    original = yaml.safe_load(
        (tmp_config.project_root / "crawler/config/windows.yaml").read_text(
            encoding="utf-8-sig"
        )
    )
    actual = yaml.safe_load(prepared["crawler_config"].read_text(encoding="utf-8"))
    assert actual.get("browser") == original["browser"]
    assert (
        actual["crawl"]["taobao"]["rate_limit"]
        == original["crawl"]["taobao"]["rate_limit"]
    )
