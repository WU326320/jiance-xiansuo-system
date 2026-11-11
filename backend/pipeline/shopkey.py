"""店铺名规范化与平台组（需求文档 v0.3 2026-11-06 聚合口径）。

- 平台组：淘宝和天猫同属 ``taobao``；京东 ``jd``；1688 ``1688``。
- 店名规范化：NFKC -> 去掉所有空白 -> 英文小写。
- 不删除「旗舰店」「专营店」「自营」、括号及括号里的内容。
- 1688 店名先 URL 解码（采集层与入库层都做一次兜底）。

聚合与白名单都只使用 ``平台组 + 规范化店名``；``shop_id`` 仅用于区分
同名不同店，缺失不是错误。
"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import unquote

PLATFORM_GROUPS = {
    "taobao": "taobao",
    "tmall": "taobao",
    "jd": "jd",
    "1688": "1688",
}
GROUP_PLATFORMS = {
    "taobao": ("taobao", "tmall"),
    "jd": ("jd",),
    "1688": ("1688",),
}
_PERCENT_RE = re.compile(r"%[0-9A-Fa-f]{2}")
_WHITESPACE_RE = re.compile(r"\s+", re.UNICODE)
_CJK_RE = re.compile(r"^[\u3400-\u9fff]")
_LEADING_HEX_ESCAPE_RE = re.compile(r"^([0-9A-Fa-f])\s*%[0-9A-Fa-f]{2}")
_DOUBLE_ENCODED_HEX_RE = re.compile(r"^%([0-9A-Fa-f]{2})%[0-9A-Fa-f]{2}")
_HEX_DIGITS = set("0123456789abcdef")


def _leading_hex_prefix(text: str) -> str | None:
    match = _LEADING_HEX_ESCAPE_RE.match(text)
    if match:
        return match.group(1).lower()
    match = _DOUBLE_ENCODED_HEX_RE.match(text)
    if match:
        char = chr(int(match.group(1), 16)).lower()
        if char in _HEX_DIGITS:
            return char
    return None


def platform_group(platform: str | None) -> str:
    text = str(platform or "").strip().lower()
    return PLATFORM_GROUPS.get(text, text)


def decode_shop_name(value: object | None) -> str | None:
    """URL 解码店名；不是百分号编码时保持原样。

    与采集层同口径清理原始字段偶尔带出的单字符 ASCII 前缀
    （如 ``f%E7%81%AB...`` -> ``火星人...``），旧 JSONL 入库也能得到干净店名。
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    prefix = _leading_hex_prefix(text)
    for _ in range(2):
        if not _PERCENT_RE.search(text):
            break
        decoded_text: str | None = None
        for encoding in ("utf-8", "gbk"):
            try:
                decoded = unquote(text, encoding=encoding, errors="strict")
            except (UnicodeDecodeError, ValueError):
                continue
            if decoded and decoded != text:
                decoded_text = decoded
                break
        if decoded_text is None:
            break
        text = decoded_text
    if prefix and len(text) > 1 and text[0].lower() == prefix:
        remainder = text[1:].lstrip()
        if remainder and _CJK_RE.match(remainder[0]):
            text = remainder
    return text or None


def normalize_shop_name(value: object | None) -> str:
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    text = _WHITESPACE_RE.sub("", text)
    return text.lower()


def normalize_ip_name(value: object | None) -> str:
    """IP 名规范化：NFKC -> 去掉所有空白 -> 英文小写。"""
    return normalize_shop_name(value)


def shop_key(platform: str | None, shop_name: object | None) -> str:
    return f"{platform_group(platform)}|{normalize_shop_name(shop_name)}"


def shop_name_missing(value: object | None) -> bool:
    return not normalize_shop_name(value)


def whitelist_sentinel(shop_name: object | None) -> str:
    """无 shop_id 的白名单行用稳定哨兵占住唯一键，不冒充真实店铺 ID。"""
    return f"name:{normalize_shop_name(shop_name)}"


def is_name_sentinel(shop_id: str | None) -> bool:
    return bool(shop_id) and str(shop_id).startswith("name:")


__all__ = [
    "GROUP_PLATFORMS",
    "PLATFORM_GROUPS",
    "decode_shop_name",
    "is_name_sentinel",
    "normalize_ip_name",
    "normalize_shop_name",
    "platform_group",
    "shop_key",
    "shop_name_missing",
    "whitelist_sentinel",
]
