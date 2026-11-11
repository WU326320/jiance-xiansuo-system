"""setup-smtp：短授权码拒绝保存；保存成功后自动执行 test-smtp。"""

from __future__ import annotations

from pathlib import Path

import yaml

from pipeline.cli import main

from tests.conftest import FakeSMTP


def _write_config(tmp_path: Path, settings) -> Path:
    path = tmp_path / "rules.yaml"
    path.write_text(
        yaml.safe_dump(settings.raw, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return path


def test_setup_smtp_rejects_short_password_without_saving(
    tmp_path, settings, fake_smtp, monkeypatch, capsys
):
    short = "1234567890abcde"  # 15 位
    monkeypatch.setattr("getpass.getpass", lambda prompt="": short)
    saved: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        "keyring.set_password",
        lambda service, user, password: saved.append((service, user, password)),
    )
    monkeypatch.setattr("keyring.get_password", lambda service, user: None)

    code = main(["--config", str(_write_config(tmp_path, settings)), "setup-smtp"])

    captured = capsys.readouterr()
    assert code == 1
    assert "16" in captured.out
    assert "拒绝保存" in captured.out
    assert saved == []
    assert FakeSMTP.instances == []


def test_setup_smtp_success_runs_test_smtp_automatically(
    tmp_path, settings, fake_smtp, monkeypatch, capsys
):
    password = "SMTP_VALID_AUTH_CODE_123456"
    stored: dict[tuple[str, str], str] = {}
    monkeypatch.setattr("getpass.getpass", lambda prompt="": password)
    monkeypatch.setattr(
        "keyring.set_password",
        lambda service, user, value: stored.__setitem__((service, user), value),
    )
    monkeypatch.setattr(
        "keyring.get_password", lambda service, user: stored.get((service, user))
    )

    code = main(["--config", str(_write_config(tmp_path, settings)), "setup-smtp"])

    captured = capsys.readouterr()
    assert code == 0
    assert "已写入 Windows 凭据管理器" in captured.out
    assert "自动测试：已发送" in captured.out
    assert password not in captured.out + captured.err

    assert list(stored.values()) == [password]
    assert len(FakeSMTP.instances) == 1
    client = FakeSMTP.instances[0]
    assert client.logged_in is True
    assert client.password == password
    assert client.messages[0][0]["Subject"] == "[侵权线索] SMTP 测试"
