"""密码哈希、令牌、来源 IP 校验。"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
import secrets
import unicodedata
from typing import Iterable

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32


class PasswordError(ValueError):
    pass


def normalize_username(value: object | None) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", "", text).strip().lower()


def validate_password(password: str, *, min_length: int = 8) -> None:
    if not isinstance(password, str) or len(password) < max(8, min_length):
        raise PasswordError(f"密码至少 {max(8, min_length)} 位")
    if not re.search(r"[A-Za-z]", password):
        raise PasswordError("密码必须包含字母")
    if not re.search(r"[0-9]", password):
        raise PasswordError("密码必须包含数字")


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    validate_password(password)
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, n, r, p, salt_hex, digest_hex = str(stored).split("$")
    except (ValueError, AttributeError):
        return False
    if algorithm != "scrypt":
        return False
    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
        actual = hashlib.scrypt(
            str(password).encode("utf-8"),
            salt=salt,
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def hash_token(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def new_invite_code() -> str:
    # 便于人工转述，去掉容易混淆的字符。
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(20))


def is_loopback(ip: str | None) -> bool:
    try:
        return ipaddress.ip_address(str(ip or "")).is_loopback
    except ValueError:
        return False


def ip_allowed(ip: str | None, networks: Iterable[str] | None) -> bool:
    try:
        address = ipaddress.ip_address(str(ip or ""))
    except ValueError:
        return False
    if address.is_loopback:
        return True
    for item in networks or ():
        try:
            if address in ipaddress.ip_network(str(item), strict=False):
                return True
        except ValueError:
            continue
    return False


__all__ = [
    "PasswordError",
    "hash_password",
    "hash_token",
    "ip_allowed",
    "is_loopback",
    "new_invite_code",
    "new_token",
    "normalize_username",
    "validate_password",
    "verify_password",
]
