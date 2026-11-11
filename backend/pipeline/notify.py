"""邮件报警。

授权码只从 Windows 凭据管理器（keyring）读取；本模块不接收、不返回、
不记录授权码。发送失败只写日志，不影响主流程。
"""

from __future__ import annotations

import logging
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Iterable

import keyring
from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.config import Settings
from pipeline.models import Lead, LeadLink, ProductSnapshot, RunLog


TEST_SUBJECT = "[侵权线索] SMTP 测试"
TEST_BODY = (
    "这是一封 SMTP 测试邮件。\n\n"
    "收到本邮件说明 config/rules.yaml 中的 SMTP 主机、端口、发件人、收件人"
    "与 keyring 授权码可用。\n"
)


class MissingCredential(RuntimeError):
    pass


@dataclass(frozen=True)
class SendResult:
    """一次邮件发送的结果；``reason`` 只保存已脱敏的失败原因。"""

    sent: bool
    reason: str | None = None


def redact_secret(text: object, secret: str | None = None) -> str:
    """把授权码/密码从任意文本中抹掉，供窗口输出和日志共用。"""

    value = str(text)
    if secret:
        value = value.replace(str(secret), "******")
    return value


def keyring_service(settings: Settings) -> str:
    return str(settings.smtp.get("keyring_service") or "ip-lead-pipeline-smtp")


def keyring_username(settings: Settings) -> str:
    return str(settings.smtp.get("from") or "")


def get_smtp_password(settings: Settings) -> str | None:
    return keyring.get_password(keyring_service(settings), keyring_username(settings))


def _send_email(
    settings: Settings,
    *,
    subject: str,
    body: str,
    logger: logging.Logger | None = None,
    respect_enabled: bool = True,
    recipients: Iterable[str] | None = None,
) -> SendResult:
    logger = logger or logging.getLogger("pipeline")
    cfg = settings.smtp
    if respect_enabled and not cfg.get("enabled", True):
        reason = "邮件报警已禁用（alert.enabled=false）"
        logger.info("%s，跳过发送: %s", reason, subject)
        return SendResult(False, reason)
    try:
        password = get_smtp_password(settings)
    except Exception as exc:  # noqa: BLE001 - 凭据后端不可用不应中断主流程
        reason = f"读取凭据管理器失败：{type(exc).__name__}: {redact_secret(exc)}"
        logger.error("%s", reason)
        return SendResult(False, reason)
    if not password:
        reason = (
            "未找到 SMTP 授权码"
            f"（服务名 {keyring_service(settings)}，用户名 {keyring_username(settings)}）；"
            "请先运行 python -m pipeline setup-smtp"
        )
        logger.error("%s", reason)
        return SendResult(False, reason)

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = keyring_username(settings)
    if recipients is None:
        recipients = cfg.get("to") or []
    if isinstance(recipients, str):
        recipients = [recipients]
    recipient_list = [str(item).strip() for item in recipients if str(item).strip()]
    if not recipient_list:
        reason = "未配置收件人，跳过发送"
        logger.info("%s: %s", reason, subject)
        return SendResult(False, reason)
    message["To"] = ", ".join(recipient_list)
    message.set_content(body)

    host = str(cfg.get("smtp_host") or "smtp.qiye.aliyun.com")
    port = int(cfg.get("smtp_port") or 465)
    use_ssl = bool(cfg.get("smtp_ssl", True))
    timeout = int(cfg.get("timeout_seconds", 30))
    try:
        if use_ssl:
            client = smtplib.SMTP_SSL(host, port, timeout=timeout)
        else:
            client = smtplib.SMTP(host, port, timeout=timeout)
        with client:
            client.login(keyring_username(settings), password)
            client.send_message(message, to_addrs=recipient_list)
        logger.info("邮件已发送: %s -> %s", subject, ", ".join(recipient_list))
        return SendResult(True)
    except Exception as exc:  # noqa: BLE001 - 邮件失败不影响主流程
        # 某些 SMTP/代理异常会把用户名、密码或授权码带进响应文本，统一脱敏。
        reason = f"{type(exc).__name__}: {redact_secret(exc, password)}"
        logger.error("报警邮件发送失败: %s", reason)
        return SendResult(False, reason)


def send_email(
    settings: Settings,
    *,
    subject: str,
    body: str,
    logger: logging.Logger | None = None,
) -> bool:
    return _send_email(settings, subject=subject, body=body, logger=logger).sent


def send_test_email(
    settings: Settings, *, logger: logging.Logger | None = None
) -> SendResult:
    """按当前 SMTP 配置发送测试邮件；显式测试不受 alert.enabled 影响。"""

    return _send_email(
        settings,
        subject=TEST_SUBJECT,
        body=TEST_BODY,
        logger=logger,
        respect_enabled=False,
    )


def _tier_for_lead(lead: Lead, threshold: float) -> str:
    total = float(lead.total_amount or 0)
    missing = str(lead.missing_flags or "")
    if "金额待核验" in missing or "金额超出范围,待核验" in missing:
        return "待核验"
    if total >= threshold:
        return "达标"
    return "临界"


def risk_level_counts(
    session: Session, *, run_id: str, settings: Settings
) -> dict[str, int]:
    """按本次 run 的线索统计三档风险数量（达标/待核验/临界）。"""

    threshold = float(settings.get("aggregate.shop_threshold", 500000) or 500000)
    counts = {"达标": 0, "待核验": 0, "临界": 0}
    rows = session.scalars(
        select(Lead)
        .join(LeadLink, LeadLink.lead_id == Lead.id)
        .join(ProductSnapshot, ProductSnapshot.id == LeadLink.snapshot_id)
        .where(ProductSnapshot.run_id == run_id)
        .distinct()
    ).all()
    for lead in rows:
        tier = _tier_for_lead(lead, threshold)
        counts[tier] = counts.get(tier, 0) + 1
    return counts


def completion_body(
    *,
    settings: Settings,
    summary,
    run_id: str,
    ip_name: str = "",
    status: str | None = None,
    extra_lines: Iterable[str] | None = None,
    risk_counts: dict[str, int] | None = None,
) -> str:
    """任务完成/失败通知正文；有没有线索都发。"""

    platforms = getattr(summary, "platforms", {}) or {}
    errors: list[str] = []
    for name, item in platforms.items():
        item_status = getattr(item, "status", "")
        if item_status not in ("ok", "disabled"):
            detail = getattr(item, "error", "") or item_status
            errors.append(f"{name}: {detail}")
    if getattr(summary, "excel_error", None):
        errors.append(str(summary.excel_error))
    if extra_lines:
        errors.extend(str(line) for line in extra_lines)
    resolved_status = status or (
        "partial" if getattr(summary, "failed", False) else "ok"
    )
    label = {
        "ok": "成功",
        "partial": "部分失败",
        "failed": "失败",
        "success": "成功",
    }.get(str(resolved_status), str(resolved_status))
    lines = [
        f"IP 名：{ip_name or '（未提供）'}",
        f"运行编号：{run_id}",
        f"结果：{label}",
        "",
        "各平台采集数：",
    ]
    total_items = 0
    for name, item in platforms.items():
        items = int(getattr(item, "items", 0) or 0)
        total_items += items
        lines.append(
            f"- {name}：采集 {items} 条，线索 {int(getattr(item, 'leads', 0) or 0)} 条，"
            f"状态 {getattr(item, 'status', '')}"
            + (
                f"，错误 {str(getattr(item, 'error', ''))[:160]}"
                if getattr(item, "error", "")
                else ""
            )
        )
    if not platforms:
        lines.append("- 无平台结果")
    counts = risk_counts or {"达标": 0, "待核验": 0, "临界": 0}
    lines.extend(
        [
            "",
            f"采集合计：{total_items} 条",
            f"候选数（线索）：{int(getattr(summary, 'leads', 0) or 0)}",
            "风险等级："
            + " / ".join(
                f"{key} {counts.get(key, 0)}" for key in ("达标", "待核验", "临界")
            ),
            f"Excel：{getattr(summary, 'excel', None) or '未生成'}",
            "异常摘要：" + ("；".join(dict.fromkeys(errors)) if errors else "无"),
        ]
    )
    return "\n".join(lines) + "\n\n（本邮件由后端自动发送；授权码不落盘。）\n"


def send_completion_email(
    settings: Settings,
    *,
    run_id: str,
    summary,
    session: Session | None = None,
    ip_name: str = "",
    notify_email: str | None = None,
    status: str | None = None,
    logger: logging.Logger | None = None,
) -> SendResult:
    """每轮结束都发一封；没有 SMTP 配置时只写日志、不报错。"""

    logger = logger or logging.getLogger("pipeline")
    lines: list[str] = []
    counts: dict[str, int] = {"达标": 0, "待核验": 0, "临界": 0}
    if session is not None:
        try:
            lines = collect_alert_lines(session, run_id=run_id, settings=settings)
            counts = risk_level_counts(session, run_id=run_id, settings=settings)
        except Exception:  # noqa: BLE001 - 统计失败不应阻断通知
            logger.exception("汇总完成通知统计失败，已使用空统计")
    recipients = [notify_email] if notify_email else (settings.smtp.get("to") or [])
    subject = (
        f"{settings.smtp.get('subject_prefix', '[侵权线索]')} 运行完成通知 {run_id}"
    )
    body = completion_body(
        settings=settings,
        summary=summary,
        run_id=run_id,
        ip_name=ip_name,
        status=status,
        extra_lines=lines,
        risk_counts=counts,
    )
    return _send_email(
        settings,
        subject=subject,
        body=body,
        logger=logger,
        recipients=recipients,
    )


_STAGE_LABELS = {
    "export": "Excel 导出",
    "jd_detail": "京东详情补抓",
    "taobao_detail": "淘宝/天猫详情补抓",
}


def collect_alert_lines(
    session: Session, *, run_id: str, settings: Settings
) -> list[str]:
    """只从本次 run 的 ``run_logs`` 汇总报警，不依赖 runner 内存旁路。"""

    rows = session.scalars(
        select(RunLog)
        .where(RunLog.run_id == run_id, RunLog.stage != "overall")
        .order_by(RunLog.id)
    ).all()
    lines: list[str] = []
    minimum = settings.min_items_per_platform
    for row in rows:
        if row.stage == "crawl":
            platform = row.platform or "?"
            if row.status == "disabled":
                continue
            if row.login_ok is False:
                lines.append(f"{platform}: 登录失效，已停止该平台")
            if row.items_count < minimum:
                lines.append(f"{platform}: 采集条数 {row.items_count} < 下限 {minimum}")
            if row.status not in ("ok", "disabled", "login_failed"):
                detail = f"：{row.error_summary[:500]}" if row.error_summary else ""
                lines.append(f"{platform}: 状态 {row.status}{detail}")
            continue

        if row.status in ("ok", "disabled"):
            continue
        label = _STAGE_LABELS.get(row.stage, row.stage)
        detail = f"：{row.error_summary[:500]}" if row.error_summary else ""
        lines.append(f"{label}: 状态 {row.status}{detail}")
    return lines


def alert_for_run(
    session: Session,
    *,
    run_id: str,
    settings: Settings,
    logger: logging.Logger | None = None,
    extra_lines: Iterable[str] | None = None,
) -> bool:
    logger = logger or logging.getLogger("pipeline")
    lines = collect_alert_lines(session, run_id=run_id, settings=settings)
    if extra_lines:
        lines.extend(str(item) for item in extra_lines)
    if not lines:
        logger.info("报警检查: 无异常")
        return False
    subject = f"{settings.smtp.get('subject_prefix', '[侵权线索]')} 运行报警 {run_id}"
    body = "本次运行触发以下报警：\n\n" + "\n".join(f"- {line}" for line in lines)
    body += "\n\n（本邮件由后端自动发送；授权码不落盘。）"
    return send_email(settings, subject=subject, body=body, logger=logger)


__all__ = [
    "MissingCredential",
    "SendResult",
    "TEST_BODY",
    "TEST_SUBJECT",
    "alert_for_run",
    "collect_alert_lines",
    "completion_body",
    "risk_level_counts",
    "send_completion_email",
    "get_smtp_password",
    "keyring_service",
    "keyring_username",
    "redact_secret",
    "send_email",
    "send_test_email",
]
