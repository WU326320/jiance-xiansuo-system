"""test-smtp 命令：复用报警邮件发送代码，失败原因必须脱敏。"""

from __future__ import annotations

import smtplib
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


def test_test_smtp_sends_expected_message(
    tmp_path, settings, fake_smtp, monkeypatch, capsys
):
    secret = "SMTP_SUCCESS_AUTH_CODE_7c1e"
    # 显式测试命令不受报警总开关影响，仍应真实走 SMTP 配置。
    settings.raw["alert"]["enabled"] = False
    monkeypatch.setattr("keyring.get_password", lambda service, user: secret)

    code = main(["--config", str(_write_config(tmp_path, settings)), "test-smtp"])

    captured = capsys.readouterr()
    assert code == 0
    assert "已发送" in captured.out
    assert secret not in captured.out + captured.err

    assert len(fake_smtp.instances) == 1
    client = fake_smtp.instances[0]
    assert client.host == "smtp.example.test"
    assert client.port == 465
    assert client.timeout == 3
    assert client.logged_in is True
    assert client.user == "sender@example.test"
    assert client.password == secret

    message, to_addrs = client.messages[0]
    assert message["Subject"] == "[侵权线索] SMTP 测试"
    assert message["From"] == "sender@example.test"
    assert message["To"] == "receiver@example.test"
    assert to_addrs == ["receiver@example.test"]
    assert "SMTP 测试邮件" in message.get_content()


class _AuthFailSMTP(FakeSMTP):
    def login(self, user, password):
        # 故意把授权码塞进服务端响应，验证输出/日志都会脱敏。
        raise smtplib.SMTPAuthenticationError(
            535, f"535 authentication failed password={password}".encode()
        )


def test_test_smtp_auth_failure_prints_reason_without_auth_code(
    tmp_path, settings, fake_smtp, monkeypatch, capsys
):
    secret = "SMTP_FAIL_AUTH_CODE_93bd"
    monkeypatch.setattr("keyring.get_password", lambda service, user: secret)
    monkeypatch.setattr("smtplib.SMTP_SSL", _AuthFailSMTP)
    monkeypatch.setattr("smtplib.SMTP", _AuthFailSMTP)

    code = main(["--config", str(_write_config(tmp_path, settings)), "test-smtp"])

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert code == 1
    assert "发送失败" in captured.out
    assert "SMTPAuthenticationError" in captured.out
    assert "******" in captured.out
    assert secret not in combined
    assert len(fake_smtp.instances) == 1
    assert fake_smtp.instances[0].messages == []
