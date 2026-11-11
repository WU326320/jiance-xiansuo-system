"""平台登录 Cookie 的只读判定。

浏览器只暴露 Cookie 名/值，不导出 Cookie 本身。CDP 模式下淘宝/1688 必须
按真实 Cookie 判定登录态，而不是仅凭“URL 离开登录页”：
- 淘宝：``_nk_`` 存在，或 ``cookie2`` 与 ``unb`` 同时存在；
- 1688：``__cn_logon__`` 的值必须为 ``true``（大小写不敏感）。
"""

from __future__ import annotations

from typing import Any


def read_cookie_values(browser: Any) -> dict[str, str] | None:
    """读取浏览器 context 的 ``{name: value}``；不支持时返回 ``None``。"""
    getter = getattr(browser, "cookie_values", None)
    if not callable(getter):
        return None
    try:
        raw = getter()
    except Exception:  # noqa: BLE001 - Cookie 读取失败按未知处理
        return None
    if not isinstance(raw, dict):
        return None
    values: dict[str, str] = {}
    for name, value in raw.items():
        if name is None:
            continue
        text = str(name)
        if text:
            values[text] = "" if value is None else str(value)
    return values


def read_cookie_names(browser: Any) -> set[str] | None:
    """读取 Cookie 名集合；优先使用带值的接口，兼容旧的 ``cookie_names``。"""
    values = read_cookie_values(browser)
    if values is not None:
        return set(values)
    getter = getattr(browser, "cookie_names", None)
    if not callable(getter):
        return None
    try:
        return {str(name) for name in getter() if name}
    except Exception:  # noqa: BLE001 - Cookie 读取失败按未知处理
        return None


def taobao_logged_in(values: dict[str, str] | None) -> bool:
    """淘宝登录 Cookie：``_nk_`` 或 ``cookie2`` + ``unb``。"""
    if not values:
        return False
    return "_nk_" in values or ("cookie2" in values and "unb" in values)


def ali1688_logged_in(values: dict[str, str] | None) -> bool:
    """1688 必须拿到 ``__cn_logon__=true``，仅有同名 Cookie 不算。"""
    if not values or "__cn_logon__" not in values:
        return False
    return str(values["__cn_logon__"]).strip().lower() == "true"


__all__ = [
    "ali1688_logged_in",
    "read_cookie_names",
    "read_cookie_values",
    "taobao_logged_in",
]
