"""限定E38E京东演练的店名恢复；默认预览，--apply备份后更新任务与断点。"""

from __future__ import annotations

import argparse
from contextlib import closing
from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "crawler")]
TASK = "WB20261110_104604_E38E"
SOURCE = ROOT / "crawler/out/runs/7c3a990fddaa466c8f97342719bb0da9/2026-11-10/jd.jsonl"
DETAILS = ROOT / "crawler/out/2026-11-10/jd_detail.jsonl"
PROGRESS = ROOT / "crawler/state/progress/jd-267475c1d33b24107d3d70f8.json"


def read_rows(path):
    return [
        json.loads(line)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def evidence_patches(rows, details):
    """只使用同平台同商品的非空、一致店名证据。"""
    wanted = {
        r["product_id"]
        for r in rows
        if r["platform"] == "jd" and not str(r.get("shop_name") or "").strip()
    }
    patches = {}
    for pid in wanted:
        evidence = [
            r
            for r in details
            if r.get("platform") == "jd"
            and r.get("product_id") == pid
            and str(r.get("shop_name") or "").strip()
        ]
        names = {r["shop_name"].strip() for r in evidence}
        if len(names) != 1 or names.intersection(
            {"京东POP", "POP", "京东自营", "null", "0"}
        ):
            raise ValueError(f"商品{pid}没有唯一真实店名证据")
        ids = {str(r["shop_id"]) for r in evidence if r.get("shop_id")}
        if len(ids) > 1:
            raise ValueError(f"商品{pid}店铺ID证据冲突")
        patches[pid] = {
            "platform": "jd",
            "product_id": pid,
            "shop_name": names.pop(),
            "shop_id": next(iter(ids), None),
        }
    return patches


def recover_rows(rows, patches):
    from crawler.runner import _inline_detail_missing_fields

    result = deepcopy(rows)
    for row in result:
        if row["platform"] != "jd":
            continue
        patch = patches.get(row["product_id"])
        if patch:
            existing = str(row.get("shop_name") or "").strip()
            if existing and existing != patch["shop_name"]:
                raise ValueError(f"商品{row['product_id']}已有店名与证据冲突")
            existing_id = str(row.get("shop_id") or "").strip()
            evidence_id = str(patch.get("shop_id") or "").strip()
            if existing_id and evidence_id and existing_id != evidence_id:
                raise ValueError(f"商品{row['product_id']}已有店铺ID与证据冲突")
            row["shop_name"] = patch["shop_name"]
            if not row.get("shop_id"):
                row["shop_id"] = patch.get("shop_id")
        flags = [
            f
            for f in row.get("flags", [])
            if f != "店铺名缺失" and not f.startswith("金额补采：")
        ]
        if not str(row.get("shop_name") or "").strip():
            flags.append("店铺名缺失")
        missing = _inline_detail_missing_fields(row)
        if missing and row.get("listed", True):
            flags.append(f"金额补采：等待详情补采；缺少{','.join(missing)}，待核验")
        row["flags"] = flags
    return result


def repair_excel(source, target, names_by_url):
    from openpyxl import load_workbook

    source, target = Path(source), Path(target)
    if source.resolve() == target.resolve() or target.exists():
        raise ValueError("修复版必须使用新路径，保留原报表")
    book = load_workbook(source)
    try:
        sheet = book.active
        assert sheet is not None, "报表没有活动工作表"
        before = list(sheet.values)
        if len(before[0]) != 10 or before[0][2] != "店铺名":
            raise ValueError("报表不是预期的十列链接明细")
        changed = set()
        for cells in sheet.iter_rows(min_row=2):
            url = cells[5].value
            if url in names_by_url and not str(cells[2].value or "").strip():
                cells[2].value = names_by_url[url]
                changed.add(url)
        if changed != set(names_by_url):
            raise ValueError("缺名商品与报表明细不一致")
        book.save(target)
    finally:
        book.close()
    check = load_workbook(target, read_only=True)
    try:
        checked_sheet = check.active
        assert checked_sheet is not None, "修复报表没有活动工作表"
        after = list(checked_sheet.values)
        assert len(before) == len(after)
        for old, new in zip(before, after):
            assert old[:2] + old[3:] == new[:2] + new[3:]
            assert new[2] == (names_by_url[old[5]] if old[5] in changed else old[2])
        links = [r for r in after[1:] if r[5]]
        assert links and all(str(r[2] or "").strip() for r in links)
        return {
            "excel_rows": len(links),
            "names_filled": len(changed),
            "missing_names": 0,
        }
    finally:
        check.close()


def main(apply=False):
    from crawler.core.jsonl import write_json_atomic
    from crawler.runner import _inline_detail_missing_fields
    from workbench.db import Database
    from utils.finalize_history_recovery import backup

    for folder in (
        "doc",
        "prototype",
        "project/frontend",
        "project/backend",
        "database",
        "utils",
    ):
        assert (ROOT / folder).is_dir(), folder
    database = Database(ROOT / "workbench/data/workbench.db")
    original = database.fetchone("SELECT * FROM tasks WHERE task_no=?", (TASK,))
    assert (
        original and original["mode"] == "dry_run" and original["status"] == "partial"
    )
    if json.loads(original["result_json"]).get("shop_name_recovery"):
        raise ValueError("该任务已经恢复，请直接验收，不要重复应用")
    assert not database.fetchone(
        "SELECT task_no FROM tasks WHERE status IN ('running','queued') LIMIT 1"
    )
    rows = read_rows(SOURCE)
    patches = evidence_patches(rows, read_rows(DETAILS))
    assert len(rows) == 60 and len(patches) == 8
    fixed = recover_rows(rows, patches)
    assert all(r.get("shop_name") for r in fixed)
    state_bytes = PROGRESS.read_bytes()
    state = json.loads(state_bytes)
    assert state["scope"]["platform"] == "jd"
    state_rows = recover_rows(list(state["rows"].values()), patches)
    assert {r["product_id"] for r in state_rows} == {r["product_id"] for r in rows}
    state["rows"] = dict(zip(state["rows"], state_rows))
    for pid, patch in patches.items():
        key = f"jd:{pid}"
        previous = state.setdefault("detail_patches", {}).get(key, {})
        state["detail_patches"][key] = {**previous, **patch}
        state.setdefault("detail_results", {})[key] = {
            "reason": "真实详情店名已补齐",
            "source": str(DETAILS),
        }
    state["detail_pending"] = list(
        dict.fromkeys(
            f"jd:{r['product_id']}"
            for r in state_rows
            if r.get("listed", True) and _inline_detail_missing_fields(r)
        )
    )
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    state["updated_at"] = datetime.now().isoformat()
    old_excel = Path(original["excel_path"])
    old_hash = hashlib.sha256(old_excel.read_bytes()).hexdigest()
    excel = old_excel.parent / f"京东店名修复_{TASK}_{stamp}.xlsx"
    expected = {
        r["url"]: patches[r["product_id"]]["shop_name"]
        for r in rows
        if r["product_id"] in patches
    }
    counts = repair_excel(old_excel, excel, expected)
    corrected = ROOT / "doc" / f"京东店名修复_{TASK}_{stamp}.jsonl"
    corrected.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in fixed),
        encoding="utf-8",
    )
    result = json.loads(original["result_json"])
    result["platforms"]["jd"]["missing_fields"]["shop_name"] = 0
    result["platforms"]["jd"]["error"] = "；".join(
        part
        for part in result["platforms"]["jd"].get("error", "").split("；")
        if "店名待补齐" not in part
    )
    result["errors"] = [
        e for e in result.get("errors", []) if not e.startswith("京东：店名待补齐")
    ]
    result["excel_path"] = str(excel)
    result["shop_name_recovery"] = {
        "at": stamp,
        "count": len(patches),
        "original_excel": str(old_excel),
        "source": str(SOURCE),
        "details": str(DETAILS),
        "corrected_jsonl": str(corrected),
        "note": "仅补店名，保留原任务截止状态、金额、明细和候选数量",
    }
    report = {
        "applied": apply,
        "task_no": TASK,
        **counts,
        "source_items": len(fixed),
        "source_missing_names": 0,
        "pending_amount_items": len(state["detail_pending"]),
        "excel": str(excel),
        "original_excel": str(old_excel),
        "original_excel_sha256": old_hash,
        "patches": patches,
    }
    if apply:
        db_backup = ROOT / "database" / f"workbench-before-jd-shops-{stamp}.db"
        state_backup = ROOT / "database" / f"jd-progress-before-shops-{stamp}.json"
        backup(database.path, db_backup)
        shutil.copyfile(PROGRESS, state_backup)
        with database.transaction() as connection:
            assert not connection.execute(
                "SELECT task_no FROM tasks WHERE status IN ('running','queued') LIMIT 1"
            ).fetchone()
            assert PROGRESS.read_bytes() == state_bytes, "断点已被其他进程更新"
            updated = connection.execute(
                "UPDATE tasks SET result_json=?,excel_path=? WHERE task_no=? AND result_json=? AND excel_path=? AND status='partial'",
                (
                    json.dumps(result, ensure_ascii=False),
                    str(excel),
                    TASK,
                    original["result_json"],
                    original["excel_path"],
                ),
            )
            assert updated.rowcount == 1, "任务已被其他进程更新"
            connection.execute(
                "INSERT INTO audit_log(actor_name,source_ip,action,object_type,object_id,before_json,after_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    "本地店名修复",
                    "127.0.0.1",
                    "task.jd_shop_recovery",
                    "task",
                    TASK,
                    original["result_json"],
                    json.dumps(result, ensure_ascii=False),
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
            write_json_atomic(PROGRESS, state, mode=0o600)
        report.update(backup=str(db_backup), progress_backup=str(state_backup))
        saved = database.fetchone("SELECT * FROM tasks WHERE task_no=?", (TASK,))
        assert saved is not None, "恢复后的任务记录不存在"
        assert saved["excel_path"] == str(excel)
        assert (saved["status"], saved["ended_at"]) == (
            original["status"],
            original["ended_at"],
        )
        with closing(sqlite3.connect(database.path)) as conn:
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert hashlib.sha256(old_excel.read_bytes()).hexdigest() == old_hash
    report_path = ROOT / "doc" / f"京东店名修复验收-{stamp}.json"
    write_json_atomic(report_path, report)
    print(
        json.dumps({**report, "report": str(report_path)}, ensure_ascii=False, indent=2)
    )
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    main(parser.parse_args().apply)
