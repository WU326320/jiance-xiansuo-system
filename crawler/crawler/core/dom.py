"""lxml DOM 解析小工具。所有选择器集中在 platforms/selectors.py。"""

from __future__ import annotations

from typing import Iterable

from lxml import html as lxml_html


def parse_html(content: str | bytes | None):
    if not content:
        return None
    try:
        # 直接传 str，lxml 才不会在没有 <meta charset> 时按 latin-1 解码中文。
        return lxml_html.fromstring(content)
    except Exception:
        return None


def normalize_space(value: object | None) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\u3000", " ").replace("\xa0", " ")
    text = " ".join(text.split())
    return text or None


def _select(node, selector: str):
    try:
        return node.cssselect(selector)
    except Exception:
        return []


def first_node(node, selectors: Iterable[str]):
    if node is None:
        return None
    for selector in selectors:
        found = _select(node, selector)
        if found:
            return found[0]
    return None


def first_text(node, selectors: Iterable[str]) -> str | None:
    target = first_node(node, selectors)
    if target is None:
        return None
    return normalize_space(target.text_content())


def all_texts(node, selectors: Iterable[str]) -> list[str]:
    if node is None:
        return []
    for selector in selectors:
        found = _select(node, selector)
        if found:
            values = [normalize_space(item.text_content()) for item in found]
            return list(dict.fromkeys(value for value in values if value))
    return []


def first_attr(node, selectors: Iterable[str], attrs: Iterable[str]) -> str | None:
    target = first_node(node, selectors)
    if target is None:
        return None
    for attr in attrs:
        value = target.get(attr)
        if value:
            return normalize_space(value)
    return None


def card_nodes(root, selectors: Iterable[str]) -> list:
    """返回第一个有匹配的选择器的节点集合，避免多个选择器重复计数。"""
    if root is None:
        return []
    for selector in selectors:
        found = _select(root, selector)
        if found:
            return found
    return []


def node_key(node) -> str:
    """稳定节点键：lxml 代理对象不能用 id() 去重，用文档路径。"""
    try:
        return node.getroottree().getpath(node)
    except Exception:
        return str(id(node))


def in_document_order(nodes: list) -> list:
    """将不同选择器/链接兜底合并的节点恢复为原文档顺序。"""
    if not nodes:
        return []
    root = nodes[0].getroottree().getroot()
    positions = {node_key(node): index for index, node in enumerate(root.iter())}
    return sorted(nodes, key=lambda node: positions.get(node_key(node), len(positions)))


def card_nodes_all(root, selectors: Iterable[str]) -> list:
    """合并所有命中选择器的节点，按页面顺序去重。

    真实页面会同时存在新旧结构（``li.gl-item``、``[data-sku]``、
    ``a.search-offer-item``），逐个选择器取第一组会漏商品；这里保留
    所有命中，由平台按商品 ID 再做一次去重。
    """
    if root is None:
        return []
    seen: set[str] = set()
    nodes: list = []
    for selector in selectors:
        for node in _select(root, selector):
            key = node_key(node)
            if key in seen:
                continue
            seen.add(key)
            nodes.append(node)
    return in_document_order(nodes)


def ancestor_card(node, *, max_depth: int = 6, price_hint: bool = True):
    """从商品链接向上找最近的可疑卡片容器（无稳定 class 时兜底）。"""
    current = node
    fallback = node
    for _ in range(max(1, max_depth)):
        parent = current.getparent()
        if parent is None:
            break
        current = parent
        fallback = current
        if parent.tag in {"body", "html"}:
            break
        attrs = " ".join(
            filter(
                None, [parent.get("class"), parent.get("id"), parent.get("data-sku")]
            )
        ).lower()
        text = normalize_space(parent.text_content()) or ""
        if parent.get("data-sku") and any(
            ch.isdigit() for ch in str(parent.get("data-sku"))
        ):
            return parent
        if any(
            marker in attrs
            for marker in (
                "gl-item",
                "search-offer",
                "offer-item",
                "product-card",
                "goods-item",
            )
        ):
            return parent
        if price_hint and ("¥" in text or "￥" in text) and len(text) > 12:
            return parent
    return fallback


def attr_from_node(node, attrs: Iterable[str]) -> str | None:
    if node is None:
        return None
    for attr in attrs:
        value = node.get(attr)
        if value:
            return normalize_space(value)
    return None


def href_of(node, selectors: Iterable[str] | None = None) -> str | None:
    target = first_node(node, selectors) if selectors else node
    if target is None:
        return None
    return normalize_space(target.get("href"))


def has_text(node, needles: Iterable[str]) -> bool:
    if node is None:
        return False
    content = normalize_space(node.text_content()) or ""
    return any(needle and needle in content for needle in needles)
