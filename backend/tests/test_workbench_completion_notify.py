"""工作台 v1：后端「每轮完成通知」替代原「仅高风险预警」。"""

from __future__ import annotations

from datetime import date

from pipeline.runner import PipelineRunner

from tests.conftest import FakeAdapter, make_item, platform_data


def test_completion_notification_sends_even_without_high_risk(
    settings, fake_smtp, monkeypatch
):
    settings.raw["alert"]["enabled"] = True
    settings.raw["alert"]["min_items_per_platform"] = 1
    monkeypatch.setattr("keyring.get_password", lambda service, user: "auth-code")
    adapter = FakeAdapter(
        {
            "taobao": platform_data(
                "taobao",
                [make_item(price=10, sales=10, comments=10)],
            )
        }
    )
    summary = PipelineRunner(settings, adapter=adapter).run(
        platforms=["taobao"], run_date=date(2026, 11, 6)
    )
    assert summary.failed is False
    assert summary.alerted is True
    assert len(fake_smtp.instances) == 1
    message, recipients = fake_smtp.instances[0].messages[0]
    body = message.get_content()
    assert "运行完成通知" in message["Subject"]
    assert "候选数（线索）" in body
    assert "风险等级" in body
    assert "异常摘要：无" in body
    assert "auth-code" not in body
