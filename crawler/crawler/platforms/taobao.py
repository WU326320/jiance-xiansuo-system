"""淘宝 / 天猫采集。

搜索优先解析 mtop / h5api JSON，DOM 兜底；天猫与淘宝同一通道，按标识输出 tmall/taobao。
"""

from __future__ import annotations

import re
from urllib.parse import quote_plus, urljoin

from crawler.core.browser import PageResult
from crawler.core.cookies import read_cookie_values, taobao_logged_in
from crawler.core.errors import RiskBlocked
from crawler.core.dom import card_nodes, first_attr, first_text, href_of, parse_html
from crawler.core.jsonutil import deep_find
from crawler.core.parse import (
    clean_text,
    clean_title,
    dedupe_items,
    extract_product_id,
    extract_shop_id,
    is_tmall_url,
    normalize_shop_id,
    normalize_image_url,
    normalize_product_url,
    numeric_shop_id,
    parse_fuzzy_int,
    parse_price,
    shop_id_from_image_url,
)
from crawler.core.schema import enrich_flags
from crawler.models import Item, ItemPatch
from crawler.platforms.base import PlatformBase
from crawler.platforms.helpers import (
    detail_price_raw,
    flatten_record,
    join_labels,
    pick,
    pick_price,
    pick_text,
)
from crawler.platforms.selectors import TAOBAO, TAOBAO_DETAIL


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = (clean_text(value) or "").lower()
    return text in {"1", "true", "yes", "y", "b", "tmall"}


# 新版 PC 搜索把首屏列表放到 mtop.taobao.wsearch.* 的 ``data.resultList`` /
# ``data.itemsArray`` 里，每条再包一层 ``item``；旧版接口是 ``data.auctions``。
_TAOBAO_LIST_KEYS = (
    "auctions",
    "items",
    "itemlist",
    "item_list",
    "list",
    "mods",
    "resultlist",
    "result_list",
    "itemsarray",
    "items_array",
    "results",
    "records",
    "data",
)
_TAOBAO_ID_KEYS = (
    "nid",
    "item_id",
    "itemid",
    "itemId",
    "id",
    "auctionid",
    "auction_id",
    "productid",
    "product_id",
)
_TAOBAO_URL_KEYS = (
    "detail_url",
    "detailUrl",
    "itemUrl",
    "item_url",
    "itemLink",
    "item_link",
    "url",
)
_TAOBAO_SHOP_ID_KEYS = (
    "sellerId",
    "seller_id",
    "shopId",
    "shop_id",
    "userId",
    "user_id",
    "sellerUserId",
    "seller_user_id",
    "userNumberId",
    "user_number_id",
    "sellerNumericId",
    "seller_numeric_id",
)
_TAOBAO_SHOP_URL_KEYS = (
    "shopUrl",
    "shop_url",
    "shopLink",
    "shop_link",
    "sellerUrl",
    "seller_url",
)
# 详情页只从商品详情节点及其卖家/店铺节点取 shop_id/shop_name。
# 绝不在全量 payload 上裸搜 nick/userId：登录态、购物车等接口里的
# nick 是当前买家昵称，userId 可能是买家 ID，会把卖家信息污染成买家。
_TAOBAO_DETAIL_ID_KEYS = (
    "itemId",
    "item_id",
    "itemid",
    "nid",
    "auctionId",
    "auction_id",
)
_TAOBAO_DETAIL_WRAPPER_KEYS = {
    "item",
    "iteminfo",
    "item_info",
    "itemdata",
    "item_data",
    "itemdo",
    "item_do",
    "itemdetail",
    "item_detail",
    "auction",
    "product",
    "productinfo",
    "product_info",
    "detail",
}
_TAOBAO_SELLER_NODE_KEYS = (
    "seller",
    "sellerInfo",
    "seller_info",
    "sellerVO",
    "seller_vo",
    "shop",
    "shopInfo",
    "shop_info",
    "shopVO",
    "shop_vo",
    "store",
    "storeInfo",
    "store_info",
)
_TAOBAO_SELLER_ID_KEYS = (
    "sellerId",
    "seller_id",
    "shopId",
    "shop_id",
    "userId",
    "user_id",
    "sellerUserId",
    "seller_user_id",
    "userNumberId",
    "user_number_id",
    "sellerNumericId",
    "seller_numeric_id",
)
# 祖先/兄弟节点只认明确的卖家 ID 键；userId 可能是登录买家，不能取。
_TAOBAO_ANCESTOR_ID_KEYS = (
    "sellerId",
    "seller_id",
    "shopId",
    "shop_id",
    "sellerUserId",
    "seller_user_id",
    "userNumberId",
    "user_number_id",
    "sellerNumericId",
    "seller_numeric_id",
)
# 商品详情节点自身的 nick 是卖家昵称（不是顶栏登录态里的买家 nick）。
_TAOBAO_DETAIL_NAME_KEYS = (
    "shopName",
    "shop_name",
    "sellerNick",
    "seller_nick",
    "nick",
)
_TAOBAO_ANCESTOR_NAME_KEYS = ("shopName", "shop_name", "sellerNick", "seller_nick")
_TAOBAO_SELLER_NAME_KEYS = (
    "shopName",
    "shop_name",
    "sellerNick",
    "seller_nick",
    "nick",
    "name",
    "title",
)
_PLACEHOLDER_NAMES = {"", "0", "null", "none", "nil", "undefined", "-", "--"}
_BUYER_NODE_KEYS = {
    "user",
    "loginuser",
    "login_user",
    "buyer",
    "buyerinfo",
    "buyer_info",
    "currentuser",
    "current_user",
    "member",
    "memberinfo",
    "member_info",
    "logininfo",
    "login_info",
    "userinfo",
    "user_info",
    "account",
    "accountinfo",
    "account_info",
}
_BUYER_NICK_KEYS = (
    "nick",
    "nickName",
    "nick_name",
    "userNick",
    "user_nick",
    "buyerNick",
    "buyer_nick",
    "displayName",
    "display_name",
)
# 详情页评价总数：接口/页面数据优先，DOM 兜底。``comments`` 可能是数组，
# 这里只取标量；数组/对象由 _scalar_count 继续向下找 count/total。
_TAOBAO_COMMENT_KEYS = (
    "commentCount",
    "comment_count",
    "commentsCount",
    "comments_count",
    "rateTotal",
    "rate_total",
    "totalComment",
    "totalComments",
    "commentTotal",
    "evaluationCount",
    "reviewCount",
    "itemCommentCount",
    "item_comment_count",
    "totalCount",
    "total_count",
    "rateCount",
    "rate_count",
    "rateCounts",
    "rate_counts",
    "rateQuantity",
    "rate_quantity",
    "totalRate",
    "total_rate",
    "rateInfo",
    "rate_info",
    "commentInfo",
    "comment_info",
    "tradeRate",
    "trade_rate",
    "comments",
)
_TAOBAO_IMAGE_KEYS = (
    "pic_path",
    "picPath",
    "pic_url",
    "picUrl",
    "imageUrl",
    "image_url",
    "mainPic",
    "mainImage",
    "image",
    "img",
)
_COMMENT_COUNT_NESTED_KEYS = (
    "count",
    "total",
    "value",
    "num",
    "number",
    "text",
    "display",
    "countText",
    "totalCount",
    "commentCount",
    "comment_count",
    "rateCount",
    "rate_count",
    "rateCounts",
    "rate_counts",
    "rateInfo",
    "totalRate",
    "total_rate",
)


def _scalar_count(value) -> str | None:
    """从接口字段里取评论文本；dict 继续找 count/total，list 不猜。"""
    if isinstance(value, dict):
        for key in _COMMENT_COUNT_NESTED_KEYS:
            nested = value.get(key)
            if nested in (None, "", [], {}):
                continue
            if isinstance(nested, (dict, list, tuple)):
                text = _scalar_count(nested)
            else:
                text = clean_text(nested)
            if text:
                return text
        return None
    if isinstance(value, (list, tuple)):
        return None
    return clean_text(value)


def _iter_comment_values(
    obj, wanted: set[str], depth: int = 0, *, product_id=None, rate_scope=False
):
    if depth > 10:
        return
    if isinstance(obj, dict):
        identity = obj.get("itemId") or obj.get("item_id") or obj.get("ssrItemId")
        item = obj.get("item")
        if isinstance(item, dict):
            identity = identity or item.get("itemId") or item.get("item_id")
        if product_id and identity and str(identity) != str(product_id):
            return
        for key, value in obj.items():
            if str(key).lower() in wanted and (
                str(key).lower() not in {"totalcount", "total_count"} or rate_scope
            ):
                yield str(key).lower(), value
        for key, value in obj.items():
            key = str(key).lower()
            if any(
                token in key
                for token in ("recommend", "ask", "question", "tag", "impression")
            ):
                continue
            yield from _iter_comment_values(
                value,
                wanted,
                depth + 1,
                product_id=product_id,
                rate_scope=key
                in {"ratevo", "rateinfo", "commentinfo", "rate", "review"},
            )
    elif isinstance(obj, list):
        for value in obj[:200]:
            yield from _iter_comment_values(
                value, wanted, depth + 1, product_id=product_id, rate_scope=rate_scope
            )


def _comment_total(payloads, product_id=None) -> str | None:
    """按字段优先级从页面数据/接口 JSON 提取评价总数。"""
    priority = {key.lower(): index for index, key in enumerate(_TAOBAO_COMMENT_KEYS)}
    wanted = set(priority)
    candidates: list[tuple[int, int, str]] = []
    order = 0
    for payload in payloads:
        for key, value in _iter_comment_values(payload, wanted, product_id=product_id):
            text = _scalar_count(value)
            if not text:
                continue
            lowered = text.lower()
            if (
                lowered in {"null", "none", "undefined", "nan", "-", "--"}
                or parse_fuzzy_int(text) is None
            ):
                continue
            candidates.append((priority.get(key, 999), order, text))
            order += 1
    if not candidates:
        return None
    if len({parse_fuzzy_int(value[2]) for value in candidates}) > 1:
        return None
    candidates.sort()
    return candidates[0][2]


def _scoped_detail_payloads(payloads, product_id):
    """ICE详情仅映射当前商品的明确字段，排除评分标题、推荐及SKU分项。"""
    scoped = []
    for payload in payloads:
        if not isinstance(payload, dict) or "loaderData" not in payload:
            scoped.append(payload)
            continue
        data = payload
        for key in ("loaderData", "home", "data", "res"):
            data = data.get(key, {}) if isinstance(data, dict) else {}
        if not isinstance(data, dict):
            continue
        item = data.get("item")
        if not isinstance(item, dict) or str(item.get("itemId")) != str(product_id):
            continue
        components = data.get("componentsVO") or {}
        seller = data.get("seller") or {}
        trade = data.get("pcTrade") or {}
        buy = trade.get("pcBuyParams") if isinstance(trade, dict) else None
        title_vo = components.get("titleVO") if isinstance(components, dict) else None
        images = item.get("images")
        scoped.append(
            {
                "itemId": product_id,
                "title": item.get("title"),
                "sales": (
                    title_vo.get("salesDesc") if isinstance(title_vo, dict) else None
                )
                or item.get("vagueSellCount"),
                "price": buy.get("current_price") if isinstance(buy, dict) else None,
                "rateVO": components.get("rateVO")
                if isinstance(components, dict)
                else None,
                "shopId": seller.get("shopId") if isinstance(seller, dict) else None,
                "shopName": seller.get("shopName")
                if isinstance(seller, dict)
                else None,
                "shopUrl": seller.get("pcShopUrl")
                if isinstance(seller, dict)
                else None,
                "imageUrl": images[0] if isinstance(images, list) and images else None,
            }
        )
    return scoped


def _clean_image_value(value) -> str | None:
    """处理懒加载属性：srcset/多 URL 时取第一个可用 http(s) 地址。"""
    text = clean_text(value)
    if not text:
        return None
    for part in re.split(r"[,\s]+", text):
        part = part.strip()
        if part.startswith(("//", "http://", "https://")):
            url = normalize_image_url(part)
            if url:
                return url
    return normalize_image_url(text)


def _image_from_node(node) -> str | None:
    if node is None:
        return None
    for attr in (
        "data-src",
        "data-ks-lazyload",
        "data-lazy-img",
        "data-original",
        "data-img",
        "srcset",
        "src",
    ):
        value = node.get(attr)
        if not value:
            continue
        url = _clean_image_value(value)
        if url:
            return url
    return None


def _image_from_card(card) -> str | None:
    if card is None:
        return None
    for selector in TAOBAO["image"]:
        try:
            nodes = card.cssselect(selector)
        except Exception:
            nodes = []
        for node in nodes:
            url = _image_from_node(node)
            if url:
                return url
    return _image_from_node(card)


def _resolve_shop_id(
    raw_value,
    *,
    shop_url: str | None = None,
    image_url: str | None = None,
) -> tuple[str | None, str | None]:
    """解析 (shop_id, shop_id_raw)。

    优先卖家数字 ID（页面 userId/sellerId、店铺链接、主图路径）；加密串只
    写入 ``shop_id_raw`` 备查。若确实拿不到数字 ID，才回退加密串，避免把
    已有店铺信息直接丢成缺失。
    """
    raw = normalize_shop_id(raw_value)
    if raw is None and shop_url:
        raw = normalize_shop_id(extract_shop_id("taobao", shop_url))
    numeric = numeric_shop_id(raw)
    if numeric:
        return numeric, (raw if raw and not raw.isdigit() else None)
    from_url = (
        numeric_shop_id(extract_shop_id("taobao", shop_url)) if shop_url else None
    )
    if from_url:
        return from_url, raw
    from_image = shop_id_from_image_url(image_url)
    if from_image:
        return from_image, raw
    if raw and not raw.isdigit():
        return raw, raw
    return None, raw


def _deep_pick(record: dict, keys) -> object | None:
    """先取顶层键，再在嵌套 seller/shop/price 对象里找同名字段。"""
    value = pick(record, keys)
    if value not in (None, "", [], {}):
        return value
    return deep_find(record, keys, max_depth=4)


def _deep_find_priority(payloads, keys):
    """按 keys 顺序在多个 payload 中查找，避免先遇到的 "0" 覆盖 sellerId。"""
    for key in keys:
        value = deep_find(payloads, (key,))
        if value not in (None, "", [], {}):
            return value
    return None


def _iter_dict_nodes(
    obj, *, depth: int = 0, max_depth: int = 10, parent_key: str | None = None
):
    """遍历 JSON 中的 dict 节点；返回 ``(node, parent_key)``。"""
    if depth > max_depth:
        return
    if isinstance(obj, dict):
        yield obj, parent_key
        for key, value in obj.items():
            if isinstance(value, (dict, list)):
                yield from _iter_dict_nodes(
                    value, depth=depth + 1, max_depth=max_depth, parent_key=str(key)
                )
    elif isinstance(obj, list):
        for value in obj[:200]:
            if isinstance(value, (dict, list)):
                yield from _iter_dict_nodes(
                    value, depth=depth + 1, max_depth=max_depth, parent_key=parent_key
                )


def _node_product_id(node: dict) -> str | None:
    for key in _TAOBAO_DETAIL_ID_KEYS:
        if key in node:
            value = node.get(key)
            if value not in (None, "", 0, "0"):
                return str(value).strip()
    return None


def _detail_nodes(payloads, product_id: str) -> list[dict]:
    """定位商品详情主节点：优先 itemId 匹配，其次常见详情包装层。"""
    wanted = str(product_id or "").strip()
    matched: list[dict] = []
    wrapped: list[dict] = []
    for payload in payloads:
        for node, parent_key in _iter_dict_nodes(payload):
            node_id = _node_product_id(node)
            if node_id and node_id == wanted:
                matched.append(node)
                continue
            if (parent_key or "").lower() not in _TAOBAO_DETAIL_WRAPPER_KEYS:
                continue
            if any(key in node for key in ("title", "raw_title", "itemTitle")):
                wrapped.append(node)
    return matched or wrapped


def _buyer_nicknames(payloads) -> set[str]:
    """收集登录态/买家节点里的昵称，用于排除详情节点误带的买家 nick。"""
    nicks: set[str] = set()

    def add(value) -> None:
        text = clean_text(value)
        if text:
            nicks.add(text)

    for payload in payloads:
        for node, parent_key in _iter_dict_nodes(payload):
            is_buyer_node = (parent_key or "").lower() in _BUYER_NODE_KEYS
            if (
                not is_buyer_node
                and not _node_product_id(node)
                and not any(key in node for key in ("title", "raw_title", "itemTitle"))
            ):
                # 裸登录用户对象：有 userId/uid 但不是卖家/商品节点。
                is_buyer_node = any(
                    key in node for key in ("userId", "user_id", "uid")
                ) and not any(key in node for key in _TAOBAO_ANCESTOR_ID_KEYS)
            if not is_buyer_node:
                continue
            for key in _BUYER_NICK_KEYS:
                if key in node:
                    add(node.get(key))
    return nicks


def _detail_shop_candidates(payloads, product_id: str) -> tuple[list[str], list[str]]:
    """从商品详情节点/卖家店铺节点提取 (shop_id 候选, shop_name 候选)。

    显式 seller/shop 节点优先于商品详情节点自身的字段，详情节点字段又优先
    于外层兄弟节点；这样即使 item 上误带了买家 nick，也不会盖过店铺节点。
    """
    seller_ids: list[str] = []
    seller_names: list[str] = []
    detail_ids: list[str] = []
    detail_names: list[str] = []
    ancestor_ids: list[str] = []
    ancestor_names: list[str] = []

    def add(target: list[str], value, *, normalize: bool = False) -> None:
        if normalize:
            text = normalize_shop_id(value)
        else:
            text = clean_text(value)
            if text and text.lower() in _PLACEHOLDER_NAMES:
                text = None
        if text and text not in target:
            target.append(text)

    def read_seller_node(value) -> None:
        if isinstance(value, dict):
            for key in _TAOBAO_SELLER_ID_KEYS:
                if key in value:
                    add(seller_ids, value.get(key), normalize=True)
            for key in _TAOBAO_SELLER_NAME_KEYS:
                if key in value:
                    add(seller_names, value.get(key))
        elif isinstance(value, str):
            add(seller_names, value)

    for node in _detail_nodes(payloads, product_id):
        for key in _TAOBAO_SELLER_ID_KEYS:
            if key in node:
                add(detail_ids, node.get(key), normalize=True)
        for key in _TAOBAO_DETAIL_NAME_KEYS:
            if key in node:
                add(detail_names, node.get(key))

    # 详情接口常把 sellerId 放在 item 的兄弟节点（如 data.sellerId），
    # 或把 seller/shop 对象放在外层；只认明确卖家键，不碰裸 nick/userId。
    for payload in payloads:
        for node, _parent_key in _iter_dict_nodes(payload):
            for key in _TAOBAO_ANCESTOR_ID_KEYS:
                if key in node:
                    add(ancestor_ids, node.get(key), normalize=True)
            for key in _TAOBAO_ANCESTOR_NAME_KEYS:
                if key in node:
                    add(ancestor_names, node.get(key))
            for key in _TAOBAO_SELLER_NODE_KEYS:
                if key in node:
                    read_seller_node(node.get(key))

    ids: list[str] = []
    names: list[str] = []
    for value in [*seller_ids, *detail_ids, *ancestor_ids]:
        if value not in ids:
            ids.append(value)
    for value in [*seller_names, *detail_names, *ancestor_names]:
        if value not in names:
            names.append(value)
    buyer_nicks = _buyer_nicknames(payloads)
    if buyer_nicks:
        names = [name for name in names if name not in buyer_nicks]
    return ids, names


def _resolve_detail_shop_id(
    candidates: list[str],
    *,
    shop_url: str | None = None,
    image_url: str | None = None,
) -> tuple[str | None, str | None]:
    """优先卖家数字 ID；加密串保留到 ``shop_id_raw``。"""
    numeric = next((value for value in candidates if value.isdigit()), None)
    raw = next((value for value in candidates if not value.isdigit()), None)
    if numeric:
        return numeric, raw
    return _resolve_shop_id(
        candidates[0] if candidates else None,
        shop_url=shop_url,
        image_url=image_url,
    )


def _deep_text(record: dict, keys) -> str | None:
    value = _deep_pick(record, keys)
    if isinstance(value, dict):
        for nested in ("text", "value", "name", "title", "priceText", "content"):
            if value.get(nested) not in (None, ""):
                return clean_text(value.get(nested))
        return None
    if isinstance(value, (list, tuple)):
        for entry in value:
            text = clean_text(entry)
            if text:
                return text
        return None
    return clean_text(value)


class TaobaoPlatform(PlatformBase):
    name = "taobao"
    login_url = "https://login.taobao.com/member/login.jhtml"
    login_check_url = "https://i.taobao.com/my_taobao.htm"
    login_cookie_names = ("_tb_token_", "cookie2", "sgcookie", "unb")
    referer = "https://s.taobao.com/"
    search_capture_patterns = (
        "mtop",
        "h5api",
        "mtop.taobao",
        "h5api.m.taobao",
        "wsearch",
        "pcsearch",
        "appsearch",
    )
    detail_capture_patterns = ("mtop", "h5api", "review", "rate")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 连续空结果计数：只在淘宝启用，命中阈值立即 RiskBlocked 停平台。
        self._empty_result_streak = 0

    def login_cookie_ok(self) -> bool | None:
        """CDP 模式淘宝必须看到 ``_nk_`` 或 ``cookie2``+``unb``。"""
        if self.config.browser_mode(self.name) != "cdp":
            return None
        values = read_cookie_values(self.browser)
        if values is None:
            return False
        return taobao_logged_in(values)

    def build_search_url(self, keyword: str, page: int) -> str:
        return f"https://s.taobao.com/search?q={quote_plus(keyword)}&page={page}&sort=sale-desc"

    def on_empty_result(self, keyword: str, page: int) -> None:
        limit = int(self.config.crawl.taobao.risk.empty_result_streak)
        self._empty_result_streak += 1
        self.log_warning(
            f"淘宝疑似频控：连续空结果 {self._empty_result_streak}/{limit} 次"
            f"（keyword={keyword!r} page={page}），达到阈值立即停止本次淘宝采集，不重试"
        )
        if self._empty_result_streak >= limit:
            raise RiskBlocked(
                self.name,
                f"连续 {self._empty_result_streak} 次空结果，疑似频控（阈值 {limit}）",
                screenshot_path=self._capture_screenshot("empty_streak"),
            )

    def on_non_empty_result(self, keyword: str, page: int) -> None:
        self._empty_result_streak = 0

    def detail_url(self, product_id: str) -> str:
        prefix, _, raw = product_id.partition(":")
        pid = raw if prefix == "tmall" and raw else product_id
        if prefix == "tmall":
            return f"https://detail.tmall.com/item.htm?id={pid}"
        return f"https://item.taobao.com/item.htm?id={pid}"

    # ---------- 搜索 ----------
    def page_action(self, page) -> None:
        """等首屏卡片出现（最多 6 秒），给 mtop XHR + React 渲染留时间。

        空壳/风控页等不到卡片也继续，解析器会记录明确原因并落 debug HTML。
        """
        wait = getattr(page, "wait_for_selector", None)
        if not callable(wait):
            return
        try:
            wait(
                "#content_items_wrapper [data-nid], "
                "#content_items_wrapper [class*='Card--'], "
                "#mainsrp-itemlist .item",
                timeout=6000,
            )
        except Exception:  # noqa: BLE001 - 空壳/风控页等不到卡片也继续
            return

    def parse_search(
        self, result: PageResult, keyword: str, page: int, ip_code: str | None
    ) -> list[Item]:
        missing_before = self.stats.drop_reasons.get("product_id解析失败", 0)
        payloads = self.payloads(result)
        records = self.records(
            payloads, list_keys=_TAOBAO_LIST_KEYS, id_keys=_TAOBAO_ID_KEYS
        )
        # DOM 与接口都要：DOM 可能已渲染出新版卡片，接口则能补齐详情字段。
        items = self._parse_dom(result, keyword, page, ip_code)
        for index, record in enumerate(records, start=1):
            item = self._from_json(
                flatten_record(record), keyword, page, index, ip_code
            )
            if item is not None:
                items.append(item)
        # 淘宝/天猫共用商品 ID。接口缺少天猫标识时会默认成淘宝，不能
        # 因默认值与另一来源的明确天猫身份不同而把同一商品写成两条。
        tmall_ids = {item.product_id for item in items if item.platform == "tmall"}
        items = [
            item.model_copy(
                update={
                    "platform": "tmall",
                    "url": normalize_product_url("tmall", item.product_id),
                    "shop_type_raw_hint": "天猫",
                }
            )
            if item.platform == "taobao" and item.product_id in tmall_ids
            else item
            for item in items
        ]
        items = dedupe_items(items)
        missing = self.stats.drop_reasons.get("product_id解析失败", 0) - missing_before
        if missing > 0:
            self.save_debug_snapshot(
                result,
                f"product_id 缺失 {missing} 条（{self.name} 搜索页）",
                keyword=keyword,
                page=page,
            )
        elif not items:
            self.save_debug_snapshot(
                result,
                f"列表解析 0 条（{self.name} 搜索页）：{self._diagnose_empty(result, payloads, records)}",
                keyword=keyword,
                page=page,
            )
        return [enrich_flags(item) for item in items]

    def _diagnose_empty(
        self, result: PageResult, payloads: list, records: list[dict]
    ) -> str:
        """淘宝 0 条时给出可操作原因：空壳页 / 接口未捕获 / 接口结构变化。"""
        content = result.html or ""
        shell_markers = (
            "boneClass_",
            "ice-container",
            "TBPC_CDN_VERSION",
            "__ICE_APP_CONTEXT__",
        )
        shell = any(marker in content for marker in shell_markers)
        captured = len(result.captured)
        base = f"captured={captured} payloads={len(payloads)} records={len(records)}"
        if shell and captured == 0:
            return (
                "CSR 空壳页：首屏 HTML 只有骨架屏（boneClass_/ice-container），"
                "商品由 s.taobao.com 的 mtop/h5api XHR 返回；本次未捕获到任何接口响应。"
                "请确认页面已登录且未命中风控，并检查 search_capture_patterns。"
                f" {base}"
            )
        if shell:
            return (
                "CSR 空壳页：已捕获接口响应但未抽到商品记录，"
                "可能是 mtop 返回结构变化或接口被风控拦截。" + f" {base}"
            )
        return "DOM/接口均无商品记录，可能是选择器失效或页面被重定向。" + f" {base}"

    def _from_json(
        self, record: dict, keyword: str, page: int, rank: int, ip_code: str | None
    ) -> Item | None:
        raw_id = _deep_pick(record, _TAOBAO_ID_KEYS)
        url_raw = _deep_text(record, _TAOBAO_URL_KEYS)
        product_id = extract_product_id("taobao", raw_id) or extract_product_id(
            "taobao", url_raw
        )
        if not product_id:
            self.stats.drop(
                "product_id解析失败", str(raw_id or url_raw or "taobao-json")
            )
            return None
        tmall = _truthy(
            _deep_pick(
                record, ("is_tmall", "isTmall", "tmall", "isTmallSeller", "isTmallShop")
            )
        ) or is_tmall_url(url_raw)
        platform = "tmall" if tmall else "taobao"
        label = join_labels(
            _deep_pick(record, ("shopLabel", "shop_label", "labels", "tags", "icon")),
            "天猫" if tmall else None,
        )
        shop_name = _deep_text(
            record, ("nick", "shopName", "shop_name", "sellerNick", "seller_nick")
        )
        if not label and shop_name and "旗舰店" in shop_name:
            label = "旗舰店"
        image = _clean_image_value(_deep_pick(record, _TAOBAO_IMAGE_KEYS))
        shop_url = _deep_text(record, _TAOBAO_SHOP_URL_KEYS) or url_raw
        shop_id, shop_id_raw = _resolve_shop_id(
            _deep_pick(record, _TAOBAO_SHOP_ID_KEYS),
            shop_url=shop_url,
            image_url=image,
        )
        price_keys = (
            "view_price",
            "price",
            "priceShow",
            "priceText",
            "reservePrice",
            "reserve_price",
        )
        price = pick_price(record, price_keys)
        price_raw = pick_text(record, price_keys)
        comment_value = _deep_pick(record, _TAOBAO_COMMENT_KEYS)
        comment_text = _scalar_count(comment_value)
        item = Item(
            platform=platform,
            product_id=product_id,
            url=normalize_product_url(platform, product_id, url_raw),
            title=clean_title(
                _deep_text(
                    record, ("raw_title", "title", "itemTitle", "item_title", "name")
                )
            )
            or "",
            shop_id=shop_id,
            shop_name=shop_name,
            shop_id_raw=shop_id_raw,
            shop_label_raw=label,
            shop_type_raw_hint="天猫"
            if tmall
            else ("旗舰店" if label and "旗舰店" in label else "淘宝C店"),
            price=price,
            price_raw=price_raw or (clean_text(price) if price is not None else None),
            sales_raw=_deep_text(
                record,
                ("view_sales", "sales", "sold", "dealCnt", "realSales", "soldQuantity"),
            ),
            sales=parse_fuzzy_int(
                _deep_pick(
                    record,
                    (
                        "view_sales",
                        "sales",
                        "sold",
                        "dealCnt",
                        "realSales",
                        "soldQuantity",
                    ),
                )
            ),
            comments_raw=comment_text,
            comments=parse_fuzzy_int(comment_text),
            main_image_url=image,
            category=_deep_text(record, ("category", "catName", "categoryName", "cat")),
            ip_code=ip_code,
            search_keyword=keyword,
            page=page,
            rank=rank,
            crawled_at=self.now(),
            listed=True,
        )
        return self.validate(item)

    def _parse_dom(
        self, result: PageResult, keyword: str, page: int, ip_code: str | None
    ) -> list[Item]:
        root = parse_html(result.html)
        cards = card_nodes(root, TAOBAO["card"])
        items: list[Item] = []
        for index, card in enumerate(cards, start=1):
            link = href_of(card, TAOBAO["link"])
            if link:
                link = urljoin("https://s.taobao.com/", link)
            raw_id = extract_product_id("taobao", link)
            if not raw_id:
                for attr in ("data-id", "data-nid", "data-itemid", "data-item-id"):
                    raw_id = extract_product_id("taobao", card.get(attr))
                    if raw_id:
                        break
            if not raw_id:
                # 淘宝列表偶尔出现无 id 的卡片（新版 class 化 / 广告位混排）。
                # 落完整 HTML + 截图，便于下次实测定位是选择器还是页面结构问题。
                self.save_debug_snapshot(
                    result,
                    "淘宝 product_id 缺失",
                    keyword=keyword,
                    page=page,
                )
                self.stats.drop("product_id解析失败", "taobao-dom")
                continue
            tmall = (
                is_tmall_url(link)
                or "tmall" in " ".join(card.get("class", "").split()).lower()
            )
            platform = "tmall" if tmall else "taobao"
            label_text = (
                " ".join(
                    filter(
                        None,
                        [first_text(card, TAOBAO["label"]), "天猫" if tmall else None],
                    )
                ).strip()
                or None
            )
            shop_link = href_of(card, TAOBAO["shop_link"])
            if shop_link:
                shop_link = urljoin("https://s.taobao.com/", shop_link)
            shop_name = first_text(card, TAOBAO["shop"])
            raw_shop = None
            for attr in (
                "data-sellerid",
                "data-shopid",
                "data-userid",
                "data-userid-number",
            ):
                if card.get(attr):
                    raw_shop = clean_text(card.get(attr))
                    break
            if not raw_shop and shop_link:
                raw_shop = extract_shop_id("taobao", shop_link)
            image_url = _image_from_card(card)
            shop_id, shop_id_raw = _resolve_shop_id(
                raw_shop,
                shop_url=shop_link,
                image_url=image_url,
            )
            price_raw = first_text(card, TAOBAO["price"])
            item = Item(
                platform=platform,
                product_id=raw_id,
                url=normalize_product_url(platform, raw_id, link),
                title=first_text(card, TAOBAO["title"]) or "",
                shop_id=shop_id,
                shop_name=shop_name,
                shop_id_raw=shop_id_raw,
                shop_label_raw=label_text,
                shop_type_raw_hint="天猫"
                if tmall
                else ("旗舰店" if label_text and "旗舰店" in label_text else "淘宝C店"),
                price=parse_price(price_raw),
                price_raw=price_raw,
                sales_raw=first_text(card, TAOBAO["sales"]),
                sales=parse_fuzzy_int(first_text(card, TAOBAO["sales"])),
                main_image_url=image_url,
                ip_code=ip_code,
                search_keyword=keyword,
                page=page,
                rank=index,
                crawled_at=self.now(),
                listed=True,
            )
            valid = self.validate(item)
            if valid is not None:
                items.append(valid)
        return items

    # ---------- 详情 ----------
    def parse_detail(self, result: PageResult, product_id: str) -> ItemPatch | None:
        prefix, _, raw = product_id.partition(":")
        pid = raw if prefix == "tmall" and raw else product_id
        # 补丁沿用调用方记录身份；淘宝链接跳转天猫不能改变合并主键。
        # 天猫列表记录由调用方显式传入 tmall:ID。
        platform = "tmall" if prefix == "tmall" else "taobao"
        payloads = _scoped_detail_payloads(self.payloads(result), pid)
        comments_raw = _comment_total(payloads, pid)
        comment_source = "structured" if comments_raw is not None else "unresolved"
        comment_conflict = False
        sales_raw = clean_text(
            deep_find(payloads, ("sales", "sold", "sellCount", "totalSold"))
        )
        price_raw = detail_price_raw(payloads, ("price", "view_price", "priceShow"))
        title = clean_title(deep_find(payloads, ("title", "raw_title", "itemTitle")))
        image = _clean_image_value(_deep_find_priority(payloads, _TAOBAO_IMAGE_KEYS))
        shop_url = clean_text(_deep_find_priority(payloads, _TAOBAO_SHOP_URL_KEYS))
        shop_ids, shop_names = _detail_shop_candidates(payloads, pid)
        shop_name = shop_names[0] if shop_names else None
        shop_id, shop_id_raw = _resolve_detail_shop_id(
            shop_ids,
            shop_url=shop_url,
            image_url=image,
        )

        root = parse_html(result.html)
        dom_comment_hit = False
        if root is not None:
            title = title or first_text(root, TAOBAO_DETAIL["title"])
            price_raw = price_raw or first_text(root, TAOBAO_DETAIL["price"])
            shop_name = shop_name or first_text(root, TAOBAO_DETAIL["shop"])
            dom_comments = first_text(root, TAOBAO_DETAIL["comments"])
            # 新版评价区标题：只认“用户评价·总数”，不取好评率/标签次数。
            for node in root.cssselect(
                "[class*='Comments--'] [class*='tabDetailItemTitle--']"
            ):
                heading = clean_text(node.text_content()) or ""
                matched = re.fullmatch(
                    r"用户评价\s*[·•]\s*([\d,.]+\s*[万亿]?\+?)", heading
                )
                if matched and parse_fuzzy_int(matched.group(1)) is not None:
                    dom_comments = matched.group(1)
                    break
            if dom_comments:
                dom_comment_hit = True
                dom_count = parse_fuzzy_int(dom_comments)
                if (
                    comments_raw is not None
                    and dom_count is not None
                    and parse_fuzzy_int(comments_raw) != dom_count
                ):
                    comment_conflict = True
                    comments_raw = None
                elif comments_raw is None and dom_count is not None:
                    comments_raw = dom_comments
                    comment_source = "dom"
            sales_raw = sales_raw or first_text(root, TAOBAO_DETAIL["sales"])
            image = image or _clean_image_value(
                first_attr(
                    root,
                    TAOBAO_DETAIL["image"],
                    ("data-src", "data-ks-lazyload", "srcset", "src"),
                )
            )
            shop_link = href_of(root, TAOBAO_DETAIL["shop_link"])
            if not shop_id or not str(shop_id).isdigit():
                # 页面数据只给到加密串时，继续尝试 DOM 店铺链接/主图数字 ID。
                shop_id, shop_id_raw = _resolve_shop_id(
                    shop_id_raw or shop_id,
                    shop_url=shop_link or result.url,
                    image_url=image,
                )
            if not shop_id:
                shop_id, shop_id_raw = _resolve_shop_id(
                    extract_shop_id("taobao", result.url),
                    shop_url=shop_link or result.url,
                    image_url=image,
                )
        comments = parse_fuzzy_int(comments_raw)
        comment_flags = []
        if comments is None:
            reason = (
                "评论来源冲突：结构化数据与页面评价总数不一致，待核验"
                if comment_conflict
                else "评论未解析到：未确认源页面是否提供有效总数，待核验"
            )
            comment_flags.append(reason)
            self.logger.info(
                "detail comments missing platform=%s product_id=%s captured=%d payloads=%d "
                "dom_selector_hit=%s reason=%s",
                self.name,
                product_id,
                len(result.captured),
                len(payloads),
                dom_comment_hit,
                reason,
            )
        else:
            self.logger.info(
                "detail field resolved platform=%s product_id=%s field=comments value=%s source=%s",
                self.name,
                product_id,
                comments,
                comment_source,
            )
        content = result.html or ""
        listed = not any(marker in content for marker in TAOBAO_DETAIL["offline"])
        patch = ItemPatch(
            platform=platform,
            product_id=pid,
            title=title,
            shop_id=shop_id,
            shop_name=shop_name,
            shop_id_raw=shop_id_raw,
            price=parse_price(price_raw),
            price_raw=price_raw,
            sales_raw=sales_raw,
            sales=parse_fuzzy_int(sales_raw),
            comments_raw=comments_raw,
            comments=comments,
            main_image_url=image,
            listed=listed,
            flags=comment_flags + ([] if listed else ["商品下架"]),
        )
        return patch
