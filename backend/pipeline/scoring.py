"""判定、金额与视觉指纹。

本模块只依赖快照/商品/店铺，不负责 Excel；所有阈值来自 rules.yaml。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.config import Settings
from pipeline.models import Product, ProductSnapshot, Shop, VisionCache
from pipeline.models import Whitelist
from pipeline.shopkey import (
    is_name_sentinel,
    normalize_shop_name,
    platform_group,
    shop_name_missing,
)
from pipeline.store import dumps_json, loads_json, normalize_shop_id, unique_list

try:  # pragma: no cover
    import imagehash
    from PIL import Image
except Exception:  # noqa: BLE001
    imagehash = None  # type: ignore[assignment]
    Image = None  # type: ignore[assignment]


MONEY = Decimal("0.01")

# 旧评论缺失标记仅用于兼容清理；销量不可用时按评论数直接估算。
COMMENTS_MISSING_FLAG = "评论缺失,待核验"
LEGACY_COMMENT_FLAGS = frozenset({"评论缺失", "待核验", "评论数缺失-未校正"})


@dataclass
class TextResult:
    score: float
    reasons: list[str] = field(default_factory=list)
    target_hit: bool = False
    genuine_hit: bool = False
    need_vision: bool = False


@dataclass
class AmountResult:
    amount: Decimal | None
    formula: str
    estimated_sales: int | None
    flags: list[str] = field(default_factory=list)


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _contains(haystack: str, needle: str) -> bool:
    return bool(needle) and needle.lower() in haystack.lower()


def _first_hit(haystack: str, words: list[str]) -> str | None:
    for word in words:
        if _contains(haystack, str(word)):
            return str(word)
    return None


def text_score(
    *,
    title: str,
    shop_name: str | None,
    shop_label: str | None,
    ip_code: str | None,
    settings: Settings,
) -> TextResult:
    cfg = settings.section("text_score")
    weights = cfg.get("weights") or {}
    words = cfg.get("words") or {}
    haystack = " ".join(part for part in (title, shop_name, shop_label) if part)
    reasons: list[str] = []
    score = 0.0

    target_names = settings.ip_names(ip_code)
    variants = settings.ip_variants(ip_code)
    other_names = settings.all_other_ip_names(ip_code)

    target_word = _first_hit(haystack, target_names)
    variant_word = _first_hit(haystack, variants)
    other_word = _first_hit(haystack, other_names)

    if target_word:
        score += float(weights.get("ip_hit", 45))
        reasons.append(f"ip_hit:{target_word}:{weights.get('ip_hit', 45)}")
    if variant_word:
        score += float(weights.get("variant_hit", 12))
        reasons.append(f"variant_hit:{variant_word}:{weights.get('variant_hit', 12)}")

    carrier = _first_hit(haystack, [str(item) for item in words.get("carrier", [])])
    if carrier:
        score += float(weights.get("carrier_hit", 16))
        reasons.append(f"carrier_hit:{carrier}:{weights.get('carrier_hit', 16)}")

    category_word = _first_hit(
        haystack, [str(item) for item in words.get("category", [])]
    )
    if category_word:
        score += float(weights.get("category_hit", 10))
        reasons.append(
            f"category_hit:{category_word}:{weights.get('category_hit', 10)}"
        )

    white_label = _first_hit(
        haystack, [str(item) for item in words.get("white_label", [])]
    )
    if white_label:
        score += float(weights.get("white_label_shop", 6))
        reasons.append(
            f"white_label_shop:{white_label}:{weights.get('white_label_shop', 6)}"
        )

    brand_word = _first_hit(
        haystack, [str(item) for item in words.get("brand_official", [])]
    )
    if brand_word:
        score += float(weights.get("brand_official", -25))
        reasons.append(
            f"brand_official:{brand_word}:{weights.get('brand_official', -25)}"
        )

    genuine_words = [
        str(item) for item in settings.get("exclude.genuine_keywords", []) or []
    ]
    genuine_word = _first_hit(haystack, genuine_words)
    genuine_hit = genuine_word is not None
    if genuine_word:
        score += float(weights.get("genuine_signal", -55))
        reasons.append(
            f"genuine_signal:{genuine_word}:{weights.get('genuine_signal', -55)}"
        )

    if target_word or variant_word:
        if other_word:
            score += float(weights.get("other_ip_also", -18))
            reasons.append(
                f"other_ip_also:{other_word}:{weights.get('other_ip_also', -18)}"
            )
    elif other_word:
        score += float(weights.get("other_ip_only", -30))
        reasons.append(
            f"other_ip_only:{other_word}:{weights.get('other_ip_only', -30)}"
        )
    else:
        score += float(weights.get("no_ip", -8))
        reasons.append(f"no_ip:-:{weights.get('no_ip', -8)}")

    short_len = int(cfg.get("short_title_len", 8))
    if len(title.strip()) < short_len:
        score += float(weights.get("short_title", -20))
        reasons.append(
            f"short_title:{len(title.strip())}:{weights.get('short_title', -20)}"
        )

    for hint in settings.get("legal_hints") or []:
        if not isinstance(hint, dict):
            continue
        token = _first_hit(haystack, [str(item) for item in hint.get("keywords", [])])
        if token:
            reasons.append(f"legal_hint:{token}:{hint.get('hint', '请人工判断')}")

    score = _clamp(score)
    rescue_min = float(cfg.get("vision_rescue_min", 12))
    need_vision = (not target_word and not genuine_hit) and score >= rescue_min
    return TextResult(
        score=round(score, 2),
        reasons=reasons,
        target_hit=bool(target_word),
        genuine_hit=genuine_hit,
        need_vision=need_vision,
    )


def is_whitelisted(
    session: Session,
    *,
    platform: str,
    shop_name: str | None,
    shop_id: str | None,
    ip_code: str | None,
) -> bool:
    """白名单键：平台组 + 规范化店名 + IP。

    双方都有 shop_id 且不同时不命中；白名单行没有 shop_id（name: 哨兵）时
    只按店名命中。没有店名的商品不参与白名单排除。
    """
    if not ip_code or shop_name_missing(shop_name):
        return False
    group = platform_group(platform)
    norm = normalize_shop_name(shop_name)
    rows = session.scalars(
        select(Whitelist).where(
            Whitelist.ip_code == ip_code,
            Whitelist.platform.in_(
                [
                    name
                    for name, gp in {
                        "taobao": "taobao",
                        "tmall": "taobao",
                        "jd": "jd",
                        "1688": "1688",
                    }.items()
                    if gp == group
                ]
                or [group]
            ),
        )
    ).all()
    for row in rows:
        if platform_group(row.platform) != group:
            continue
        row_norm = row.shop_name_norm or normalize_shop_name(row.shop_name)
        if not row_norm:
            # 旧 CSV 只有 shop_id：保留按 ID 兼容。
            if shop_id and row.shop_id == shop_id:
                return True
            continue
        if row_norm != norm:
            continue
        if (
            shop_id
            and row.shop_id
            and not is_name_sentinel(row.shop_id)
            and row.shop_id != shop_id
        ):
            continue
        return True
    return False


def hard_exclusions(
    session: Session,
    *,
    settings: Settings,
    platform: str,
    shop_name: str | None,
    product: Product,
    shop_id: str | None = None,
    shop_label: str | None,
    ip_code: str | None,
    title: str,
) -> list[str]:
    """5.1 硬规则；返回命中原因，非空即不进入线索。"""

    reasons: list[str] = []
    if is_whitelisted(
        session,
        platform=platform,
        shop_name=shop_name,
        shop_id=shop_id if shop_id is not None else product.shop_id,
        ip_code=ip_code,
    ):
        reasons.append("排除:白名单:平台组+店名+IP")

    haystack = " ".join(part for part in (title, shop_name or "", shop_label) if part)
    for word in settings.get("exclude.genuine_keywords", []) or []:
        if _contains(haystack, str(word)):
            reasons.append(f"排除:正版信号:{word}")
            break
    for label in settings.get("exclude.genuine_shop_labels", []) or []:
        if shop_label and _contains(shop_label, str(label)):
            reasons.append(f"排除:正版店铺标签:{label}")
            break
    categories = settings.get("exclude.categories", []) or []
    product_category = product.category or ""
    if product_category and any(
        product_category == str(item) or str(item) in product_category
        for item in categories
    ):
        reasons.append(f"排除:排除品类:{product_category}")
    return reasons


def combine_scores(text: float, visual: float | None, settings: Settings) -> float:
    cfg = settings.section("combined")
    if visual is None:
        return round(_clamp(text), 2)
    final = (
        float(cfg.get("w_text", 0.45)) * text
        + float(cfg.get("w_visual", 0.55)) * visual
    )
    if visual >= float(settings.get("visual.confirm", 70)):
        final = max(final, float(cfg.get("floor_on_confirm", 75)))
    if visual <= float(settings.get("visual.reject", 25)):
        final = min(final, float(cfg.get("cap_on_reject", 35)))
    return round(_clamp(final), 2)


def _reference_paths(settings: Settings, ip_code: str | None) -> list[Path]:
    entry = settings.ip_config(ip_code)
    if not entry:
        return []
    paths: list[Path] = []
    for raw in entry.get("reference_images") or []:
        if not raw:
            continue
        candidate = Path(str(raw)).expanduser()
        options = (
            [candidate]
            if candidate.is_absolute()
            else [
                (settings.root / candidate).resolve(),
                (settings.path.parent / candidate).resolve(),
                (settings.crawler_dir / candidate).resolve(),
            ]
        )
        paths.extend(path for path in options if path.is_file())
    return paths


def _compute_phash(image_path: Path) -> str | None:
    if imagehash is None or Image is None:
        return None
    try:
        with Image.open(image_path) as image:
            return str(imagehash.phash(image))
    except Exception:  # noqa: BLE001
        return None


def visual_score(
    session: Session,
    *,
    settings: Settings,
    image_phash: str | None,
    image_path: str | None,
    ip_code: str | None,
) -> tuple[float | None, list[str]]:
    if not ip_code:
        return None, []
    if not image_phash and image_path:
        candidate = Path(image_path)
        if not candidate.is_absolute():
            candidate = (settings.root / candidate).resolve()
        if candidate.is_file():
            image_phash = _compute_phash(candidate)
    if not image_phash:
        return None, []

    cached = session.scalar(
        select(VisionCache).where(
            VisionCache.image_phash == image_phash, VisionCache.ip_code == ip_code
        )
    )
    if cached is not None:
        reason = cached.reason or "视觉缓存"
        return cached.visual_score, [f"visual_cache:{reason}:{cached.visual_score}"]

    refs = _reference_paths(settings, ip_code)
    if not refs:
        return None, ["visual:无参考图:待人工"]
    target = Path(image_path) if image_path else None
    if target is not None and not target.is_absolute():
        target = (settings.root / target).resolve()
    if target is None or not target.is_file():
        return None, ["visual:主图缺失:待人工"]

    target_hash = imagehash.hex_to_hash(image_phash) if imagehash else None
    best_distance: int | None = None
    for ref in refs:
        ref_hash_text = _compute_phash(ref)
        if not ref_hash_text or target_hash is None:
            continue
        distance = target_hash - imagehash.hex_to_hash(ref_hash_text)
        best_distance = (
            distance if best_distance is None else min(best_distance, distance)
        )
    if best_distance is None:
        return None, ["visual:参考图不可读:待人工"]
    score = round(_clamp(100 - best_distance * 3), 2)
    reason = f"pHash距离{best_distance}"
    session.add(
        VisionCache(
            image_phash=image_phash,
            ip_code=ip_code,
            visual_score=score,
            reason=reason,
            config_version=settings.config_version,
        )
    )
    session.flush()
    return score, [f"visual:{reason}:{score}"]


def compute_amount(
    *,
    price: Decimal | None,
    sales: int | None,
    comments: int | None,
    sales_raw: str | None,
    comments_raw: str | None,
    existing_flags: list[str],
    settings: Settings,
) -> AmountResult:
    from utils.commerce_numbers import count_field, price_value

    flags = [
        str(flag)
        for flag in existing_flags
        if str(flag)
        not in LEGACY_COMMENT_FLAGS
        | {
            COMMENTS_MISSING_FLAG,
            "销量缺失",
            "价格缺失",
            "待人工补齐",
            "销量缺失或无法识别",
            "评论缺失或无法识别",
            "价格缺失或非法",
            "销量原文无法确定,待核验",
            "评论原文无法确定,待核验",
            "销量字段含评论信息,待核验",
            "评论字段含销量信息,待核验",
            "金额待核验",
            "金额超出范围,待核验",
            "销量和评论均缺失或无法识别",
            "按评论数估算",
            "按下限估算",
        }
        and not (str(flag) == "价格无法识别,待核验" and price is not None)
    ]
    sales, sales_issue = count_field(sales, sales_raw, field="sales")
    comments, comments_issue = count_field(comments, comments_raw, field="comments")
    price = price_value(price)
    estimated = sales if sales is not None else comments
    by_comments = sales is None and comments is not None
    issues = []
    if price is None:
        issues.append("价格缺失或非法")
        flags.append("价格缺失")
    if estimated is None:
        issues.extend(issue for issue in (sales_issue, comments_issue) if issue)
        issues.append("销量和评论均缺失或无法识别")
        flags.append("销量缺失")
    if issues:
        return AmountResult(
            None,
            "待核验：" + "；".join(issues),
            None,
            unique_list([*flags, *issues, "金额待核验"]),
        )
    assert price is not None and estimated is not None
    flags = [flag for flag in flags if flag != "金额待核验"]
    quantity_raw = comments_raw if by_comments else sales_raw
    lower_bound = any(token in (quantity_raw or "") for token in ("+", "＋", "以上"))
    if by_comments:
        flags.append("按评论数估算")
    if lower_bound:
        flags.append("按下限估算")
    unrounded = price * estimated
    if unrounded > Decimal("999999999999.99"):
        return AmountResult(
            None,
            "待核验：金额超出存储范围",
            None,
            unique_list([*flags, "金额超出范围,待核验"]),
        )
    amount = unrounded.quantize(MONEY, rounding=ROUND_HALF_UP)
    expression = f"{price:.2f}×{estimated}"
    if by_comments:
        expression += "（按评论数估算）"
    if lower_bound:
        expression += "（按下限估算）"
    return AmountResult(
        amount, f"{expression}={amount:.2f}", estimated, unique_list(flags)
    )


def score_snapshot(
    session: Session,
    *,
    settings: Settings,
    snapshot: ProductSnapshot,
    product: Product,
    shop: Shop | None,
) -> dict[str, Any]:
    ip_code = snapshot.ip_code
    shop_name = product.shop_name or (shop.shop_name if shop else None)
    shop_label = product.shop_label_raw or (shop.shop_label_raw if shop else None)
    text = text_score(
        title=product.title,
        shop_name=shop_name,
        shop_label=shop_label,
        ip_code=ip_code,
        settings=settings,
    )
    visual, visual_reasons = visual_score(
        session,
        settings=settings,
        image_phash=product.image_phash,
        image_path=product.image_path,
        ip_code=ip_code,
    )
    final = combine_scores(text.score, visual, settings)

    existing_flags = loads_json(snapshot.missing_flags, [])
    amount = compute_amount(
        price=snapshot.price,
        sales=snapshot.sales,
        comments=snapshot.comments,
        sales_raw=snapshot.sales_raw,
        comments_raw=snapshot.comments_raw,
        existing_flags=existing_flags,
        settings=settings,
    )
    exclusions = hard_exclusions(
        session,
        settings=settings,
        platform=product.platform,
        shop_name=shop_name,
        product=product,
        shop_label=shop_label,
        ip_code=ip_code,
        title=product.title,
    )
    reasons = [*text.reasons, *visual_reasons, *exclusions]
    if text.need_vision:
        reasons.append("need_vision:无IP词待视觉:0")

    min_final = float(settings.get("combined.min_final_score", 35))
    eligible = (not exclusions) and (final >= min_final or text.need_vision)
    if not eligible and not exclusions:
        reasons.append(f"gate:综合分{final:.2f}<{min_final:g}:0")

    snapshot.text_score = text.score
    snapshot.visual_score = visual
    snapshot.final_score = final
    snapshot.need_vision = text.need_vision
    snapshot.eligible = eligible
    snapshot.exclusion_reason = "；".join(exclusions) if exclusions else None
    snapshot.hit_reasons = dumps_json(unique_list(reasons))
    snapshot.amount = amount.amount
    snapshot.formula = amount.formula
    snapshot.estimated_sales = amount.estimated_sales
    flags = [flag for flag in amount.flags if flag != "店铺名缺失"]
    if shop_name_missing(shop_name):
        # 店名才参与聚合；无店名只进链接明细，不再标记/阻断 shop_id 缺失。
        flags.append("店铺名缺失")
    snapshot.missing_flags = dumps_json(unique_list(flags))
    snapshot.config_version = settings.config_version
    snapshot.caliber_version = settings.caliber_version
    session.flush()
    return {
        "eligible": eligible,
        "excluded": bool(exclusions),
        "need_vision": text.need_vision,
        "text_score": text.score,
        "visual_score": visual,
        "final_score": final,
        "amount": amount.amount,
        "formula": amount.formula,
        "reasons": reasons,
    }


def add_reason(snapshot: ProductSnapshot, reason: str) -> None:
    reasons = loads_json(snapshot.hit_reasons, [])
    reasons.append(reason)
    snapshot.hit_reasons = dumps_json(unique_list(reasons))


__all__ = [
    "AmountResult",
    "COMMENTS_MISSING_FLAG",
    "LEGACY_COMMENT_FLAGS",
    "TextResult",
    "add_reason",
    "combine_scores",
    "compute_amount",
    "hard_exclusions",
    "is_whitelisted",
    "score_snapshot",
    "text_score",
    "visual_score",
]
