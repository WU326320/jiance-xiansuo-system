"""单表导出：用户十列格式、店铺/IP分组、金额与既有只读/安全契约。"""

from datetime import date
from decimal import Decimal
from pathlib import Path
import sqlite3
import zipfile

import openpyxl
import pytest

from lead_excel import (
    ExportConfigError,
    ExportDataError,
    ExportDatabaseError,
    ExportError,
    ExportWriteError,
    export_leads_excel,
)
from lead_excel.api import _dedupe_link_rows, _resolve_versions, _sort_summary
from lead_excel.config import resolve_settings
from lead_excel.images import make_thumbnail
from lead_excel.workbook import ExportStats, write_workbook, save_workbook_atomic

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


def _load(path):
    return openpyxl.load_workbook(path)


def _products(ws):
    return [row for row in ws.iter_rows(min_row=2, values_only=True) if row[5]]


def _build(tmp_path, links, summaries=None):
    path = tmp_path / "report.xlsx"
    wb, stats = write_workbook(
        summaries or [],
        links,
        snapshot=date(2026, 11, 5),
        output_path=path,
        settings=resolve_settings({}),
        versions={},
        db_dir=tmp_path,
    )
    save_workbook_atomic(wb, path)
    return _load(path).active, stats


def _link(pid, **kwargs):
    return dict(
        {
            "lead_id": "L1",
            "platform": "taobao",
            "shop_id": "S1",
            "shop_name": "旗舰店",
            "ip_code": "IP-A",
            "title": pid,
            "url": f"https://item.example/{pid}",
            "amount": 0.1,
        },
        **kwargs,
    )


def test_single_sheet_exact_ten_headers(exported):
    wb = _load(exported[0])
    assert wb.sheetnames == ["链接明细"]
    ws = wb.active
    assert [cell.value for cell in ws[1]] == HEADERS
    assert ws.max_column == 10
    assert len(_products(ws)) == 5
    assert _products(ws)[0][:4] == (
        "tmall",
        "huluxiongdi",
        "企业旗舰店",
        "葫芦娃手办 企业旗舰店",
    )
    assert ws.freeze_panes == "A2"
    assert ws.auto_filter.ref == f"A1:J{ws.max_row - 1}"


def test_repeated_shop_ip_contiguous_same_fill_and_subtotal(tmp_path):
    ws, stats = _build(
        tmp_path,
        [
            _link("a", amount=0.1),
            _link("other-ip", ip_code="IP-B", amount=10),
            _link("b", amount=0.2),
        ],
    )
    assert [ws.cell(r, 4).value for r in range(2, 7)] == [
        "a",
        "b",
        "同IP小计（2条）",
        "other-ip",
        "总计（3条）",
    ]
    assert ws["J2"].value == 0.1 and ws["J3"].value == 0.2
    assert ws["J4"].value == 0.3
    assert ws["J6"].value == 10.3  # 小计不再累计一次。
    assert stats.link_count == 3
    color = ws["A2"].fill.fgColor.rgb
    assert ws["A2"].fill.patternType == "solid"
    assert all(
        cell.fill.fgColor.rgb == color
        for row in ws.iter_rows(min_row=2, max_row=4)
        for cell in row
    )
    assert ws["A5"].fill.patternType is None
    assert ws["J4"].font.bold and ws["J6"].font.bold


def test_multiple_groups_have_distinct_colors(tmp_path):
    ws, _ = _build(
        tmp_path,
        [
            _link("a"),
            _link("b"),
            _link("c", ip_code="IP-B"),
            _link("d", ip_code="IP-B"),
        ],
    )
    assert ws["A2"].fill.fgColor.rgb != ws["A5"].fill.fgColor.rgb
    assert ws["J8"].value == 0.4


@pytest.mark.parametrize(
    "changes",
    [
        {"shop_id": "S2"},
        {"platform": "jd"},
        {"shop_name": ""},
        {"ip_code": ""},
    ],
)
def test_different_shops_platforms_or_unknown_identity_never_merge(tmp_path, changes):
    ws, _ = _build(tmp_path, [_link("a"), _link("b", **changes)])
    assert ws.max_row == 4
    assert ws["D4"].value == "总计（2条）"
    assert ws["A2"].fill.patternType is None


def test_backend_shop_key_can_group_runs_but_separates_shop_ids(tmp_path):
    summaries = [
        {
            "lead_id": "L1",
            "shop_key": "taobao|旗舰店",
            "shop_id": "S1",
            "platform_group": "taobao",
        },
        {
            "lead_id": "L2",
            "shop_key": "taobao|旗舰店",
            "shop_id": "S1",
            "platform_group": "taobao",
        },
        {
            "lead_id": "L3",
            "shop_key": "taobao|旗舰店",
            "shop_id": "S2",
            "platform_group": "taobao",
        },
    ]
    ws, _ = _build(
        tmp_path,
        [
            _link("a"),
            _link("b", lead_id="L2", platform="tmall"),
            _link("c", lead_id="L3", shop_id="S2"),
        ],
        summaries,
    )
    assert ws["D4"].value == "同IP小计（2条）"
    assert ws["A2"].value == "taobao" and ws["A3"].value == "tmall"
    assert ws["D5"].value == "c"


def test_empty_shop_rows_stay_independent_even_without_lead(tmp_path):
    ws, _ = _build(
        tmp_path,
        [
            _link("a", lead_id=None, shop_name=""),
            _link("b", lead_id=None, shop_name=""),
        ],
    )
    assert ws.max_row == 4


def test_combined_ip_order_is_normalized_without_double_counting(tmp_path):
    ws, _ = _build(tmp_path, [_link("a", ip_code="A、B"), _link("b", ip_code="B、A")])
    assert ws["D4"].value == "同IP小计（2条）"
    assert ws["J5"].value == 0.2


@pytest.mark.parametrize(
    "amounts,expected,missing",
    [
        ([None, None], None, 2),
        ([None, 0], 0, 1),
        ([None, 2.34], 2.34, 1),
        ([Decimal("0.10"), Decimal("0.20")], 0.3, 0),
    ],
)
def test_missing_amount_not_zero_and_decimal_sums(tmp_path, amounts, expected, missing):
    ws, _ = _build(
        tmp_path, [_link(str(i), amount=amount) for i, amount in enumerate(amounts)]
    )
    assert ws["J4"].value == expected
    assert ws["J5"].value == expected
    if missing:
        assert f"{missing} 条商品金额缺失" in ws["J5"].comment.text
    else:
        assert ws["J5"].comment is None


def test_amounts_preserve_backend_values_and_raw_counts(exported):
    ws = _load(exported[0]).active
    products = _products(ws)
    assert [r[9] for r in products] == [500000, 300000, 550000, 900000, 600000]
    # P3 后端金额为600000，与单价乘估算销量不同，必须原样保留。
    assert products[-1][6:10] == (99, None, "800", 600000)
    assert ws.cell(ws.max_row, 10).value == 2850000
    for row in range(2, ws.max_row + 1):
        assert ws.cell(row, 10).number_format == "0.00"


def test_product_unknown_amount_keeps_reason_and_zero_stays_numeric(tmp_path):
    ws, _ = _build(
        tmp_path,
        [
            _link(
                "unknown",
                amount=None,
                amount_formula="待核验：评论缺失",
                missing_flags='["评论缺失"]',
            ),
            _link("zero", amount=0, amount_formula="0.00×min(100,10×10)=0.00"),
        ],
    )
    by_title = {ws.cell(row, 4).value: row for row in range(2, ws.max_row + 1)}
    unknown = ws.cell(by_title["unknown"], 10)
    zero = ws.cell(by_title["zero"], 10)
    assert unknown.value is None
    assert "评论缺失" in unknown.comment.text
    assert zero.value == 0 and zero.data_type == "n"
    assert ws.cell(ws.max_row, 10).value == 0


def test_thumbnails_embedded_in_column_e_and_keep_aspect(exported):
    path = exported[0]
    ws = _load(path).active
    assert len(ws._images) == 2
    assert sorted(
        (round(i.anchor.ext.cx / 9525), round(i.anchor.ext.cy / 9525))
        for i in ws._images
    ) == [(80, 60), (80, 120)]
    for image in ws._images:
        assert image.anchor._from.col == 4
        assert ws.row_dimensions[image.anchor._from.row + 1].height >= 63
    assert all(ws.cell(r, 5).value is None for r in range(2, ws.max_row + 1))
    with zipfile.ZipFile(path) as archive:
        assert len([n for n in archive.namelist() if n.startswith("xl/media/")]) == 2


def test_product_links_are_clickable(exported):
    ws = _load(exported[0]).active
    for row in range(2, ws.max_row):
        cell = ws.cell(row, 6)
        assert cell.hyperlink.target == cell.value


def test_non_http_and_formula_text_remain_safe(tmp_path):
    ws, stats = _build(
        tmp_path, [_link("=1+1", shop_name="=SUM(1)", url="javascript:alert(1)")]
    )
    assert ws["C2"].data_type == ws["D2"].data_type == "s"
    assert ws["D2"].value == "=1+1"
    assert ws["F2"].hyperlink is None
    assert stats.non_http_link_count == 1


def test_date_filter_and_summary_priority(exported):
    ws = _load(exported[0]).active
    assert [row[2] for row in _products(ws)] == [
        "企业旗舰店",
        "企业旗舰店",
        "个体户商行",
        "未知类型店",
        "个人小店",
    ]
    assert "昨日链接" not in [r[3] for r in _products(ws)]


def test_filename_directory_overwrite_and_atomic_cleanup(exported):
    path, db_path, config = exported
    assert path.name == "线索_20261105.xlsx"
    assert path.parent == Path(config["export"]["dir"]).resolve()
    path.write_bytes(b"OLD")
    assert Path(export_leads_excel(db_path, "2026-11-05", config)) == path
    assert _load(path).sheetnames == ["链接明细"]
    assert not list(path.parent.glob(".lead_excel_*.xlsx"))


def test_empty_snapshot_only_has_ten_headers(fixture_db):
    db, config = fixture_db
    ws = _load(export_leads_excel(db, "2026-11-06", config)).active
    assert ws.max_row == 1 and ws.max_column == 10
    assert ws.auto_filter.ref == "A1:J1"
    assert not ws._images


def test_default_directory_and_relative_db(fixture_db, tmp_path, monkeypatch):
    db, config = fixture_db
    profile = tmp_path / "User Profile"
    monkeypatch.setenv("USERPROFILE", str(profile))
    path = Path(export_leads_excel(db, "20261105", {}))
    assert path == profile / "Documents" / "侵权线索" / "线索_20261105.xlsx"
    monkeypatch.chdir(db.parent.parent)
    assert Path(export_leads_excel(Path("data/leads.db"), "20261105", config)).is_file()
    assert Path(
        export_leads_excel(
            None, "20261105", {**config, "storage": {"db_path": str(db)}}
        )
    ).is_file()


def test_only_views_queried_and_no_writes(exported, monkeypatch):
    import lead_excel.api as api

    _, db, config = exported
    captured = []
    real = api.connect_readonly

    def traced(path):
        conn = real(path)
        conn.set_trace_callback(captured.append)
        return conn

    monkeypatch.setattr(api, "connect_readonly", traced)
    export_leads_excel(db, "2026-11-05", config)
    sql = "\n".join(captured)
    assert 'FROM "v_lead_summary"' in sql and 'FROM "v_lead_links"' in sql
    assert 'FROM "leads"' not in sql and 'FROM "lead_links"' not in sql
    assert sql.count('WHERE "snapshot_date" =') == 2
    assert all(s.strip().split()[0].upper() in {"SELECT", "PRAGMA"} for s in captured)


def test_missing_view_and_table_rejected(tmp_path):
    db = tmp_path / "empty.db"
    with sqlite3.connect(db):
        pass
    with pytest.raises(ExportDatabaseError, match="v_lead_summary"):
        export_leads_excel(db, "2026-11-05", {})
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE v_lead_summary (lead_id TEXT)")
    with pytest.raises(ExportDatabaseError, match="而不是视图"):
        export_leads_excel(db, "2026-11-05", {})


def test_missing_required_column_and_invalid_date(fixture_db):
    db, config = fixture_db
    with sqlite3.connect(db) as conn:
        conn.execute("DROP VIEW v_lead_links")
        conn.execute(
            "CREATE VIEW v_lead_links AS SELECT lead_no AS lead_id, snapshot_date FROM lead_links"
        )
    with pytest.raises(ExportDatabaseError, match="ip_code"):
        export_leads_excel(db, "2026-11-05", config)
    with pytest.raises(ExportConfigError, match="不是合法日期"):
        export_leads_excel(db, "2026-13-40", config)


def test_duplicate_products_preserve_distinct_platforms_and_ips():
    rows = [_link("same", product_id="P1"), _link("same", product_id="P1")]
    rows += [
        _link("same", product_id="P1", platform="jd"),
        _link("same", product_id="P1", ip_code="IP-B"),
    ]
    assert len(_dedupe_link_rows(rows)) == 3


def test_orphan_links_skipped_and_reported(caplog):
    from lead_excel.api import _order_links

    stats = ExportStats()
    result = _order_links(
        [{"lead_id": "L1"}],
        [
            _link("orphan", lead_id="other"),
            _link("valid"),
            _link("standalone", lead_id=None),
        ],
        stats,
    )
    assert [r["title"] for r in result] == ["valid", "standalone"]
    assert stats.orphan_link_count == 1
    assert "已跳过" in caplog.text


def test_missing_optional_columns_work_with_legacy_fixture(exported):
    # fixture视图没有新平台/店铺键字段，平台从summary回填，不应读取不存在的列。
    assert _products(_load(exported[0]).active)[0][0] == "tmall"


def test_versions_prefer_view_and_summary_rank():
    settings = resolve_settings({"caliber_version": "config", "config_version": "cfg"})
    rows = [
        {"lead_id": "L2", "shop_type": "个人", "caliber_version": "view"},
        {"lead_id": "L1", "shop_type": "企业"},
    ]
    assert _resolve_versions(rows, settings) == {
        "caliber_version": "view",
        "config_version": "cfg",
    }
    assert _sort_summary(rows)[0]["lead_id"] == "L1"


def test_build_and_save_failures_raise_export_error(fixture_db, monkeypatch):
    import lead_excel.api as api

    db, config = fixture_db

    def boom(*args, **kwargs):
        raise RuntimeError("模拟 openpyxl 崩溃")

    monkeypatch.setattr(api, "write_workbook", boom)
    with pytest.raises(ExportDataError, match="构建 Excel 工作簿失败"):
        export_leads_excel(db, "2026-11-05", config)
    monkeypatch.undo()
    monkeypatch.setattr(api, "save_workbook_atomic", boom)
    with pytest.raises(ExportWriteError, match="写入 Excel 失败"):
        export_leads_excel(db, "2026-11-05", config)
    assert issubclass(ExportWriteError, ExportError)


def test_atomic_save_failure_preserves_previous_file(tmp_path, monkeypatch):
    import lead_excel.workbook as module

    target = tmp_path / "report.xlsx"
    target.write_bytes(b"previous")

    def denied(*args):
        raise PermissionError("locked")

    monkeypatch.setattr(module.os, "replace", denied)
    with pytest.raises(ExportWriteError, match="关闭后重试"):
        save_workbook_atomic(openpyxl.Workbook(), target)
    assert target.read_bytes() == b"previous"
    assert not list(tmp_path.glob(".lead_excel_*.xlsx"))


def test_thumbnail_preserves_aspect_and_does_not_upscale(tmp_path):
    from PIL import Image

    for name, size, expected in [
        ("wide.png", (400, 100), (80, 20)),
        ("small.png", (40, 20), (40, 20)),
    ]:
        path = tmp_path / name
        Image.new("RGB", size, "green").save(path)
        thumb, reason = make_thumbnail(path, target_width=80, max_height=120)
        assert reason is None and (thumb.width, thumb.height) == expected
    assert "文件不存在" in make_thumbnail(tmp_path / "nope.png")[1]


def test_relative_image_resolves_against_backend_root(fixture_db):
    import shutil

    db, config = fixture_db
    target = db.parent / "images" / "relative.png"
    target.parent.mkdir()
    shutil.copyfile(db.parent.parent / "images/a.png", target)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE lead_links SET image_path='data/images/relative.png' WHERE product_id='P1'"
        )
    assert len(_load(export_leads_excel(db, "2026-11-05", config)).active._images) == 2
