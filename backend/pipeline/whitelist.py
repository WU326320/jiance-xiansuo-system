"""白名单文件加载与同步。

2026-11-06 口径：白名单键是 ``平台组 + 规范化店名 + IP``。文件格式为
``平台,店铺名[,shop_id]``，``#`` 开头为注释；1688 店名先 URL 解码。
旧的 CSV（含 ``platform,shop_id,ip_code`` 列）继续兼容，避免存量文件失效。

运行时人工录入的白名单文件在 ``whitelist/<规范化IP>.txt``；本模块只负责
把文件内容同步进 ``whitelist`` 表。文件不存在时保留现有白名单。
"""

from __future__ import annotations

import csv
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.config import Settings
from pipeline.models import Whitelist
from pipeline.shopkey import (
    decode_shop_name,
    normalize_ip_name,
    normalize_shop_name,
    platform_group,
    whitelist_sentinel,
)

WHITELIST_TEMPLATE = """# 白名单：每行「平台,店铺名」，# 开头为注释。
# 平台可填 taobao / tmall / jd / 1688（taobao 与 tmall 同属淘宝组）。
# 店铺名按 NFKC、去空白、英文小写后匹配；1688 店名会自动 URL 解码。
# 例：
# taobao,某某旗舰店
# jd,某某专营店
"""


@dataclass
class WhitelistSyncStats:
    added: int = 0
    updated: int = 0
    removed: int = 0
    skipped: int = 0


@dataclass(frozen=True)
class WhitelistEntry:
    platform_group: str
    shop_name: str
    shop_name_norm: str
    shop_id: str
    ip_code: str = ""
    note: str | None = None


def resolve_whitelist_path(settings: Settings) -> Path:
    configured = str(settings.get("whitelist_file", "whitelist.csv") or "").strip()
    if not configured:
        configured = "whitelist.csv"
    candidate = Path(configured).expanduser()
    if candidate.is_absolute():
        return candidate
    options = [(settings.path.parent / candidate).resolve(), (settings.root / candidate).resolve()]
    for path in options:
        if path.exists():
            return path
    return options[0]


def _truthy(value: Any, default: bool = True) -> bool:
    if value in (None, ""):
        return default
    return str(value).strip().lower() not in {"0", "false", "no", "n", "否", "禁用"}


def _split_line(text: str) -> list[str]:
    if "," in text:
        return [part.strip() for part in text.split(",")]
    return [part.strip() for part in re.split(r"\t+", text)]


def parse_whitelist_text(text: str) -> list[WhitelistEntry]:
    """解析「平台,店铺名[,shop_id]」文本；忽略注释和空行。"""
    entries: list[WhitelistEntry] = []
    seen: set[tuple[str, str, str]] = set()
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = _split_line(line)
        if not parts:
            continue
        platform = parts[0].strip().lower()
        group = platform_group(platform)
        if group not in ("taobao", "jd", "1688"):
            continue
        shop_name = decode_shop_name(parts[1]) if len(parts) > 1 else None
        if not shop_name:
            continue
        shop_id = parts[2].strip() if len(parts) > 2 else ""
        if shop_id.lower() in {"-", "0", "null", "none"}:
            shop_id = ""
        norm = normalize_shop_name(shop_name)
        key = (group, norm, shop_id)
        if key in seen:
            continue
        seen.add(key)
        entries.append(
            WhitelistEntry(
                platform_group=group,
                shop_name=shop_name,
                shop_name_norm=norm,
                shop_id=shop_id or whitelist_sentinel(shop_name),
            )
        )
    return entries


def _parse_legacy_csv(text: str) -> list[WhitelistEntry]:
    entries: list[WhitelistEntry] = []
    reader = csv.DictReader(text.splitlines())
    if not reader.fieldnames:
        return entries
    for row in reader:
        normalized = {str(key or "").strip().lower(): value for key, value in row.items()}
        platform = str(
            normalized.get("platform")
            or normalized.get("平台")
            or ""
        ).strip().lower()
        group = platform_group(platform)
        if group not in ("taobao", "jd", "1688"):
            continue
        shop_id = str(
            normalized.get("shop_id")
            or normalized.get("shop")
            or normalized.get("店铺id")
            or normalized.get("店铺ID")
            or ""
        ).strip()
        shop_name = decode_shop_name(
            normalized.get("shop_name")
            or normalized.get("店铺名")
            or normalized.get("name")
            or ""
        )
        if not shop_id or not _truthy(normalized.get("active") or normalized.get("enabled") or ""):
            continue
        entries.append(
            WhitelistEntry(
                platform_group=group,
                shop_name=shop_name or "",
                shop_name_norm=normalize_shop_name(shop_name),
                shop_id=shop_id,
                ip_code=normalize_ip_name(
                    normalized.get("ip_code")
                    or normalized.get("ip")
                    or normalized.get("IP")
                    or ""
                ),
                note=str(normalized.get("reason") or normalized.get("note") or "") or None,
            )
        )
    return entries


def read_whitelist_file(path: Path) -> list[WhitelistEntry]:
    text = path.read_text(encoding="utf-8-sig")
    first_line = text.splitlines()[0].lower() if text.splitlines() else ""
    if first_line.startswith("platform,") or "shop_id" in first_line:
        entries = _parse_legacy_csv(text)
        if entries:
            return entries
    return parse_whitelist_text(text)


def _entry_key(entry: WhitelistEntry, ip_code: str) -> tuple[str, str, str]:
    return (entry.platform_group, entry.shop_id, ip_code)


def sync_whitelist(
    session: Session,
    settings: Settings,
    *,
    logger: logging.Logger | None = None,
    default_ip: str | None = None,
) -> WhitelistSyncStats:
    """读取白名单文件并同步到 ``whitelist`` 表。"""
    logger = logger or logging.getLogger("pipeline")
    stats = WhitelistSyncStats()
    path = resolve_whitelist_path(settings)
    if not path.exists():
        logger.info("白名单文件不存在，保留现有白名单: %s", path)
        return stats

    try:
        entries = read_whitelist_file(path)
    except (OSError, csv.Error, UnicodeDecodeError) as exc:
        logger.error("读取白名单文件失败，保留现有白名单 %s: %s", path, exc)
        return stats

    default = normalize_ip_name(
        default_ip or settings.get("run.ip_code") or settings.get("ip_code") or ""
    )
    desired: dict[tuple[str, str, str], WhitelistEntry] = {}
    for entry in entries:
        entry_ip = entry.ip_code or default
        if not entry_ip:
            continue
        desired[(entry.platform_group, entry.shop_id, entry_ip)] = entry
    try:
        head = path.read_text(encoding="utf-8-sig").splitlines()
        first_line = head[0].lower() if head else ""
    except OSError:
        first_line = ""
    legacy_header = first_line.startswith("platform,") and "ip_code" in first_line

    existing = {
        (row.platform, row.shop_id, row.ip_code): row
        for row in session.scalars(select(Whitelist)).all()
    }
    if not desired:
        if default:
            affected_ips = {default}
        elif legacy_header:
            # 旧 CSV 只剩表头：按原口径清空（文件自身带 ip_code 列）。
            affected_ips = {row.ip_code for row in existing.values()}
        else:
            logger.warning("白名单文件没有可同步条目（缺少 IP 或缺店名）: %s", path)
            return stats
    else:
        affected_ips = {ip for _group, _shop, ip in desired}

    for key, entry in desired.items():
        row = existing.get(key)
        if row is None:
            session.add(
                Whitelist(
                    platform=entry.platform_group,
                    shop_id=entry.shop_id,
                    shop_name=entry.shop_name or None,
                    shop_name_norm=entry.shop_name_norm or None,
                    ip_code=entry.ip_code or default,
                    note=entry.note,
                )
            )
            stats.added += 1
        else:
            note = entry.note
            if (row.note or None) != note or (row.shop_name or "") != entry.shop_name:
                row.note = note
                row.shop_name = entry.shop_name or None
                row.shop_name_norm = entry.shop_name_norm or None
                stats.updated += 1
    for key, row in existing.items():
        if row.ip_code in affected_ips and key not in desired:
            session.delete(row)
            stats.removed += 1
    session.flush()
    logger.info(
        "白名单同步完成 file=%s ip=%s added=%d updated=%d removed=%d skipped=%d",
        path,
        ",".join(sorted(affected_ips)),
        stats.added,
        stats.updated,
        stats.removed,
        stats.skipped,
    )
    return stats


__all__ = [
    "WHITELIST_TEMPLATE",
    "WhitelistEntry",
    "WhitelistSyncStats",
    "parse_whitelist_text",
    "read_whitelist_file",
    "resolve_whitelist_path",
    "sync_whitelist",
]
