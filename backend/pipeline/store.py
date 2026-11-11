"""采集结果入库：products / shops / product_snapshots。

采集模块既可以直接 import :func:`upsert_crawl_item` 写这两张表，也可以沿用
现有 JSONL 输出，由 :mod:`pipeline.ingest` 适配。两种方式最终都经过这里，
保证“商品唯一键 + 每任务每商品每 IP 一条判定快照”。
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from html import unescape
from pathlib import Path
from typing import Any, Iterable

from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.config import Settings
from pipeline.models import Product, ProductSnapshot, Shop, now_local
from pipeline.shopkey import decode_shop_name, shop_name_missing

try:  # pragma: no cover - 依赖缺失时给出清晰错误
    import imagehash
except Exception:  # noqa: BLE001
    imagehash = None  # type: ignore[assignment]


def dumps_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def loads_json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


_INVALID_SHOP_IDS = {"", "0", "null", "none", "nil", "undefined", "-", "--"}


def normalize_shop_id(value: Any) -> str | None:
    """统一店铺 ID：``"0"``/空串/占位符一律视为缺失。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in _INVALID_SHOP_IDS:
        return None
    if text.isdigit() and int(text) == 0:
        return None
    return text


def unique_list(values: Iterable[str]) -> list[str]:
    return list(
        dict.fromkeys(str(value) for value in values if value not in (None, ""))
    )


def split_ip_codes(value: Any) -> list[str]:
    """把一个 run 内的 IP 集合字段（逗号分隔）拆成列表。"""
    if value is None:
        return []
    return [part.strip() for part in str(value).split(",") if part.strip()]


def parse_datetime(value: Any, default: datetime | None = None) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str) and value.strip():
        try:
            result = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            result = default or now_local()
    else:
        result = default or now_local()
    if result.tzinfo is not None:
        # 采集端时间戳已经是本地墙钟时间（+08:00）；去掉 tzinfo 时保留墙钟，
        # 避免在非 +08 时区的测试机/服务器上把日期算错。
        result = result.replace(tzinfo=None)
    return result.replace(microsecond=0)


def parse_decimal(value: Any) -> Decimal | None:
    from utils.commerce_numbers import price_value

    return price_value(value)


def parse_int(value: Any) -> int | None:
    from utils.commerce_numbers import count_value

    return count_value(value)


def normalize_shop_type(
    platform: str,
    hint: str | None,
    label: str | None,
    settings: Settings,
) -> str:
    mapping = settings.get("shop_type_mapping", {}) or {}
    for token in (hint, label):
        if token and token in mapping:
            return str(mapping[token])
    text = f"{hint or ''} {label or ''}"
    for raw, normalized in mapping.items():
        if raw and raw in text:
            return str(normalized)
    if "个体" in text:
        return "个体户"
    if "个人" in text:
        return "个人"
    if any(word in text for word in ("企业", "公司", "旗舰店", "天猫", "实力商家")):
        return "企业"
    return "未知"


def _resolve_local_image(raw_path: str | None, settings: Settings) -> Path | None:
    if not raw_path:
        return None
    candidate = Path(raw_path).expanduser()
    candidates: list[Path] = []
    if candidate.is_absolute():
        candidates.append(candidate)
    else:
        candidates.extend(
            [
                (settings.root / candidate).resolve(),
                (settings.crawler_dir / candidate).resolve(),
                (Path.cwd() / candidate).resolve(),
            ]
        )
    for path in candidates:
        if path.is_file():
            return path
    return None


def _phash(path: Path) -> tuple[str | None, str | None]:
    if imagehash is None:
        return None, None
    try:
        with Image.open(path) as image:
            return str(imagehash.phash(image)), None
    except Exception as exc:  # noqa: BLE001 - 坏图/缺解码器不应中断整批
        return None, f"指纹计算失败:{type(exc).__name__}"


def store_image(
    raw_path: str | None, settings: Settings
) -> tuple[str | None, str | None, str | None]:
    """复制一份主图到 data/images/<phash前2位>/<phash>.jpg。

    返回 ``(image_path, image_phash, warning)``；image_path 为后端根目录相对路径，
    便于整个目录搬到 Xuan 的 Windows 机器后仍然可用。
    """

    source = _resolve_local_image(raw_path, settings)
    if source is None:
        return None, None, None
    phash, warning = _phash(source)
    if not phash:
        return None, None, warning
    destination = settings.images_dir / phash[:2] / f"{phash}.jpg"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        try:
            with Image.open(source) as image:
                image.convert("RGB").save(destination, format="JPEG", quality=92)
        except Exception as exc:  # noqa: BLE001
            return None, phash, f"主图保存失败:{type(exc).__name__}"
    try:
        relative = destination.relative_to(settings.root).as_posix()
    except ValueError:  # pragma: no cover - 自定义 images_dir 在根目录外时
        relative = str(destination)
    return relative, phash, warning


def upsert_shop(
    session: Session,
    *,
    platform: str,
    shop_id: str | None,
    shop_name: str | None,
    shop_type: str,
    shop_label_raw: str | None,
) -> Shop | None:
    if not shop_id:
        return None
    shop = session.scalar(
        select(Shop).where(Shop.platform == platform, Shop.shop_id == shop_id)
    )
    if shop is None:
        shop = Shop(
            platform=platform,
            shop_id=shop_id,
            shop_name=shop_name,
            shop_type=shop_type,
            shop_label_raw=shop_label_raw,
        )
        session.add(shop)
        session.flush()
        return shop
    shop.shop_name = shop_name or shop.shop_name
    shop.shop_type = shop_type or shop.shop_type
    shop.shop_label_raw = shop_label_raw or shop.shop_label_raw
    shop.updated_at = now_local()
    return shop


def upsert_crawl_item(
    session: Session,
    *,
    platform: str,
    data: dict[str, Any],
    settings: Settings,
    run_id: str,
    snapshot_date: date,
) -> ProductSnapshot | None:
    """把采集模块的一条商品写进 products + product_snapshots。

    返回快照；商品关键字段非法时返回 None（调用方计入丢弃数）。
    """

    product_id = str(data.get("product_id") or "").strip()
    url = str(data.get("url") or "").strip()
    title = re.sub(
        r"\s+",
        " ",
        unescape(re.sub(r"</?[a-zA-Z][^>]*>", "", str(data.get("title") or ""))),
    ).strip()
    if not product_id or not url:
        return None

    # 淘宝通道的 JSONL 可能同时含天猫商品（platform=tmall），不能再用通道名
    # 覆盖商品自身平台，否则 leads 编号和白名单匹配都会丢失 tmall。
    data_platform = str(data.get("platform") or "").strip()
    if platform in ("taobao", "tmall") and data_platform in ("taobao", "tmall"):
        effective_platform = data_platform
    else:
        effective_platform = platform

    crawled_at = parse_datetime(data.get("crawled_at"))
    shop_id = normalize_shop_id(data.get("shop_id"))
    shop_name = (
        str(data["shop_name"]).strip()
        if data.get("shop_name") not in (None, "")
        else None
    )
    if effective_platform == "1688":
        shop_name = decode_shop_name(shop_name)
    shop_label = (
        str(data["shop_label_raw"]).strip()
        if data.get("shop_label_raw") not in (None, "")
        else None
    )
    hint = (
        str(data["shop_type_raw_hint"]).strip()
        if data.get("shop_type_raw_hint") not in (None, "")
        else None
    )
    shop_type = normalize_shop_type(effective_platform, hint, shop_label, settings)
    upsert_shop(
        session,
        platform=effective_platform,
        shop_id=shop_id,
        shop_name=shop_name,
        shop_type=shop_type,
        shop_label_raw=shop_label,
    )

    product = session.scalar(
        select(Product).where(
            Product.platform == effective_platform,
            Product.product_id == product_id,
        )
    )
    image_path, image_phash, image_warning = store_image(
        data.get("image_path") or data.get("local_image_path"), settings
    )
    if product is None:
        product = Product(
            platform=effective_platform,
            product_id=product_id,
            url=url,
            title=title,
            shop_id=shop_id,
            shop_name=shop_name,
            shop_type=shop_type,
            shop_label_raw=shop_label,
            shop_id_missing=shop_id is None,
            main_image_url=data.get("main_image_url"),
            image_path=image_path,
            image_phash=image_phash,
            category=data.get("category"),
            first_seen_at=crawled_at,
            last_seen_at=crawled_at,
        )
        session.add(product)
        session.flush()
    else:
        product.url = url or product.url
        product.title = title or product.title
        product.shop_id = shop_id
        product.shop_name = shop_name or product.shop_name
        product.shop_type = shop_type or product.shop_type
        product.shop_label_raw = shop_label or product.shop_label_raw
        product.shop_id_missing = shop_id is None
        product.main_image_url = data.get("main_image_url") or product.main_image_url
        product.image_path = image_path or product.image_path
        product.image_phash = image_phash or product.image_phash
        product.category = data.get("category") or product.category
        product.last_seen_at = crawled_at

    ip_code = str(data.get("ip_code") or "").strip()
    snapshot = session.scalar(
        select(ProductSnapshot).where(
            ProductSnapshot.product_id == product.id,
            ProductSnapshot.snapshot_date == snapshot_date,
            ProductSnapshot.run_id == run_id,
            ProductSnapshot.ip_code == ip_code,
        )
    )
    raw_flags = data.get("flags") or []
    if isinstance(raw_flags, str):
        raw_flags = [raw_flags]
    flags = unique_list(raw_flags)
    if not str(product.title or "").strip():
        flags.append("商品标题缺失")
    if image_warning:
        flags.append(image_warning)
    if shop_name_missing(shop_name):
        flags.append("店铺名缺失")
    from utils.commerce_numbers import count_field

    sales, sales_issue = count_field(
        data.get("sales"), data.get("sales_raw"), field="sales"
    )
    comments, comments_issue = count_field(
        data.get("comments"), data.get("comments_raw"), field="comments"
    )
    flags.extend(issue for issue in (sales_issue, comments_issue) if issue)
    raw_price = data.get("price_raw")
    price = parse_decimal(
        raw_price if raw_price not in (None, "") else data.get("price")
    )
    if price is None:
        flags.append("价格缺失或无法识别,待核验")
    # 每个 IP 独立打分和匹配白名单；同商品金额去重在聚合层完成。
    values = dict(
        product_id=product.id,
        snapshot_date=snapshot_date,
        crawled_at=crawled_at,
        run_id=run_id,
        ip_code=ip_code,
        search_keyword=data.get("search_keyword"),
        price=price,
        price_raw=raw_price,
        sales_raw=data.get("sales_raw"),
        sales=sales,
        comments_raw=data.get("comments_raw"),
        comments=comments,
        listed=bool(data.get("listed", True)),
        need_vision=False,
        eligible=True,
        exclusion_reason=None,
        hit_reasons="[]",
        amount=None,
        formula="",
        estimated_sales=None,
        missing_flags=dumps_json(unique_list(flags)),
        config_version=settings.config_version,
        caliber_version=settings.caliber_version,
    )
    if snapshot is None:
        snapshot = ProductSnapshot(**values)
        session.add(snapshot)
    else:
        for key, value in values.items():
            setattr(snapshot, key, value)
        snapshot.text_score = None
        snapshot.visual_score = None
        snapshot.final_score = None
    session.flush()
    return snapshot


def upsert_many(
    session: Session,
    *,
    platform: str,
    items: Iterable[dict[str, Any]],
    settings: Settings,
    run_id: str,
    snapshot_date: date,
) -> tuple[list[ProductSnapshot], int]:
    snapshots: list[ProductSnapshot] = []
    dropped = 0
    for item in items:
        snapshot = upsert_crawl_item(
            session,
            platform=platform,
            data=item,
            settings=settings,
            run_id=run_id,
            snapshot_date=snapshot_date,
        )
        if snapshot is None:
            dropped += 1
        else:
            snapshots.append(snapshot)
    session.flush()
    return snapshots, dropped


__all__ = [
    "dumps_json",
    "split_ip_codes",
    "loads_json",
    "normalize_shop_id",
    "normalize_shop_type",
    "parse_datetime",
    "parse_decimal",
    "parse_int",
    "store_image",
    "unique_list",
    "upsert_crawl_item",
    "upsert_many",
    "upsert_shop",
]
