"""采集和入库共用的数字口径；不从未知文本中截取第一个数字。"""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

CENT = Decimal("0.01")
NUMBER = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
UNITS = {
    "": 1,
    "千": 1000,
    "k": 1000,
    "万": 10000,
    "w": 10000,
    "亿": 100000000,
    "m": 1000000,
}


def normalized(value):
    return unicodedata.normalize("NFKC", str(value)).strip().replace("，", ",")


def count_value(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            number = Decimal(str(value))
            return (
                int(number)
                if (
                    number.is_finite()
                    and number >= 0
                    and number == number.to_integral_value()
                    and number <= 9223372036854775807
                )
                else None
            )
        except (InvalidOperation, ValueError):
            return None
    text = normalized(value).lower()
    # 只接受一个数量及已知字段标签；约数、范围、金额、百分比均不猜测。
    match = re.fullmatch(
        rf"(?:累计|总计|总|月|近30天|近一个月)?\s*"
        rf"(?:销量|月销|已售|已拼|售出|成交|评价|评论|评价数|评论数|收货|已销售)?\s*[:：]?\s*"
        rf"({NUMBER})\s*(千|万|亿|k|w|m)?\s*(?:\+|以上)?\s*"
        rf"(?:件|笔|条|人)?\s*(?:付款|已付款|收货|已收货|评价|评论|成交|已售|已拼)?",
        text,
    )
    if not match:
        return None
    try:
        number = Decimal(match[1].replace(",", "")) * UNITS[match[2] or ""]
        return count_value(number)
    except (InvalidOperation, ValueError):
        return None


def price_value(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            number = Decimal(str(value))
            if number.is_finite() and 0 <= number <= Decimal("999999999999.99"):
                return number.quantize(CENT, rounding=ROUND_HALF_UP)
        except (InvalidOperation, ValueError):
            pass
        return None
    if isinstance(value, dict):
        # 显式价格对象中的最小价/券后价，不展开任意商品或数量字段。
        for key in (
            "couponPrice",
            "priceAfterCoupon",
            "finalPrice",
            "min",
            "minPrice",
            "price",
            "value",
            "text",
            "priceText",
        ):
            if key in value and value[key] not in (None, ""):
                return price_value(value[key])
        return None
    if isinstance(value, (list, tuple)):
        prices = [price_value(part) for part in value]
        return min(prices) if prices and all(p is not None for p in prices) else None
    text = normalized(value)
    # 明确给出的券后价优先；满减门槛/优惠券面额不当作售价。
    coupon = re.search(
        rf"(?:券后(?:价)?|到手价)\s*[:：]?\s*[¥￥]?\s*({NUMBER})(?:元)?", text
    )
    if coupon:
        return price_value(Decimal(coupon[1].replace(",", "")))
    match = re.fullmatch(
        rf"(?:人民币|RMB|CNY|价格|售价|单价|最低价)?\s*[:：]?\s*[¥￥]?\s*"
        rf"({NUMBER})(?:\s*(?:元)?\s*[-~～—–至]\s*[¥￥]?\s*({NUMBER}))?"
        rf"\s*(?:元)?\s*(?:起|起售)?(?:\s*/\s*(?:件|个|套|只|双|包|盒))?",
        text,
        re.I,
    )
    if not match:
        return None
    prices = [
        price_value(Decimal(number.replace(",", "")))
        for number in match.groups()
        if number
    ]
    return min(prices) if prices and all(p is not None for p in prices) else None


def count_field(value, raw, *, field):
    """有原文但不可识别时，不能用上游错误解析的数字掩盖歧义。"""
    if raw is not None and str(raw).strip():
        text = normalized(raw)
        if field == "sales" and re.search(r"评论|评价|好评|评分", text):
            return None, "销量字段含评论信息,待核验"
        if field == "comments" and re.search(r"销量|已售|付款|月销|成交", text):
            return None, "评论字段含销量信息,待核验"
        parsed = count_value(raw)
        if parsed is None:
            return None, f"{'销量' if field == 'sales' else '评论'}原文无法确定,待核验"
        return parsed, None
    return count_value(value), None
