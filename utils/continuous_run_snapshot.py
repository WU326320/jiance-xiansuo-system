"""按采集运行ID记录真实四字段覆盖快照，只读原始任务和采集数据。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIELDS = ("shop_name", "title", "price", "sales")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", required=True)
    parser.add_argument("--run-id", nargs="+", required=True)
    args = parser.parse_args()
    for folder in (
        "doc",
        "prototype",
        "project/frontend",
        "project/backend",
        "database",
        "utils",
    ):
        assert (ROOT / folder).is_dir()
    connection = sqlite3.connect(
        (ROOT / "workbench/data/workbench.db").as_uri() + "?mode=ro", uri=True
    )
    connection.row_factory = sqlite3.Row
    try:
        task = connection.execute(
            "SELECT task_no,status,started_at,json_extract(payload_json,'$.ends_at') AS ends_at,ended_at,excel_path FROM tasks WHERE task_no=?",
            (args.task,),
        ).fetchone()
    finally:
        connection.close()
    if task is None:
        raise ValueError("未找到任务")
    report = {
        "observed_at": datetime.now().isoformat(),
        "task": dict(task),
        "platforms": {},
    }
    found = set()
    for path in (ROOT / "crawler/out/runs").glob("*/*/*.run.json"):
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("run_id") not in args.run_id:
            continue
        found.add(record["run_id"])
        platform = record["platform"]
        output = path.with_name(platform + ".jsonl")
        rows = (
            [
                json.loads(line)
                for line in output.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            if output.exists()
            else []
        )
        missing = {
            field: sum(
                row.get(field) is None or str(row.get(field)).strip() == ""
                for row in rows
            )
            for field in FIELDS
        }
        reasons = Counter(
            flag
            for row in rows
            for flag in row.get("flags", [])
            if "缺" in flag or "补采" in flag
        )
        report["platforms"][platform] = {
            "record": str(path.relative_to(ROOT)),
            "run_id": record["run_id"],
            "status": record.get("status"),
            "started_at": record.get("started_at"),
            "ended_at": record.get("ended_at"),
            "deadline_reached": record.get("deadline_reached"),
            "items_saved": len(rows),
            "missing_fields": missing,
            "four_fields_complete": sum(
                all(
                    row.get(f) is not None and str(row.get(f)).strip() != ""
                    for f in FIELDS
                )
                for row in rows
            ),
            "sales_zero": sum(row.get("sales") == 0 for row in rows),
            "sales_evidence_samples": [
                {
                    k: row.get(k)
                    for k in ("product_id", "url", "sales", "sales_raw", "comments")
                }
                for row in rows
                if row.get("sales") is not None
            ][:5],
            "missing_reasons": dict(reasons),
            "errors": record.get("errors", []),
        }
    report["missing_run_ids"] = sorted(set(args.run_id) - found)
    report["final"] = task["status"] not in ("running", "queued")
    target = (
        ROOT
        / "doc"
        / ("continuous_snapshot_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".json")
    )
    target.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({"report": str(target), **report}, ensure_ascii=False, indent=2))
    return 1 if report["missing_run_ids"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
