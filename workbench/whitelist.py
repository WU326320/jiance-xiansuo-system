"""IP 档案与白名单。

白名单条目有两种归属：
- ``scope_type='ip'``：挂在某个 IP 档案下（开任务时增删）；
- ``scope_type='company'``：按授权公司共享，任务可勾选是否并入。

匹配规则完全复用 backend 的 ``shop_key``：平台组 + 规范化店名。
``shop_id`` 只用于区分同名不同店；双方都有 ID 且不同则不排除。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

from workbench import backend_bridge as bridge
from workbench.auth import AuthError, PermissionError_, ROLE_ADMIN, ROLE_SUPERVISOR
from workbench.config import WorkbenchConfig
from workbench.db import Database, now_text
from workbench.security import normalize_username


@dataclass(frozen=True)
class WhitelistMatch:
    matched: bool
    marker: str
    entry: dict[str, Any] | None = None
    risk: bool = False


def _now() -> str:
    return now_text()


def _norm_shop(value: object | None) -> str:
    return bridge.normalize_shop(bridge.decode_shop_name(value))


def _group(value: str | None) -> str:
    group = bridge.platform_group(value)
    if group not in ("taobao", "jd", "1688"):
        raise AuthError(f"平台组不支持：{value}")
    return group


def _clean_shop_id(value: object | None) -> str:
    text = str(value or "").strip()
    if text.lower() in {"-", "0", "null", "none", "name:"}:
        return ""
    return text


def normalize_entry(entry: dict[str, Any], *, default_company: str = "") -> dict[str, Any]:
    scope = str(entry.get("scope_type") or entry.get("scope") or "ip").strip().lower()
    if scope not in ("ip", "company"):
        raise AuthError("白名单归属只能是 ip 或 company")
    group = _group(entry.get("platform_group") or entry.get("platform"))
    shop_name = str(bridge.decode_shop_name(entry.get("shop_name")) or "").strip()
    if not shop_name:
        raise AuthError("白名单店铺名不能为空")
    company = str(entry.get("company_code") or default_company or "").strip().lower()
    if scope == "company" and not company:
        raise AuthError("按公司共享的白名单必须填写公司代码")
    return {
        "scope_type": scope,
        "company_code": company,
        "platform_group": group,
        "shop_name": shop_name,
        "shop_name_norm": _norm_shop(shop_name),
        "shop_id": _clean_shop_id(entry.get("shop_id")),
        "note": str(entry.get("note") or "").strip() or None,
    }


def entry_row_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "ip_id": row.get("ip_id"),
        "scope_type": row["scope_type"],
        "company_code": row.get("company_code") or "",
        "platform_group": row["platform_group"],
        "shop_name": row["shop_name"],
        "shop_name_norm": row["shop_name_norm"],
        "shop_id": row.get("shop_id") or "",
        "note": row.get("note") or "",
        "active": bool(row.get("active")),
        "created_at": row.get("created_at"),
    }


def match_whitelist(
    entries: Iterable[dict[str, Any]],
    platform: str | None,
    shop_name: object | None,
    shop_id: object | None = None,
) -> WhitelistMatch:
    """判断一条线索是否命中白名单。"""

    norm = _norm_shop(shop_name)
    if not norm:
        return WhitelistMatch(False, "店铺名缺失")
    group = bridge.platform_group(platform)
    if group not in ("taobao", "jd", "1688"):
        return WhitelistMatch(False, f"不支持平台组:{group}")
    product_id = _clean_shop_id(shop_id)
    same_name = [
        entry
        for entry in entries
        if entry.get("platform_group") == group
        and entry.get("shop_name_norm") == norm
        and entry.get("active", True)
    ]
    if not same_name:
        return WhitelistMatch(False, "未命中白名单")

    id_entries = [
        entry for entry in same_name if _clean_shop_id(entry.get("shop_id"))
    ]
    if id_entries:
        if product_id:
            exact = [
                entry
                for entry in id_entries
                if _clean_shop_id(entry.get("shop_id")) == product_id
            ]
            if exact:
                return WhitelistMatch(True, "按店铺ID+店名排除", exact[0], risk=False)
            return WhitelistMatch(False, "同名不同店", id_entries[0], risk=True)
        # 商品没有 ID，只能按店名；带 ID 的白名单仍会命中，但必须提示同名风险。
        return WhitelistMatch(
            True, "按店铺名排除-存在同名风险", id_entries[0], risk=True
        )

    return WhitelistMatch(True, "按店铺名排除-存在同名风险", same_name[0], risk=True)


class IPService:
    def __init__(self, config: WorkbenchConfig, db: Database):
        self.config = config
        self.db = db

    def find(self, name: str) -> dict[str, Any] | None:
        norm = normalize_username(name)
        if not norm:
            return None
        return self.db.fetchone("SELECT * FROM ip_assets WHERE name_norm=?", (norm,))

    def get(self, ip_id: int) -> dict[str, Any]:
        row = self.db.fetchone("SELECT * FROM ip_assets WHERE id=?", (int(ip_id),))
        if row is None:
            raise AuthError("IP 档案不存在", code="not_found", status=404)
        return row

    def get_or_create(
        self,
        name: str,
        *,
        company_code: str = "",
        company_name: str = "",
        keywords: list[str] | None = None,
        ref_images: list[str] | None = None,
        actor: dict[str, Any] | None = None,
        source_ip: str = "",
    ) -> dict[str, Any]:
        display = str(name or "").strip()
        norm = normalize_username(display)
        if not norm:
            raise AuthError("IP 名不能为空")
        existing = self.find(display)
        now = _now()
        if existing is None:
            with self.db.transaction() as connection:
                cursor = connection.execute(
                    """INSERT INTO ip_assets(name, name_norm, company_code, company_name,
                        keywords_json, ref_images_json, active, created_at, updated_at, created_by)
                       VALUES(?,?,?,?,?,?,1,?,?,?)""",
                    (
                        display,
                        norm,
                        str(company_code or "").strip().lower(),
                        str(company_name or "").strip(),
                        _json(keywords or []),
                        _json(ref_images or []),
                        now,
                        now,
                        (actor or {}).get("id"),
                    ),
                )
                ip_id = int(cursor.lastrowid)
                connection.execute(
                    """INSERT INTO audit_log(actor_id, actor_name, source_ip, action,
                        object_type, object_id, before_json, after_json, created_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        (actor or {}).get("id"),
                        (actor or {}).get("username", ""),
                        source_ip,
                        "ip.create",
                        "ip",
                        str(ip_id),
                        "{}",
                        '{"name":"%s"}' % display.replace('"', ""),
                        now,
                    ),
                )
            return self.get(ip_id)

        # 已存在：任务当场填写的关键词/公司信息写回档案。
        updates: list[str] = []
        params: list[Any] = []
        if str(company_code or "").strip().lower() != (existing.get("company_code") or ""):
            updates.append("company_code=?")
            params.append(str(company_code or "").strip().lower())
        if str(company_name or "").strip() != (existing.get("company_name") or ""):
            updates.append("company_name=?")
            params.append(str(company_name or "").strip())
        if keywords:
            updates.append("keywords_json=?")
            params.append(_json(keywords))
        if updates:
            updates.append("updated_at=?")
            params.append(now)
            params.append(existing["id"])
            self.db.execute(
                f"UPDATE ip_assets SET {', '.join(updates)} WHERE id=?", tuple(params)
            )
        return self.get(existing["id"])

    def update(
        self,
        actor: dict[str, Any],
        ip_id: int,
        *,
        company_code: str | None = None,
        company_name: str | None = None,
        keywords: list[str] | None = None,
        ref_images: list[str] | None = None,
        active: bool | None = None,
        source_ip: str = "",
    ) -> dict[str, Any]:
        self._require_admin(actor)
        existing = self.get(ip_id)
        fields: dict[str, Any] = {}
        if company_code is not None:
            fields["company_code"] = str(company_code).strip().lower()
        if company_name is not None:
            fields["company_name"] = str(company_name).strip()
        if keywords is not None:
            fields["keywords_json"] = _json(keywords)
        if ref_images is not None:
            fields["ref_images_json"] = _json(ref_images)
        if active is not None:
            fields["active"] = 1 if active else 0
        if not fields:
            return existing
        fields["updated_at"] = _now()
        assignments = ", ".join(f"{key}=?" for key in fields)
        self.db.execute(
            f"UPDATE ip_assets SET {assignments} WHERE id=?",
            tuple(fields.values()) + (int(ip_id),),
        )
        self.db.audit(
            actor_id=actor["id"],
            actor_name=actor["username"],
            source_ip=source_ip,
            action="ip.update",
            object_type="ip",
            object_id=str(ip_id),
            before={key: existing.get(key) for key in fields if key in existing},
            after=fields,
        )
        return self.get(ip_id)

    def list_ips(self, include_inactive: bool = False) -> list[dict[str, Any]]:
        where = "" if include_inactive else "WHERE active=1"
        rows = self.db.fetchall(f"SELECT * FROM ip_assets {where} ORDER BY name_norm")
        for row in rows:
            row["keywords"] = _loads(row.get("keywords_json"))
            row["ref_images"] = _loads(row.get("ref_images_json"))
            row["whitelist_count"] = self.db.fetchone(
                "SELECT COUNT(*) AS n FROM whitelist_entries WHERE ip_id=? AND active=1",
                (row["id"],),
            )["n"]
        return rows

    def entries(self, ip_id: int, *, include_company: bool = True) -> list[dict[str, Any]]:
        ip = self.get(ip_id)
        rows = self.db.fetchall(
            "SELECT * FROM whitelist_entries WHERE ip_id=? AND active=1 ORDER BY id",
            (int(ip_id),),
        )
        entries = [entry_row_to_dict(row) for row in rows]
        if include_company and ip.get("company_code"):
            company_rows = self.db.fetchall(
                """SELECT * FROM whitelist_entries
                   WHERE scope_type='company' AND company_code=? AND active=1 ORDER BY id""",
                (ip["company_code"],),
            )
            entries.extend(entry_row_to_dict(row) for row in company_rows)
        return entries

    def preview(self, name: str) -> dict[str, Any] | None:
        ip = self.find(name)
        if ip is None:
            return None
        return {
            "ip": {
                "id": ip["id"],
                "name": ip["name"],
                "name_norm": ip["name_norm"],
                "company_code": ip["company_code"],
                "company_name": ip["company_name"],
                "keywords": _loads(ip.get("keywords_json")),
                "ref_images": _loads(ip.get("ref_images_json")),
                "active": bool(ip["active"]),
            },
            "whitelist": self.entries(ip["id"], include_company=True),
        }

    def save_task_whitelist(
        self,
        ip_id: int,
        entries: Iterable[dict[str, Any]],
        *,
        actor: dict[str, Any],
        source_ip: str = "",
    ) -> list[dict[str, Any]]:
        """开任务时写回该 IP 的白名单（软删除旧条目，保留审计）。"""

        ip = self.get(ip_id)
        now = _now()
        normalized: list[dict[str, Any]] = []
        for raw in entries:
            item = normalize_entry(raw, default_company=ip.get("company_code") or "")
            if item["scope_type"] == "ip":
                normalized.append(item)
        before = [
            entry_row_to_dict(row)
            for row in self.db.fetchall(
                "SELECT * FROM whitelist_entries WHERE ip_id=? AND active=1 AND scope_type='ip' ORDER BY id",
                (int(ip_id),),
            )
        ]
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE whitelist_entries SET active=0, deleted_at=? WHERE ip_id=? AND scope_type='ip' AND active=1",
                (now, int(ip_id)),
            )
            for item in normalized:
                existing = connection.execute(
                    """SELECT id FROM whitelist_entries
                       WHERE ip_id=? AND scope_type='ip' AND platform_group=?
                         AND shop_name_norm=? AND shop_id=? LIMIT 1""",
                    (int(ip_id), item["platform_group"], item["shop_name_norm"], item["shop_id"]),
                ).fetchone()
                if existing:
                    connection.execute(
                        """UPDATE whitelist_entries SET shop_name=?, note=?, active=1,
                           deleted_at=NULL WHERE id=?""",
                        (item["shop_name"], item["note"], existing["id"]),
                    )
                else:
                    connection.execute(
                        """INSERT INTO whitelist_entries(ip_id, scope_type, company_code,
                            platform_group, shop_name, shop_name_norm, shop_id, note,
                            active, created_at, created_by)
                           VALUES(?, 'ip', '', ?, ?, ?, ?, ?, 1, ?, ?)""",
                        (
                            int(ip_id),
                            item["platform_group"],
                            item["shop_name"],
                            item["shop_name_norm"],
                            item["shop_id"],
                            item["note"],
                            now,
                            actor.get("id"),
                        ),
                    )
            connection.execute(
                """INSERT INTO audit_log(actor_id, actor_name, source_ip, action,
                    object_type, object_id, before_json, after_json, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    actor.get("id"),
                    actor.get("username", ""),
                    source_ip,
                    "whitelist.save",
                    "ip",
                    str(ip_id),
                    _json(before),
                    _json(normalized),
                    now,
                ),
            )
        return self.entries(ip_id, include_company=True)

    def add_company_entries(
        self,
        actor: dict[str, Any],
        company_code: str,
        entries: Iterable[dict[str, Any]],
        *,
        source_ip: str = "",
    ) -> list[dict[str, Any]]:
        self._require_admin(actor)
        company = str(company_code or "").strip().lower()
        if not company:
            raise AuthError("公司代码不能为空")
        now = _now()
        with self.db.transaction() as connection:
            for raw in entries:
                item = normalize_entry(
                    {**raw, "scope_type": "company", "company_code": company}
                )
                existing = connection.execute(
                    """SELECT id FROM whitelist_entries
                       WHERE scope_type='company' AND company_code=? AND platform_group=?
                         AND shop_name_norm=? AND shop_id=? LIMIT 1""",
                    (company, item["platform_group"], item["shop_name_norm"], item["shop_id"]),
                ).fetchone()
                if existing:
                    connection.execute(
                        "UPDATE whitelist_entries SET shop_name=?, note=?, active=1, deleted_at=NULL WHERE id=?",
                        (item["shop_name"], item["note"], existing["id"]),
                    )
                else:
                    connection.execute(
                        """INSERT INTO whitelist_entries(ip_id, scope_type, company_code,
                            platform_group, shop_name, shop_name_norm, shop_id, note,
                            active, created_at, created_by)
                           VALUES(NULL, 'company', ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                        (
                            company,
                            item["platform_group"],
                            item["shop_name"],
                            item["shop_name_norm"],
                            item["shop_id"],
                            item["note"],
                            now,
                            actor.get("id"),
                        ),
                    )
            connection.execute(
                """INSERT INTO audit_log(actor_id, actor_name, source_ip, action,
                    object_type, object_id, before_json, after_json, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    actor.get("id"),
                    actor.get("username", ""),
                    source_ip,
                    "whitelist.company_add",
                    "company",
                    company,
                    "{}",
                    _json(list(entries)),
                    now,
                ),
            )
        return self.db.fetchall(
            "SELECT * FROM whitelist_entries WHERE scope_type='company' AND company_code=? AND active=1 ORDER BY id",
            (company,),
        )

    def remove_entry(
        self, actor: dict[str, Any], entry_id: int, *, source_ip: str = ""
    ) -> None:
        self._require_admin(actor)
        entry = self.db.fetchone("SELECT * FROM whitelist_entries WHERE id=?", (int(entry_id),))
        if entry is None:
            raise AuthError("白名单不存在", code="not_found", status=404)
        self.db.execute(
            "UPDATE whitelist_entries SET active=0, deleted_at=? WHERE id=?",
            (_now(), int(entry_id)),
        )
        self.db.audit(
            actor_id=actor["id"],
            actor_name=actor["username"],
            source_ip=source_ip,
            action="whitelist.remove",
            object_type="whitelist",
            object_id=str(entry_id),
            before=entry_row_to_dict(entry),
        )

    @staticmethod
    def _require_admin(actor: dict[str, Any] | None) -> None:
        if not actor or actor.get("role") not in (ROLE_SUPERVISOR, ROLE_ADMIN):
            raise PermissionError_("只有管理员可以修改 IP 档案/公司白名单")


def build_whitelist_text(entries: Iterable[dict[str, Any]]) -> str:
    lines = [
        "# 工作台本次任务白名单：平台,店铺名[,店铺ID]",
        "# 平台组 taobao 同时代表淘宝/天猫；店铺名按 NFKC、去空白、英文小写匹配。",
    ]
    seen: set[tuple[str, str, str]] = set()
    for entry in entries:
        key = (
            entry.get("platform_group") or "",
            entry.get("shop_name_norm") or "",
            _clean_shop_id(entry.get("shop_id")),
        )
        if key in seen:
            continue
        seen.add(key)
        shop = str(entry.get("shop_name") or "").replace(",", "，").strip()
        if not shop:
            continue
        shop_id = _clean_shop_id(entry.get("shop_id"))
        lines.append(f"{key[0]},{shop}" + (f",{shop_id}" if shop_id else ""))
    return "\n".join(lines) + "\n"


def _json(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, default=str)


def _loads(value: Any) -> Any:
    import json

    try:
        return json.loads(value) if value else []
    except (TypeError, ValueError):
        return []


__all__ = [
    "IPService",
    "WhitelistMatch",
    "build_whitelist_text",
    "entry_row_to_dict",
    "match_whitelist",
    "normalize_entry",
]
