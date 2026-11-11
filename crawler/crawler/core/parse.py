"""字段解析：模糊数值、价格区间、链接 / ID 规范化、去重。

所有解析函数的共同口径：解析不出来返回 ``None``，不返回 0。
"""

from __future__ import annotations

import re
import unicodedata
from html import unescape
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Iterable
from urllib.parse import parse_qs, quote_from_bytes, urlparse

from crawler.core.errors import KeywordEncodingError
from crawler.models import Item

_NUMBER_RE = re.compile(r"(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)")
_FUZZY_RE = re.compile(r"(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(亿|万|千|百)?")
_UNIT = {
    "亿": Decimal("100000000"),
    "万": Decimal("10000"),
    "千": Decimal("1000"),
    "百": Decimal("100"),
}
_LOWER_BOUND_HINT = re.compile(r"[+＋]|亿|万|千|多|余")
_MEANINGLESS_PRICE = re.compile(r"面议|询价|电议|价格异常|暂无|未知|免费")
_URL_ID_PATTERNS = {
    "taobao": (
        re.compile(r"[?&](?:id|itemId|item_id)=(\d{5,})", re.I),
        re.compile(r"/(\d{5,})\.html", re.I),
    ),
    "tmall": (
        re.compile(r"[?&](?:id|itemId|item_id)=(\d{5,})", re.I),
        re.compile(r"/(\d{5,})\.html", re.I),
    ),
    "jd": (
        re.compile(r"item\.jd\.com/(\d{5,})\.html", re.I),
        re.compile(r"[?&](?:sku|skuId|sku_id)=(\d{5,})", re.I),
    ),
    "1688": (
        re.compile(r"offer/(\d{5,})\.html", re.I),
        re.compile(r"[?&](?:offerId|offer_id|offerIds|offer_ids|id)=(\d{5,})", re.I),
        re.compile(r"offerIds?[=:]\s*['\"]?(\d{5,})", re.I),
    ),
}
_JD_SHOP_RE = re.compile(
    r"(?:mall\.jd\.com/index-|shopId=|shop_id=|venderId=|vender_id=)(\d+)", re.I
)
# 1688 店铺子域名不一定是纯数字（如 shop0c1039px78200.1688.com）；
# member_id 也可能是 b2b-xxxx 形式，因此统一按稳定字符串取。
_ALI_SHOP_RE = re.compile(
    r"(?:memberId|member_id|companyId|company_id|sellerId|seller_id|userId|user_id)"
    r"\s*[@=]\s*([0-9A-Za-z_-]+)|shop([0-9A-Za-z]+)\.1688\.com",
    re.I,
)
_TAOBAO_SHOP_RE = re.compile(r"shop(\d+)\.(?:taobao|tmall)\.com", re.I)
# 主图 CDN 路径里的卖家数字 ID：i4/i1/916148540/... 或 ..._!!2455999666-0-picasso.jpg
_ALI_IMAGE_SELLER_RE = re.compile(r"/(?:i\d+/)+(\d{5,})(?:/|$)")
_ALI_IMAGE_BANG_SELLER_RE = re.compile(r"!!(\d{5,})(?:[-._]|$)")
_INVALID_SHOP_IDS = {"", "0", "null", "none", "nil", "undefined", "-", "--"}


def clean_text(value: object | None) -> str | None:
    if value is None:
        return None
    text = unicodedata.normalize("NFKC", str(value)).replace("\u3000", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def clean_title(value: object | None) -> str | None:
    """移除商品标题的高亮标签，保留相邻关键词与原有空格。"""
    if value is None:
        return None
    return clean_text(unescape(re.sub(r"</?[a-zA-Z][^>]*>", "", str(value))))


def parse_fuzzy_int(value: object | None) -> int | None:
    """把“1万+”“2.5万+”“300+”“已售1000+件”解析为下限整数。

    例：``1万+ -> 10000``，``2.5万+ -> 25000``，``300+ -> 300``。
    解析失败返回 ``None``。
    """
    from utils.commerce_numbers import count_value

    return count_value(value)


def is_lower_bound(value: object | None) -> bool:
    """原文是否属于“下限/约数”（用于 flags 的 ``按下限估算``）。"""
    text = clean_text(value)
    return bool(text and _LOWER_BOUND_HINT.search(text))


def parse_price(value: object | None) -> Decimal | None:
    """解析价格；区间取最低价。

    ``¥29.90-39.90`` -> ``29.90``；``面议`` / 空 -> ``None``（不是 0）。
    """
    from utils.commerce_numbers import price_value

    return price_value(value)


def normalize_image_url(value: object | None) -> str | None:
    text = clean_text(value)
    if not text:
        return None
    if text.startswith("//"):
        return "https:" + text
    if text.startswith(("http://", "https://")):
        return text
    if text.startswith("/"):
        return None
    return None


def extract_product_id(platform: str, value: object | None) -> str | None:
    """从链接或纯 ID 中提取商品 ID；失败返回 None（调用方应丢弃该条）。"""
    text = clean_text(value)
    if not text:
        return None
    if text.isdigit() and len(text) >= 4:
        return text
    for pattern in _URL_ID_PATTERNS.get(platform, ()):
        match = pattern.search(text)
        if match:
            return match.group(1)
    return None


def normalize_product_url(
    platform: str, product_id: str, raw_url: object | None = None
) -> str:
    """规范化商品链接；平台错误链接也回退到按 ID 构造的规范链接。"""
    text = clean_text(raw_url)
    if text:
        text = "https:" + text if text.startswith("//") else text
        parsed = urlparse(text)
        host = parsed.netloc.lower()
        if platform in {"taobao", "tmall"} and (
            host.endswith("item.taobao.com")
            or host.endswith("detail.tmall.com")
            or host.endswith("chaoshi.detail.tmall.com")
        ):
            if platform == "tmall" or host.endswith("tmall.com"):
                return f"https://detail.tmall.com/item.htm?id={product_id}"
            return f"https://item.taobao.com/item.htm?id={product_id}"
        if platform == "jd" and host.endswith("jd.com"):
            return f"https://item.jd.com/{product_id}.html"
        if platform == "1688" and host.endswith("1688.com"):
            return f"https://detail.1688.com/offer/{product_id}.html"
    if platform in {"taobao", "tmall"}:
        if platform == "tmall":
            return f"https://detail.tmall.com/item.htm?id={product_id}"
        return f"https://item.taobao.com/item.htm?id={product_id}"
    if platform == "jd":
        return f"https://item.jd.com/{product_id}.html"
    if platform == "1688":
        return f"https://detail.1688.com/offer/{product_id}.html"
    raise ValueError(f"未知平台: {platform}")


def is_tmall_url(value: object | None) -> bool:
    text = (clean_text(value) or "").lower()
    return "tmall.com" in text or "detail.tmall" in text


def normalize_shop_id(value: object | None) -> str | None:
    """统一店铺 ID：``"0"``、空串、null/None 一律视为缺失（返回 None）。

    天猫详情页会把未登录/占位数据写成 ``"0"``；这类值不能当真实店铺 ID。
    2026-11-06 起 shop_id 只是辅助字段：缺失不再标记，也不再阻断聚合。
    """
    text = clean_text(value)
    if not text:
        return None
    lowered = text.lower()
    if lowered in _INVALID_SHOP_IDS:
        return None
    if text.isdigit() and int(text) == 0:
        return None
    return text


def numeric_shop_id(value: object | None) -> str | None:
    """仅返回纯数字店铺 ID（淘宝/天猫卖家数字 ID）。"""
    text = normalize_shop_id(value)
    if text and text.isdigit():
        return text
    return None


def shop_id_from_image_url(value: object | None) -> str | None:
    """从主图 URL 解析卖家数字 ID。

    支持两类真实 CDN 形态：
    * ``.../uploaded/i4/i1/916148540/O1CN...jpg`` -> ``916148540``
    * ``...O1CN..._!!2455999666-0-picasso.jpg`` -> ``2455999666``
    """
    text = clean_text(value)
    if not text:
        return None
    for pattern in (_ALI_IMAGE_SELLER_RE, _ALI_IMAGE_BANG_SELLER_RE):
        match = pattern.search(text)
        if match:
            return normalize_shop_id(match.group(1))
    return None


def extract_shop_id(platform: str, value: object | None) -> str | None:
    text = clean_text(value)
    if not text:
        return None
    if platform == "jd":
        match = _JD_SHOP_RE.search(text)
        return match.group(1) if match else None
    if platform == "1688":
        match = _ALI_SHOP_RE.search(text)
        if match:
            return match.group(1) or match.group(2)
        return None
    if platform in {"taobao", "tmall"}:
        parsed = urlparse(text)
        query = parse_qs(parsed.query)
        for key in (
            "sellerId",
            "seller_id",
            "sellerUserId",
            "seller_user_id",
            "shopId",
            "shop_id",
            "userId",
            "user_id",
            "user_number_id",
        ):
            values = query.get(key)
            if values and values[0].strip():
                return values[0].strip()
        # 列表/详情里的店铺链接常见为 shop2001.taobao.com、shop2001.tmall.com。
        match = _TAOBAO_SHOP_RE.search(text)
        return match.group(1) if match else None
    return None


def gbk_quote(keyword: str) -> str:
    """1688 中文关键词必须按 GBK 百分号编码。

    不能编码的字符（如 emoji）直接报错，避免静默混用 UTF-8 导致兜底商品。
    """
    if not keyword or not keyword.strip():
        raise KeywordEncodingError("关键词为空")
    try:
        raw = keyword.encode("gbk")
    except UnicodeEncodeError as exc:
        bad = keyword[exc.start : exc.end]
        raise KeywordEncodingError(f"关键词包含 GBK 无法编码的字符: {bad!r}") from exc
    return quote_from_bytes(raw, safe="")


def keyword_hit_ratio(keyword: str, titles: Iterable[str | None]) -> float:
    """标题精确包含关键词的比例；用于 1688 编码校验 warning。"""
    needle = (keyword or "").strip().lower()
    if not needle:
        return 0.0
    total = 0
    hit = 0
    for title in titles:
        if not title:
            continue
        total += 1
        if needle in title.lower():
            hit += 1
    return hit / total if total else 0.0


def dedupe_items(items: Iterable[Item]) -> list[Item]:
    """按 platform+product_id 去重；商业数值与其原文成对合并。"""
    seen: dict[tuple[str, str], Item] = {}
    order: list[tuple[str, str]] = []
    merge_fields = (
        "title",
        "shop_id",
        "shop_name",
        "shop_id_raw",
        "shop_label_raw",
        "shop_type_raw_hint",
        "main_image_url",
        "image_path",
        "category",
        "ip_code",
    )
    for item in items:
        key = (item.platform, item.product_id)
        if key not in seen:
            seen[key] = item.model_copy(deep=True)
            order.append(key)
            continue
        current = seen[key]
        updates: dict[str, object] = {}
        for field in merge_fields:
            if getattr(current, field) is None and getattr(item, field) is not None:
                updates[field] = getattr(item, field)
        for field in ("price", "sales", "comments"):
            raw_field = field + "_raw"
            # 保留首个有效数值（包括 0），不能再混入另一来源的原文。
            if getattr(current, field) is not None:
                continue
            if getattr(item, field) is not None:
                updates[field] = getattr(item, field)
                # 数字来源没有原文时也要清掉旧文案，避免保存时被判为空。
                updates[raw_field] = getattr(item, raw_field)
            elif getattr(current, raw_field) in (None, ""):
                updates[raw_field] = getattr(item, raw_field)
        flags = list(dict.fromkeys([*current.flags, *item.flags]))
        updates["flags"] = flags
        seen[key] = Item.model_validate({**current.model_dump(), **updates})
    return [seen[key] for key in order]


def dedupe_strings(values: Iterable[str | None]) -> list[str]:
    return list(dict.fromkeys(v.strip() for v in values if v and v.strip()))


def now_local(timezone_name: str = "Asia/Shanghai") -> datetime:
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo(timezone_name))


def safe_filename(value: str, fallback: str = "item") -> str:
    cleaned = re.sub(r"[^0-9A-Za-z_.-]+", "_", value).strip("._")
    return cleaned[:120] or fallback
