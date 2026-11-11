"""淘宝候选列表的轻量文本打分（仅用于详情页候选筛选）。

判定模块的完整打分在 backend；采集层只需要一个稳定、可解释的“是否值得
打开详情页”门槛。权重和词表沿用 ``text_score`` 配置，避免另造一套口径。
"""

from __future__ import annotations

from typing import Any, Mapping

from crawler.config import AppConfig


def _text(value: Any) -> str:
    return str(value or "").strip().lower()


def _hit(text: str, words) -> bool:
    return any((word or "").strip().lower() in text for word in words or [] if str(word or "").strip())


def _ip_match(config: AppConfig, text: str, ip_code: str | None = None) -> tuple[bool, bool]:
    """返回 (命中本 IP, 命中其他 IP)。"""
    own = False
    other = False
    for ip in config.ips:
        names = [ip.name, *ip.aliases, *ip.variants]
        if not _hit(text, names):
            continue
        if ip_code and ip.code == ip_code:
            own = True
        else:
            other = True
    # 临时关键词/人工 ID 的 ip_code 可能是 "manual" 或未知 code；
    # 此时只要标题命中任一 IP 就按“本 IP”计，不能把明确命中当成其他 IP 扣分。
    known_code = bool(ip_code) and any(ip.code == ip_code for ip in config.ips)
    if not known_code:
        own = own or other
        other = False
    return own, other


def score_listing(
    config: AppConfig,
    *,
    title: str | None,
    shop_name: str | None = None,
    shop_label: str | None = None,
    ip_code: str | None = None,
) -> int:
    """按 ``text_score`` 配置给一条列表候选打分，返回整数。"""
    raw = getattr(config, "text_score", None) or {}
    weights = raw.get("weights", {}) if isinstance(raw, dict) else {}
    words = raw.get("words", {}) if isinstance(raw, dict) else {}
    short_len = int(raw.get("short_title_len", 8)) if isinstance(raw, dict) else 8
    title_text = _text(title)
    shop_text = " ".join(filter(None, [_text(shop_name), _text(shop_label)]))
    score = 0

    own_ip, other_ip = _ip_match(config, title_text, ip_code)
    if own_ip:
        score += int(weights.get("ip_hit", 0))
    if _hit(title_text, words.get("variant", [])):
        score += int(weights.get("variant_hit", 0))
    if _hit(title_text, words.get("carrier", [])):
        score += int(weights.get("carrier_hit", 0))
    if _hit(title_text, words.get("category", [])):
        score += int(weights.get("category_hit", 0))
    if _hit(shop_text, words.get("white_label", [])):
        score += int(weights.get("white_label_shop", 0))
    if _hit(" ".join(filter(None, [title_text, shop_text])), words.get("brand_official", [])):
        score += int(weights.get("brand_official", 0))
    genuine_keywords = [
        *(getattr(config, "exclude", None) or {}).get("genuine_keywords", []),
        *(getattr(config, "exclude", None) or {}).get("genuine_shop_labels", []),
    ]
    if _hit(" ".join(filter(None, [title_text, shop_text])), genuine_keywords):
        score += int(weights.get("genuine_signal", 0))
    if other_ip and own_ip:
        score += int(weights.get("other_ip_also", 0))
    elif other_ip:
        score += int(weights.get("other_ip_only", 0))
    if not (own_ip or other_ip):
        score += int(weights.get("no_ip", 0))
    if title_text and len(title_text) < short_len:
        score += int(weights.get("short_title", 0))
    return score


def score_item_row(config: AppConfig, row: Mapping[str, Any]) -> int:
    """对 JSONL 列表行打分（详情候选筛选用）。"""
    return score_listing(
        config,
        title=row.get("title"),
        shop_name=row.get("shop_name"),
        shop_label=row.get("shop_label_raw"),
        ip_code=row.get("ip_code"),
    )


__all__ = ["score_listing", "score_item_row"]
