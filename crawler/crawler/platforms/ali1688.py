"""1688 采集。

中文关键词必须 GBK 百分号编码；价格区间取最低；成交/已售解析下限。
列表解析采用多策略兜底（2026 年搜索页已从 ``.offer-list-row`` 改为
``a.search-offer-item`` / ``#sm-offer-list > div``，旧选择器会整页 0 条）：

1. DOM：新旧卡片选择器 + 从 offer 链接反推卡片；
2. 页面内嵌 JSON：``window.__INIT_DATA__`` / ``window._offerList`` 等；
3. XHR：``h5api.m.1688.com`` / ``mtop`` / JSONP 响应。
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urljoin

from crawler.core.browser import PageResult
from crawler.core.cookies import ali1688_logged_in, read_cookie_values
from crawler.core.errors import (
    DailyLimitExceeded,
    LoginRequired,
    RetryableError,
    RetryExhausted,
    RiskBlocked,
)
from crawler.core.dom import (
    all_texts,
    ancestor_card,
    attr_from_node,
    card_nodes_all,
    first_attr,
    first_node,
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
    gbk_quote,
    keyword_hit_ratio,
    normalize_image_url,
    normalize_product_url,
    normalize_shop_id,
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
from crawler.platforms.selectors import ALI1688, ALI1688_DETAIL, ALI1688_SHOP

_ALI_LIST_KEYS = (
    "offerlist",
    "offer_list",
    "offers",
    "results",
    "items",
    "datalist",
    "data",
    "list",
    "records",
    "products",
    "productList",
    "offerListDTO",
    "offerListData",
    "docs",
)
_ALI_ID_KEYS = (
    "offerId",
    "offer_id",
    "offerID",
    "offerid",
    "productId",
    "product_id",
    "itemId",
    "item_id",
    "id",
)
_ALI_TITLE_KEYS = (
    "subject",
    "title",
    "offerTitle",
    "offer_title",
    "name",
    "productTitle",
    "product_title",
    "seoTitle",
)
_ALI_URL_KEYS = (
    "offerUrl",
    "offer_url",
    "offerUrlPC",
    "offer_url_pc",
    "detailUrl",
    "detail_url",
    "detailUrlPC",
    "url",
    "link",
)
_ALI_PRICE_KEYS = (
    "price",
    "priceRange",
    "price_range",
    "priceInfo",
    "price_info",
    "unitPrice",
    "unit_price",
    "showPrice",
    "show_price",
    "salePrice",
    "sale_price",
    "minPrice",
    "min_price",
    "priceText",
    "priceModel",
)
_ALI_SHOP_KEYS = (
    "companyName",
    "company_name",
    "shopName",
    "shop_name",
    "sellerName",
    "seller_name",
    "supplierName",
    "supplier_name",
    "memberName",
    "member_name",
    "companyTitle",
    "company_title",
    "shopTitle",
    "shop_title",
    "sellerNick",
    "seller_nick",
    "supplierTitle",
    "supplier_title",
)
_ALI_SHOP_ID_KEYS = (
    "memberId",
    "member_id",
    "memberID",
    "sellerMemberId",
    "seller_member_id",
    "shopId",
    "shop_id",
    "companyId",
    "company_id",
    "sellerId",
    "seller_id",
    "userId",
    "user_id",
    "loginId",
    "login_id",
    "sellerLoginId",
    "seller_login_id",
)
_ALI_SALES_KEYS = (
    "sales",
    "saleQuantity",
    "sale_quantity",
    "soldQuantity",
    "sold_quantity",
    "bookedCount",
    "booked_count",
    "tradeQuantity",
    "trade_quantity",
    "sold",
    "saleCount",
    "sale_count",
    "dealCount",
    "deal_count",
    "quantitySold",
    "saleNum",
    "monthSold",
)
_ALI_COMMENT_KEYS = (
    "commentCount",
    "comment_count",
    "comments",
    "commentsCount",
    "comments_count",
    "rateCount",
    "rate_count",
    "reviewCount",
    "review_count",
    "totalComment",
    "totalComments",
    "commentTotal",
    "evaluationCount",
    "itemCommentCount",
)
_ALI_IMAGE_KEYS = (
    "picUrl",
    "pic_url",
    "imageUrl",
    "image_url",
    "mainImage",
    "main_image",
    "img",
    "pic",
    "offerPicUrl",
)
_ALI_MEMBER_ATTR_RE = re.compile(
    r"member_id[@=]([0-9A-Za-z_-]+)|memberId=([0-9A-Za-z_-]+)", re.I
)
_PRICE_TEXT_RE = re.compile(
    r"[¥￥]\s*([\d,]+(?:\.\d+)?(?:\s*[-~至]\s*[\d,]+(?:\.\d+)?)?)"
)
_PRICE_RANGE_RE = re.compile(
    r"(?<![\d.])([\d,]+(?:\.\d+)?\s*[-~至]\s*[\d,]+(?:\.\d+)?)(?![\d.])"
)
_SALES_TEXT_RE = re.compile(r"(?:已售|成交|销量)\s*([\d,.]+\s*[万万千百]?\+?)")
_SALES_PIECE_RE = re.compile(r"([\d,.]+\s*[万万千百]?\+?)\s*件")
_COMMENT_TEXT_RE = re.compile(r"([\d,.]+\s*[万万千百]?\+?)\s*(?:条)?(?:评价|评论|反馈)")


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return (clean_text(value) or "").lower() in {
        "1",
        "true",
        "yes",
        "y",
        "是",
        "实力商家",
        "super",
    }


def _ali_product_id(value: object | None) -> str | None:
    return extract_product_id("1688", clean_text(value))


def _price_from_text(text: str | None) -> str | None:
    value = normalize_space(text) or ""
    # 价格容器会同时包住运费和成交量，只提取明确商品价，不能把运费当售价。
    value = re.sub(r"(?:运费|邮费)\s*[:：]?\s*[¥￥]?\s*[\d,.]+\s*元?", " ", value)
    for pattern in (_PRICE_TEXT_RE, _PRICE_RANGE_RE):
        match = pattern.search(value)
        if match:
            return match.group(0)
    return None


def _sales_from_text(text: str | None) -> str | None:
    value = normalize_space(text) or ""
    value = re.sub(r"[\d,.]+\s*[万千百]?\+?\s*件\s*(?:起批|起订|起售|装)", " ", value)
    value = re.sub(r"(?:起批|起订|起售|最少|每包)\s*[:：]?\s*[\d,.]+\s*件", " ", value)
    match = _SALES_TEXT_RE.search(value) or _SALES_PIECE_RE.search(value)
    return match.group(0) if match else None


def _comments_from_text(text: str | None) -> str | None:
    match = _COMMENT_TEXT_RE.search(normalize_space(text) or "")
    return match.group(0) if match else None


def _dsr_comment_count(payloads) -> str | None:
    """从 1688 的 queryDsrRateDataV2 响应里取“全部”评价数。

    典型结构：``{"data":{"model":{"commonTagNodeList":[
    {"name":"全部","count":2497}, ...]}}}``。
    """

    def walk(obj, depth: int = 0):
        if depth > 8:
            return None
        if isinstance(obj, dict):
            tags = obj.get("commonTagNodeList") or obj.get("common_tag_node_list")
            if isinstance(tags, list):
                for tag in tags:
                    if not isinstance(tag, dict):
                        continue
                    name = clean_text(tag.get("name")) or ""
                    if name in {"全部", "总评价", "累计评价", "所有评价"}:
                        count = tag.get("count") or tag.get("value")
                        if count not in (None, ""):
                            return clean_text(count)
            for value in obj.values():
                found = walk(value, depth + 1)
                if found is not None:
                    return found
        elif isinstance(obj, list):
            for value in obj[:200]:
                found = walk(value, depth + 1)
                if found is not None:
                    return found
        return None

    for payload in payloads:
        found = walk(payload)
        if found is not None:
            return found
    return None


def _member_id_from_card(card) -> str | None:
    """从卡片自身/子节点的 data-* 或埋点属性里找 1688 member_id。"""
    if card is None:
        return None
    for node in card.iter():
        for key, value in node.attrib.items():
            text = f"{key}={value}"
            match = _ALI_MEMBER_ATTR_RE.search(text)
            if match:
                return match.group(1) or match.group(2)
            shop_id = extract_shop_id("1688", value)
            if shop_id:
                return shop_id
    return None


def _title_from_text(text: str | None) -> str | None:
    value = normalize_space(text) or ""
    if not value:
        return None
    value = _PRICE_TEXT_RE.sub(" ", value)
    value = _PRICE_RANGE_RE.sub(" ", value)
    value = re.sub(r"^(?:广告|实力商家|诚信通\S*)\s*", "", value)
    value = re.sub(r"\s*(?:已售|成交|销量)\s*[\d,.]+.*$", "", value)
    value = re.sub(r"\s*[\d,.]+\s*[万万千百]?\+?\s*件.*$", "", value)
    value = normalize_space(value)
    return value[:200] if value and len(value) >= 2 else None


_SHOP_NAME_HINT_RE = re.compile(
    r"([\u4e00-\u9fa5A-Za-z0-9()（）·&]{2,40}(?:有限公司|公司|工厂|厂|商行|贸易|旗舰店|专营店|专卖店|店))"
)
_SHOP_TYPE_MARKERS = (
    ("超级工厂", "超级工厂"),
    ("实力商家", "实力商家"),
    ("诚信通", "诚信通"),
    ("源头工厂", "源头工厂"),
    ("深度验厂", "深度验厂"),
    ("1688严选", "1688严选"),
)


_PERCENT_SHOP_RE = re.compile(r"%[0-9A-Fa-f]{2}")
_CJK_NAME_RE = re.compile(r"^[\u3400-\u9fff]")
# 原始字段有时在百分号串前带一个 ASCII 十六进制字符（图标字形/截取残留），
# 例如 ``f%E7%81%AB...``。只在“该字符后紧跟 %XX 且解码结果以中文开头”时
# 判定为前缀并剔除；正常英文店名（如 ``forescoco旗舰店``）不受影响。
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


def decode_shop_name(value: str | None) -> str | None:
    """1688 卡片/接口返回的店名可能是 UTF-8 百分号编码，入库前解码。

    同时清理原始字段偶尔带出的单字符 ASCII 前缀（如 ``f%E7%81%AB...``
    解码后为 ``f火星人...``）；该前缀不是店名，且只在解码结果以中文
    开头时剔除，避免误伤 ``forescoco旗舰店`` 这类正常英文开头店名。
    """
    text = normalize_space(value)
    if not text:
        return None
    prefix = _leading_hex_prefix(text)
    # 兼容单层/双层百分号编码；最多解两层，避免异常输入无限循环。
    for _ in range(2):
        if not _PERCENT_SHOP_RE.search(text):
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
        if remainder and _CJK_NAME_RE.match(remainder[0]):
            text = remainder
    return normalize_space(text)


def _shop_name_from_card(card, title: str | None = None) -> str | None:
    """从列表卡片取公司名/店铺名（真实 1688 卡片 class 不稳定）。

    先读明确字段/属性，再从 winport/member/shop 链接文本和卡片文本里的
    公司后缀兜底；标题文本不能当店铺名。
    """
    if card is None:
        return None
    name_attrs = (
        "data-company-name",
        "data-companyname",
        "data-shop-name",
        "data-shopname",
        "data-supplier-name",
        "data-member-name",
        "data-nick",
    )
    for node in card.iter():
        for attr in name_attrs:
            value = normalize_space(node.get(attr))
            if value:
                return decode_shop_name(value)
    node = first_node(card, ALI1688["shop"])
    if node is not None:
        value = normalize_space(node.text_content())
        if value:
            return decode_shop_name(value)
    for selector in (
        "a[href*='winport']",
        "a[href*='memberId']",
        "a[href*='member_id']",
        "a[href*='company']",
        "a[href*='shop'][href*='1688.com']",
    ):
        try:
            anchors = card.cssselect(selector)
        except Exception:
            anchors = []
        for anchor in anchors:
            value = normalize_space(anchor.text_content()) or normalize_space(
                anchor.get("title") or anchor.get("aria-label")
            )
            if value and value != title:
                return decode_shop_name(value)
    text = normalize_space(card.text_content()) or ""
    if title:
        text = text.replace(title, " ")
    match = _SHOP_NAME_HINT_RE.search(text)
    return decode_shop_name(match.group(1)) if match else None


def _shop_labels_from_card(card) -> list[str]:
    labels = all_texts(card, ALI1688["label"])
    text = normalize_space(card.text_content()) or ""
    for marker, label in _SHOP_TYPE_MARKERS:
        if marker in text:
            labels.append(label)
    # 诚信通 N 年：优先保留年份，便于后端判店铺类型。
    for match in re.finditer(r"诚信通\s*(\d+)\s*年", text):
        labels.append(f"诚信通{match.group(1)}年")
    for attr in ("data-strength", "data-is-strength", "data-super-factory"):
        value = normalize_space(card.get(attr))
        if not value:
            continue
        lowered = value.lower()
        if lowered in {"1", "true", "yes", "y", "是"} or "实力" in value:
            labels.append("实力商家")
        if "超级工厂" in value or "super" in lowered:
            labels.append("超级工厂")
    return list(dict.fromkeys(label for label in labels if label))


def _shop_type_hint(labels: list[str]) -> str:
    joined = " ".join(labels)
    if "超级工厂" in joined:
        return "1688超级工厂"
    if "实力" in joined:
        return "1688实力商家"
    if "诚信通" in joined:
        return "1688诚信通"
    return "1688店铺"


class Ali1688Platform(PlatformBase):
    name = "1688"
    login_url = "https://login.1688.com/member/signin.htm"
    login_check_url = "https://work.1688.com/"
    login_cookie_names = ("__cn_logon__", "__cn_logon_id__", "lid", "ali_apache_id")
    referer = "https://s.1688.com/"
    # 不使用宽泛的 "1688.com"：否则页面 JS/CSS 会占满 ResponseRecorder 的
    # 40 条配额，真正的列表 XHR 反而采不到。JSON 响应仍会按 content-type 捕获。
    search_capture_patterns = (
        "offer_search",
        "offerList",
        "searchOffer",
        "h5api",
        "mtop",
        "s.1688.com/selloffer",
        "search.1688.com",
        "async=true",
    )
    detail_capture_patterns = (
        "h5api",
        "mtop",
        "detail.1688.com",
        "rateservice",
        "querydsrratedatav2",
        "queryItemRatedList",
        "comment",
        "review",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 每次运行（每个平台实例）的店名补抓计数，跨关键词累计，受配置硬上限约束。
        self._shopname_backfills = 0
        self._shopname_attempted: set[str] = set()

    def login_cookie_ok(self) -> bool | None:
        """CDP 模式 1688 必须拿到 ``__cn_logon__=true``，仅有 Cookie 名不算。"""
        if self.config.browser_mode(self.name) != "cdp":
            return None
        values = read_cookie_values(self.browser)
        if values is None:
            return False
        return ali1688_logged_in(values)

    def build_search_url(self, keyword: str, page: int) -> str:
        return (
            "https://s.1688.com/selloffer/offer_search.htm"
            f"?keywords={gbk_quote(keyword)}&beginPage={page}&sortType=booked"
        )

    def detail_url(self, product_id: str) -> str:
        return f"https://detail.1688.com/offer/{product_id}.html"

    def shop_urls(self, shop_id: str | None) -> list[str]:
        """1688 店铺页候选 URL；memberId 与 shopXXXX 两种子域都试。"""
        value = normalize_space(shop_id)
        if not value:
            return []
        urls = [f"https://{value}.1688.com/"]
        if not value.lower().startswith("shop"):
            urls.append(f"https://shop{value}.1688.com/")
        return list(dict.fromkeys(urls))

    def parse_shop_name(self, result: PageResult) -> str | None:
        """从 1688 店铺首页取公司名/店铺名（只取店名，不解析商品）。"""
        payloads = self.payloads(result)
        value = deep_find(payloads, _ALI_SHOP_KEYS)
        if isinstance(value, dict):
            value = pick_text(
                value, ("text", "value", "name", "title", "companyName", "shopName")
            )
        else:
            value = clean_text(value)
        if value:
            return decode_shop_name(value)
        root = parse_html(result.html)
        if root is None:
            return None
        value = first_text(root, ALI1688_SHOP["name"])
        if value:
            # 店铺页 <title> 常见“公司名 - 阿里巴巴”；只取分隔符前的店名。
            value = re.split(r"\s+[-_|–—]\s+", value)[0]
            if value and "1688" not in value:
                return decode_shop_name(value)
        value = first_attr(root, ALI1688_SHOP["meta"], ("content",))
        if value:
            value = re.split(r"\s+[-_|–—]\s+", value)[0]
            if value and "1688" not in value:
                return decode_shop_name(value)
        return None

    def page_action(self, page) -> None:
        if page is None:
            return
        # 1688 列表懒加载；轻滚动两三轮，不深翻，降低风控概率。
        ali = getattr(self.config.crawl, "ali1688", None)
        steps = int(
            getattr(ali, "scroll_steps", None)
            or getattr(self.config.crawl, "ali1688_scroll_steps", 3)
            or 3
        )
        wait_ms = int(
            getattr(ali, "scroll_wait_ms", None)
            or getattr(self.config.crawl, "ali1688_scroll_wait_ms", 600)
            or 600
        )
        try:
            for _ in range(max(1, steps)):
                page.evaluate("window.scrollBy(0, Math.max(window.innerHeight, 800))")
                page.wait_for_timeout(wait_ms)
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(wait_ms)
        except Exception:
            pass

    # ---------- 搜索 ----------
    def parse_search(
        self, result: PageResult, keyword: str, page: int, ip_code: str | None
    ) -> list[Item]:
        missing_before = self.stats.drop_reasons.get("product_id解析失败", 0)
        payloads = self.payloads(result)
        items = self._parse_dom(result, keyword, page, ip_code)
        records = self.records(payloads, list_keys=_ALI_LIST_KEYS, id_keys=_ALI_ID_KEYS)
        for index, record in enumerate(records, start=1):
            item = self._from_json(
                flatten_record(record), keyword, page, index, ip_code
            )
            if item is not None:
                items.append(item)
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

    def _labels(self, record: dict) -> list[str]:
        labels = text_list(
            pick(
                record,
                (
                    "flags",
                    "tags",
                    "badges",
                    "serviceTags",
                    "service_tags",
                    "labels",
                    "icon",
                    "sellingPoints",
                ),
            )
        )
        if _truthy(pick(record, ("isStrength", "is_strength", "strength"))):
            labels.append("实力商家")
        if _truthy(
            pick(
                record,
                ("isSuperFactory", "is_super_factory", "superFactory", "super_factory"),
            )
        ):
            labels.append("超级工厂")
        years = pick(
            record,
            ("tpYear", "tp_years", "trustPassYears", "trust_pass_years", "memberYears"),
        )
        if years not in (None, ""):
            text = clean_text(years)
            if text and text.isdigit():
                labels.append(f"诚信通{int(text)}年")
            elif text:
                labels.append(f"诚信通{text}")
        elif _truthy(
            pick(
                record,
                ("isGoldSupplier", "is_gold_supplier", "trustPass", "trust_pass"),
            )
        ):
            labels.append("诚信通")
        return list(dict.fromkeys(labels))

    def _from_json(
        self, record: dict, keyword: str, page: int, rank: int, ip_code: str | None
    ) -> Item | None:
        title = pick_text(record, _ALI_TITLE_KEYS)
        url_raw = pick_text(record, _ALI_URL_KEYS)
        if not title and not (
            self.complete_fields
            and (url_raw or pick(record, (*_ALI_SHOP_KEYS, *_ALI_IMAGE_KEYS)))
        ):
            return None
        raw_id = pick(record, _ALI_ID_KEYS)
        product_id = _ali_product_id(raw_id) or _ali_product_id(url_raw)
        if not product_id:
            self.stats.drop("product_id解析失败", str(raw_id or url_raw or "1688-json"))
            return None
        shop_url = pick_text(
            record,
            (
                "shopUrl",
                "shop_url",
                "companyUrl",
                "company_url",
                "sellerUrl",
                "seller_url",
                "winportUrl",
                "winport_url",
                "shopLink",
                "shop_link",
            ),
        )
        shop_id = clean_text(pick(record, _ALI_SHOP_ID_KEYS))
        if not shop_id:
            shop_id = extract_shop_id("1688", shop_url or url_raw)
        labels = self._labels(record)
        price = pick_price(record, _ALI_PRICE_KEYS)
        price_raw = pick_text(record, _ALI_PRICE_KEYS)
        if price_raw is None and price is not None:
            price_raw = str(price)
        sales_raw = pick_text(record, _ALI_SALES_KEYS)
        comments_raw = pick_text(record, _ALI_COMMENT_KEYS)
        item = Item(
            platform="1688",
            product_id=product_id,
            url=normalize_product_url("1688", product_id, url_raw or shop_url),
            title=title or "",
            shop_id=shop_id,
            shop_name=decode_shop_name(pick_text(record, _ALI_SHOP_KEYS)),
            shop_label_raw=join_labels(labels),
            shop_type_raw_hint=_shop_type_hint(labels),
            price=price,
            price_raw=price_raw,
            sales_raw=sales_raw,
            sales=pick_int(record, _ALI_SALES_KEYS),
            comments_raw=comments_raw,
            comments=pick_int(record, _ALI_COMMENT_KEYS),
            main_image_url=normalize_image_url(pick(record, _ALI_IMAGE_KEYS)),
            category=pick_text(record, ("category", "catName", "categoryName", "cat")),
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
        if root is None:
            return []
        cards = [
            card
            for card in card_nodes_all(root, ALI1688["card"])
            if self._looks_like_card(card)
        ]
        cards.extend(self._anchor_cards(root))
        cards = self._dedupe_cards(cards)
        items: list[Item] = []
        for index, card in enumerate(cards, start=1):
            item = self._item_from_card(card, keyword, page, index, ip_code)
            if item is not None:
                items.append(item)
        return items

    def _looks_like_card(self, card) -> bool:
        """过滤 ``#sm-offer-list > div`` 可能带出来的标题/价格字段 div。"""
        attrs = " ".join(filter(None, [card.get("class"), card.get("id")])).lower()
        if any(
            marker in attrs
            for marker in (
                "offer-card",
                "offer-list",
                "sm-offer",
                "search-offer",
                "card-container",
            )
        ):
            return True
        if attr_from_node(card, ("data-offer-id", "data-offerid", "data-id")):
            return True
        return bool(href_of(card, ALI1688["link"]))

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
        """从 offer 链接反推最近卡片（新旧 DOM 都作为补充策略）。"""
        cards = []
        seen: set[str] = set()
        for selector in ("a[href*='offer/']", "a[href*='offerIds']"):
            try:
                anchors = root.cssselect(selector)
            except Exception:
                anchors = []
            for anchor in anchors:
                href = anchor.get("href") or ""
                if not _ali_product_id(href):
                    continue
                card = ancestor_card(anchor)
                key = node_key(card)
                if key in seen:
                    continue
                seen.add(key)
                cards.append(card)
        return cards

    def _offer_id(self, card) -> str | None:
        # 显式 offer 属性 > 卡片/子链接 href > 通用 data-id，避免广告位
        # 的 data-id 覆盖真实 offer ID。
        value = attr_from_node(card, ("data-offer-id", "data-offerid", "data-offerId"))
        product_id = _ali_product_id(value)
        if product_id:
            return product_id
        nodes = [card]
        try:
            nodes.extend(card.iter("a"))
        except Exception:
            pass
        for node in nodes:
            href = node.get("href")
            product_id = _ali_product_id(href)
            if product_id:
                return product_id
            value = attr_from_node(
                node, ("data-offer-id", "data-offerid", "data-offerId")
            )
            product_id = _ali_product_id(value)
            if product_id:
                return product_id
        value = attr_from_node(card, ("data-id", "data-itemid", "data-item-id"))
        product_id = _ali_product_id(value)
        if product_id:
            return product_id
        text = card.text_content() or ""
        match = re.search(r"offerIds?[\"'\s:=]+(\d{5,})", text, re.I)
        return match.group(1) if match else None

    def _item_from_card(
        self, card, keyword: str, page: int, index: int, ip_code: str | None
    ) -> Item | None:
        link = href_of(card, ALI1688["link"])
        if not link and card.tag == "a":
            link = card.get("href")
        if not link:
            link = href_of(card, ("a[href*='offer/']", "a[href*='offerIds']"))
        if link:
            link = urljoin("https://s.1688.com/", link)
        product_id = self._offer_id(card) or _ali_product_id(link)
        card_text = normalize_space(card.text_content()) or ""
        if not product_id:
            if first_text(card, ALI1688["title"]) or _price_from_text(card_text):
                self.stats.drop("product_id解析失败", "1688-dom")
            return None
        title = first_text(card, ALI1688["title"])
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
        if title and ("¥" in title or "￥" in title):
            title = _title_from_text(title) or title
        price_text = first_text(card, ALI1688["price"])
        price_raw = (
            price_text
            if parse_price(price_text) is not None
            else _price_from_text(price_text or card_text)
        )
        if not title and not price_raw and not self.complete_fields:
            return None
        shop_link = href_of(card, ALI1688["shop_link"])
        if shop_link:
            shop_link = urljoin("https://s.1688.com/", shop_link)
        shop_id = (
            extract_shop_id("1688", shop_link)
            or clean_text(
                attr_from_node(
                    card,
                    (
                        "data-member-id",
                        "data-memberid",
                        "data-shopid",
                        "data-shop-id",
                        "data-companyid",
                    ),
                )
            )
            or _member_id_from_card(card)
        )
        card_labels = _shop_labels_from_card(card)
        labels_text = join_labels(card_labels)
        sales_text = first_text(card, ALI1688["sales"])
        sales_raw = (
            sales_text
            if parse_fuzzy_int(sales_text) is not None
            else _sales_from_text(sales_text or card_text)
        )
        comments_raw = first_text(card, ALI1688["comments"]) or _comments_from_text(
            card_text
        )
        image = first_attr(
            card,
            ALI1688["image"],
            (
                "data-src",
                "data-ks-lazyload",
                "data-lazy-img",
                "data-original",
                "srcset",
                "src",
            ),
        )
        if not image:
            image = first_attr(
                card,
                ("img",),
                (
                    "data-src",
                    "data-ks-lazyload",
                    "data-lazy-img",
                    "data-original",
                    "srcset",
                    "src",
                ),
            )
        shop_name = _shop_name_from_card(card, title)
        item = Item(
            platform="1688",
            product_id=product_id,
            url=normalize_product_url("1688", product_id, link),
            title=title or "",
            shop_id=shop_id,
            shop_name=shop_name,
            shop_label_raw=labels_text,
            shop_type_raw_hint=_shop_type_hint(card_labels),
            price=parse_price(price_raw),
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

    def after_search(self, keyword: str, items: list[Item]) -> None:
        ratio = keyword_hit_ratio(keyword, [item.title for item in items])
        threshold = self.config.crawl.keyword_hit_warn_ratio
        if items and ratio < threshold:
            self.log_warning(
                f"1688 关键词命中率偏低 keyword={keyword!r} ratio={ratio:.2f} < {threshold:.2f}; "
                "检查 GBK 编码/关键词是否被兜底"
            )
        self._backfill_missing_shop_names(keyword, items)

    # ---------- 店名补抓 ----------
    def _backfill_missing_shop_names(self, keyword: str, items: list[Item]) -> None:
        """命中关键词且店名为空的候选，补抓详情页/店铺页店名。

        遍历全部缺店名候选，复用详情节流间隔，不额外并发、不绕过登录态。
        """
        settings = getattr(self.config.crawl, "ali1688", None)
        backfill = getattr(settings, "shop_name_backfill", None)
        if backfill is None or not getattr(backfill, "enabled", False):
            return
        needle = (keyword or "").strip().lower()
        if not needle:
            return
        source = str(getattr(backfill, "source", "auto") or "auto").lower()
        requested = success = failed = 0
        candidates = [
            (index, item)
            for index, item in enumerate(items)
            if item.listed
            and not clean_text(item.shop_name)
            and (self.complete_fields or needle in (item.title or "").lower())
        ]
        progress = self.search_progress
        attempted = (
            progress.state.setdefault("shop_name_attempted", []) if progress else []
        )
        from collections import Counter

        counts = Counter(attempted)
        candidates.sort(
            key=lambda pair: counts[f"{pair[1].platform}:{pair[1].product_id}"]
        )
        for _, item in candidates:
            self.detail_failures.setdefault(
                f"{item.platform}:{item.product_id}", "1688店名补采尚未执行"
            )
        for index, item in candidates:
            key = f"{item.platform}:{item.product_id}"
            if key in self._shopname_attempted:
                continue
            self._shopname_attempted.add(key)
            attempted.append(key)
            if progress:
                progress.save()
            self._shopname_backfills += 1
            requested += 1
            try:
                name = self._fetch_shop_name(item, source=source)
            except (RiskBlocked, LoginRequired, DailyLimitExceeded) as exc:
                self.detail_failures[key] = f"1688店名补采停止：{exc}"
                raise
            if name:
                items[index] = enrich_flags(item.model_copy(update={"shop_name": name}))
                # 将店名作为补丁持久保存，后续同一商品的旧列表行不能覆盖它。
                if self.checkpoint_patch is not None:
                    self.checkpoint_patch(
                        ItemPatch(
                            platform=item.platform,
                            product_id=item.product_id,
                            shop_name=name,
                        )
                    )
                self.detail_failures.pop(key, None)
                if self.checkpoint_items is not None:
                    self.checkpoint_items(items)
                success += 1
                self.logger.info(
                    "1688 店名补抓成功 product_id=%s shop_name=%s",
                    item.product_id,
                    name,
                )
            else:
                self.detail_failures[key] = (
                    "1688店名补采按现有来源及重试规则处理后仍未取得店名"
                )
                failed += 1
        if requested:
            self.log_warning(
                f"1688 店名补抓：候选缺名 {requested} 个，成功 {success}，失败 {failed}，"
                f"（keyword={keyword!r}）"
            )

    def _fetch_shop_name(self, item: Item, *, source: str = "auto") -> str | None:
        """按配置来源抓一个店名；详情页优先，失败再店铺页。"""
        sources: list[tuple[str, str]] = []
        if (
            source in ("auto", "detail")
            and item.product_id not in self.detail_attempted
        ):
            sources.append(("detail", self.detail_url(item.product_id)))
        if source in ("auto", "shop"):
            sources.extend(("shop", url) for url in self.shop_urls(item.shop_id))
        for kind, url in sources:
            self.throttle.wait_detail_interval()
            try:
                result = self._request(
                    url,
                    action=None,
                    capture_patterns=self.detail_capture_patterns,
                    extra_headers={"Referer": self.referer},
                )
            except DailyLimitExceeded as exc:
                self.log_warning(f"1688 店名补抓停止：{exc}")
                raise
            except (RiskBlocked, LoginRequired):
                raise
            except (RetryableError, RetryExhausted) as exc:
                self.stats.error(f"1688 店名补抓请求失败 url={url}: {exc}")
                continue
            except Exception as exc:  # noqa: BLE001 - 补抓失败不能影响已采列表
                self.stats.error(
                    f"1688 店名补抓异常 url={url}: {type(exc).__name__}: {exc}"
                )
                continue
            self.stats.page_fetched()
            try:
                blocked = self._classify_result(result)
            except Exception as exc:  # noqa: BLE001 - 单页分类失败不阻断后续
                self.stats.error(f"1688 店名补抓页面分类失败 url={url}: {exc}")
                continue
            if blocked.is_risk or blocked.is_login:
                self.log_warning(
                    f"1688 店名补抓停止 url={url}: {blocked.reason}（列表数据保留，不重试）"
                )
                if blocked.is_risk:
                    raise RiskBlocked(
                        self.name,
                        blocked.reason,
                        screenshot_path=self._capture_screenshot("shop_name_risk"),
                    )
                self.last_login_reason = blocked.reason
                raise LoginRequired(self.name, blocked.reason)
            name: str | None = None
            try:
                if kind == "detail":
                    patch = self.parse_detail(result, item.product_id)
                    name = patch.shop_name if patch is not None else None
                else:
                    name = self.parse_shop_name(result)
            except Exception as exc:  # noqa: BLE001 - 解析失败尝试下一来源
                self.stats.error(
                    f"1688 店名补抓解析失败 url={url}: {type(exc).__name__}: {exc}"
                )
            name = decode_shop_name(name)
            if name:
                return name
        return None

    # ---------- 详情 ----------
    def parse_detail(self, result: PageResult, product_id: str) -> ItemPatch | None:
        payloads = self.payloads(result)
        comments_raw = clean_text(deep_find(payloads, _ALI_COMMENT_KEYS))
        if not comments_raw:
            comments_raw = _dsr_comment_count(payloads)
        sales_raw = clean_text(
            deep_find(
                payloads,
                ("saleQuantity", "soldQuantity", "bookedCount", "sold", "sales"),
            )
        )
        price_raw = detail_price_raw(payloads, ("price", "priceRange", "unitPrice"))
        title = clean_text(deep_find(payloads, ("subject", "title", "offerTitle")))
        shop_name = decode_shop_name(
            clean_text(deep_find(payloads, ("companyName", "shopName", "sellerName")))
        )
        shop_id = clean_text(deep_find(payloads, _ALI_SHOP_ID_KEYS))
        image = normalize_image_url(
            deep_find(payloads, ("picUrl", "imageUrl", "mainImage"))
        )

        root = parse_html(result.html)
        if root is not None:
            title = title or first_text(root, ALI1688_DETAIL["title"])
            price_raw = price_raw or first_text(root, ALI1688_DETAIL["price"])
            shop_name = shop_name or decode_shop_name(
                first_text(root, ALI1688_DETAIL["shop"])
            )
            comments_raw = comments_raw or first_text(root, ALI1688_DETAIL["comments"])
            sales_raw = sales_raw or first_text(root, ALI1688_DETAIL["sales"])
            image = image or normalize_image_url(
                first_attr(root, ALI1688_DETAIL["image"], ("src", "data-src"))
            )
            if not shop_id:
                shop_link = href_of(root, ALI1688_DETAIL["shop_link"])
                shop_id = extract_shop_id("1688", shop_link)
            if not shop_id:
                shop_id = extract_shop_id("1688", result.url)
            if not shop_id:
                shop_id = _member_id_from_card(root)
        content = result.html or ""
        listed = not any(marker in content for marker in ALI1688_DETAIL["offline"])
        return ItemPatch(
            platform="1688",
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
