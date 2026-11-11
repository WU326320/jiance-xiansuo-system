"""平台 JSON 字段读取小工具（大小写不敏感、dict 值取常见文本键）。"""

from __future__ import annotations

from typing import Any, Iterable

from crawler.core.parse import clean_text, parse_fuzzy_int, parse_price
from crawler.core.jsonutil import deep_find


def detail_price_raw(payloads: Any, keys: Iterable[str]) -> str | None:
    """详情显式券后价优先，其次最低价，最后平台普通价格。"""
    for key in ("couponPrice", "priceAfterCoupon", "finalPrice", "minPrice", *keys):
        value = deep_find(payloads, (key,))
        if value is not None:
            if isinstance(value, (dict, list, tuple)):
                parsed = parse_price(value)
                return str(parsed) if parsed is not None else None
            return clean_text(value)
    return None


def _lower_map(record: dict) -> dict[str, Any]:
    return {str(key).lower(): value for key, value in record.items()}


def flatten_record(
    record: dict,
    nested_keys: Iterable[str] = (
        "offer",
        "offerInfo",
        "item",
        "product",
        "sku",
        "data",
    ),
) -> dict:
    """把 ``{"offer": {...}, "index": 1}`` 这类外壳展开成商品字段。

    顶层字段优先，只展开 dict 形态的常见嵌套；列表/字符串保持原样。
    """
    if not isinstance(record, dict):
        return {}
    merged: dict[str, Any] = {}
    lowered = _lower_map(record)
    for key in nested_keys:
        nested = lowered.get(key.lower())
        if isinstance(nested, dict):
            merged.update(nested)
    merged.update(record)
    return merged


def pick(record: dict, keys: Iterable[str]) -> Any:
    if not isinstance(record, dict):
        return None
    lowered = _lower_map(record)
    keys = tuple(keys)
    if any("price" in key.lower() or key == "p" for key in keys):
        for key in ("couponPrice", "priceAfterCoupon", "finalPrice", "minPrice"):
            value = lowered.get(key.lower())
            if value not in (None, "", [], {}):
                return value
    for key in keys:
        if key.lower() in lowered:
            value = lowered[key.lower()]
            if value not in (None, "", [], {}):
                return value
    return None


def pick_text(record: dict, keys: Iterable[str]) -> str | None:
    value = pick(record, keys)
    if isinstance(value, dict):
        for nested in ("text", "value", "name", "title", "priceText", "content"):
            if value.get(nested) not in (None, ""):
                return clean_text(value.get(nested))
        return None
    if isinstance(value, (list, tuple)):
        for item in value:
            text = clean_text(item)
            if text:
                return text
        return None
    return clean_text(value)


def pick_price(record: dict, keys: Iterable[str]):
    value = pick(record, keys)
    return parse_price(value)


def pick_int(record: dict, keys: Iterable[str]) -> int | None:
    value = pick(record, keys)
    return parse_fuzzy_int(value)


def text_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [clean_text(value)] if clean_text(value) else []
    if isinstance(value, dict):
        result: list[str] = []
        for nested in ("text", "name", "title", "value", "label"):
            if value.get(nested) not in (None, ""):
                result.append(str(clean_text(value.get(nested))))
        return [item for item in result if item]
    if isinstance(value, (list, tuple)):
        result = []
        for item in value:
            result.extend(text_list(item))
        return list(dict.fromkeys(result))
    return []


def join_labels(*values: Any, sep: str = " / ") -> str | None:
    labels: list[str] = []
    for value in values:
        labels.extend(text_list(value))
    labels = list(dict.fromkeys(label for label in labels if label))
    return sep.join(labels) if labels else None
