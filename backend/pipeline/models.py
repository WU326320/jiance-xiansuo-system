"""SQLAlchemy ORM 模型（对应后端方案第 3 节）。

所有列名使用 snake_case；面向 Excel 的中文列名只出现在两个只读视图里。
表结构不依赖 SQLite 专有语法，迁移到 PostgreSQL 只需改连接串。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def now_local() -> datetime:
    """Windows 本机时间（SQLite 存 naive datetime）。"""

    return datetime.now().replace(microsecond=0)


class Base(DeclarativeBase):
    pass


class ConfigVersion(Base):
    __tablename__ = "config_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    config_version: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    caliber_version: Mapped[str] = mapped_column(String(32), nullable=False)
    yaml_text: Mapped[str] = mapped_column(Text, nullable=False)
    first_used_at: Mapped[datetime] = mapped_column(
        DateTime, default=now_local, nullable=False
    )


class Shop(Base):
    __tablename__ = "shops"
    __table_args__ = (
        UniqueConstraint("platform", "shop_id", name="uq_shops_platform_shop"),
        Index("ix_shops_platform", "platform"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    platform: Mapped[str] = mapped_column(String(16), nullable=False)
    shop_id: Mapped[str] = mapped_column(String(64), nullable=False)
    shop_name: Mapped[str | None] = mapped_column(String(255))
    shop_type: Mapped[str] = mapped_column(String(16), default="未知", nullable=False)
    shop_label_raw: Mapped[str | None] = mapped_column(String(255))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=now_local, onupdate=now_local, nullable=False
    )


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("platform", "product_id", name="uq_products_platform_product"),
        Index("ix_products_phash", "image_phash"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    platform: Mapped[str] = mapped_column(String(16), nullable=False)
    product_id: Mapped[str] = mapped_column(String(64), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    shop_id: Mapped[str | None] = mapped_column(String(64))
    # 店名是聚合主键来源；即使没有 shop_id 也必须持久化。
    shop_name: Mapped[str | None] = mapped_column(String(255))
    shop_type: Mapped[str] = mapped_column(String(16), default="未知", nullable=False)
    shop_label_raw: Mapped[str | None] = mapped_column(String(255))
    shop_id_missing: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    main_image_url: Mapped[str | None] = mapped_column(Text)
    image_path: Mapped[str | None] = mapped_column(Text)
    image_phash: Mapped[str | None] = mapped_column(String(64))
    category: Mapped[str | None] = mapped_column(String(128))
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime, default=now_local, nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime, default=now_local, nullable=False
    )

    snapshots: Mapped[list["ProductSnapshot"]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )


class ProductSnapshot(Base):
    __tablename__ = "product_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "product_id",
            "snapshot_date",
            "run_id",
            "ip_code",
            name="uq_snapshots_product_run_ip",
        ),
        Index("ix_snapshots_date", "snapshot_date"),
        Index("ix_snapshots_run", "run_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    crawled_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    ip_code: Mapped[str | None] = mapped_column(String(64), index=True)
    search_keyword: Mapped[str | None] = mapped_column(String(255))

    price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    price_raw: Mapped[str | None] = mapped_column(Text)
    sales_raw: Mapped[str | None] = mapped_column(Text)
    sales: Mapped[int | None] = mapped_column(Integer)
    comments_raw: Mapped[str | None] = mapped_column(Text)
    comments: Mapped[int | None] = mapped_column(Integer)
    listed: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    text_score: Mapped[float | None] = mapped_column(Float)
    visual_score: Mapped[float | None] = mapped_column(Float)
    final_score: Mapped[float | None] = mapped_column(Float)
    need_vision: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    eligible: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    exclusion_reason: Mapped[str | None] = mapped_column(Text)
    hit_reasons: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    formula: Mapped[str] = mapped_column(Text, default="", nullable=False)
    estimated_sales: Mapped[int | None] = mapped_column(Integer)
    missing_flags: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    config_version: Mapped[str] = mapped_column(String(32), nullable=False)
    caliber_version: Mapped[str] = mapped_column(String(32), nullable=False)

    product: Mapped[Product] = relationship(back_populates="snapshots")
    lead_links: Mapped[list["LeadLink"]] = relationship(back_populates="snapshot")


class VisionCache(Base):
    __tablename__ = "vision_cache"
    __table_args__ = (
        UniqueConstraint("image_phash", "ip_code", name="uq_vision_phash_ip"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    image_phash: Mapped[str] = mapped_column(String(64), nullable=False)
    ip_code: Mapped[str] = mapped_column(String(64), nullable=False)
    visual_score: Mapped[float] = mapped_column(Float, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    config_version: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=now_local, nullable=False
    )


class Whitelist(Base):
    __tablename__ = "whitelist"
    __table_args__ = (
        UniqueConstraint(
            "platform", "shop_id", "ip_code", name="uq_whitelist_platform_shop_ip"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 平台存平台组（taobao/jd/1688）；shop_id 为真实 ID 或 name:<规范化店名> 哨兵。
    platform: Mapped[str] = mapped_column(String(16), nullable=False)
    shop_id: Mapped[str] = mapped_column(String(255), nullable=False)
    shop_name: Mapped[str | None] = mapped_column(String(255))
    shop_name_norm: Mapped[str | None] = mapped_column(String(255))
    ip_code: Mapped[str] = mapped_column(String(64), nullable=False)
    note: Mapped[str | None] = mapped_column(String(255))
    added_at: Mapped[datetime] = mapped_column(
        DateTime, default=now_local, nullable=False
    )


class Lead(Base):
    __tablename__ = "leads"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "platform_group",
            "shop_key",
            "shop_id",
            name="uq_leads_run_shopkey",
        ),
        CheckConstraint(
            "status IN ('待复核','已确认','驳回','已录入SaaS')",
            name="ck_leads_status",
        ),
        Index("ix_leads_date_status", "snapshot_date", "status"),
        Index("ix_leads_run", "run_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lead_no: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    # 一次任务（run）是最小口径单位：同一 run 内多个 IP 命中的同店金额合并，
    # 不同 run 之间绝不合并且互不覆盖。
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    # platform 保留明细里的 taobao/tmall；platform_group 用于聚合与白名单。
    platform: Mapped[str] = mapped_column(String(16), nullable=False)
    platform_group: Mapped[str] = mapped_column(
        String(16), default="taobao", nullable=False
    )
    shop_key: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    shop_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    shop_name: Mapped[str | None] = mapped_column(String(255))
    shop_type: Mapped[str] = mapped_column(String(16), default="未知", nullable=False)
    shop_label_raw: Mapped[str | None] = mapped_column(String(255))
    total_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    link_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ip_codes: Mapped[str] = mapped_column(Text, default="", nullable=False)
    payment_tags: Mapped[str] = mapped_column(Text, default="", nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    hit_summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    missing_flags: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="待复核", nullable=False)
    review_result: Mapped[str | None] = mapped_column(String(16))
    reviewer: Mapped[str | None] = mapped_column(String(64))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    saas_entered_at: Mapped[datetime | None] = mapped_column(DateTime)
    caliber_version: Mapped[str] = mapped_column(String(32), nullable=False)
    config_version: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=now_local, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=now_local, onupdate=now_local, nullable=False
    )

    links: Mapped[list["LeadLink"]] = relationship(
        back_populates="lead", cascade="all, delete-orphan"
    )
    history: Mapped[list["LeadStatusHistory"]] = relationship(
        back_populates="lead", cascade="all, delete-orphan"
    )


class LeadLink(Base):
    __tablename__ = "lead_links"
    __table_args__ = (
        UniqueConstraint("lead_id", "snapshot_id", name="uq_lead_links_lead_snapshot"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lead_id: Mapped[int] = mapped_column(
        ForeignKey("leads.id", ondelete="CASCADE"), nullable=False
    )
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("product_snapshots.id", ondelete="CASCADE"), nullable=False
    )
    ip_code: Mapped[str | None] = mapped_column(String(64))
    amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    formula: Mapped[str] = mapped_column(Text, default="", nullable=False)

    lead: Mapped[Lead] = relationship(back_populates="links")
    snapshot: Mapped[ProductSnapshot] = relationship(back_populates="lead_links")


class LeadStatusHistory(Base):
    __tablename__ = "lead_status_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lead_id: Mapped[int] = mapped_column(
        ForeignKey("leads.id", ondelete="CASCADE"), nullable=False
    )
    old_status: Mapped[str | None] = mapped_column(String(16))
    new_status: Mapped[str] = mapped_column(String(16), nullable=False)
    operator: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    changed_at: Mapped[datetime] = mapped_column(
        DateTime, default=now_local, nullable=False
    )
    note: Mapped[str | None] = mapped_column(Text)

    lead: Mapped[Lead] = relationship(back_populates="history")


class RunLog(Base):
    __tablename__ = "run_logs"
    __table_args__ = (Index("ix_run_logs_run", "run_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    platform: Mapped[str | None] = mapped_column(String(16))
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    login_ok: Mapped[bool | None] = mapped_column(Boolean)
    items_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_summary: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(
        DateTime, default=now_local, nullable=False
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(16), default="ok", nullable=False)


__all__ = [
    "Base",
    "ConfigVersion",
    "Lead",
    "LeadLink",
    "LeadStatusHistory",
    "Product",
    "ProductSnapshot",
    "RunLog",
    "Shop",
    "VisionCache",
    "Whitelist",
    "now_local",
]
