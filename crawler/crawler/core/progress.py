"""按采集范围持久保存页码和商品；数据与下一页一起原子提交。"""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path

from crawler.core.jsonl import write_json_atomic
from crawler.models import Item, ItemPatch, item_json_dict
from crawler.core.schema import apply_patch


class DeadlineReached(Exception):
    """常规采集截止；已产生的补采仍需收尾。"""


_supplemental = ContextVar("crawler_supplemental", default=False)


@contextmanager
def supplemental_work():
    """仅当前调用链的补采不受常规截止影响，不修改并发平台的环境。"""
    token = _supplemental.set(True)
    try:
        yield
    finally:
        _supplemental.reset(token)


def remaining_seconds():
    value = os.environ.get("CRAWLER_ENDS_AT")
    if not value:
        return None
    deadline = datetime.fromisoformat(value)
    return max(0.0, (deadline - datetime.now(deadline.tzinfo)).total_seconds())


def check_deadline():
    if _supplemental.get():
        return
    remaining = remaining_seconds()
    if remaining is not None and remaining <= 0:
        raise DeadlineReached("到达常规采集截止时间，停止新搜索并等待补采收尾")


class SearchProgress:
    def __init__(self, state_root, platform, matrix, *, continuous=False):
        scope = {
            "version": 1,
            "platform": platform,
            "keywords": matrix,
            "continuous": continuous,
        }
        # 工作台每个任务是独立一轮；同一任务内重试仍使用相同检查点。
        # 只隔离进度，不改变共用的每日额度、登录态和风控状态目录。
        task_scope = os.environ.get("CRAWLER_PROGRESS_SCOPE", "").strip()
        if task_scope:
            scope["task_scope"] = task_scope
        fingerprint = hashlib.sha256(
            json.dumps(scope, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:24]
        self.path = Path(state_root) / "progress" / f"{platform}-{fingerprint}.json"
        self.state = {
            "scope": scope,
            "active": True,
            "keywords": {},
            "rows": {},
            "status": "running",
            "round": 1,
        }
        self.resumed = False
        if self.path.exists():
            # 损坏的进度不能静默当作第一页，保留原文件并报告。
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            if saved.get("scope") != scope:
                raise ValueError(f"采集进度范围不匹配: {self.path}")
            if saved.get("active"):
                self.state = saved
                self.resumed = True

    @staticmethod
    def key(keyword, ip_code):
        return json.dumps([keyword, ip_code], ensure_ascii=False)

    def position(self, keyword, ip_code):
        return self.state["keywords"].get(self.key(keyword, ip_code), {})

    def begin(self, keyword, ip_code, page):
        self.state["current"] = {"keyword": keyword, "ip_code": ip_code, "page": page}
        self.save()

    def commit(
        self,
        keyword,
        ip_code,
        page,
        items,
        *,
        done=False,
        signature=None,
        stop_reason=None,
    ):
        self.update_items(items, save=False)
        signatures = list(self.position(keyword, ip_code).get("signatures", []))
        if signature:
            digest = self.signature_digest(signature)
            if digest not in signatures:
                signatures.append(digest)
        self.state["keywords"][self.key(keyword, ip_code)] = {
            "last_completed_page": page,
            "next_page": page + 1,
            "done": done,
            "signature": signature,
            "signatures": signatures,
            "stop_reason": stop_reason,
        }
        self.save()

    @staticmethod
    def signature_digest(signature):
        return hashlib.sha256(json.dumps(sorted(signature)).encode()).hexdigest()

    def block(self, keyword, ip_code, page, reason):
        position = self.state["keywords"].setdefault(self.key(keyword, ip_code), {})
        position.update(
            done=False, next_page=page, stop_reason="pagination_blocked", reason=reason
        )
        self.save()

    def finish_keyword(self, keyword, ip_code, *, reason=None):
        position = self.state["keywords"].setdefault(self.key(keyword, ip_code), {})
        position["done"] = True
        if reason:
            position["stop_reason"] = reason
        self.save()

    def update_items(self, items, *, save=True):
        for item in items:
            patch = self.state.get("detail_patches", {}).get(self.detail_key(item))
            if patch:
                item = apply_patch(item, ItemPatch.model_validate(patch))
            row = item_json_dict(item)
            key = json.dumps([item.platform, item.product_id, item.ip_code])
            previous = self.state["rows"].get(key, {})
            if not row.get("image_path") and previous.get("image_path"):
                row["image_path"] = previous["image_path"]
            self.state["rows"][key] = row
        if save:
            self.save()

    @staticmethod
    def detail_key(item):
        return f"{item.platform}:{item.product_id}"

    def apply_detail_patch(self, patch):
        patches = self.state.setdefault("detail_patches", {})
        previous = patches.get(self.detail_key(patch), {})
        patches[self.detail_key(patch)] = {
            **previous,
            **{
                key: value
                for key, value in patch.model_dump(mode="json").items()
                if value is not None and value != "" and value != []
            },
        }
        self.update_items([Item.model_validate(row) for row in self.rows])

    def describe(self):
        current = self.state.get("current") or {}
        keyword = current.get("keyword")
        if keyword is None:
            return "尚未开始搜索，下次从第一页继续"
        position = self.position(keyword, current.get("ip_code"))
        return (
            f"关键词 {keyword!r} 已完成第 {position.get('last_completed_page', 0)} 页，"
            f"下次从第 {position.get('next_page', 1)} 页继续"
        )

    def next_round(self):
        self.state["keywords"] = {}
        self.state["round"] += 1
        self.save()

    def finish(self, status):
        self.state["status"] = status
        self.state["active"] = status != "ok" or bool(self.state.get("detail_pending"))
        self.save()

    def save(self):
        self.state["updated_at"] = datetime.now().isoformat()
        write_json_atomic(self.path, self.state, mode=0o600)

    @property
    def rows(self):
        return list(self.state["rows"].values())
