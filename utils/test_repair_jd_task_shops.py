"""京东店名恢复工具；所有写入均使用临时目录和临时数据库。"""

from copy import deepcopy
import json
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook
from openpyxl.comments import Comment
from openpyxl.drawing.image import Image
from openpyxl.styles import Font
from PIL import Image as PILImage

from utils import repair_jd_task_shops as repair
from workbench.db import Database


HEADERS = [
    "平台",
    "IP",
    "店铺名",
    "标题",
    "缩略图",
    "商品链接",
    "单价",
    "销量",
    "评论",
    "总金额",
]


def row(pid="123456", **changes):
    return {
        "platform": "jd",
        "product_id": pid,
        "url": f"https://item.jd.com/{pid}.html",
        "title": "原商品标题",
        "shop_name": None,
        "shop_id": None,
        "price": 20,
        "sales": None,
        "comments": 0,
        "listed": True,
        "ip_code": "测试IP",
        "search_keyword": "测试IP",
        "crawled_at": "2026-11-10T10:00:00+08:00",
        "flags": ["店铺名缺失", "按下限估算", "金额补采：等待详情补采"],
        **changes,
    }


def detail(pid="123456", **changes):
    return {
        "platform": "jd",
        "product_id": pid,
        "shop_name": "真实测试店",
        "shop_id": "42",
        **changes,
    }


@pytest.mark.parametrize(
    "details",
    [
        [],
        [detail(platform="taobao")],
        [detail(pid="654321")],
        [detail(shop_name="京东POP")],
        [detail(shop_name=" ")],
        [detail(), detail(shop_name="另一家店")],
        [detail(), detail(shop_id="43")],
    ],
)
def test_evidence_rejects_missing_placeholder_or_conflicting_sources(details):
    with pytest.raises(ValueError):
        repair.evidence_patches([row()], details)


def test_recovery_uses_matching_evidence_and_preserves_original_fields():
    source = [row(), row("234567", price=None), row("345678", platform="taobao")]
    before = deepcopy(source)
    patches = repair.evidence_patches(source, [detail(), detail(), detail("234567")])
    fixed = repair.recover_rows(source, patches)
    assert source == before
    assert fixed[2] == source[2]
    for old, new in zip(source[:2], fixed[:2]):
        assert new["shop_name"] == "真实测试店"
        assert new["shop_id"] == "42"
        assert {
            k: v for k, v in old.items() if k not in {"shop_name", "shop_id", "flags"}
        } == {
            k: v for k, v in new.items() if k not in {"shop_name", "shop_id", "flags"}
        }
        assert "店铺名缺失" not in new["flags"]
        assert "按下限估算" in new["flags"]
    assert not any(f.startswith("金额补采：") for f in fixed[0]["flags"])
    assert any("缺少price" in f for f in fixed[1]["flags"])


@pytest.mark.parametrize("changes", [{"shop_name": "另一家店"}, {"shop_id": "43"}])
def test_recovery_rejects_existing_shop_conflict(changes):
    with pytest.raises(ValueError, match="冲突"):
        repair.recover_rows([row(**changes)], {"123456": detail()})


def make_excel(path, rows):
    book = Workbook()
    sheet = book.active
    sheet.title = "链接明细"
    sheet.append(HEADERS)
    for item in rows:
        sheet.append(
            [
                "jd",
                "测试IP",
                item["shop_name"],
                item["title"],
                None,
                item["url"],
                item["price"],
                item["sales"],
                item["comments"],
                None if item["price"] is None else item["price"] * item["comments"],
            ]
        )
    sheet.append(
        [
            None,
            None,
            None,
            f"总计（{len(rows)}条）",
            None,
            None,
            None,
            None,
            None,
            "=SUM(J2:J28)",
        ]
    )
    sheet["J2"].comment = Comment("原金额说明", "测试")
    sheet["C2"].font = Font(bold=True, color="123456")
    sheet["F2"].hyperlink = rows[0]["url"]
    sheet.freeze_panes = "A2"
    sheet.column_dimensions["C"].width = 25
    sheet.row_dimensions[2].height = 60
    image_path = path.parent / "test-product.png"
    PILImage.new("RGB", (8, 8), "white").save(image_path)
    sheet.add_image(Image(image_path), "E2")
    book.save(path)
    book.close()


def test_excel_only_changes_names_and_preserves_images_and_format(tmp_path):
    source, target = tmp_path / "original.xlsx", tmp_path / "fixed.xlsx"
    make_excel(source, [row(), row("234567", shop_name="已有店名")])
    original_bytes = source.read_bytes()
    report = repair.repair_excel(source, target, {row()["url"]: "真实测试店"})
    assert report == {"excel_rows": 2, "names_filled": 1, "missing_names": 0}
    assert source.read_bytes() == original_bytes
    book = load_workbook(target)
    try:
        sheet = book.active
        assert sheet["C2"].value == "真实测试店"
        assert sheet["C3"].value == "已有店名"
        assert sheet["J2"].value == 0 and sheet["J2"].comment.text == "原金额说明"
        assert sheet["C2"].font.bold and sheet["C2"].font.color.rgb == "00123456"
        assert sheet["F2"].hyperlink.target == row()["url"]
        assert sheet.freeze_panes == "A2" and sheet.column_dimensions["C"].width == 25
        assert sheet.row_dimensions[2].height == 60
        assert len(sheet._images) == 1
    finally:
        book.close()
    with pytest.raises(ValueError, match="新路径"):
        repair.repair_excel(source, target, {})
    with pytest.raises(ValueError, match="新路径"):
        repair.repair_excel(source, source, {})
    with pytest.raises(ValueError, match="不一致"):
        repair.repair_excel(source, tmp_path / "wrong.xlsx", {"unknown-url": "某店"})


@pytest.fixture
def isolated_task(tmp_path, monkeypatch):
    for folder in (
        "doc",
        "prototype",
        "project/frontend",
        "project/backend",
        "database",
        "utils",
        "workbench/data/exports",
    ):
        (tmp_path / folder).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(repair, "ROOT", tmp_path)
    monkeypatch.setattr(repair, "SOURCE", tmp_path / "source.jsonl")
    monkeypatch.setattr(repair, "DETAILS", tmp_path / "details.jsonl")
    monkeypatch.setattr(repair, "PROGRESS", tmp_path / "progress.json")
    rows = [
        row(
            str(100000 + index),
            shop_name=None if index < 8 else "已有店名",
            price=None if 8 <= index < 15 else 20,
        )
        for index in range(60)
    ]
    details = [detail(item["product_id"]) for item in rows[:8]]
    for path, items in ((repair.SOURCE, rows), (repair.DETAILS, details)):
        path.write_text(
            "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in items),
            encoding="utf-8",
        )
    state = {
        "scope": {"platform": "jd"},
        "active": True,
        "status": "deadline",
        "rows": {
            json.dumps(["jd", item["product_id"], "测试IP"]): item for item in rows
        },
        "keywords": {"original-keyword": {"done": True, "next_page": 2}},
        "detail_attempted": ["jd:100000"],
        "detail_pending": ["jd:100000"],
    }
    repair.PROGRESS.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    excel = tmp_path / "workbench/data/exports/original.xlsx"
    make_excel(excel, rows[:27])
    result = {
        "status": "partial",
        "stop_reason": "deadline",
        "candidates": 4,
        "excel_path": str(excel),
        "risks": {"达标": 4},
        "platforms": {
            "jd": {
                "items": 60,
                "amount_complete": 53,
                "amount_missing": 7,
                "missing_fields": {"shop_name": 8, "price": 7, "sales": 60},
                "error": "金额待核验7条；店名待补齐8条",
            }
        },
        "errors": ["京东：店名待补齐8条", "京东：金额待核验7条"],
    }
    database = Database(tmp_path / "workbench/data/workbench.db")
    database.init_schema()
    for task_no in (repair.TASK, "OTHER-TASK"):
        database.execute(
            "INSERT INTO tasks(task_no,ip_name,ip_norm,platforms_json,mode,status,result_json,excel_path,created_at,ended_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                task_no,
                "测试IP",
                "测试ip",
                '["jd"]',
                "dry_run",
                "partial",
                json.dumps(result, ensure_ascii=False),
                str(excel),
                "2026-11-10 10:46:04",
                "2026-11-10 10:50:00",
            ),
        )
    return database


def test_preview_then_apply_is_scoped_backed_up_and_not_repeatable(isolated_task):
    db = isolated_task
    before = db.fetchall("SELECT * FROM tasks ORDER BY task_no")
    state_before = repair.PROGRESS.read_bytes()
    source_before = repair.SOURCE.read_bytes()
    preview = repair.main()
    assert preview["names_filled"] == 8 and preview["pending_amount_items"] == 7
    assert preview["excel_rows"] == 27 and not preview["applied"]
    assert db.fetchall("SELECT * FROM tasks ORDER BY task_no") == before
    assert repair.PROGRESS.read_bytes() == state_before
    report = repair.main(apply=True)
    assert report["applied"] and Path(report["backup"]).is_file()
    assert Path(report["progress_backup"]).read_bytes() == state_before
    assert repair.SOURCE.read_bytes() == source_before
    saved = db.fetchone("SELECT * FROM tasks WHERE task_no=?", (repair.TASK,))
    old = next(task for task in before if task["task_no"] == repair.TASK)
    assert {
        k: v for k, v in saved.items() if k not in {"excel_path", "result_json"}
    } == {k: v for k, v in old.items() if k not in {"excel_path", "result_json"}}
    assert db.fetchone("SELECT * FROM tasks WHERE task_no='OTHER-TASK'") == before[0]
    result = json.loads(saved["result_json"])
    assert result["candidates"] == 4 and result["stop_reason"] == "deadline"
    assert result["platforms"]["jd"]["missing_fields"] == {
        "shop_name": 0,
        "price": 7,
        "sales": 60,
    }
    state = json.loads(repair.PROGRESS.read_bytes())
    assert len(state["detail_patches"]) == 8 and len(state["detail_pending"]) == 7
    assert state["active"] and state["status"] == "deadline"
    assert state["keywords"] == json.loads(state_before)["keywords"]
    assert len(db.fetchall("SELECT * FROM audit_log")) == 1
    with pytest.raises(ValueError, match="已经恢复"):
        repair.main(apply=True)


def test_progress_write_failure_rolls_back_task(isolated_task, monkeypatch):
    import crawler.core.jsonl as jsonl

    tasks_before = isolated_task.fetchall("SELECT * FROM tasks ORDER BY task_no")
    state_before = repair.PROGRESS.read_bytes()
    write = jsonl.write_json_atomic

    def fail_progress(path, *args, **kwargs):
        if Path(path) == repair.PROGRESS:
            raise OSError("模拟断点写入失败")
        return write(path, *args, **kwargs)

    monkeypatch.setattr(jsonl, "write_json_atomic", fail_progress)
    with pytest.raises(OSError, match="断点写入失败"):
        repair.main(apply=True)
    assert (
        isolated_task.fetchall("SELECT * FROM tasks ORDER BY task_no") == tasks_before
    )
    assert repair.PROGRESS.read_bytes() == state_before
    assert isolated_task.fetchall("SELECT * FROM audit_log") == []
