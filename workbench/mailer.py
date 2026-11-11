"""任务完成通知邮件。

每轮任务只调用一次。没有 SMTP 配置、没有授权码或发送失败时只写日志和
``mail_logs``，绝不把异常抛回主流程。授权码只从 keyring 或环境变量读取，
不写入数据库、日志或页面。
"""

from __future__ import annotations

import logging
import os
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any, Callable, Iterable

from workbench.config import WorkbenchConfig
from workbench.db import Database, now_text

Transport = Callable[[EmailMessage, list[str]], None]


@dataclass(frozen=True)
class MailResult:
    sent: bool
    skipped: bool = False
    reason: str | None = None


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return [str(item).strip() for item in value if str(item).strip()]


class Mailer:
    def __init__(
        self,
        config: WorkbenchConfig,
        db: Database | None = None,
        *,
        transport: Transport | None = None,
        logger: logging.Logger | None = None,
    ):
        self.config = config
        self.db = db
        self.transport = transport
        self.logger = logger or logging.getLogger("workbench.mailer")

    # ------------------------------------------------------------------
    @property
    def smtp(self) -> dict[str, Any]:
        return dict(self.config.smtp or {})

    def _password(self) -> str | None:
        password = self.smtp.get("password") or os.environ.get("WORKBENCH_SMTP_PASSWORD")
        if password:
            return str(password)
        try:
            import keyring  # noqa: PLC0415

            service = str(
                self.smtp.get("keyring_service") or "ip-lead-pipeline-smtp"
            )
            username = str(self.smtp.get("from") or self.smtp.get("sender") or "")
            return keyring.get_password(service, username) if username else None
        except Exception:  # noqa: BLE001 - 缺少凭据后端不视为错误
            return None

    def configured(self) -> bool:
        enabled = self.smtp.get("enabled", True)
        if str(enabled).strip().lower() in {"0", "false", "no", "off", "否"}:
            return False
        host = self.smtp.get("smtp_host") or self.smtp.get("host")
        sender = self.smtp.get("from") or self.smtp.get("sender")
        return bool(host and sender)

    def status(self) -> dict[str, Any]:
        host = self.smtp.get("smtp_host") or self.smtp.get("host") or ""
        sender = self.smtp.get("from") or self.smtp.get("sender") or ""
        return {
            "configured": bool(host and sender),
            "host": host,
            "port": self.smtp.get("smtp_port") or self.smtp.get("port") or 465,
            "from": sender,
            "credential": bool(self._password()),
        }

    # ------------------------------------------------------------------
    def send(
        self,
        *,
        subject: str,
        body: str,
        recipients: Iterable[str] | str | None,
        task_no: str | None = None,
    ) -> MailResult:
        to = _as_list(recipients)
        if not to:
            return self._record(
                task_no, [], subject, "skipped", "未填写收件人"
            )
        if not self.configured() and self.transport is None:
            self.logger.warning(
                "SMTP 未配置，任务 %s 的完成通知只写日志：%s", task_no, subject
            )
            return self._record(task_no, to, subject, "skipped", "SMTP 未配置")
        password = self._password()
        if not password and self.transport is None:
            self.logger.warning(
                "SMTP 已配置但未找到授权码，任务 %s 的完成通知只写日志", task_no
            )
            return self._record(task_no, to, subject, "skipped", "未找到 SMTP 授权码")

        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = str(self.smtp.get("from") or self.smtp.get("sender"))
        message["To"] = ", ".join(to)
        message.set_content(body)
        try:
            if self.transport is not None:
                self.transport(message, to)
            else:
                self._smtp_send(message, to, password or "")
        except Exception as exc:  # noqa: BLE001 - 邮件失败不影响任务
            reason = f"{type(exc).__name__}: {self._redact(exc, password)}"
            self.logger.error("完成通知发送失败：%s", reason)
            return self._record(task_no, to, subject, "failed", reason)
        self.logger.info("完成通知已发送：%s -> %s", subject, ", ".join(to))
        return self._record(task_no, to, subject, "sent", None)

    def _smtp_send(self, message: EmailMessage, to: list[str], password: str) -> None:
        host = str(self.smtp.get("smtp_host") or self.smtp.get("host"))
        port = int(self.smtp.get("smtp_port") or self.smtp.get("port") or 465)
        use_ssl = bool(self.smtp.get("smtp_ssl", self.smtp.get("ssl", True)))
        timeout = int(self.smtp.get("timeout_seconds", 30))
        if use_ssl:
            client = smtplib.SMTP_SSL(host, port, timeout=timeout)
        else:
            client = smtplib.SMTP(host, port, timeout=timeout)
        with client:
            client.login(str(self.smtp.get("from") or self.smtp.get("sender")), password)
            client.send_message(message, to_addrs=to)

    def _redact(self, value: object, secret: str | None) -> str:
        text = str(value)
        if secret:
            text = text.replace(str(secret), "******")
        return text

    def _record(
        self,
        task_no: str | None,
        recipients: list[str],
        subject: str,
        status: str,
        error: str | None,
    ) -> MailResult:
        if self.db is not None:
            try:
                self.db.execute(
                    """INSERT INTO mail_logs(task_no, recipients, subject, status, error, sent_at)
                       VALUES(?,?,?,?,?,?)""",
                    (
                        task_no,
                        ",".join(recipients),
                        subject,
                        status,
                        (error or "")[:1000] or None,
                        now_text(),
                    ),
                )
            except Exception:  # noqa: BLE001 - 日志库失败不影响任务
                self.logger.exception("写入 mail_logs 失败")
        if status == "sent":
            return MailResult(True)
        if status == "skipped":
            return MailResult(False, skipped=True, reason=error)
        return MailResult(False, reason=error)


__all__ = ["MailResult", "Mailer", "Transport"]
