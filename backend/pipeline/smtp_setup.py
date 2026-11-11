"""setup-smtp：交互写入 Windows 凭据管理器，不落任何文件。"""

from __future__ import annotations

import getpass
from dataclasses import dataclass

import keyring

from pipeline.config import Settings
from pipeline.notify import keyring_service, keyring_username

MIN_PASSWORD_LENGTH = 16


@dataclass
class SetupResult:
    service: str
    username: str
    stored: bool


def setup_smtp(
    settings: Settings,
    *,
    email: str | None = None,
    password: str | None = None,
    service: str | None = None,
) -> SetupResult:
    username = (email or keyring_username(settings)).strip()
    if not username:
        raise ValueError("发件邮箱不能为空")
    resolved_service = service or keyring_service(settings)
    if password is None:
        first = getpass.getpass("请输入 SMTP 授权码（输入不回显）：")
        second = getpass.getpass("请再次输入授权码：")
        if first != second:
            raise ValueError("两次输入不一致")
        password = first
    if not password:
        raise ValueError("授权码不能为空")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(
            f"授权码长度不足 {MIN_PASSWORD_LENGTH} 位（当前 {len(password)} 位），"
            "已拒绝保存；请粘贴邮箱服务商生成的完整授权码。"
        )
    keyring.set_password(resolved_service, username, password)
    stored = keyring.get_password(resolved_service, username) == password
    if not stored:
        raise RuntimeError("写入凭据管理器后校验失败")
    return SetupResult(service=resolved_service, username=username, stored=True)


__all__ = ["MIN_PASSWORD_LENGTH", "SetupResult", "setup_smtp"]
