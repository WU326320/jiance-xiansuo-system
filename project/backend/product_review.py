"""独立的商品审核计算与 CSV 导出；不沿用旧版店铺聚合口径。"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

THRESHOLD = Decimal("500000")
NUMBER = r"\d+(?:\.\d+)?"
HEADERS = {
    "ip_name": "IP名称",
    "platform": "平台",
    "target_site": "目标网站",
    "title": "商品名称",
    "url": "商品链接",
    "shop_name": "店铺名称",
    "shop_id": "店铺标识",
    "shop_url": "店铺链接",
    "price_raw": "商品单价原文",
    "price": "商品单价计算值",
    "sales_raw": "网站展示销量原文",
    "sales": "网站展示销量计算值",
    "comments_raw": "评论数原文",
    "comments": "评论数计算值",
    "initial_amount": "初始销售额",
    "corrected_sales": "修正销量",
    "corrected_amount": "修正销售额",
    "crawled_at": "数据采集时间",
    "detail_crawled_at": "详情采集时间",
    "status": "处理状态",
    "needs_detail": "需要详情评论",
    "anomalies": "数据异常说明",
    "whitelist_match": "白名单命中依据",
    "policy": "筛选口径",
    "review_result": "人工审核结论",
    "notes": "备注",
}


@dataclass(frozen=True)
class Policy:
    # 所有选项必填，调用方只能在用户确认后传入。
    threshold_boundary: str  # inclusive / exclusive
    corrected_filter: str  # keep_initial / require_threshold
    range_price: str  # review / minimum / maximum
    missing_data: str  # review / exclude
    ambiguous_counts: str  # review / lower_bound

    def __post_init__(self):
        choices = {
            "threshold_boundary": {"inclusive", "exclusive"},
            "corrected_filter": {"keep_initial", "require_threshold"},
            "range_price": {"review", "minimum", "maximum"},
            "missing_data": {"review", "exclude"},
            "ambiguous_counts": {"review", "lower_bound"},
        }
        for name, allowed in choices.items():
            if getattr(self, name) not in allowed:
                raise ValueError(f"筛选口径 {name} 必须明确选择：{sorted(allowed)}")

    def passes(self, amount: Decimal) -> bool:
        return (
            amount >= THRESHOLD
            if self.threshold_boundary == "inclusive"
            else amount > THRESHOLD
        )


def canonical_url(value: str) -> str:
    """保留标识性查询参数，避免把不同 ID 的店铺 URL 合并。"""
    try:
        parts = urlsplit(value.strip())
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            raise ValueError("链接必须为完整 HTTP(S) 地址")
        if parts.username or parts.password:
            raise ValueError("链接不得包含账号信息")
        port = parts.port
        host = parts.hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        if port and (parts.scheme.lower(), port) not in {("http", 80), ("https", 443)}:
            host += f":{port}"
        return urlunsplit(
            (
                parts.scheme.lower(),
                host,
                parts.path.rstrip("/"),
                urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True))),
                "",
            )
        )
    except (ValueError, AttributeError) as exc:
        raise ValueError("无效的 HTTP(S) 链接") from exc


def parse_price(raw: str, policy: Policy) -> tuple[Decimal | None, list[str]]:
    text = raw.strip()
    if not text:
        return None, ["价格缺失"]
    # 只接受单一人民币价格或明确区间，多规格/优惠条件不得抽取第一个数字。
    match = re.fullmatch(
        rf"[¥￥]?\s*({NUMBER})\s*(?:[-~～至]\s*[¥￥]?\s*({NUMBER}))?\s*元?", text
    )
    if not match:
        return None, ["价格无法解析或含多规格/优惠条件，待人工核查"]
    low = Decimal(match[1])
    if not match[2]:
        return (low, []) if low > 0 else (None, ["价格必须大于零"])
    high = Decimal(match[2])
    if low <= 0 or high < low:
        return None, ["价格区间无效"]
    if policy.range_price == "review":
        return None, ["区间价格待人工核查"]
    price = low if policy.range_price == "minimum" else high
    return price, [f"区间价按已确认策略 {policy.range_price} 计算"]


def parse_count(raw: str, label: str, policy: Policy) -> tuple[int | None, list[str]]:
    text = raw.strip()
    if not text:
        return None, [f"{label}缺失"]
    if any(token in text for token in ("人付款", "人已付款", "人收货")):
        return None, [f"{label}展示为人数指标，不能转换为售出件数或评论数，待人工核查"]
    # 仅在同一指标内解析数字；模糊下限策略不能授权指标之间的转换。
    prefix = "已售|已拼|销量" if label == "销量" else "评论数|评论|评价"
    suffix = "件" if label == "销量" else "条|条评价|人评价|评价"
    match = re.fullmatch(
        rf"(?:{prefix})?\s*({NUMBER}|\d{{1,3}}(?:,\d{{3}})+)\s*([万亿]?)\s*(\+|以上)?\s*(?:{suffix})?",
        text,
    )
    if not match:
        return None, [f"{label}无法解析"]
    ambiguous = bool(match[3])
    if ambiguous and policy.ambiguous_counts == "review":
        return None, [f"{label}为模糊下限，待人工核查"]
    value = (
        Decimal(match[1].replace(",", ""))
        * {"": 1, "万": 10000, "亿": 100000000}[match[2]]
    )
    if value != value.to_integral_value():
        return None, [f"{label}不是整数"]
    return int(value), ([f"{label}按原文下限估算"] if ambiguous else [])


def timestamp_valid(value: str) -> bool:
    try:
        return (
            datetime.fromisoformat(value.replace("Z", "+00:00")).utcoffset() is not None
        )
    except (ValueError, AttributeError):
        return False


def whitelist_match(row: dict, whitelist: list[dict]) -> str:
    for entry in whitelist:
        if (
            entry.get("platform") != row["platform"]
            or entry.get("ip_name") != row["ip_name"]
        ):
            continue
        # 有 ID 时以 ID 为准，防止冲突的店铺链接误排除。
        if row["shop_id"] and entry.get("shop_id"):
            if row["shop_id"] == entry["shop_id"]:
                return f"shop_id:{row['shop_id']}"
            continue
        if row["shop_url"] and entry.get("shop_url"):
            try:
                if canonical_url(row["shop_url"]) == canonical_url(entry["shop_url"]):
                    return f"shop_url:{canonical_url(row['shop_url'])}"
            except ValueError:
                pass
    return ""


def evaluate_product(
    product: dict,
    *,
    ip_name: str,
    platform: str,
    target_site: str,
    policy: Policy,
    whitelist: list[dict],
) -> dict:
    row: dict = {key: "" for key in HEADERS}
    for key in (
        "title",
        "url",
        "shop_name",
        "shop_id",
        "shop_url",
        "price_raw",
        "sales_raw",
        "comments_raw",
        "crawled_at",
        "detail_crawled_at",
    ):
        value = product.get(key)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"{key} 必须提供原始字符串，不能用数值替代原文")
        row[key] = value or ""
    row.update(
        ip_name=ip_name,
        platform=platform,
        target_site=target_site,
        policy=json.dumps(asdict(policy), ensure_ascii=False, sort_keys=True),
        needs_detail=False,
    )
    issues = []
    identity_ok = True
    for key, label in (("title", "商品名称"), ("shop_name", "店铺名称")):
        if not row[key].strip():
            issues.append(f"{label}缺失")
            identity_ok = False
    for key, label in (("url", "商品链接"), ("shop_url", "店铺链接")):
        if key == "shop_url" and not row[key]:
            continue
        try:
            canonical_url(row[key])
        except ValueError:
            issues.append(f"{label}无效")
            identity_ok = False
    if not row["shop_id"].strip() and not row["shop_url"].strip():
        issues.append("店铺唯一标识缺失，白名单无法可靠核查")
        identity_ok = False
    if not timestamp_valid(row["crawled_at"]):
        issues.append("采集时间缺失或不含时区")
        identity_ok = False
    price, flags = parse_price(row["price_raw"], policy)
    issues.extend(flags)
    sales, flags = parse_count(row["sales_raw"], "销量", policy)
    issues.extend(flags)
    row.update(
        price=price,
        sales=sales,
        comments=None,
        initial_amount=None,
        corrected_sales=None,
        corrected_amount=None,
    )
    unresolved = "待人工核查" if policy.missing_data == "review" else "异常排除"
    row["status"] = unresolved
    if price is not None and sales is not None:
        initial = price * sales
        row["initial_amount"] = initial.quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        if not policy.passes(initial):
            row["status"] = "初始金额未达门槛"
        else:
            comments, flags = parse_count(row["comments_raw"], "评论数", policy)
            issues.extend(flags)
            row["comments"] = comments
            row["needs_detail"] = comments is None
            if comments is not None:
                detail_time_ok = timestamp_valid(row["crawled_at"])
                if not timestamp_valid(row["detail_crawled_at"]):
                    issues.append("详情评论采集时间缺失或不含时区")
                    detail_time_ok = False
                elif detail_time_ok and datetime.fromisoformat(
                    row["detail_crawled_at"].replace("Z", "+00:00")
                ) < datetime.fromisoformat(row["crawled_at"].replace("Z", "+00:00")):
                    issues.append(
                        "详情评论采集时间早于列表采集时间，缓存适用规则未确认，待人工核查"
                    )
                    detail_time_ok = False
                if not detail_time_ok:
                    identity_ok = False
                    row["needs_detail"] = True
                else:
                    corrected_sales = min(sales, comments * 10)
                    corrected = corrected_sales * price
                    row.update(
                        corrected_sales=corrected_sales,
                        corrected_amount=corrected.quantize(
                            Decimal("0.01"), rounding=ROUND_HALF_UP
                        ),
                    )
                    row["status"] = "人工审核候选"
                    if (
                        policy.corrected_filter == "require_threshold"
                        and not policy.passes(corrected)
                    ):
                        row["status"] = "修正金额未达门槛"
    if not identity_ok:
        row["status"] = unresolved
    row["whitelist_match"] = whitelist_match(row, whitelist)
    if row["whitelist_match"]:
        row["status"] = "白名单排除"
        row["needs_detail"] = False
    row["anomalies"] = "；".join(issues)
    return row


def evaluate_request(request: dict) -> list[dict]:
    for key in ("ip_name", "platform", "target_site"):
        if not isinstance(request.get(key), str) or not request[key].strip():
            raise ValueError(f"必须明确输入 {key}")
    canonical_url(request["target_site"])
    if request.get("policy_confirmed") is not True:
        raise ValueError("筛选口径尚未确认：policy_confirmed 必须为 true")
    policy = Policy(**request["policy"])
    whitelist = request.get("whitelist")
    if not isinstance(whitelist, list):
        raise ValueError("必须提供白名单列表；确认无授权店铺时显式提供 []")
    for entry in whitelist:
        if not isinstance(entry, dict) or not all(
            isinstance(entry.get(key), str) and entry[key].strip()
            for key in ("platform", "ip_name")
        ):
            raise ValueError("白名单条目必须包含平台和 IP 名称")
        if not entry.get("shop_id") and not entry.get("shop_url"):
            raise ValueError("白名单必须使用店铺 ID 或链接，不能仅靠店名")
        if entry.get("shop_id") is not None and (
            not isinstance(entry["shop_id"], str) or not entry["shop_id"].strip()
        ):
            raise ValueError("白名单店铺 ID 必须为非空字符串")
        if entry.get("shop_url"):
            canonical_url(entry["shop_url"])
    products = request.get("products")
    if not isinstance(products, list) or not all(
        isinstance(item, dict) for item in products
    ):
        raise ValueError("products 必须为商品对象列表")
    return [
        evaluate_product(
            product,
            ip_name=request["ip_name"],
            platform=request["platform"],
            target_site=request["target_site"],
            policy=policy,
            whitelist=whitelist,
        )
        for product in products
    ]


def export_csv(rows: list[dict]) -> str:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(HEADERS.values())
    for row in rows:
        cells = []
        for key in HEADERS:
            value = row[key]
            # CSV 的引号不能阻止电子表格执行公式；仅导出展示时转义，原始输入不变。
            if isinstance(value, str) and value.lstrip().startswith(
                ("=", "+", "-", "@")
            ):
                value = "'" + value
            cells.append(value)
        writer.writerow(cells)
    return stream.getvalue()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="按已确认的商品口径计算并导出人工审核 CSV"
    )
    parser.add_argument("input", type=Path, help="包含原始商品记录和已确认口径的 JSON")
    parser.add_argument("output", type=Path, help="输出 CSV，不覆盖已有文件")
    args = parser.parse_args()
    try:
        request = json.loads(args.input.read_text(encoding="utf-8-sig"))
        rows = evaluate_request(request)
        with args.output.open("x", encoding="utf-8-sig", newline="") as target:
            target.write(export_csv(rows))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        parser.exit(2, f"导出失败：{exc}\n")
    print(
        json.dumps(
            {
                "rows": len(rows),
                "candidates": sum(row["status"] == "人工审核候选" for row in rows),
                "needs_detail": sum(row["needs_detail"] for row in rows),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
