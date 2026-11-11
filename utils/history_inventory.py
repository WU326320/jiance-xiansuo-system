"""只读盘点历史代码包、覆盖前备份及活动源码引用；不解压、不删除。

从项目根目录运行 python -B -X utf8 utils/history_inventory.py。
JSON 只记录路径、大小、哈希与差异，不复制配置值或数据库内容。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import zipfile
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED = {
    ".git",
    ".venv",
    ".deps",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "data",
    "profiles",
    "state",
    "logs",
    "out",
    "cleanup_recovery",
    "node_modules",
}
ROUNDS = ["", "b", "c", "d", "e"]


def digest(data):
    return hashlib.sha256(data).hexdigest()


def compare(left, right):
    return {
        "same": sum(left[k] == right[k] for k in left.keys() & right.keys()),
        "changed": sorted(k for k in left.keys() & right.keys() if left[k] != right[k]),
        "only_left": sorted(left.keys() - right.keys()),
        "only_right": sorted(right.keys() - left.keys()),
    }


def main():
    for directory in (
        "doc",
        "prototype",
        "project/frontend",
        "project/backend",
        "database",
        "utils",
    ):
        assert (ROOT / directory).is_dir(), directory
    report = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "read_only_inventory_no_extraction_no_deletion",
        "archives": [],
        "backups": [],
        "references": [],
        "comparisons": [],
    }
    variants, sources = {}, defaultdict(list)
    for path in sorted(ROOT.glob("*.zip")):
        if not zipfile.is_zipfile(path):
            report["archives"].append(
                {
                    "path": path.name,
                    "bytes": path.stat().st_size,
                    "sha256": digest(path.read_bytes()),
                    "crc_ok": False,
                    "error": "BadZipFile: invalid ZIP structure; not extracted",
                }
            )
            continue
        with zipfile.ZipFile(path) as archive:
            records, fingerprints = [], {}
            bad = archive.testzip()
            assert bad is None, (path.name, bad)
            for member in archive.infolist():
                if member.is_dir():
                    continue
                name = member.filename.replace("\\", "/")
                fingerprint = digest(archive.read(member))
                assert name not in fingerprints, (path.name, "duplicate member", name)
                fingerprints[name] = fingerprint
                sources[(name, fingerprint)].append(path.name + "::" + name)
                records.append(
                    {"path": name, "bytes": member.file_size, "sha256": fingerprint}
                )
            variants[path.name] = fingerprints
            report["archives"].append(
                {
                    "path": path.name,
                    "bytes": path.stat().st_size,
                    "sha256": digest(path.read_bytes()),
                    "crc_ok": True,
                    "files": records,
                    "tops": dict(Counter(n.split("/")[0] for n in fingerprints)),
                    "zip_comment_bytes": len(archive.comment),
                }
            )
    for directory in sorted(ROOT.glob("_backup_*")):
        records, fingerprints = [], {}
        for path in sorted(directory.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(directory).as_posix()
            logical = relative.removeprefix("code/")
            fingerprint = digest(path.read_bytes())
            current = ROOT / logical
            records.append(
                {
                    "path": relative,
                    "logical_path": logical,
                    "bytes": path.stat().st_size,
                    "sha256": fingerprint,
                    "current_exists": current.is_file(),
                    "same_as_current": current.is_file()
                    and digest(current.read_bytes()) == fingerprint,
                }
            )
            fingerprints[relative] = fingerprint
            sources[(logical, fingerprint)].append(directory.name + "/" + relative)
        report["backups"].append(
            {
                "path": directory.name,
                "count": len(records),
                "bytes": sum(r["bytes"] for r in records),
                "files": records,
            }
        )
        variants[directory.name] = fingerprints
    for item in report["backups"]:
        unique = []
        for record in item["files"]:
            elsewhere = [
                p
                for p in sources[(record["logical_path"], record["sha256"])]
                if not p.startswith(item["path"] + "/")
            ]
            record["identical_history_copies"] = elsewhere
            if not elsewhere and not record["same_as_current"]:
                unique.append(record["path"])
        item["unique_vs_current_and_history"] = unique
    report["deletion_plan_checks"] = []
    for proposed in [
        ["_backup_20261106b", "_backup_20261106c", "_backup_20261106d"],
        ["_backup_20261106b", "_backup_20261106d"],
    ]:
        uncovered, total_files, total_bytes = [], 0, 0
        for item in report["backups"]:
            if item["path"] not in proposed:
                continue
            for record in item["files"]:
                total_files += 1
                total_bytes += record["bytes"]
                providers = [
                    p
                    for p in record["identical_history_copies"]
                    if not any(p.startswith(name + "/") for name in proposed)
                ]
                if not providers and not record["same_as_current"]:
                    uncovered.append(item["path"] + "/" + record["path"])
        report["deletion_plan_checks"].append(
            {
                "proposed": proposed,
                "files": total_files,
                "bytes": total_bytes,
                "uncovered": uncovered,
            }
        )
    for a, b in zip(ROUNDS, ROUNDS[1:]):
        left, right = f"code_20261106{a}.zip", f"code_20261106{b}.zip"
        report["comparisons"].append(
            {"left": left, "right": right, **compare(variants[left], variants[right])}
        )
    for suffix in ROUNDS:
        package = f"code_20261106{suffix}.zip"
        current = {
            name: digest((ROOT / name).read_bytes())
            for name in variants[package]
            if (ROOT / name).is_file()
        }
        report["comparisons"].append(
            {
                "left": package,
                "right": "current_workspace_at_package_paths",
                **compare(variants[package], current),
            }
        )
    pattern = re.compile(
        r"_backup_\w+|_code_snapshot\.zip|code_20261106[b-e]?\.zip|round_e_chain|validate_20261106[b-e]?"
    )
    extensions = {
        ".py",
        ".ps1",
        ".bat",
        ".md",
        ".yaml",
        ".yml",
        ".json",
        ".toml",
        ".ini",
        ".sh",
        ".cjs",
    }
    for parent, directories, files in os.walk(ROOT):
        directories[:] = [
            d for d in directories if d not in EXCLUDED and not d.startswith("_backup_")
        ]
        for filename in files:
            path = Path(parent) / filename
            if path.suffix.lower() not in extensions or path.stat().st_size > 2_000_000:
                continue
            if path.name in {
                "history_inventory.py",
                "历史版本盘点_20261108.json",
                "历史版本清理建议_20261108.md",
            }:
                continue
            try:
                text = path.read_text(encoding="utf-8-sig")
            except UnicodeError:
                try:
                    text = path.read_text(encoding="utf-16")
                except UnicodeError:
                    continue
            for line_no, line in enumerate(text.splitlines(), 1):
                matches = sorted(set(pattern.findall(line)))
                if matches:
                    report["references"].append(
                        {
                            "path": path.relative_to(ROOT).as_posix(),
                            "line": line_no,
                            "names": matches,
                        }
                    )
    output = ROOT / "doc/历史版本盘点_20261108.json"
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "archives": [
                    {k: v for k, v in r.items() if k != "files"}
                    for r in report["archives"]
                ],
                "backups": [
                    {k: v for k, v in r.items() if k != "files"}
                    for r in report["backups"]
                ],
                "comparisons": [
                    {k: (len(v) if isinstance(v, list) else v) for k, v in c.items()}
                    for c in report["comparisons"]
                ],
                "references": report["references"],
                "deletion_plan_checks": report["deletion_plan_checks"],
                "report": str(output),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
