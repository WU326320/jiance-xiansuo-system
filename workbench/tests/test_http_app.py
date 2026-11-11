from __future__ import annotations

import pytest

from workbench.tests.conftest import Client


def test_task_end_time_http_starts_now(client, supervisor, monkeypatch):
    calls = []
    client.app.tasks.pipeline_executor = lambda service, task, prepared: (
        calls.append(task) or {"status": "success"}
    )
    assert client.login("supervisor", "Super1234")[0] == 200
    status, body, _ = client.request(
        "POST",
        "/api/tasks",
        json_body={
            "ip_name": "即时任务",
            "platforms": ["taobao"],
            "ends_at": "2099-01-01T10:00",
            "scheduled_at": "2098-01-01T10:00",
            "_sync": True,
        },
    )
    assert status == 201
    assert len(calls) == 1
    assert body["task"]["status"] == "success"
    assert body["task"]["started_at"]
    task_no = body["task"]["task_no"]
    status, body, _ = client.request("GET", f"/api/tasks/{task_no}")
    assert status == 200
    assert body["task"]["ends_at"] == "2099-01-01 10:00:00"
    assert client.app.tasks.scheduled() == []
    for value in ("not-a-time", "2020-01-01T10:00"):
        status, body, _ = client.request(
            "POST",
            "/api/tasks",
            json_body={
                "ip_name": "非法截止任务",
                "platforms": ["taobao"],
                "ends_at": value,
            },
        )
        assert status == 400 and body["code"] == "bad_datetime"
    assert len(calls) == 1


@pytest.mark.parametrize("deadline", [{}, {"ends_at": ""}, {"ends_at": None}])
def test_task_http_without_deadline_collects_all_keywords(client, supervisor, deadline):
    calls = []

    def executor(service, task, prepared):
        calls.append(task["keywords"])
        assert not prepared["settings"].get("run.ends_at")
        return {"status": "success"}

    client.app.tasks.pipeline_executor = executor
    assert client.login("supervisor", "Super1234")[0] == 200
    keywords = [f"不限量关键词{n}" for n in range(12)]
    status, body, _ = client.request(
        "POST",
        "/api/tasks",
        json_body={
            "ip_name": "不限量接口验收",
            "platforms": ["taobao", "jd", "1688"],
            "keywords": keywords,
            "notify_email": "",
            "_sync": True,
            **deadline,
        },
    )
    assert status == 201, body
    assert calls == [keywords]
    assert body["task"]["ends_at"] == ""
    assert body["task"]["result"]["stop_reason"] is None


@pytest.mark.parametrize("outcome", ["success", "partial"])
@pytest.mark.parametrize("notify", [True, False])
def test_deadline_waits_for_details_before_terminal_state_and_mail(
    client, supervisor, mailbox, outcome, notify
):
    from concurrent.futures import ThreadPoolExecutor
    from datetime import datetime, timedelta
    from threading import Event
    from workbench.auth import AuthError

    entered, release = Event(), Event()
    clock = [datetime.now()]
    deadline = clock[0] + timedelta(seconds=5)
    running = []
    client.app.tasks.clock = lambda: clock[0]

    def executor(service, task, prepared):
        clock[0] = deadline + timedelta(seconds=1)
        assert prepared["settings"].get("run.ends_at") == deadline.strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        running.append(task["task_no"])
        entered.set()
        assert release.wait(10), "测试必须释放补采等待"
        return {
            "status": outcome,
            "stop_reason": "deadline",
            "errors": ["1688:123 补采重试耗尽"] if outcome == "partial" else [],
        }

    client.app.tasks.pipeline_executor = executor
    assert client.login("supervisor", "Super1234")[0] == 200
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            client.request,
            "POST",
            "/api/tasks",
            json_body={
                "ip_name": "截止补采接口验收",
                "platforms": ["1688"],
                "ends_at": deadline.isoformat(timespec="seconds"),
                "_sync": True,
                "notify_email": "owner@example.test" if notify else "",
            },
        )
        try:
            assert entered.wait(10)
            assert clock[0] > deadline
            status, body, _ = client.request("GET", f"/api/tasks/{running[0]}")
            assert status == 200 and body["task"]["status"] == "running"
            assert body["task"]["ended_at"] is None
            assert mailbox == []
            assert client.app.tasks.tick(now=clock[0]) == []
            with pytest.raises(AuthError):
                client.app.tasks.execute(running[0])
        finally:
            release.set()
        status, body, _ = future.result(timeout=10)
    assert status == 201 and body["task"]["status"] == outcome
    assert body["task"]["ended_at"]
    assert body["task"]["mail_status"] == ("sent" if notify else "skipped")
    assert len(mailbox) == int(notify)
    if notify and outcome == "partial":
        assert "补采重试耗尽" in mailbox[0][0].get_content()
    with pytest.raises(AuthError):
        client.app.tasks.execute(running[0])
    assert len(mailbox) == int(notify)


def test_login_and_setup_pages(client):
    status, body, headers = client.request("GET", "/login")
    assert status == 200 and "登录" in body
    status, body, headers = client.request("GET", "/setup")
    assert status == 200 and "主管" in body
    status, body, _ = client.request(
        "POST",
        "/api/setup",
        json_body={"username": "boss", "password": "Boss1234", "confirm": "Boss1234"},
    )
    assert status == 200 and body["user"]["role"] == "supervisor"
    status, body, _ = client.request("GET", "/")
    assert status == 200 and "侵权线索工作台" in body
    status, body, _ = client.request("GET", "/login")
    assert status == 200


def test_network_allowlist_denies_unknown_source(client):
    status, body, _ = client.request("GET", "/api/health", remote_addr="8.8.8.8")
    assert status == 403 and body["code"] == "network_denied"


def test_settings_admin_only_and_no_secret(client, admin, normal_user):
    assert client.login("admin", "Admin1234")[0] == 200
    status, body, _ = client.request("GET", "/api/settings")
    assert status == 200
    assert "password" not in str(body).lower()
    status, body, _ = client.request(
        "PUT",
        "/api/settings",
        json_body={"allowed_networks": "127.0.0.1/32,10.0.0.0/8", "smtp_port": "465"},
    )
    assert status == 200
    assert body["settings"]["allowed_networks"] == "127.0.0.1/32,10.0.0.0/8"

    assert client.login("user1", "User1234")[0] == 200
    assert client.request("GET", "/api/settings")[0] == 403
    assert (
        client.request("PUT", "/api/settings", json_body={"smtp_port": "25"})[0] == 403
    )


def test_audit_log_records_writes(client, supervisor):
    assert client.login("supervisor", "Super1234")[0] == 200
    assert client.request("POST", "/api/ips", json_body={"name": "审计测试"})[0] == 201
    status, body, _ = client.request("GET", "/api/audit?limit=100")
    assert status == 200
    actions = {row["action"] for row in body["items"]}
    assert "ip.create" in actions
    assert any(row["source_ip"] == "127.0.0.1" for row in body["items"])


def test_change_password_and_reauth(client, normal_user):
    assert client.login("user1", "User1234")[0] == 200
    status, body, _ = client.request(
        "POST",
        "/api/account/password",
        json_body={
            "old_password": "User1234",
            "new_password": "User5678",
            "confirm": "User5678",
        },
    )
    assert status == 200
    assert client.request("POST", "/api/logout")[0] == 200
    assert client.login("user1", "User1234")[0] == 401
    assert client.login("user1", "User5678")[0] == 200


def test_register_via_http_with_invite(client, supervisor, tmp_config):
    assert client.login("supervisor", "Super1234")[0] == 200
    status, body, _ = client.request(
        "POST",
        "/api/invites",
        json_body={"role": "user", "max_uses": 2, "expires_minutes": 30},
    )
    assert status == 201
    code = body["invite"]["code"]
    fresh = Client(client.app)
    status, body, _ = fresh.request(
        "POST",
        "/api/register",
        json_body={
            "code": code,
            "username": "newbie",
            "password": "Newbie123",
            "confirm": "Newbie123",
        },
    )
    assert status == 200 and body["user"]["role"] == "user"
    assert fresh.request("GET", "/api/me")[0] == 200
    # 第二次注册仍可用（max_uses=2），第三次失败
    second = Client(client.app)
    assert (
        second.request(
            "POST",
            "/api/register",
            json_body={
                "code": code,
                "username": "newbie2",
                "password": "Newbie123",
                "confirm": "Newbie123",
            },
        )[0]
        == 200
    )
    third = Client(client.app)
    status, body, _ = third.request(
        "POST",
        "/api/register",
        json_body={
            "code": code,
            "username": "newbie3",
            "password": "Newbie123",
            "confirm": "Newbie123",
        },
    )
    assert status == 400


def test_missing_api_returns_404(client, normal_user):
    assert client.login("user1", "User1234")[0] == 200
    assert client.request("GET", "/api/nope")[0] == 404


def test_bind_address_rejects_wildcard_and_public(tmp_config):
    import pytest

    from workbench.config import ConfigError

    tmp_config.host = "0.0.0.0"
    with pytest.raises(ConfigError):
        tmp_config.validate_host()
    tmp_config.host = "8.8.8.8"
    with pytest.raises(ConfigError):
        tmp_config.validate_host()
    tmp_config.host = "192.168.1.20"
    assert tmp_config.validate_host() == "192.168.1.20"


def test_task_http_preserves_profile_and_rejects_invalid_schedule_without_changes(
    client, supervisor
):
    from datetime import datetime, timedelta

    assert client.login("supervisor", "Super1234")[0] == 200
    status, body, _ = client.request(
        "POST",
        "/api/ips",
        json_body={
            "name": "已有IP",
            "company_code": "owner",
            "company_name": "授权方",
            "keywords": ["已有别名"],
        },
    )
    assert status == 201
    payload = {
        "ip_name": "已有IP",
        "platforms": ["taobao"],
        "scheduled_at": (datetime.now() + timedelta(hours=2)).strftime(
            "%Y-%m-%d %H:%M"
        ),
    }
    status, body, _ = client.request("POST", "/api/tasks", json_body=payload)
    assert status == 201
    assert body["task"]["company_code"] == "owner"
    assert body["task"]["keywords"] == ["已有别名"]
    status, before, _ = client.request(
        "POST", "/api/tasks/preview-ip", json_body={"name": "已有IP"}
    )
    assert status == 200
    status, body, _ = client.request(
        "POST",
        "/api/tasks",
        json_body={
            **payload,
            "company_code": "changed",
            "scheduled_at": "2020-01-01 10:00",
        },
    )
    assert status == 400
    _, after, _ = client.request(
        "POST", "/api/tasks/preview-ip", json_body={"name": "已有IP"}
    )
    assert after == before


def test_task_export_requires_safe_existing_xlsx(client, supervisor, tmp_config):
    assert client.request("GET", "/api/tasks/unknown/export")[0] == 401
    assert client.login("supervisor", "Super1234")[0] == 200
    status, body, _ = client.request(
        "POST",
        "/api/tasks",
        json_body={
            "ip_name": "导出测试",
            "platforms": ["taobao"],
            "scheduled_at": "2099-01-01 10:00",
        },
    )
    assert status == 201
    task_no = body["task"]["task_no"]
    assert client.request("GET", f"/api/tasks/{task_no}/export")[0] == 404
    export_dir = tmp_config.exports_dir
    export_dir.mkdir(parents=True, exist_ok=True)
    export_file = export_dir / "线索_测试.xlsx"
    export_file.write_bytes(b"xlsx")
    client.app.db.execute(
        "UPDATE tasks SET excel_path=? WHERE task_no=?", (str(export_file), task_no)
    )
    status, body, headers = client.request("GET", f"/api/tasks/{task_no}/export")
    assert status == 200 and body == "xlsx"
    assert headers["Content-Type"].startswith("application/vnd.openxmlformats")
    assert "filename*=UTF-8''" in headers["Content-Disposition"]
    headers["Content-Disposition"].encode("ascii")
    outside = tmp_config.data_dir.parent / "outside.xlsx"
    outside.write_bytes(b"private")
    client.app.db.execute(
        "UPDATE tasks SET excel_path=? WHERE task_no=?",
        (str(outside), task_no),
    )
    assert client.request("GET", f"/api/tasks/{task_no}/export")[0] == 404
    invalid = export_dir / "not-excel.txt"
    invalid.write_text("private", encoding="utf-8")
    client.app.db.execute(
        "UPDATE tasks SET excel_path=? WHERE task_no=?", (str(invalid), task_no)
    )
    assert client.request("GET", f"/api/tasks/{task_no}/export")[0] == 404


def test_task_api_exposes_incomplete_amounts(client, supervisor):
    platform = {
        "items": 3,
        "amount_complete": 1,
        "amount_missing": 2,
        "missing_fields": {"comments": 2, "price": 1, "sales": 1},
        "status": "ok",
    }
    client.app.tasks.pipeline_executor = lambda *_: {
        "status": "success",
        "platforms": {"1688": platform},
        "stop_reason": "deadline",
    }
    assert client.login("supervisor", "Super1234")[0] == 200
    status, body, _ = client.request(
        "POST",
        "/api/tasks",
        json_body={"ip_name": "金额缺失回归", "platforms": ["1688"], "_sync": True},
    )
    assert status == 201
    task = body["task"]
    assert task["status"] == "partial"
    assert task["status_label"] == "部分完成（金额待核验）"
    status, body, _ = client.request("GET", f"/api/tasks/{task['task_no']}")
    assert status == 200
    assert body["task"]["result"]["platforms"]["1688"] == platform
    assert body["task"]["result"]["stop_reason"] == "deadline"
    status, body, _ = client.request("GET", "/api/tasks")
    assert status == 200
    assert body["items"][0]["result"]["platforms"]["1688"]["amount_missing"] == 2
