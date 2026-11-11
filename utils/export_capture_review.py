"""把本轮原始采集记录整理为可筛选的 Excel，不修改原始数据。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "crawler" / "out" / "2026-11-06"
DESTINATION = ROOT.parent / "葫芦兄弟_真实采集数据_2026-11-06.xlsx"
COLUMNS = [
    ("平台", "platform", 14),
    ("商品标题", "title", 66),
    ("店铺名称", "shop_name", 30),
    ("价格（采集解析值）", "price", 20),
    ("价格原文", "price_raw", 32),
    ("销量（采集解析值）", "sales", 22),
    ("销量原文", "sales_raw", 24),
    ("评论（采集解析值）", "comments", 22),
    ("评论原文", "comments_raw", 20),
    ("商品链接", "url", 46),
    ("数据标记", "flags", 32),
    ("商品ID", "product_id", 24),
    ("店铺ID", "shop_id", 24),
    ("店铺标签原文", "shop_label_raw", 34),
    ("店铺类型提示", "shop_type_raw_hint", 20),
    ("关键词", "search_keyword", 20),
    ("IP代码", "ip_code", 20),
    ("采集时间", "crawled_at", 34),
    ("页码", "page", 10),
    ("页内顺序", "rank", 12),
    ("主图链接", "main_image_url", 46),
    ("本地主图路径", "image_path", 30),
    ("类目", "category", 20),
    ("在售（采集值）", "listed", 18),
    ("店铺ID原文", "shop_id_raw", 32),
]


def read_source():
    grouped = {}
    for name in ("taobao", "jd", "1688"):
        source = SOURCE / f"{name}.jsonl"
        grouped[name] = [
            json.loads(line)
            for line in source.read_text(encoding="utf-8-sig").splitlines()
            if line.strip()
        ]
    return grouped


def style(sheet, widths):
    sheet.freeze_panes = "D2" if sheet.title != "采集说明" else "A2"
    sheet.sheet_view.showGridLines = False
    for cell in sheet[1]:
        cell.fill = PatternFill("solid", fgColor="234E80")
        cell.font = Font(name="微软雅黑", size=11, bold=True, color="FFFFFF")
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    sheet.row_dimensions[1].height = 32
    for index, width in enumerate(widths, 1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    for row in sheet.iter_rows(min_row=2):
        sheet.row_dimensions[row[0].row].height = 48
        for cell in row:
            cell.font = Font(name="微软雅黑", size=10, color="24344B")
            cell.alignment = Alignment(vertical="center", wrap_text=True)
            if cell.row % 2 == 0:
                cell.fill = PatternFill("solid", fgColor="F1F5FA")


def text_value(cell, value):
    if isinstance(value, list):
        value = "；".join(str(item) for item in value)
    elif isinstance(value, dict):
        value = json.dumps(value, ensure_ascii=False)
    cell.value = value
    # 外部商品文本作为文本保存，避免 Excel 将其解释为公式。
    if isinstance(value, str):
        cell.data_type = "s"


def add_products(book, name, records):
    sheet = book.create_sheet(name)
    sheet.append([title for title, _, _ in COLUMNS])
    for row_index, record in enumerate(records, 2):
        for column_index, (_, field, _) in enumerate(COLUMNS, 1):
            text_value(sheet.cell(row_index, column_index), record.get(field))
    style(sheet, [width for _, _, width in COLUMNS])
    sheet.auto_filter.ref = sheet.dimensions
    for row in sheet.iter_rows(min_row=2):
        for index in (9, 20):
            cell = row[index]
            if cell.value and urlsplit(str(cell.value)).scheme in ("http", "https"):
                cell.hyperlink = cell.value
                cell.font = Font(
                    name="微软雅黑", size=10, color="255BD6", underline="single"
                )
        row[3].number_format = "0.00"
        for index in (11, 12, 24):
            row[index].number_format = "@"


def main():
    grouped = read_source()
    records = [record for rows in grouped.values() for record in rows]
    book = Workbook()
    info = book.active
    info.title = "采集说明"
    notes = [
        ("内容", "说明"),
        ("数据性质", "真实网站采集的原始商品记录，尚未进行侵权判定，不代表侵权认定。"),
        ("搜索关键词", "葫芦兄弟"),
        ("采集范围", "2026-11-06；每平台一个关键词、一个搜索结果页；不下载主图。"),
        ("记录总数", len(records)),
        ("淘宝/天猫", len(grouped["taobao"])),
        ("京东", f"{len(grouped['jd'])} 条；登录通过，搜索页面未呈现商品列表。"),
        ("1688", len(grouped["1688"])),
        (
            "数据质量提示",
            "1688 混有非目标商品；价格、销量字段可能存在解析偏差，请结合原文及商品链接核验。",
        ),
        ("缺失值", "空白表示未采集到；不补零。数值为 0 的单元格保留采集原值。"),
        (
            "销量口径",
            "同时保留销量原文和采集解析值。带 + 等模糊销量按采集程序下限解析，未经人工验证。",
        ),
        ("补抓范围", "1688 最终列表试采未进行店名详情补全。"),
        (
            "查看方法",
            "在“全部商品”表头下拉筛选，或切换平台明细页；点击商品链接可打开原商品。",
        ),
        ("原始数据目录", str(SOURCE)),
        ("导出时间", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    ]
    for note in notes:
        info.append(note)
    style(info, [24, 110])
    for sheet_name, rows in (
        ("全部商品", records),
        ("淘宝天猫", grouped["taobao"]),
        ("1688", grouped["1688"]),
    ):
        add_products(book, sheet_name, rows)
    destination = DESTINATION
    if destination.exists():
        destination = destination.with_stem(
            destination.stem + "_" + datetime.now().strftime("%H%M%S")
        )
    book.save(destination)
    print(
        json.dumps(
            {
                "file": str(destination),
                "total": len(records),
                "platforms": {key: len(value) for key, value in grouped.items()},
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
