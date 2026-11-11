"""京东采集。

列表解析采用多策略兜底（2026-09 京东搜索页改版后旧 ``.p-name/.p-price``
结构经常整页 0 条）：

1. DOM：``li.gl-item`` / ``[data-sku]`` / hash class 商品卡，含无 ``<a href>``
   的 JS 跳转卡片；
2. 页面内嵌 JSON：``window.__INIT_DATA__`` / ``__NEXT_DATA__`` / ``pageData`` 等；
3. XHR：``api.m.jd.com``、``p.3.cn``、``c0.3.cn`` 等列表/价格响应。

列表页价格异步加载：搜索后滚动到底并等待价格；同时监听 p.3.cn / c0.3.cn
价格 JSON。销量列表页常缺：sales=null + flag ``销量缺失``。
"""

from __future__ import annotations

import re
from decimal import Decimal
from urllib.parse import quote_plus, urljoin

from crawler.core.browser import PageResult
from crawler.core.dom import (
    ancestor_card,
    attr_from_node,
    card_nodes_all,
    first_attr,
    first_text,
    href_of,
    node_key,
    normalize_space,
    parse_html,
    in_document_order,
)
from crawler.core.jsonutil import deep_find
from crawler.core.parse import (
    clean_text,
    dedupe_items,
    extract_product_id,
    extract_shop_id,
    normalize_image_url,
    normalize_product_url,
    parse_fuzzy_int,
    parse_price,
)
from crawler.core.schema import enrich_flags
from crawler.models import Item, ItemPatch
from crawler.platforms.base import PlatformBase
from crawler.platforms.helpers import (
    detail_price_raw,
    flatten_record,
    join_labels,
    pick,
    pick_int,
    pick_price,
    pick_text,
    text_list,
)
from crawler.platforms.selectors import JD, JD_DETAIL

_JD_LIST_KEYS = (
    "warelist",
    "ware_list",
    "skus",
    "sku_list",
    "products",
    "product_list",
    "goods",
    "goodslist",
    "goods_list",
    "items",
    "item_list",
    "list",
    "result",
    "results",
    "data",
    "searchResult",
    "search_result",
)
_JD_ID_KEYS = (
    "skuId",
    "sku_id",
    "sku",
    "wareId",
    "ware_id",
    "itemId",
    "item_id",
    "productId",
    "product_id",
    "id",
)
_JD_TITLE_KEYS = (
    "title",
    "wareName",
    "ware_name",
    "wname",
    "skuName",
    "sku_name",
    "itemName",
    "item_name",
    "productName",
    "product_name",
    "goodsName",
    "goods_name",
    "adTitle",
)
_JD_URL_KEYS = (
    "itemUrl",
    "item_url",
    "wareUrl",
    "ware_url",
    "detailUrl",
    "detail_url",
    "clickUrl",
    "click_url",
    "jumpUrl",
    "jump_url",
    "url",
    "link",
)
_PRICE_TEXT_RE = re.compile(r"[¥￥]\s*([\d,]+(?:\.\d+)?)")
_PRICE_YUAN_RE = re.compile(r"([\d,]+(?:\.\d+)?)\s*元")
_COMMENT_TEXT_RE = re.compile(r"([\d,.]+\s*[万万千百]?\+?)\s*(?:条)?(?:评价|评论|晒图)")
_COMMENT_INLINE_RE = re.compile(
    r"(?:评价|评论)\s*[（(]\s*([\d,.]+\s*[万万千百]?\+?)\s*[)）]"
)
_SALES_TEXT_RE = re.compile(r"(?:已售|销量|成交)\s*([\d,.]+\s*[万万千百]?\+?)")


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return (clean_text(value) or "").lower() in {"1", "true", "yes", "y", "自营", "jd"}


def _jd_product_id(value: object | None) -> str | None:
    text = clean_text(value)
    if not text:
        return None
    if text.upper().startswith("J_"):
        text = text[2:]
    return extract_product_id("jd", text)


def _price_from_text(text: str | None) -> str | None:
    text = normalize_space(text) or ""
    for pattern in (_PRICE_TEXT_RE, _PRICE_YUAN_RE):
        match = pattern.search(text)
        if match:
            return match.group(0)
    return None


def _comments_from_text(text: str | None) -> str | None:
    value = normalize_space(text) or ""
    match = _COMMENT_TEXT_RE.search(value)
    if match:
        return match.group(0)
    inline = _COMMENT_INLINE_RE.search(value)
    return inline.group(1) if inline else None


def _sales_from_text(text: str | None) -> str | None:
    match = _SALES_TEXT_RE.search(normalize_space(text) or "")
    return match.group(1) if match else None


def _title_from_text(text: str | None) -> str | None:
    """新卡片没有 title class 时，从卡片文本里剥掉价格/促销词取标题。"""
    value = normalize_space(text) or ""
    if not value:
        return None
    value = _PRICE_TEXT_RE.sub(" ", value)
    value = _PRICE_YUAN_RE.sub(" ", value)
    value = re.sub(r"^(?:广告|自营|京东自营|京东POP|POP)\s*", "", value)
    value = re.sub(r"\s*(?:已售|销量|成交)\s*[\d,.]+.*$", "", value)
    value = re.sub(r"\s*[\d,.]+\s*[万万千百]?\+?\s*(?:条)?(?:评价|评论).*$", "", value)
    value = normalize_space(value)
    return value[:200] if value and len(value) >= 2 else None


def _detail_payloads_for_product(payloads, product_id: str):
    """排除明确属于其他 SKU 的子树，避免推荐价格/标题污染当前商品。"""
    budget = [10000]

    def prune(value, depth=0):
        budget[0] -= 1
        if depth > 10 or budget[0] < 0:
            return None
        if isinstance(value, dict):
            identity = _jd_product_id(pick(value, _JD_ID_KEYS))
            if identity is not None and identity != product_id:
                return None
            return {key: prune(part, depth + 1) for key, part in value.items()}
        if isinstance(value, list):
            return [prune(part, depth + 1) for part in value[:200]]
        return value

    return prune(payloads)


class JdPlatform(PlatformBase):
    name = "jd"
    login_url = "https://passport.jd.com/new/login.aspx"
    login_check_url = "https://home.jd.com/"
    login_cookie_names = ("pt_key", "thor", "pin", "pt_pin")
    referer = "https://search.jd.com/"
    search_capture_patterns = (
        "p.3.cn",
        "c0.3.cn",
        "p.3.cn/prices",
        "c0.3.cn/stock",
        "api.m.jd.com",
        "api.jd.com",
        "dx.3.cn",
        "search.jd.com/api",
        "search.jd.com/client",
    )
    detail_capture_patterns = (
        "p.3.cn",
        "c0.3.cn",
        "item.jd.com",
        "club.jd.com",
        "productCommentSummaries",
        "comment/product",
        "comment",
    )

    def build_search_url(self, keyword: str, page: int) -> str:
        # 京东搜索页按奇数页展示主列表：第 1 页 page=1，第 2 页 page=3。
        jd_page = 2 * page - 1
        return f"https://search.jd.com/Search?keyword={quote_plus(keyword)}&enc=utf-8&page={jd_page}&psort=3"

    def detail_url(self, product_id: str) -> str:
        return f"https://item.jd.com/{product_id}.html"

    def page_action(self, page) -> None:
        if page is None:
            return
        steps = self.config.crawl.jd_scroll_steps
        wait_ms = self.config.crawl.jd_scroll_wait_ms
        try:
            for _ in range(max(1, steps)):
                page.evaluate("window.scrollBy(0, Math.max(window.innerHeight, 900))")
                page.wait_for_timeout(wait_ms)
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(wait_ms)
        except Exception:
            pass
        try:
            page.wait_for_selector(
                "[data-sku], #J_goodsList li, .gl-item",
                timeout=min(3000, self.config.crawl.jd_price_wait_ms),
            )
        except Exception:
            pass
        try:
            page.wait_for_selector(
                ".p-price i, .p-price strong i, [class*='price' i]",
                timeout=self.config.crawl.jd_price_wait_ms,
            )
        except Exception:
            pass

    # ---------- 搜索 ----------
    def parse_search(
        self, result: PageResult, keyword: str, page: int, ip_code: str | None
    ) -> list[Item]:
        missing_before = self.stats.drop_reasons.get("product_id解析失败", 0)
        payloads = self.payloads(result)
        price_map = self._price_map(payloads)
        items = self._parse_dom(result, keyword, page, ip_code, price_map)
        items.extend(self._from_records(payloads, keyword, page, ip_code, price_map))
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
                f"列表解析 0 条（{self.name} 搜索页）",
                keyword=keyword,
                page=page,
            )
        return [enrich_flags(item) for item in items]

    def _from_records(
        self,
        payloads,
        keyword: str,
        page: int,
        ip_code: str | None,
        price_map: dict[str, Decimal],
    ) -> list[Item]:
        records = self.records(payloads, list_keys=_JD_LIST_KEYS, id_keys=_JD_ID_KEYS)
        items: list[Item] = []
        for index, record in enumerate(records, start=1):
            item = self._from_json(
                flatten_record(record), keyword, page, index, ip_code, price_map
            )
            if item is not None:
                items.append(item)
        return items

    def _from_json(
        self,
        record: dict,
        keyword: str,
        page: int,
        rank: int,
        ip_code: str | None,
        price_map: dict[str, Decimal],
    ) -> Item | None:
        title = pick_text(record, _JD_TITLE_KEYS)
        url_raw = pick_text(record, _JD_URL_KEYS)
        # 缺标题但带商品链接/店铺/主图的记录仍进入补采；单独价格响应不算商品。
        if not title and not (
            self.complete_fields
            and (
                url_raw
                or pick(record, ("shopName", "venderName", "imageUrl", "skuPic"))
            )
        ):
            return None
        product_id = _jd_product_id(pick(record, _JD_ID_KEYS)) or _jd_product_id(
            url_raw
        )
        if not product_id:
            self.stats.drop(
                "product_id解析失败",
                str(pick(record, _JD_ID_KEYS) or url_raw or "jd-json"),
            )
            return None
        price_raw = pick_text(
            record,
            (
                "price",
                "p",
                "jdPrice",
                "salePrice",
                "finalPrice",
                "currentPrice",
                "priceText",
                "showPrice",
            ),
        )
        price = pick_price(
            record,
            (
                "price",
                "p",
                "jdPrice",
                "salePrice",
                "finalPrice",
                "currentPrice",
                "showPrice",
            ),
        )
        if price is None:
            price = price_map.get(product_id)
        if price_raw is None and price is not None:
            price_raw = str(price)
        labels = text_list(
            pick(record, ("tags", "icons", "labels", "shopLabel", "shop_label"))
        )
        self_operated = _truthy(
            pick(
                record,
                (
                    "selfOperated",
                    "self_operated",
                    "isSelf",
                    "isZx",
                    "isJdSelf",
                    "jdSelf",
                ),
            )
        )
        if "自营" in (title or "") or "自营" in " ".join(labels):
            self_operated = True
        shop_link = pick_text(
            record,
            (
                "shopUrl",
                "shop_url",
                "mallUrl",
                "mall_url",
                "venderUrl",
                "vender_url",
                "shopLink",
                "shop_link",
                "storeUrl",
                "store_url",
            ),
        )
        shop_id = clean_text(
            pick(
                record,
                (
                    "shopId",
                    "shop_id",
                    "venderId",
                    "vender_id",
                    "storeId",
                    "store_id",
                    "shop_id_str",
                ),
            )
        )
        if not shop_id:
            shop_id = extract_shop_id("jd", shop_link)
        comment_keys = (
            "CommentCount",
            "CommentCountStr",
            "commentCount",
            "comment_count",
            "comments",
            "evaluationCount",
            "commentNum",
            "commentTotal",
            "totalComment",
            "totalComments",
        )
        comments_raw = pick_text(record, comment_keys)
        sales_raw = pick_text(
            record,
            (
                "sales",
                "sold",
                "saleCount",
                "sale_count",
                "inOrderCount",
                "volume",
                "saleQuantity",
            ),
        )
        item = Item(
            platform="jd",
            product_id=product_id,
            url=normalize_product_url("jd", product_id, url_raw),
            title=title or "",
            shop_id=shop_id,
            shop_name=pick_text(
                record,
                (
                    "shopName",
                    "shop_name",
                    "venderName",
                    "vender_name",
                    "storeName",
                    "sellerName",
                    "mallName",
                ),
            ),
            shop_label_raw=join_labels(
                "京东自营" if self_operated else "京东POP", labels
            ),
            shop_type_raw_hint="京东自营" if self_operated else "京东POP",
            price=price,
            price_raw=price_raw,
            sales_raw=sales_raw,
            sales=pick_int(
                record,
                (
                    "sales",
                    "sold",
                    "saleCount",
                    "sale_count",
                    "inOrderCount",
                    "volume",
                    "saleQuantity",
                ),
            ),
            comments_raw=comments_raw,
            comments=pick_int(record, comment_keys),
            main_image_url=normalize_image_url(
                pick(
                    record,
                    (
                        "imageUrl",
                        "image_url",
                        "image",
                        "img",
                        "pic",
                        "mainImage",
                        "imgUrl",
                        "skuPic",
                        "logo",
                    ),
                )
            ),
            category=pick_text(
                record, ("category", "catName", "categoryName", "cidName")
            ),
            ip_code=ip_code,
            search_keyword=keyword,
            page=page,
            rank=rank,
            crawled_at=self.now(),
            listed=True,
        )
        return self.validate(item)

    def _parse_dom(
        self,
        result: PageResult,
        keyword: str,
        page: int,
        ip_code: str | None,
        price_map: dict[str, Decimal],
    ) -> list[Item]:
        root = parse_html(result.html)
        if root is None:
            return []
        cards = card_nodes_all(root, JD["card"])
        cards.extend(self._anchor_cards(root))
        cards = self._dedupe_cards(cards)
        items: list[Item] = []
        for index, card in enumerate(cards, start=1):
            item = self._item_from_card(card, keyword, page, index, ip_code, price_map)
            if item is not None:
                items.append(item)
        return items

    def _dedupe_cards(self, cards: list) -> list:
        seen: set[str] = set()
        result = []
        for card in cards:
            key = node_key(card)
            if key in seen:
                continue
            seen.add(key)
            result.append(card)
        return in_document_order(result)

    def _anchor_cards(self, root) -> list:
        """从 item.jd.com 链接反推最近卡片（新旧 DOM 都作为补充策略）。"""
        cards = []
        seen: set[str] = set()
        for selector in ("a[href*='item.jd.com']", "a[href*='/item/']"):
            try:
                anchors = root.cssselect(selector)
            except Exception:
                anchors = []
            for anchor in anchors:
                href = anchor.get("href") or ""
                if not _jd_product_id(href):
                    continue
                card = ancestor_card(anchor)
                key = node_key(card)
                if key in seen:
                    continue
                seen.add(key)
                cards.append(card)
        return cards

    def _item_from_card(
        self,
        card,
        keyword: str,
        page: int,
        index: int,
        ip_code: str | None,
        price_map: dict[str, Decimal],
    ) -> Item | None:
        link = href_of(card, JD["link"])
        if not link and card.tag == "a":
            link = card.get("href")
        if not link:
            link = href_of(card, ("a[href*='item.jd.com']",))
        if link:
            link = urljoin("https://search.jd.com/", link)

        raw_id = (
            clean_text(card.get("data-sku"))
            or attr_from_node(
                card,
                (
                    "data-skuid",
                    "data-sku-id",
                    "data-wareid",
                    "data-ware-id",
                    "data-itemid",
                    "data-id",
                ),
            )
            or _jd_product_id(link)
        )
        product_id = _jd_product_id(raw_id)
        if not product_id:
            # 没有任何商品 ID 的占位/广告卡不计数，避免把整页 wrapper 当失败。
            if clean_text(first_text(card, JD["title"])) or _price_from_text(
                card.text_content()
            ):
                self.stats.drop("product_id解析失败", "jd-dom")
            return None

        card_text = normalize_space(card.text_content()) or ""
        title = first_text(card, JD["title"])
        if not title:
            title = (
                clean_text(card.get("title"))
                or clean_text(card.get("data-title"))
                or clean_text(card.get("aria-label"))
            )
        if not title:
            title = first_attr(card, ("img[alt]", "img[title]"), ("alt", "title"))
        if not title:
            title = _title_from_text(card_text)
        elif "¥" in title or "￥" in title:
            title = _title_from_text(title) or title
        price_raw = first_text(card, JD["price"]) or _price_from_text(card_text)
        price = parse_price(price_raw)
        if price is None:
            price = price_map.get(product_id)
        if price_raw is None and price is not None:
            price_raw = str(price)
        if not title and not price_raw and not self.complete_fields:
            return None

        labels = first_text(card, JD["label"])
        self_operated = "自营" in (labels or "") or "自营" in card_text[:120]
        extra_labels = [
            label
            for label in text_list(labels)
            if label not in {"自营", "POP", "京东自营", "京东POP"}
        ]
        shop_label = join_labels(
            "京东自营" if self_operated else "京东POP", extra_labels
        )
        shop_name = first_text(card, JD["shop"])
        shop_link = href_of(card, JD["shop_link"])
        if shop_link:
            shop_link = urljoin("https://search.jd.com/", shop_link)
        shop_id = extract_shop_id("jd", shop_link) or clean_text(
            attr_from_node(
                card, ("data-shopid", "data-shop-id", "data-venderid", "data-vender-id")
            )
        )
        comments_raw = first_text(card, JD["comments"]) or _comments_from_text(
            card_text
        )
        sales_raw = _sales_from_text(card_text)
        image = first_attr(
            card,
            JD["image"],
            ("data-lazy-img", "src", "data-src", "data-img", "data-original"),
        )
        if not image:
            image = first_attr(
                card,
                ("img",),
                ("data-lazy-img", "src", "data-src", "data-img", "data-original"),
            )
        item = Item(
            platform="jd",
            product_id=product_id,
            url=normalize_product_url("jd", product_id, link),
            title=title or "",
            shop_id=shop_id,
            shop_name=shop_name,
            shop_label_raw=shop_label,
            shop_type_raw_hint="京东自营" if self_operated else "京东POP",
            price=price,
            price_raw=price_raw,
            sales_raw=sales_raw,
            sales=parse_fuzzy_int(sales_raw),
            comments_raw=comments_raw,
            comments=parse_fuzzy_int(comments_raw),
            main_image_url=normalize_image_url(image),
            ip_code=ip_code,
            search_keyword=keyword,
            page=page,
            rank=index,
            crawled_at=self.now(),
            listed=True,
        )
        return self.validate(item)

    def _price_map(self, payloads) -> dict[str, Decimal]:
        prices: dict[str, Decimal] = {}

        def walk(obj, depth: int = 0) -> None:
            if depth > 8:
                return
            if isinstance(obj, dict):
                lowered = {str(key).lower(): value for key, value in obj.items()}
                raw_id = (
                    lowered.get("id")
                    or lowered.get("skuid")
                    or lowered.get("sku_id")
                    or lowered.get("sku")
                )
                raw_price = pick(
                    obj, ("p", "price", "op", "currentprice", "current_price")
                )
                if raw_id not in (None, "") and raw_price not in (None, ""):
                    sku = str(raw_id).upper().removeprefix("J_")
                    price = parse_price(raw_price)
                    if sku.isdigit() and price is not None:
                        prices.setdefault(sku, price)
                # 形如 {"1001": {"p": "59.9"}}
                for key, value in obj.items():
                    if str(key).isdigit() and isinstance(value, dict):
                        raw_price = pick(value, ("p", "price"))
                        price = parse_price(raw_price)
                        if price is not None:
                            prices.setdefault(str(key), price)
                for value in obj.values():
                    walk(value, depth + 1)
            elif isinstance(obj, list):
                for value in obj[:200]:
                    walk(value, depth + 1)

        for payload in payloads:
            walk(payload)
        return prices

    # ---------- 详情 ----------
    def parse_detail(self, result: PageResult, product_id: str) -> ItemPatch | None:
        payloads = _detail_payloads_for_product(self.payloads(result), product_id)
        comment_keys = (
            "CommentCount",
            "CommentCountStr",
            "commentCount",
            "comment_count",
            "comments",
            "commentTotal",
            "totalComment",
            "totalComments",
            "evaluationCount",
            "commentNum",
        )
        comments_raw = clean_text(deep_find(payloads, comment_keys))
        sales_raw = clean_text(deep_find(payloads, ("sales", "sold", "saleCount")))
        matched_price = self._price_map(payloads).get(product_id)
        price_raw = (
            str(matched_price)
            if matched_price is not None
            else detail_price_raw(payloads, ("p", "price"))
        )
        title = clean_text(
            deep_find(payloads, ("skuName", "wareName", "wname", "productName"))
        )
        shop_name = clean_text(
            deep_find(payloads, ("shopName", "shop_name", "venderName"))
        )
        shop_id = clean_text(
            deep_find(
                payloads,
                ("shopId", "shop_id", "venderId", "vender_id", "storeId", "store_id"),
            )
        )
        image = normalize_image_url(deep_find(payloads, ("image", "img", "mainImage")))

        root = parse_html(result.html)
        if root is not None:
            title = first_text(root, JD_DETAIL["title"]) or title
            title = title or first_attr(
                root, ("meta[property='og:title']",), ("content",)
            )
            price_raw = price_raw or first_text(root, JD_DETAIL["price"])
            shop_name = shop_name or first_text(root, JD_DETAIL["shop"])
            comments_raw = comments_raw or first_text(root, JD_DETAIL["comments"])
            sales_raw = sales_raw or first_text(root, JD_DETAIL["sales"])
            image = image or normalize_image_url(
                first_attr(
                    root, JD_DETAIL["image"], ("src", "data-src", "data-lazy-img")
                )
            )
            if not shop_id:
                shop_link = href_of(root, JD_DETAIL["shop_link"])
                shop_id = extract_shop_id("jd", shop_link)
            if not shop_id:
                shop_id = extract_shop_id("jd", result.url)
        if not comments_raw:
            # 新版详情页把评价数放在“买家评价(5万+)”文本里。
            comments_raw = _comments_from_text(result.html or "")
        content = result.html or ""
        listed = not any(marker in content for marker in JD_DETAIL["offline"])
        return ItemPatch(
            platform="jd",
            product_id=product_id,
            title=title,
            shop_id=shop_id,
            shop_name=shop_name,
            price=parse_price(price_raw),
            price_raw=price_raw,
            sales_raw=sales_raw,
            sales=parse_fuzzy_int(sales_raw),
            comments_raw=comments_raw,
            comments=parse_fuzzy_int(comments_raw),
            main_image_url=image,
            listed=listed,
            flags=[] if listed else ["商品下架"],
        )
