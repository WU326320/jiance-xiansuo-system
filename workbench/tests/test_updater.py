from __future__ import annotations

import base64
import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest

from workbench.updater import UpdateError, VersionManager


def _zip_bytes(version: str, *, marker: str = "ok", extra: dict[str, str] | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "workbench/version.json", json.dumps({"version": version}, ensure_ascii=False)
        )
        archive.writestr("workbench/app.py", f"# {version} {marker}\n")
        for name, content in (extra or {}).items():
            archive.writestr(name, content)
    return buffer.getvalue()


def test_update_backs_up_and_rollback_restores_previous(tmp_config, db, supervisor):
    manager = VersionManager(tmp_config, db)
    v1 = manager.install(_zip_bytes("1.0.0", marker="one"), filename="v1.zip", actor=supervisor)
    assert manager.current()["version"] == "1.0.0"
    v2 = manager.install(_zip_bytes("1.1.0", marker="two"), filename="v2.zip", actor=supervisor)
    assert manager.current()["version"] == "1.1.0"
    assert list(tmp_config.version_backups_dir.glob("*.zip"))
    assert db.fetchone("SELECT COUNT(*) AS n FROM users")["n"] == 1
    rolled = manager.rollback(actor=supervisor)
    assert rolled["version"] == "1.0.0"
    assert manager.current()["version"] == "1.0.0"
    assert (tmp_config.data_dir / "versions").exists()
    # 回滚后数据库仍在原位置，未被删除
    assert tmp_config.db_path.exists()


def test_update_rejects_bad_zip_and_path_traversal(tmp_config, db):
    manager = VersionManager(tmp_config, db)
    with pytest.raises(UpdateError):
        manager.install(b"not-a-zip")
    with pytest.raises(UpdateError):
        manager.install(_zip_bytes("1.0.0", extra={"../evil.txt": "x"}))
    with pytest.raises(UpdateError):
        manager.install(_zip_bytes("1.0.0", extra={"/abs.txt": "x"}))


def test_rollback_without_previous(tmp_config, db):
    manager = VersionManager(tmp_config, db)
    with pytest.raises(UpdateError):
        manager.rollback()


def test_http_update_is_admin_only_and_rollback(client, tmp_config, supervisor, admin, normal_user):
    v1 = base64.b64encode(_zip_bytes("2.0.0")).decode()
    v2 = base64.b64encode(_zip_bytes("2.1.0")).decode()
    v3 = base64.b64encode(_zip_bytes("2.2.0")).decode()
    assert client.login("supervisor", "Super1234")[0] == 200
    status, body, _ = client.request(
        "POST", "/api/admin/update", json_body={"filename": "v2.zip", "content_base64": v1}
    )
    assert status == 200 and body["version"]["version"] == "2.0.0"
    assert client.request(
        "POST", "/api/admin/update", json_body={"filename": "v21.zip", "content_base64": v2}
    )[0] == 200
    assert client.request("GET", "/api/admin/versions")[0] == 200
    assert client.request("POST", "/api/admin/rollback")[0] == 200
    versions = client.request("GET", "/api/admin/versions")[1]
    assert versions["current"]["version"] == "2.0.0"

    assert client.login("admin", "Admin1234")[0] == 200
    assert client.request(
        "POST", "/api/admin/update", json_body={"filename": "v22.zip", "content_base64": v3}
    )[0] == 200
    assert client.request("POST", "/api/admin/rollback")[0] == 200

    assert client.login("user1", "User1234")[0] == 200
    assert client.request(
        "POST", "/api/admin/update", json_body={"filename": "v23.zip", "content_base64": v1}
    )[0] == 403
    assert client.request("POST", "/api/admin/rollback")[0] == 403


def test_launcher_resolves_installed_release(tmp_config, db, supervisor, monkeypatch):
    from workbench import launcher

    monkeypatch.setenv("WORKBENCH_DATA_DIR", str(tmp_config.data_dir))
    manager = VersionManager(tmp_config, db)
    data = _zip_bytes("3.0.0", extra={"workbench/__main__.py": "print('new')\n"})
    state = manager.install(data, filename="v3.zip", actor=supervisor)
    assert launcher.resolve_release() == Path(state["path"])
