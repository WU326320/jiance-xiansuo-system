"""线索查询与复核：直接读写 backend 的 leads.db。

页面复核与 review.bat / Excel 回写使用同一批字段：
``status``、``review_result``、``reviewer``、``reviewed_at``、``saas_entered_at``
以及只增不改的 ``lead_status_history``。
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable

from workbench import backend_bridge as bridge
from workbench.auth import AuthError, ROLE_SUPERVISOR, ROLE_ADMIN
from workbench.config import WorkbenchConfig
from workbench.db import Database

STATUS_PENDING = "待复核"
STATUS_CONFIRMED = "已确认"
STATUS_REJECTED = "驳回"
STATUS_SAAS = "已录入SaaS"
VALID_RESULTS = (STATUS_CONFIRMED, STATUS_REJECTED, STATUS_SAAS)


def _json_list(value: Any) -> list[Any]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except (TypeError, ValueError):
        return []


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except Exception:  # noqa: BLE001
        return Decimal("0")


def tier_for(lead: Any, threshold: float = 500000.0) -> str:
    total = _decimal(getattr(lead, "total_amount", 0))
    missing = _json_list(getattr(lead, "missing_flags", ""))
    if any("金额" in str(item) and "待核验" in str(item) for item in missing):
        return "待核验"
    if total >= Decimal(str(threshold)):
        return "达标"
    return "临界"


def lead_to_dict(lead: Any, *, threshold: float = 500000.0) -> dict[str, Any]:
    return {
        "lead_no": lead.lead_no,
        "snapshot_date": lead.snapshot_date.isoformat() if lead.snapshot_date else "",
        "platform": lead.platform,
        "platform_group": lead.platform_group,
        "shop_key": lead.shop_key,
        "shop_id": lead.shop_id,
        "shop_name": lead.shop_name or "",
        "shop_type": lead.shop_type or "未知",
        "shop_label_raw": lead.shop_label_raw or "",
        "ip_codes": [item for item in str(lead.ip_codes or "").split("、") if item],
        "link_count": int(lead.link_count or 0),
        "total_amount": float(lead.total_amount or 0),
        "confidence": float(lead.confidence or 0),
        "tier": tier_for(lead, threshold),
        "status": lead.status,
        "review_result": lead.review_result or "",
        "reviewer": lead.reviewer or "",
        "reviewed_at": lead.reviewed_at.isoformat(sep=" ", timespec="seconds")
        if lead.reviewed_at
        else "",
        "hit_summary": lead.hit_summary or "",
        "missing_flags": _json_list(lead.missing_flags),
        "payment_tags": lead.payment_tags or "",
        "config_version": lead.config_version,
    }


class LeadsService:
    def __init__(self, config: WorkbenchConfig, db: Database):
        self.config = config
        self.db = db

    def _settings(self):
        return bridge.load_backend_settings(self.config)

    def ensure_ready(self) -> None:
        settings = self._settings()
        bridge.ensure_leads_schema(settings)

    @property
    def threshold(self) -> float:
        try:
            return float(self._settings().get("aggregate.shop_threshold", 500000))
        except Exception:  # noqa: BLE001
            return 500000.0

    # ------------------------------------------------------------------
    def list_leads(self, filters: dict[str, Any]) -> dict[str, Any]:
        bridge.ensure_backend_paths()
        from sqlalchemy import or_  # noqa: PLC0415
        from pipeline.models import Lead  # noqa: PLC0415

        settings = self._settings()
        bridge.ensure_leads_schema(settings)
        threshold = self.threshold
        with bridge.leads_session(settings) as session:
            query = session.query(Lead)
            minimum = str(filters.get("min_amount") or "0").strip()
            if minimum not in {"0", "50000", "100000", "200000", "400000", "500000"}:
                raise AuthError(
                    "金额筛选请选择不限制或5万、10万、20万、40万、50万元以上"
                )
            if minimum != "0":
                query = query.filter(Lead.total_amount >= Decimal(minimum))
            status = str(filters.get("status") or "").strip()
            if status and status != "all":
                query = query.filter(Lead.status == status)
            platform = str(filters.get("platform_group") or "").strip()
            if platform and platform != "all":
                query = query.filter(Lead.platform_group == platform)
            ip_code = str(filters.get("ip_code") or "").strip()
            if ip_code:
                query = query.filter(Lead.ip_codes.like(f"%{ip_code}%"))
            date_from = str(filters.get("date_from") or "").strip()
            date_to = str(filters.get("date_to") or "").strip()
            if date_from:
                query = query.filter(
                    Lead.snapshot_date >= date.fromisoformat(date_from)
                )
            if date_to:
                query = query.filter(Lead.snapshot_date <= date.fromisoformat(date_to))
            needle = str(filters.get("q") or "").strip()
            if needle:
                like = f"%{needle}%"
                query = query.filter(
                    or_(
                        Lead.shop_name.like(like),
                        Lead.hit_summary.like(like),
                        Lead.ip_codes.like(like),
                        Lead.lead_no.like(like),
                    )
                )
            rows = list(query.all())
            items = [lead_to_dict(row, threshold=threshold) for row in rows]
            tier = str(filters.get("tier") or "").strip()
            if tier and tier != "all":
                items = [item for item in items if item["tier"] == tier]
            sort = str(filters.get("sort") or "date_desc")
            if sort == "amount_desc":
                items.sort(key=lambda item: item["total_amount"], reverse=True)
            elif sort == "confidence_desc":
                items.sort(key=lambda item: item["confidence"], reverse=True)
            elif sort == "date_asc":
                items.sort(key=lambda item: (item["snapshot_date"], item["lead_no"]))
            else:
                items.sort(
                    key=lambda item: (item["snapshot_date"], item["total_amount"]),
                    reverse=True,
                )
            total = len(items)
            page = max(1, int(filters.get("page") or 1))
            page_size = max(1, min(int(filters.get("page_size") or 20), 200))
            start = (page - 1) * page_size
            page_items = items[start : start + page_size]
            stats = {
                "total": total,
                "pending": sum(1 for item in items if item["status"] == STATUS_PENDING),
                "confirmed": sum(
                    1 for item in items if item["status"] == STATUS_CONFIRMED
                ),
                "rejected": sum(
                    1 for item in items if item["status"] == STATUS_REJECTED
                ),
                "saas": sum(1 for item in items if item["status"] == STATUS_SAAS),
                "tiers": {
                    "达标": sum(1 for item in items if item["tier"] == "达标"),
                    "待核验": sum(1 for item in items if item["tier"] == "待核验"),
                    "临界": sum(1 for item in items if item["tier"] == "临界"),
                },
            }
            return {
                "items": page_items,
                "total": total,
                "page": page,
                "page_size": page_size,
                "stats": stats,
            }

    def get_lead(self, lead_no: str) -> dict[str, Any]:
        bridge.ensure_backend_paths()
        from sqlalchemy import select  # noqa: PLC0415
        from pipeline.models import Lead, LeadLink, Product, ProductSnapshot  # noqa: PLC0415

        settings = self._settings()
        with bridge.leads_session(settings) as session:
            lead = session.scalar(select(Lead).where(Lead.lead_no == str(lead_no)))
            if lead is None:
                raise AuthError("线索不存在", code="not_found", status=404)
            data = lead_to_dict(lead, threshold=self.threshold)
            link_rows = session.execute(
                select(LeadLink, ProductSnapshot, Product)
                .join(ProductSnapshot, ProductSnapshot.id == LeadLink.snapshot_id)
                .join(Product, Product.id == ProductSnapshot.product_id)
                .where(LeadLink.lead_id == lead.id)
                .order_by(LeadLink.amount.desc())
            ).all()
            links = []
            for link, snapshot, product in link_rows:
                links.append(
                    {
                        "product_id": product.product_id,
                        "shop_name": product.shop_name or data["shop_name"],
                        "title": product.title,
                        "url": product.url,
                        "image_path": product.image_path or "",
                        "main_image_url": product.main_image_url or "",
                        "price": float(snapshot.price)
                        if snapshot.price is not None
                        else None,
                        "sales_raw": snapshot.sales_raw or "",
                        "comments_raw": snapshot.comments_raw or "",
                        "estimated_sales": snapshot.estimated_sales,
                        "amount": float(link.amount)
                        if link.amount is not None
                        else None,
                        "formula": link.formula or snapshot.formula or "",
                        "text_score": float(snapshot.text_score or 0),
                        "visual_score": float(snapshot.visual_score or 0),
                        "final_score": float(snapshot.final_score or 0),
                        "hit_reasons": _json_list(snapshot.hit_reasons),
                        "missing_flags": _json_list(snapshot.missing_flags),
                        "ip_code": link.ip_code or "",
                    }
                )
            data["links"] = links
            return data

    # ------------------------------------------------------------------
    def review(
        self,
        lead_nos: Iterable[str],
        result: str,
        *,
        reviewer: str,
        actor: dict[str, Any],
        confirm_text: str = "",
        source_ip: str = "",
    ) -> dict[str, Any]:
        bridge.ensure_backend_paths()
        from sqlalchemy import select  # noqa: PLC0415
        from pipeline.models import Lead, LeadStatusHistory  # noqa: PLC0415

        if result not in VALID_RESULTS:
            raise AuthError(f"非法复核结论：{result}")
        numbers = [str(item).strip() for item in lead_nos if str(item).strip()]
        if not numbers:
            raise AuthError("请选择要复核的线索")
        if (
            result == STATUS_REJECTED
            and len(numbers) > 1
            and confirm_text != "确认删除"
        ):
            raise AuthError("批量驳回需要手动输入「确认删除」")
        if not str(reviewer or "").strip():
            raise AuthError("请填写复核人")
        settings = self._settings()
        now = datetime.now().replace(microsecond=0)
        updated: list[str] = []
        skipped: list[dict[str, str]] = []
        missing: list[str] = []
        before_rows: list[dict[str, Any]] = []
        with bridge.leads_session(settings) as session:
            for lead_no in numbers:
                lead = session.scalar(select(Lead).where(Lead.lead_no == lead_no))
                if lead is None:
                    missing.append(lead_no)
                    continue
                if lead.status == STATUS_SAAS:
                    skipped.append({"lead_no": lead_no, "reason": "已录入SaaS，不覆盖"})
                    continue
                old_status = lead.status
                before_rows.append(
                    {
                        "lead_no": lead_no,
                        "status": old_status,
                        "reviewer": lead.reviewer or "",
                    }
                )
                if old_status == result and (lead.reviewer or "") == reviewer:
                    skipped.append({"lead_no": lead_no, "reason": "结论未变化"})
                    continue
                lead.status = result
                lead.review_result = result
                lead.reviewer = reviewer
                lead.reviewed_at = now
                if result == STATUS_SAAS:
                    lead.saas_entered_at = now
                session.add(
                    LeadStatusHistory(
                        lead_id=lead.id,
                        old_status=old_status,
                        new_status=result,
                        operator=reviewer,
                        changed_at=now,
                        note=f"workbench:{actor.get('username', '')}",
                    )
                )
                updated.append(lead_no)
            session.flush()
        self.db.audit(
            actor_id=actor.get("id"),
            actor_name=actor.get("username", ""),
            source_ip=source_ip,
            action="lead.review",
            object_type="lead",
            object_id=",".join(updated[:20]),
            before={"leads": before_rows},
            after={"result": result, "reviewer": reviewer, "updated": len(updated)},
        )
        return {
            "updated": updated,
            "skipped": skipped,
            "missing": missing,
            "result": result,
            "reviewer": reviewer,
        }

    # ------------------------------------------------------------------
    def risk_counts(self, run_id: str) -> dict[str, int]:
        bridge.ensure_backend_paths()
        from sqlalchemy import select  # noqa: PLC0415
        from pipeline.models import Lead, LeadLink, ProductSnapshot  # noqa: PLC0415

        settings = self._settings()
        bridge.ensure_leads_schema(settings)
        counts = {"达标": 0, "待核验": 0, "临界": 0}
        if not run_id:
            return counts
        with bridge.leads_session(settings) as session:
            rows = session.scalars(
                select(Lead)
                .join(LeadLink, LeadLink.lead_id == Lead.id)
                .join(ProductSnapshot, ProductSnapshot.id == LeadLink.snapshot_id)
                .where(ProductSnapshot.run_id == run_id)
                .distinct()
            ).all()
            for lead in rows:
                counts[tier_for(lead, self.threshold)] = (
                    counts.get(tier_for(lead, self.threshold), 0) + 1
                )
        return counts

    def export(self, snapshot_date: str | None = None) -> dict[str, Any]:
        settings = self._settings()
        bridge.ensure_leads_schema(settings)
        day = snapshot_date or date.today().isoformat()
        path = bridge.export_leads(settings, day, output_dir=self.config.exports_dir)
        self.db.audit(
            actor_id=None,
            actor_name="workbench",
            source_ip="",
            action="export",
            object_type="excel",
            object_id=str(path),
            after={"snapshot_date": day},
        )
        return {"path": str(path), "snapshot_date": day}


__all__ = [
    "LeadsService",
    "STATUS_CONFIRMED",
    "STATUS_PENDING",
    "STATUS_REJECTED",
    "STATUS_SAAS",
    "VALID_RESULTS",
    "lead_to_dict",
    "tier_for",
]
