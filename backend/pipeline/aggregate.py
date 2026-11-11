"""店铺聚合、去重压制与线索生成（后端方案第 3–5 节）。

2026-11-06 口径：聚合键是 ``任务号 + 平台组 + 规范化店名``，``shop_id`` 只用于
区分同名不同店（双方都有且不同才拆开）。店名为空的商品只进链接明细。

一次任务是最小口径单位：同一任务内多个 IP 命中的同店金额合并计算；不同任务
之间金额绝不合并，线索也互不覆盖。
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Iterable

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from pipeline.config import Settings
from pipeline.jd_detail import JdDetailTarget
from pipeline.models import Lead, LeadLink, Product, ProductSnapshot, Shop
from pipeline.scoring import COMMENTS_MISSING_FLAG, add_reason
from pipeline.shopkey import (
    normalize_shop_name,
    platform_group,
    shop_key as make_shop_key,
    shop_name_missing,
)
from pipeline.store import (
    split_ip_codes,
    dumps_json,
    loads_json,
    normalize_shop_id,
    unique_list,
)

STATUS_PENDING = "待复核"
STATUS_CONFIRMED = "已确认"
STATUS_REJECTED = "驳回"
STATUS_SAAS = "已录入SaaS"
PENDING_STATUSES = (STATUS_PENDING,)
PROCESSED_STATUSES = (STATUS_CONFIRMED, STATUS_REJECTED, STATUS_SAAS)
MISSING_COMMENTS_VERIFY_FLAG = COMMENTS_MISSING_FLAG
SAME_NAME_FLAG = "同名不同店"
SHOP_NAME_MISSING_FLAG = "店铺名缺失"


def _as_datetime(value: date | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.combine(value, time.min)


def _effective_processed_at(lead: Lead) -> datetime:
    if lead.status == STATUS_SAAS:
        return (
            lead.saas_entered_at
            or lead.reviewed_at
            or lead.updated_at
            or lead.created_at
        )
    if lead.status in (STATUS_CONFIRMED, STATUS_REJECTED):
        return lead.reviewed_at or lead.updated_at or lead.created_at
    return lead.updated_at or lead.created_at


def suppression_reason(
    session: Session,
    *,
    settings: Settings,
    platform: str | None = None,
    shop_name: str | None = None,
    shop_id: str | None,
    ip_code: str | None,
    snapshot_date: date,
    platform_group_name: str | None = None,
    shop_key: str | None = None,
) -> str | None:
    """按 平台组 + 规范化店名 + IP 查历史线索；返回压制原因或 None。

    双方都有 shop_id 且不同时不算同一家店；只有一边有或都没有时按店名压制。
    快照的 ``ip_code`` 可能是同任务内多个 IP 的逗号集合，只要历史线索命中其中
    任意一个 IP 即视为已处理。
    """
    wanted = {item for item in split_ip_codes(ip_code)}
    if not wanted:
        return None
    group = platform_group_name or platform_group(platform)
    key = shop_key or make_shop_key(group, shop_name)
    rows = session.execute(
        select(Lead, LeadLink.ip_code)
        .join(LeadLink, LeadLink.lead_id == Lead.id)
        .where(
            Lead.platform_group == group,
            Lead.shop_key == key,
            Lead.status.in_(PROCESSED_STATUSES),
        )
    ).all()
    current_id = normalize_shop_id(shop_id)
    day_start = _as_datetime(snapshot_date)
    for lead, link_ip in rows:
        if wanted.isdisjoint(split_ip_codes(link_ip)):
            continue
        lead_id = normalize_shop_id(lead.shop_id)
        if current_id and lead_id and current_id != lead_id:
            continue
        days = (
            int(settings.get("dedup.suppress_days_rejected", 30))
            if lead.status == STATUS_REJECTED
            else int(settings.get("dedup.suppress_days", 90))
        )
        cutoff = day_start - timedelta(days=days)
        if _effective_processed_at(lead) >= cutoff:
            return f"已处理压制:{lead.status}:{lead.snapshot_date.isoformat()}"
    return None


def needs_jd_detail(total: Decimal | float, settings: Settings) -> bool:
    threshold = Decimal(str(settings.get("aggregate.shop_threshold", 500000)))
    low = Decimal(str(settings.get("fetch.jd.near_band_low", 0.6)))
    high = Decimal(str(settings.get("fetch.jd.near_band_high", 1.0)))
    value = Decimal(str(total))
    return threshold * low <= value < threshold * high


@dataclass
class ShopGroup:
    run_id: str
    platform_group: str
    shop_key: str
    shop_name: str
    shop_id: str
    platform: str
    shop_type: str = "未知"
    shop_label_raw: str | None = None
    snapshots: list[ProductSnapshot] = field(default_factory=list)
    same_name_split: bool = False


def _product_shop(
    session: Session, product: Product
) -> tuple[str | None, str, str | None]:
    shop_name = product.shop_name
    shop_type = product.shop_type or "未知"
    label = product.shop_label_raw
    if product.shop_id:
        shop = session.scalar(
            select(Shop).where(
                Shop.platform == product.platform, Shop.shop_id == product.shop_id
            )
        )
        if shop is not None:
            shop_name = shop_name or shop.shop_name
            shop_type = shop_type or shop.shop_type
            label = label or shop.shop_label_raw
    return shop_name, shop_type or "未知", label


def _collect_groups(
    session: Session,
    *,
    settings: Settings,
    snapshots: Iterable[ProductSnapshot],
    snapshot_date: date,
) -> dict[tuple[str, str, str, str], ShopGroup]:
    """按与线索聚合完全相同的门槛收集同店快照。

    分桶键含 ``run_id``：同一任务内多个 IP 命中同店会被合并成一组，
    不同任务之间永远分开，金额不会跨任务相加。
    """

    min_final = float(settings.get("combined.min_final_score", 35))
    buckets: dict[
        tuple[str, str, str],
        list[tuple[ProductSnapshot, Product, str, str, str | None]],
    ] = defaultdict(list)
    seen_snapshots: set[int] = set()
    for snapshot in snapshots:
        if snapshot.id in seen_snapshots:
            continue
        seen_snapshots.add(snapshot.id or id(snapshot))
        product = session.get(Product, snapshot.product_id)
        if product is None:
            continue
        if not snapshot.eligible:
            continue
        if not snapshot.listed:
            add_reason(snapshot, "下架:不计入聚合:0")
            continue
        if snapshot.final_score is None:
            continue
        if float(snapshot.final_score) < min_final and not snapshot.need_vision:
            continue
        shop_name, shop_type, label = _product_shop(session, product)
        if shop_name_missing(shop_name):
            add_reason(snapshot, "店铺名缺失:不参与聚合:0")
            continue
        group = platform_group(product.platform)
        key = make_shop_key(group, shop_name)
        reason = suppression_reason(
            session,
            settings=settings,
            platform_group_name=group,
            shop_key=key,
            shop_id=product.shop_id,
            ip_code=snapshot.ip_code,
            snapshot_date=snapshot_date,
        )
        if reason:
            add_reason(snapshot, reason)
            continue
        buckets[
            (str(snapshot.run_id or ""), group, normalize_shop_name(shop_name))
        ].append((snapshot, product, str(shop_name).strip(), shop_type, label))

    groups: dict[tuple[str, str, str, str], ShopGroup] = {}
    for (run_id, group, _norm), entries in buckets.items():
        ids = sorted(
            {
                sid
                for _snapshot, product, _name, _type, _label in entries
                if (sid := normalize_shop_id(product.shop_id))
            }
        )
        same_split = len(ids) > 1
        by_id: dict[
            str, list[tuple[ProductSnapshot, Product, str, str, str | None]]
        ] = defaultdict(list)
        if not same_split:
            # 只有一边有 shop_id 或两边都没有：照常合并，不按 ID 拆。
            only_id = ids[0] if ids else ""
            by_id[only_id].extend(entries)
        else:
            for entry in entries:
                sid = normalize_shop_id(entry[1].shop_id) or ""
                by_id[sid].append(entry)
        for sid, items in by_id.items():
            snapshot, product, shop_name, shop_type, label = items[0]
            key = make_shop_key(group, shop_name)
            bucket_key = (run_id, group, key, sid)
            existing = groups.get(bucket_key)
            if existing is None:
                existing = ShopGroup(
                    run_id=run_id,
                    platform_group=group,
                    shop_key=key,
                    shop_name=shop_name,
                    shop_id=sid,
                    platform=product.platform,
                    shop_type=shop_type,
                    shop_label_raw=label,
                    same_name_split=same_split,
                )
                groups[bucket_key] = existing
            existing.snapshots.extend(item[0] for item in items)
    return groups


def _business_product_key(
    session: Session, snapshot: ProductSnapshot
) -> tuple[str, str]:
    product = session.get(Product, snapshot.product_id)
    if product is None:
        return ("missing", str(snapshot.product_id))
    return (platform_group(product.platform), str(product.product_id))


def _normalized_links(
    session: Session, links: list[ProductSnapshot]
) -> list[ProductSnapshot]:
    # 同商品命中多个 IP 时保留最新采集的一份金额，分数仅用于同时间择优。
    unique: dict[tuple[str, str], ProductSnapshot] = {}
    for item in links:
        key = _business_product_key(session, item)
        previous = unique.get(key)
        if previous is None or (
            item.crawled_at,
            float(item.final_score or 0),
            item.id or 0,
        ) > (previous.crawled_at, float(previous.final_score or 0), previous.id or 0):
            unique[key] = item
    return sorted(unique.values(), key=lambda item: item.id or 0)


def _group_total(links: list[ProductSnapshot]) -> Decimal:
    return sum(
        (Decimal(str(item.amount or 0)) for item in links), Decimal("0")
    ).quantize(Decimal("0.01"))


def _critical_lower(settings: Settings) -> Decimal:
    threshold = Decimal(str(settings.get("aggregate.shop_threshold", 500000)))
    low = Decimal(str(settings.get("fetch.jd.near_band_low", 0.6)))
    return threshold * low


def _lead_qualifies(total: Decimal, settings: Settings) -> bool:
    """保留全部合格候选店铺，金额门槛由列表筛选决定。"""
    return total >= 0


def jd_near_band_targets(
    session: Session,
    *,
    settings: Settings,
    snapshots: Iterable[ProductSnapshot],
    snapshot_date: date,
) -> list[JdDetailTarget]:
    """返回店铺合计落在京东临界带 0.6–1.0 门槛内的店铺及其链接。"""

    snapshots = list(snapshots)
    groups = _collect_groups(
        session,
        settings=settings,
        snapshots=snapshots,
        snapshot_date=snapshot_date,
    )
    targets: list[JdDetailTarget] = []
    for group in groups.values():
        if group.platform_group != "jd" or not group.shop_id:
            continue
        links = _normalized_links(session, group.snapshots)
        total = _group_total(links)
        missing = [
            s
            for s in links
            if any(getattr(s, field) is None for field in ("price", "sales"))
        ]
        if not needs_jd_detail(total, settings) and not missing:
            continue
        if not needs_jd_detail(total, settings):
            links = missing
        product_ids: list[str] = []
        for snapshot in links:
            product = session.get(Product, snapshot.product_id)
            if product is not None and product.product_id:
                product_ids.append(str(product.product_id))
        targets.append(
            JdDetailTarget(
                shop_id=group.shop_id,
                total_amount=total,
                product_ids=tuple(dict.fromkeys(product_ids)),
            )
        )
    selected = {pid for target in targets for pid in target.product_ids}
    for snapshot in snapshots:
        product = session.get(Product, snapshot.product_id)
        if (
            product is None
            or product.platform != "jd"
            or not snapshot.eligible
            or snapshot.exclusion_reason
            or not snapshot.listed
            or product.product_id in selected
        ):
            continue
        if not str(product.shop_name or "").strip():
            targets.append(
                JdDetailTarget(
                    shop_id=product.shop_id or "",
                    total_amount=Decimal(str(snapshot.amount or 0)),
                    product_ids=(str(product.product_id),),
                )
            )
            selected.add(product.product_id)
    return targets


def near_threshold_shops(
    session: Session,
    *,
    settings: Settings,
    snapshots: Iterable[ProductSnapshot],
    snapshot_date: date,
) -> list[dict[str, object]]:
    """返回合计落在临界带 ``[threshold*near_band_low, threshold]`` 的店铺。"""

    threshold = Decimal(str(settings.get("aggregate.shop_threshold", 500000)))
    low = Decimal(str(settings.get("fetch.jd.near_band_low", 0.6)))
    lower = threshold * low
    groups = _collect_groups(
        session,
        settings=settings,
        snapshots=snapshots,
        snapshot_date=snapshot_date,
    )
    rows: list[dict[str, object]] = []
    for group in groups.values():
        links = _normalized_links(session, group.snapshots)
        total = _group_total(links)
        if not (lower <= total <= threshold):
            continue
        rows.append(
            {
                "platform": group.platform,
                "platform_group": group.platform_group,
                "shop_id": group.shop_id or None,
                "shop_name": group.shop_name,
                "snapshot_date": snapshot_date.isoformat(),
                "shop_amount": str(total),
                "link_count": len(links),
                "near_threshold_low": str(lower.quantize(Decimal("0.01"))),
                "threshold_amount": str(threshold.quantize(Decimal("0.01"))),
            }
        )
    rows.sort(
        key=lambda item: (
            -Decimal(str(item["shop_amount"])),
            str(item["platform_group"]),
            str(item["shop_name"]),
        )
    )
    return rows


def _next_lead_no(session: Session, platform: str, day: date) -> str:
    prefix = f"{platform}-{day.strftime('%Y%m%d')}-"
    rows = session.scalars(
        select(Lead.lead_no).where(Lead.lead_no.like(f"{prefix}%"))
    ).all()
    sequence = 0
    for lead_no in rows:
        tail = str(lead_no).rsplit("-", 1)[-1]
        if tail.isdigit():
            sequence = max(sequence, int(tail))
    return f"{prefix}{sequence + 1:04d}"


def _confidence(links: list[ProductSnapshot], settings: Settings) -> float:
    if not links:
        return 0.0
    amounts = [Decimal(str(item.amount or 0)) for item in links]
    total = sum(amounts, Decimal("0"))
    if total > 0:
        weighted = sum(
            float(item.final_score or 0) * float(amount)
            for item, amount in zip(links, amounts)
        ) / float(total)
    else:
        weighted = sum(float(item.final_score or 0) for item in links) / len(links)

    flags: set[str] = set()
    for item in links:
        flags.update(str(flag) for flag in loads_json(item.missing_flags, []))
    missing = flags & {
        "销量缺失",
        "价格缺失",
        SHOP_NAME_MISSING_FLAG,
        "主图缺失",
        "待人工补齐",
    }
    if missing:
        weighted -= 10
    confirm = float(settings.get("visual.confirm", 70))
    if links and all(
        item.visual_score is not None and float(item.visual_score) >= confirm
        for item in links
    ):
        weighted += 10
    return round(max(0.0, min(100.0, weighted)), 2)


def _payment_tags(shop_type: str | None, label: str | None, settings: Settings) -> str:
    tags: list[str] = []
    configured = settings.get("payment_ability.signals") or []
    haystack = " ".join(part for part in ((shop_type or ""), (label or "")) if part)
    for signal in configured:
        if isinstance(signal, dict):
            text = str(signal.get("label") or signal.get("name") or "")
            keyword = str(signal.get("keyword") or text)
        else:
            text = keyword = str(signal)
        if text and keyword and keyword in haystack:
            tags.append(text)
    if shop_type == "企业":
        tags.append("企业")
    for keyword in ("天猫", "旗舰店", "皇冠", "实力商家", "上市", "国企"):
        if keyword in haystack:
            tags.append(keyword)
    if not tags:
        tags.append("未知")
    return "、".join(unique_list(tags))


def _hit_summary(links: list[ProductSnapshot], limit: int = 6) -> str:
    counter: Counter[str] = Counter()
    for item in links:
        for reason in loads_json(item.hit_reasons, []):
            text = str(reason)
            key = text.split(":", 1)[0]
            counter[key] += 1
    if not counter:
        return "命中规则"
    return "；".join(f"{key}×{count}" for key, count in counter.most_common(limit))


def _missing_flags(
    links: list[ProductSnapshot], *, same_name_split: bool = False
) -> list[str]:
    flags: list[str] = []
    if same_name_split:
        flags.append(SAME_NAME_FLAG)
    for item in links:
        flags.extend(str(flag) for flag in loads_json(item.missing_flags, []))
    return unique_list(flags)


def _replace_links(
    session: Session,
    lead: Lead,
    links: list[ProductSnapshot],
    all_snapshots: list[ProductSnapshot],
) -> None:
    session.execute(delete(LeadLink).where(LeadLink.lead_id == lead.id))
    session.flush()
    for snapshot in links:
        session.add(
            LeadLink(
                lead_id=lead.id,
                snapshot_id=snapshot.id,
                ip_code="、".join(
                    unique_list(
                        item.ip_code
                        for item in all_snapshots
                        if _business_product_key(session, item)
                        == _business_product_key(session, snapshot)
                        and item.ip_code
                    )
                ),
                amount=(
                    Decimal(str(snapshot.amount)).quantize(Decimal("0.01"))
                    if snapshot.amount is not None
                    else None
                ),
                formula=snapshot.formula or "",
            )
        )
    session.flush()


def aggregate_snapshots(
    session: Session,
    *,
    settings: Settings,
    snapshots: Iterable[ProductSnapshot],
    snapshot_date: date,
    run_id: str,
) -> list[Lead]:
    """对指定快照集合做压制、聚合、门槛与线索 upsert。

    线索以 ``run_id`` 为口径：同一任务内多个 IP 命中的同店合并成一条，
    不同任务各自独立成条、互不覆盖。
    """

    groups = _collect_groups(
        session,
        settings=settings,
        snapshots=[
            s
            for s in snapshots
            if s.run_id == run_id and s.snapshot_date == snapshot_date
        ],
        snapshot_date=snapshot_date,
    )

    produced: list[Lead] = []
    for bucket_key in sorted(groups):
        group = groups[bucket_key]
        links = _normalized_links(session, group.snapshots)
        if not links:
            continue
        total = _group_total(links)
        lead = session.scalar(
            select(Lead).where(
                Lead.run_id == group.run_id,
                Lead.platform_group == group.platform_group,
                Lead.shop_key == group.shop_key,
                Lead.shop_id == group.shop_id,
            )
        )
        desired_status = STATUS_PENDING
        if not _lead_qualifies(total, settings):
            if lead is not None and lead.status in PENDING_STATUSES:
                session.execute(delete(LeadLink).where(LeadLink.lead_id == lead.id))
                session.delete(lead)
                session.flush()
            continue
        if lead is not None and lead.status not in PENDING_STATUSES:
            continue
        if lead is None:
            lead = Lead(
                lead_no=_next_lead_no(session, group.platform, snapshot_date),
                run_id=group.run_id,
                snapshot_date=snapshot_date,
                platform=group.platform,
                platform_group=group.platform_group,
                shop_key=group.shop_key,
                shop_id=group.shop_id,
                shop_name=group.shop_name,
                shop_type=group.shop_type,
                shop_label_raw=group.shop_label_raw,
                total_amount=total,
                link_count=0,
                ip_codes="",
                payment_tags="",
                confidence=0.0,
                hit_summary="",
                missing_flags="[]",
                status=desired_status,
                caliber_version=settings.caliber_version,
                config_version=settings.config_version,
            )
            session.add(lead)
            session.flush()
        elif lead.status != desired_status:
            lead.status = desired_status
        lead.snapshot_date = snapshot_date
        lead.platform = group.platform
        lead.shop_name = group.shop_name
        lead.shop_type = group.shop_type
        lead.shop_label_raw = group.shop_label_raw
        lead.total_amount = total
        lead.link_count = len(links)
        # 同一 run 内多个 IP 命中的同店金额合并，ip_codes 记录全部参与 IP。
        lead.ip_codes = "、".join(
            unique_list(item.ip_code for item in group.snapshots if item.ip_code)
        )
        lead.payment_tags = _payment_tags(
            group.shop_type, group.shop_label_raw, settings
        )
        lead.confidence = _confidence(links, settings)
        lead.hit_summary = _hit_summary(links)
        lead.missing_flags = dumps_json(
            _missing_flags(links, same_name_split=group.same_name_split)
        )
        lead.caliber_version = settings.caliber_version
        lead.config_version = settings.config_version
        _replace_links(session, lead, links, group.snapshots)
        produced.append(lead)
    session.flush()
    return produced


def aggregate_day(
    session: Session,
    *,
    settings: Settings,
    snapshot_date: date,
    run_id: str,
    platform: str | None = None,
) -> list[Lead]:
    query = (
        select(ProductSnapshot)
        .join(Product, Product.id == ProductSnapshot.product_id)
        .where(
            ProductSnapshot.snapshot_date == snapshot_date,
            ProductSnapshot.run_id == run_id,
            ProductSnapshot.eligible.is_(True),
            or_(
                ProductSnapshot.final_score
                >= float(settings.get("combined.min_final_score", 35)),
                ProductSnapshot.need_vision.is_(True),
            ),
        )
    )
    if platform:
        query = query.where(Product.platform == platform)
    snapshots = list(session.scalars(query).all())
    return aggregate_snapshots(
        session,
        settings=settings,
        snapshots=snapshots,
        snapshot_date=snapshot_date,
        run_id=run_id,
    )


__all__ = [
    "MISSING_COMMENTS_VERIFY_FLAG",
    "PENDING_STATUSES",
    "PROCESSED_STATUSES",
    "SAME_NAME_FLAG",
    "SHOP_NAME_MISSING_FLAG",
    "STATUS_CONFIRMED",
    "STATUS_PENDING",
    "STATUS_REJECTED",
    "STATUS_SAAS",
    "aggregate_day",
    "aggregate_snapshots",
    "jd_near_band_targets",
    "near_threshold_shops",
    "needs_jd_detail",
    "suppression_reason",
]
