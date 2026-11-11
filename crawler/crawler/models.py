"""统一输出模型。

字段与《采集方案 v0.1》第 3 节一致；缺失一律 ``None``，绝不填 0。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _validate_commerce(data):
    if not isinstance(data, dict):
        return data
    from utils.commerce_numbers import count_field, price_value

    data = dict(data)
    flags = list(data.get("flags") or [])
    for field in ("sales", "comments"):
        value, issue = count_field(
            data.get(field), data.get(field + "_raw"), field=field
        )
        data[field] = value
        if issue:
            flags.append(issue)
    raw = data.get("price_raw")
    original_price = data.get("price")
    data["price"] = price_value(raw if raw not in (None, "") else original_price)
    if data["price"] is None and (raw is not None or original_price is not None):
        if raw in (None, "") and original_price is not None:
            data["price_raw"] = str(original_price)
        flags.append("价格无法识别,待核验")
    data["flags"] = list(dict.fromkeys(flags))
    return data


Platform = Literal["taobao", "tmall", "jd", "1688"]


class Item(BaseModel):
    """搜索列表页的一条商品。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    platform: Platform
    product_id: str
    url: str
    title: str
    shop_id: str | None = None
    shop_name: str | None = None
    # 页面原始店铺 ID（淘宝长加密串等），仅备查；对外比对优先用 shop_id。
    shop_id_raw: str | None = None
    shop_label_raw: str | None = None
    shop_type_raw_hint: str | None = None
    price: Decimal | None = None
    price_raw: str | None = None
    sales_raw: str | None = None
    sales: int | None = None
    comments_raw: str | None = None
    comments: int | None = None
    main_image_url: str | None = None
    image_path: str | None = None
    category: str | None = None
    ip_code: str | None = None
    search_keyword: str
    page: int | None = None
    rank: int | None = None
    crawled_at: datetime
    listed: bool = True
    flags: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _commerce(cls, data):
        return _validate_commerce(data)

    @field_validator("price", mode="before")
    @classmethod
    def _coerce_price(cls, value: Any) -> Any:
        if value is None or isinstance(value, Decimal):
            return value
        if isinstance(value, (int, float, str)):
            from crawler.core.parse import parse_price

            return parse_price(value)
        return value

    @field_validator("sales", "comments", mode="before")
    @classmethod
    def _coerce_int(cls, value: Any) -> Any:
        if value is None or isinstance(value, int):
            return value
        if isinstance(value, (float, str)):
            from crawler.core.parse import parse_fuzzy_int

            return parse_fuzzy_int(value)
        return value


class ItemPatch(BaseModel):
    """详情页补抓的字段；只覆盖非空字段。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    platform: Platform
    product_id: str
    title: str | None = None
    shop_id: str | None = None
    shop_name: str | None = None
    shop_id_raw: str | None = None
    shop_label_raw: str | None = None
    shop_type_raw_hint: str | None = None
    price: Decimal | None = None
    price_raw: str | None = None
    sales_raw: str | None = None
    sales: int | None = None
    comments_raw: str | None = None
    comments: int | None = None
    main_image_url: str | None = None
    category: str | None = None
    listed: bool | None = None
    flags: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _commerce(cls, data):
        return _validate_commerce(data)


class RunRecord(BaseModel):
    """每次平台运行结束写一条，供后端写 run_logs。"""

    model_config = ConfigDict(extra="forbid")

    platform: str
    # 每次运行唯一编号（列表/详情各自一条，便于后端 run_logs 串联）。
    run_id: str | None = None
    started_at: datetime
    ended_at: datetime
    login_ok: bool = False
    risk_blocked: bool = False
    # 本次运行的详情阶段状态：列表运行为 skipped，详情运行为最终 status。
    detail_status: str | None = "skipped"
    # 本次补采已处理至终态的失败/受限项，不代表还有自动重试在运行。
    detail_failures: dict[str, str] = Field(default_factory=dict)
    deadline_reached: bool = False
    # 本次运行是否由采集端启动了新的 Chrome/Edge（CDP 端口原本没开）。
    # wu_validate.ps1 只关闭 browser_started=true 的端口实例。
    browser_started: bool | None = None
    keywords_done: int = 0
    # 本次运行实际请求的关键词（平台、关键词、页数、条数一起写 run_logs）。
    keywords: list[str] = Field(default_factory=list)
    # 淘宝分批：本批序号 / 总批数 / 下一批序号（1 起；无分批时均为 null）。
    batch_index: int | None = None
    batch_total: int | None = None
    batch_next: int | None = None
    batch_date: str | None = None
    batch_keywords: list[str] = Field(default_factory=list)
    pages_fetched: int = 0
    items_ok: int = 0
    items_dropped: int = 0
    items_duplicate: int = 0
    drop_reasons: dict[str, int] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    screenshot_path: str | None = None
    status: str = "ok"
    login_reason: str | None = None
    config_version: str | None = None
    caliber_version: str | None = None
    output_path: str | None = None
    log_path: str | None = None


def item_json_dict(item: Item) -> dict[str, Any]:
    """按方案字段顺序输出 JSONL 字典，Decimal 转 JSON number。"""
    data = item.model_dump(mode="python")
    price = data.get("price")
    if isinstance(price, Decimal):
        data["price"] = float(price.quantize(Decimal("0.01")))
    crawled_at = data.get("crawled_at")
    if isinstance(crawled_at, datetime):
        data["crawled_at"] = crawled_at.isoformat()
    data["flags"] = list(dict.fromkeys(data.get("flags") or []))
    return data


def patch_json_dict(patch: ItemPatch) -> dict[str, Any]:
    data = patch.model_dump(mode="python")
    price = data.get("price")
    if isinstance(price, Decimal):
        data["price"] = float(price.quantize(Decimal("0.01")))
    data["flags"] = list(dict.fromkeys(data.get("flags") or []))
    return data


def run_record_json_dict(record: RunRecord) -> dict[str, Any]:
    data = record.model_dump(mode="python")
    for key in ("started_at", "ended_at"):
        value = data.get(key)
        if isinstance(value, datetime):
            data[key] = value.isoformat()
    return data
