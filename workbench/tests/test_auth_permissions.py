from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from workbench.auth import AuthError, AuthService, PermissionError_
from workbench.db import Database, now_text
from workbench.security import hash_password, verify_password


def test_first_start_supervisor_is_unique_and_password_hashed(tmp_config, db):
    auth = AuthService(tmp_config, db)
    assert not auth.has_supervisor()
    user = auth.create_supervisor("boss", "Boss1234", source_ip="127.0.0.1")
    assert user["role"] == "supervisor"
    row = db.fetchone("SELECT * FROM users WHERE id=?", (user["id"],))
    assert "Boss1234" not in row["password_hash"]
    assert row["password_hash"].startswith("scrypt$")
    assert verify_password("Boss1234", row["password_hash"])
    with pytest.raises(AuthError):
        auth.create_supervisor("boss2", "Boss5678")


def test_three_level_permissions_and_supervisor_grant(client, supervisor, admin, normal_user):
    status, body = client.login("supervisor", "Super1234")
    assert status == 200 and body["user"]["role"] == "supervisor"
    assert client.request("POST", "/api/invites", json_body={"role": "admin"})[0] == 201

    # 先增加第二个管理员，验证「不能降级最后一个管理员」和正常升降级。
    invite = client.app.auth.create_invite(supervisor, role="admin")
    client.app.auth.register(invite["code"], "admin2", "Admin1234")
    status, body, _ = client.request(
        "POST", "/api/users/%d/role" % admin["id"], json_body={"role": "user"}
    )
    assert status == 200
    status, _, _ = client.request(
        "POST", "/api/users/%d/role" % admin["id"], json_body={"role": "admin"}
    )
    assert status == 200

    assert client.login("admin", "Admin1234")[0] == 200
    assert client.request("POST", "/api/invites", json_body={"role": "admin"})[0] == 403
    assert client.request("POST", "/api/invites", json_body={"role": "user"})[0] == 201
    assert client.request("GET", "/api/settings")[0] == 200
    status, body, _ = client.request("POST", "/api/users/%d/disable" % normal_user["id"])
    assert status == 200
    assert client.request("POST", "/api/users/%d/disable" % admin["id"])[0] == 403

    assert client.login("user1", "User1234")[1]["error"] == "账号或密码错误"
    # 重新启用一个用户用于普通用户权限断言
    db = client.app.db
    db.execute("UPDATE users SET status='active' WHERE id=?", (normal_user["id"],))
    assert client.login("user1", "User1234")[0] == 200
    assert client.request("GET", "/api/settings")[0] == 403
    assert client.request("POST", "/api/ips", json_body={"name": "x"})[0] == 403
    assert client.request("GET", "/api/leads")[0] == 200


def test_unauthenticated_pages_and_apis(client):
    status, _, headers = client.request("GET", "/")
    assert status == 302
    assert headers["Location"] == "/setup"
    status, body, _ = client.request("GET", "/api/leads")
    assert status == 401 and body["code"] == "unauthenticated"


def test_bootstrap_setup_only_loopback(client):
    status, body, _ = client.request(
        "POST",
        "/api/setup",
        json_body={"username": "boss", "password": "Boss1234", "confirm": "Boss1234"},
        remote_addr="192.168.1.9",
    )
    assert status == 403
    status, body, _ = client.request(
        "POST",
        "/api/setup",
        json_body={"username": "boss", "password": "Boss1234", "confirm": "Boss1234"},
    )
    assert status == 200 and body["user"]["role"] == "supervisor"
    status, _, headers = client.request("GET", "/")
    assert status == 200


def test_invite_expiry_and_use_limit(db, tmp_config, supervisor):
    auth = AuthService(tmp_config, db)
    invite = auth.create_invite(supervisor, role="user", max_uses=1, expires_minutes=10)
    user = auth.register(invite["code"], "u1", "User1234")
    assert user["role"] == "user"
    with pytest.raises(AuthError):
        auth.register(invite["code"], "u2", "User1234")
    expired = auth.create_invite(supervisor, role="user", max_uses=5, expires_minutes=10)
    last = db.fetchone("SELECT id FROM invites ORDER BY id DESC LIMIT 1")
    db.execute(
        "UPDATE invites SET expires_at=? WHERE id=?",
        ((datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S"), last["id"]),
    )
    with pytest.raises(AuthError):
        auth.register(expired["code"], "u3", "User1234")


def test_disable_account_revokes_session(client, db, supervisor, normal_user):
    from workbench.tests.conftest import Client

    user_client = Client(client.app)
    assert user_client.login("user1", "User1234")[0] == 200
    assert user_client.request("GET", "/api/me")[0] == 200
    assert client.login("supervisor", "Super1234")[0] == 200
    assert client.request("POST", "/api/users/%d/disable" % normal_user["id"])[0] == 200
    assert user_client.request("GET", "/api/me")[0] == 401


def test_login_lock_after_failures(client, supervisor):
    for _ in range(5):
        status, body = client.login("supervisor", "wrong-pass")
        assert status == 401
    status, body = client.login("supervisor", "Super1234")
    assert status == 429
    rows = client.app.db.fetchall("SELECT * FROM audit_log WHERE action='login.failure'")
    assert len(rows) >= 5


def test_supervisor_cannot_be_demoted_or_disabled(db, tmp_config, supervisor):
    auth = AuthService(tmp_config, db)
    with pytest.raises(PermissionError_):
        auth.disable_user(supervisor, supervisor["id"])
    with pytest.raises(PermissionError_):
        auth.set_role(supervisor, supervisor["id"], "user")
