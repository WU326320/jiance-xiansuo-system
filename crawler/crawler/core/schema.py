"""输出字段校验与补全。

product_id / url / title 是最低要求；解析失败丢弃并记原因。
price 等字段缺失时保留记录、填 null 并加 flags（不填 0）。
"""

from __future__ import annotations

from crawler.core.parse import clean_text, is_lower_bound, normalize_shop_id
from crawler.models import Item, ItemPatch, Platform


def _add_flag(flags: list[str], flag: str) -> None:
    if flag not in flags:
        flags.append(flag)


# 这些 flag 只能由最终字段是否为空推导，不能从上游原样继承。
_AUTO_MISSING_FLAGS = {
    "价格缺失",
    "主图缺失",
    "店铺ID缺失",
    "店铺名缺失",
    "商品标题缺失",
    "销量缺失",
    "评论缺失",
    "按下限估算",
}


def enrich_flags(item: Item) -> Item:
    """按最终字段重新生成派生 flags。

    平台解析时可能先按“原文是否存在”打了缺失标记，随后又被 JSON/详情页
    补齐；如果沿用到输出就会出现“shop_id/销量/主图明明有值却标缺失”。
    因此这里先清掉所有派生标记，再只根据最终字段重新添加。
    """
    flags = [
        flag for flag in dict.fromkeys(item.flags) if flag not in _AUTO_MISSING_FLAGS
    ]
    for field, label in (("price", "价格"), ("sales", "销量"), ("comments", "评论")):
        if getattr(item, field) is not None:
            flags = [
                flag
                for flag in flags
                if flag
                not in {
                    f"{label}无法识别,待核验",
                    f"{label}原文无法确定,待核验",
                    f"{label}字段含评论信息,待核验",
                    f"{label}字段含销量信息,待核验",
                }
            ]
    updates: dict[str, object] = {}
    # "0" / "" / None 都不是有效店铺 ID；归一为 None，但不再因此标缺失。
    shop_id = normalize_shop_id(item.shop_id)
    if shop_id != item.shop_id:
        updates["shop_id"] = shop_id
    shop_id_raw = normalize_shop_id(item.shop_id_raw)
    if shop_id_raw != item.shop_id_raw:
        updates["shop_id_raw"] = shop_id_raw
    if item.price is None:
        _add_flag(flags, "价格缺失")
    if item.main_image_url is None:
        _add_flag(flags, "主图缺失")
    if not clean_text(item.shop_name):
        _add_flag(flags, "店铺名缺失")
    if len((item.title or "").strip()) < 2:
        _add_flag(flags, "商品标题缺失")
    if item.sales is None:
        _add_flag(flags, "销量缺失")
    if item.comments is None:
        _add_flag(flags, "评论缺失")
    if is_lower_bound(item.sales_raw) or is_lower_bound(item.comments_raw):
        _add_flag(flags, "按下限估算")
    updates["flags"] = flags
    return item.model_copy(update=updates)


def validate_item(
    item: Item, *, allow_missing_title: bool = False
) -> tuple[Item | None, str | None]:
    if item.platform not in ("taobao", "tmall", "jd", "1688"):
        return None, "platform非法"
    if (
        not item.product_id
        or not str(item.product_id).isdigit()
        or len(str(item.product_id)) < 4
    ):
        return None, "product_id解析失败"
    if not item.url or not item.url.startswith(("http://", "https://")):
        return None, "url缺失"
    if not allow_missing_title and (not item.title or len(item.title.strip()) < 2):
        return None, "title缺失"
    if item.price is not None and item.price < 0:
        return None, "price非法"
    return enrich_flags(item), None


_PATCH_PLACEHOLDER_TEXT = {"", "0", "null", "none", "nil", "undefined", "-", "--"}


def _clean_patch_value(field: str, value: object) -> object:
    """详情补丁字段归一：占位店铺 ID/店名不能覆盖列表页已有合法值。"""
    if field in ("shop_id", "shop_id_raw"):
        return normalize_shop_id(value)
    if field in ("shop_name", "title"):
        text = clean_text(value)
        if text and text.lower() in _PATCH_PLACEHOLDER_TEXT:
            return None
        if field == "title" and len(text or "") < 2:
            return None
        return text
    return value


def apply_patch(item: Item, patch: ItemPatch) -> Item:
    """把详情页补抓的非空字段合并回列表项。"""
    if (patch.platform, patch.product_id) != (item.platform, item.product_id):
        return item
    updates: dict[str, object] = {}
    for field in (
        "title",
        "shop_id",
        "shop_name",
        "shop_id_raw",
        "shop_label_raw",
        "shop_type_raw_hint",
        "price",
        "price_raw",
        "sales_raw",
        "sales",
        "comments_raw",
        "comments",
        "main_image_url",
        "category",
    ):
        value = _clean_patch_value(field, getattr(patch, field))
        if value is not None:
            updates[field] = value
    if patch.listed is not None:
        updates["listed"] = patch.listed
    for field in ("price", "sales", "comments"):
        value, raw = getattr(patch, field), getattr(patch, field + "_raw")
        if raw is not None:
            updates[field] = value
        elif value is not None:
            updates[field + "_raw"] = str(value)
    updates["flags"] = list(dict.fromkeys([*item.flags, *patch.flags]))
    return enrich_flags(Item.model_validate({**item.model_dump(), **updates}))


def platform_or_default(raw: str, is_tmall: bool = False) -> Platform:
    if raw == "taobao" and is_tmall:
        return "tmall"
    if raw in ("taobao", "tmall", "jd", "1688"):
        return raw  # type: ignore[return-value]
    return "taobao"
