"""三平台选择器集中处。改版时优先只改这个文件。

选择器为 2026-11 常见页面结构（旧版 + 新版类名）的保守集合；
真实站点会 A/B / 改版，需按实际 DOM 调整。
"""

_SALES_CONTROL = (
    "a:text-is('销量'), button:text-is('销量'), [role='tab']:text-is('销量'), "
    "[class*='sort' i] :text-is('销量'), [class*='filter' i] :text-is('销量'), "
    "[class*='sort' i]:text-is('销量'), [class*='sort' i] :text-is('销量优先'), "
    "[class*='sort' i] :text-is('销量从高到低')"
)

SALES_SORT = {
    "taobao": {
        "control": "#mainsrp-sortbar a:has-text('销量'), " + _SALES_CONTROL,
        "cards": "#mainsrp-itemlist .item, #content_items_wrapper [data-nid], "
        "#content_items_wrapper [class*='doubleCardWrapper']",
    },
    "jd": {
        "control": "#J_filter .f-sort a:has-text('销量'), " + _SALES_CONTROL,
        "cards": "#J_goodsList li, [data-sku]",
    },
    "1688": {
        "control": "[data-sort-type='booked'], " + _SALES_CONTROL,
        "cards": ".offer-list-row, a.search-offer-item, a[class*='offerCard--'], [data-offer-id], #sm-offer-list > div",
    },
}

# 淘宝/京东新版控件于 2026-11-10、1688 于 2026-11-11 在真实页面核对。其余候选只有
# 在当前页面可见时才使用；缺少控件不等于末页，不拼接下一页 URL。
SEARCH_PAGINATION = {
    "root": ".next-pagination, [class*='pagination_center_'], .p-num, "
    "#mainsrp-pager, .fui-paging, .sm-pagination, .pagination, [class*='pagination--'], [role='navigation'][aria-label*='分页']",
    "next": ".next-pagination .next-next, [class*='pagination_next_'], "
    ".p-num .pn-next, #mainsrp-pager .next, .fui-paging-next, .sm-pagination-next, "
    ".pagination .next, a[rel='next'], button[aria-label^='下一页'], "
    "[role='button'][aria-label^='下一页'], .pagination :text-is('下一页'), "
    "[class*='pagination--'] [class*='nextArrow--']",
    "current": ".next-pagination .next-current, .next-pagination-display em, "
    "[class*='pagination_item_'][class*='_active_'], .p-num .curr, "
    "#mainsrp-pager .active, .fui-paging .fui-current, .pagination [aria-current='page'], "
    "[class*='pagination--'] [class*='pageCurrent--']",
    "more": "button:text-is('加载更多'), a:text-is('加载更多'), [role='button']:text-is('加载更多')",
    "end": ".no-more, [class*='noMore'], [class*='no-more'], .list-end",
    "empty": ".no-result, .no-results, [class*='noResult'], [class*='no-result'], .search-empty",
}

TAOBAO = {
    # 2026-11 新版 PC 搜索（s.taobao.com）是 React/ICE 渲染的卡片：
    # 卡片根节点带 data-nid / data-item-id 或 Card--doubleCardWrapper 类名；
    # 旧版 #mainsrp-itemlist .item 继续兼容。不要再把 #content_items_wrapper
    # 的所有子 div 当卡片——空壳页里那是 loadingBox，会把“无数据”误记成
    # “product_id 缺失”。
    "card": [
        "#mainsrp-itemlist .item",
        "#content_items_wrapper > div[data-nid]",
        "#content_items_wrapper > div[data-item-id]",
        "#content_items_wrapper > div[class*='Card--']",
        "#content_items_wrapper > div[class*='doubleCard']",
        "#content_items_wrapper > div[class*='cardWrapper']",
        ".items .item",
        "div[data-category='auctions'] .item",
        "[data-nid]",
        "[data-item-id]",
        "[class*='doubleCardWrapper']",
        "[class*='Card--doubleCardWrapper']",
    ],
    "title": [
        ".row.row-2.title a",
        ".title a",
        "[class*='Title--title']",
        "[class*='titleText']",
        "[class*='descWrapper'] span",
        "a[href*='item.htm'] span",
    ],
    "price": [
        ".price strong",
        "[class*='Price--priceInt']",
        "[class*='Price--priceText']",
        "[class*='priceInt']",
        "[class*='innerPriceWrapper']",
        "[class*='price']",
        ".price",
    ],
    "shop": [
        ".shop a",
        ".shopName",
        "[class*='ShopInfo--shopName']",
        "[class*='shopNameText']",
        "[class*='ShopInfo--'] a",
        "[class*='shopName']",
    ],
    "sales": [
        ".deal-cnt",
        "[class*='Sales--realSales']",
        "[class*='realSales']",
        "[class*='sales']",
        "[class*='Sales']",
    ],
    "image": [
        ".pic img",
        "img[class*='mainPic']",
        "[class*='MainPic'] img",
        "[class*='mainPicAdaptWrapper'] img",
        "img",
    ],
    "link": [
        "a[href*='item.taobao.com']",
        "a[href*='detail.tmall.com']",
        "a[href*='item.htm']",
        "a[href*='//item.taobao.com']",
        "a",
    ],
    "shop_link": [
        "a[href*='shop']",
        ".shop a",
        "a[href*='sellerId=']",
        "a[href*='seller_id=']",
    ],
    "label": [
        ".icon-tmall",
        "[class*='tmall']",
        "[class*='Icon--']",
        "[class*='icon--']",
        "[class*='icon']",
        ".icons",
    ],
}

TAOBAO_DETAIL = {
    "title": ["#J_Title .tb-main-title", ".tb-main-title", ".tb-detail-hd h1", "h1"],
    "price": [
        "#J_StrPrice .tb-rmb-num",
        ".tm-price",
        ".tb-rmb-num",
        "[class*='price']",
    ],
    "shop": ["#J_ShopInfo .tb-shop-name", ".slogo-shopname", "[class*='shopName']"],
    "shop_link": [
        "#J_ShopInfo a[href*='shop']",
        "a[href*='shop'][href*='taobao.com']",
        "a[href*='sellerId=']",
        "a[href*='seller_id=']",
        "a[href*='shop']",
    ],
    "comments": [
        "#J_RateCounter",
        "#J_CommentCount",
        ".tb-rate-count",
        ".tb-rate-counter",
        "[class*='Comment--'] [class*='count']",
        "[class*='Comment--count']",
        "[class*='comment--count']",
        "[class*='CommentCount']",
        "[class*='commentCount']",
        "[class*='comment-count']",
        "[class*='Comment'] [class*='count']",
        "[class*='comment'] [class*='count']",
        "[class*='Rate'] .tm-count",
        "[class*='Comment'] .tm-count",
    ],
    "sales": ["#J_SellCounter", ".tm-count", "[class*='sales']", "[class*='Sell']"],
    "image": ["#J_ImgBooth", ".tb-booth img", "img"],
    "offline": ["该商品已下架", "宝贝不存在", "item not found", "您查看的宝贝不存在"],
}

JD = {
    # 2026-09 京东搜索页改版：卡片是 hash class 的 [data-sku] 元素，
    # 没有 <a href>，跳转靠 JS；旧版 gl-item 兼容保留。
    "card": [
        "#J_goodsList li.gl-item",
        "#J_goodsList > ul > li",
        "li[data-sku]",
        "[data-sku]",
        ".gl-item",
        "[class*='goodsItem']",
        "[class*='goods-item']",
        "[class*='product-item']",
        "[class*='search-item']",
        "[class*='sku-item']",
    ],
    "title": [
        ".p-name em",
        ".p-name a em",
        ".p-name a",
        "[class*='p-name']",
        "[class*='sku-title' i]",
        "[class*='skuName' i]",
        "[class*='goods-name' i]",
        "[class*='goodsName' i]",
        "[class*='item-name' i]",
        "[class*='itemTitle' i]",
        "[class*='productName' i]",
        "[class*='wareName' i]",
        "[class*='title' i]",
        "a[title]",
        "h3",
    ],
    "price": [
        ".p-price i",
        ".p-price strong i",
        "[data-price]",
        ".p-price",
        "[class*='price' i]",
        "strong i",
        "i",
    ],
    "shop": [
        ".p-shop a",
        ".p-shop span a",
        ".p-shop",
        "[class*='shop' i] a",
        "[class*='shop' i]",
        "[class*='store' i]",
        "[class*='vender' i]",
        "[class*='seller' i]",
    ],
    "shop_link": [
        ".p-shop a[href*='mall.jd.com']",
        ".p-shop a",
        "a[href*='mall.jd.com']",
        "a[href*='shop.jd.com']",
    ],
    "comments": [
        ".p-commit strong a",
        ".p-commit a",
        "[class*='commit' i] a",
        "[class*='comment' i]",
        "[class*='evaluate' i]",
    ],
    "image": [".p-img img", "img[data-lazy-img]", "img[data-src]", "img"],
    "link": [".p-name a", "a[href*='item.jd.com']", "a[href*='/item/']", "a"],
    "label": [
        ".p-icons i",
        ".p-icons",
        "[class*='icons' i] i",
        "[class*='tag' i]",
        "[class*='label' i]",
    ],
}

JD_DETAIL = {
    "title": [
        ".sku-name",
        ".itemInfo-wrap .sku-name",
        "div.product-intro h1",
        "[itemprop='name']",
    ],
    "price": [
        ".p-price .price",
        ".summary-price .p-price",
        ".price",
        "[class*='price']",
    ],
    "shop": [
        ".J-hove-wrap .name a",
        ".shopName",
        ".J-shop-name",
        "[class*='shopName']",
    ],
    "shop_link": [".J-hove-wrap a[href*='mall.jd.com']", "a[href*='mall.jd.com']"],
    "comments": [
        "#comment-count",
        ".comment-count",
        "[data-comment-count]",
        ".product-price-panel--options-comment-count",
        ".comment-title",
        ".J-comm-pt",
        "[class*='comment-count']",
        "[class*='commentCount']",
        "[class*='CommentCount']",
        "[class*='comment'] [class*='count']",
        "[class*='comment']",
    ],
    "sales": ["#sales-volume", ".sales-volume", "[class*='sales']"],
    "image": ["#spec-img", ".jqzoom img", "img"],
    "offline": ["该商品已下架", "商品已下架", "很抱歉，该商品已下架", "item not found"],
}

ALI1688 = {
    # 2026-09 搜索页实测：主卡片是 a.search-offer-item（offerId 在 href
    # 或内部 offerIds 参数）；旧版 #sm-offer-list > div / mojar-* 结构兼容。
    "card": [
        ".offer-list-row",
        ".sm-offer-item",
        "a.search-offer-item",
        "a[class*='offerCard--']",
        "#sm-offer-list > div",
        "[data-offer-id]",
        "[data-offerid]",
        ".card-container",
        ".space-offer-card-box",
        ".J_offerList .offer",
        "[class*='offer-card' i]",
        "[class*='offerItem' i]",
    ],
    "title": [
        ".title",
        ".offer-title",
        "[class*='title' i] a",
        "[class*='title' i]",
        "[class*='offer-title' i]",
        "[class*='offerTitle' i]",
        "[class*='subject' i]",
        "a[href*='offer/']",
        "a",
        "img[alt]",
    ],
    "price": [
        ".price",
        ".price-item",
        "[class*='price' i]",
        ".offer-price",
        "[class*='showPrice' i]",
        "div.price",
    ],
    "shop": [
        ".company-name",
        ".shop-name",
        "[class*='company' i]",
        "[class*='shopName' i]",
        "[class*='seller' i]",
        "[class*='supplier' i]",
    ],
    "shop_link": [
        "a[href*='shop'][href*='1688.com']",
        "a[href*='memberId=']",
        "a[href*='member_id=']",
        "a[href*='company']",
        "a[href*='winport']",
        "[class*='shopRow--'] a",
    ],
    "sales": [
        ".sale-quantity",
        ".sold",
        "[class*='sale' i]",
        "[class*='sold' i]",
        "[class*='trade' i]",
    ],
    "comments": [
        ".comment-count",
        "[data-comment-count]",
        "[class*='commentCount']",
        "[class*='CommentCount']",
        "[class*='comment-count']",
        "[class*='rate-count']",
        "[class*='comment' i]",
        "[class*='rate' i]",
        "[class*='review' i]",
    ],
    "image": [".offer-img img", ".img img", "img[class*='offer' i]", "img"],
    "link": ["a[href*='offer/']", "a[href*='offerIds']", "a"],
    "label": [".tags", "[class*='tag' i]", "[class*='badge' i]", "[class*='label' i]"],
}

# 1688 店铺首页（winport）的店名节点；详情页兜底失败时才请求。
ALI1688_SHOP = {
    "name": [
        ".company-name",
        ".shop-name",
        "[class*='companyName']",
        "[class*='company-name']",
        "[class*='shopName']",
        "[class*='shop-name']",
        "[class*='company'] h1",
        "[class*='shop'] h1",
        "h1",
        "title",
    ],
    "meta": [
        "meta[property='og:title']",
        "meta[name='title']",
        "meta[name='description']",
    ],
}

ALI1688_DETAIL = {
    "title": [".title-text", ".d-title", "h1", ".offer-title"],
    "price": [".price-text", ".price", "[class*='price']"],
    "shop": [".company-name", ".shop-name", "[class*='company']"],
    "shop_link": [
        "a[href*='shop'][href*='1688.com']",
        "a[href*='memberId=']",
        "a[href*='member_id=']",
        "a[href*='winport']",
    ],
    "comments": [
        ".comment-count",
        "[data-comment-count]",
        "[class*='commentCount']",
        "[class*='CommentCount']",
        "[class*='comment-count']",
        "[class*='rate-count']",
        "[class*='comment']",
        "[class*='rate']",
    ],
    "sales": [".sale-quantity", ".sold", "[class*='sale']", "[class*='Sold']"],
    "image": [".main-image img", ".detail-gallery img", "img"],
    "offline": [
        "该商品已下架",
        "商品已下架",
        "offer not found",
        "您查看的 offer 不存在",
    ],
}
