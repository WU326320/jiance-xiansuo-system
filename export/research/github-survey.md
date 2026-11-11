# GitHub 调研：openpyxl 嵌图 / Excel 报表导出（2026-11-05）

> 目的：在为 `export_leads_excel()` 选型前，按星数检索成熟项目/库，确认“openpyxl + Pillow 嵌图”的做法不是自造轮子，并核对许可证。
> 数据来源：GitHub Search API / Repos API（按 stars 排序）+ 各仓库 `/commits?per_page=1` + README；星数、最后提交日期为本次实测，不是 README 自述。最后提交取默认分支最新 commit 的 committer date。
> 检索关键词：`xlsxwriter`、`exceljs`、`EasyExcel`、`ClosedXML`、`excelize`、`openpyxl image excel`、`excel report generator`。

## 1. 结果

| 项目 | 链接 | 星数 | 最后提交 | 语言/运行时 | 嵌图能力 | 许可证 | 与本项目匹配度 |
|---|---|---:|---|---|---|---|---|
| jmcnamara/XlsxWriter | https://github.com/jmcnamara/XlsxWriter | 3,991 | 2026-10-28 | Python | `insert_image()`，支持单元格锚点/缩放 | BSD-2-Clause | 高：同为 Python xlsx 写入库，但只写不读 |
| exceljs/exceljs | https://github.com/exceljs/exceljs | 15,511 | 2024-01-12 | JavaScript / Node | `workbook.addImage()` + 单元格/区域锚点 | MIT | 中：功能接近，但需引入 Node 运行时 |
| alibaba/easyexcel | https://github.com/alibaba/easyexcel | 33,562 | 2024-10-29 | Java | 以数据导入导出为主，图片写入非核心场景 | Apache-2.0 | 低：需 JVM，报表模型偏 POI |
| ClosedXML/ClosedXML | https://github.com/ClosedXML/ClosedXML | 5,751 | 2026-10-21 | C# / .NET | `AddPicture()`，锚点/缩放 | MIT | 低：需 .NET，无法与 Python 主流程同进程 |
| qax-os/excelize | https://github.com/qax-os/excelize | 21,071 | 2026-11-02 | Go | `AddPicture()`，支持缩放/锚点 | BSD-3-Clause | 低：需 Go 二进制/子进程 |
| SheetJS/sheetjs | https://github.com/SheetJS/sheetjs | 36,341 | 2024-02-01 | JavaScript | 社区版以读写数据为主，嵌图能力有限 | Apache-2.0 | 低：不满足“缩略图嵌入”要求 |

补充核查：

- **openpyxl**：PyPI 3.1.5，MIT；官方源码在 Heptapod（https://foss.heptapod.net/openpyxl/openpyxl），**不在 GitHub 主站**。GitHub 上的 `ericgazoni/openpyxl` 是 2014 年的旧镜像（202 stars），不能拿来代表当前版本。它的 `openpyxl.drawing.image.Image` + `Worksheet.add_image()` 原生支持嵌图，`Cell.hyperlink` 支持超链接，且能 `load_workbook()` 读回，正好覆盖“生成 + 自测读回核对”。
- **Pillow**：PyPI 13.x，HPND 许可（宽松）。负责本地主图缩放、EXIF 方向修正、统一转 PNG。
- **haris-musa/excel-mcp-server**（4,276 stars，MIT）是 openpyxl 的上层封装，说明“openpyxl 做 Excel 图片/图表自动化”是成熟路线；它是 MCP 服务，不是可复用报表库，故不引入。
- 星数很高但许可证不适合产品化的项目（如 GPL/AGPL 类）未列入候选；上表均为 MIT / BSD / Apache-2.0 宽松许可。

## 2. 结论：选 openpyxl + Pillow

**选择**：`openpyxl>=3.1,<4` 负责工作簿、样式、超链接、嵌图；`Pillow>=10,<14` 负责把本地 `image_path` 缩到宽约 80px 并转 PNG。SQLite 用标准库 `sqlite3`。

理由（按权重）：

1. **运行时匹配**：目标机是 Windows 10 + Python 3.13，主流程、爬虫、后端都在同一 Python 环境；引入 Node/Java/.NET/Go 只为写一个 xlsx 不划算，也增加部署与故障面。
2. **功能完整**：openpyxl 同时支持图片锚点、超链接、数字格式、合并单元格、列宽/行高、冻结窗格，方案 §4 的要求全部覆盖。
3. **可自测**：openpyxl 能读回自己生成的文件，测试可直接断言 Sheet、表头、嵌入图数量/尺寸、超链接目标；XlsxWriter 只写不读，读回验证要再引入一个库。
4. **许可证干净**：openpyxl（MIT）、Pillow（HPND）、PyYAML（MIT）都是宽松许可，可随项目分发。
5. **不重复造轮子**：没有手写 OOXML/ZIP，也没有用 csv 改后缀；图片嵌入交给 openpyxl 的 drawing 层。

**不选 XlsxWriter 的原因**：它同样是优秀且更快的写入库（BSD-2），`insert_image()` 也成熟；但本任务明确要求 openpyxl，且导出模块需要“写完后用同一库读回自测”，XlsxWriter 不提供读取能力。若未来导出量极大、只写不读，可把它作为性能备选，但需要单独评估，不在本轮改选。

**不选 ExcelJS / EasyExcel / ClosedXML / Excelize 的原因**：都是成熟项目，但分别绑定 Node、JVM、.NET、Go；为本模块引入第二运行时，收益仅是“换一种语言写 xlsx”，与 Windows 单机 Python 部署目标冲突。

**许可证注意**：

- 只借鉴 API 用法和报表结构，不复制上述项目的源码；本模块代码为自写。
- 依赖清单锁定宽松许可：openpyxl MIT、Pillow HPND、PyYAML MIT；后续若引入模板引擎/图表库，需重新核对许可证，避免 GPL/AGPL 进入闭源分发。
- `openpyxl` 官方文档与源码在 Heptapod，引用时不要误写成 GitHub 仓库。

## 3. 对本模块的落地映射

| 需求 | openpyxl/Pillow 实现 |
|---|---|
| 缩略图约 80px、可见 | Pillow `thumbnail((80, 120))` 保比例 → PNG BytesIO → `openpyxl.drawing.image.Image` → `ws.add_image(img, "D{row}")`；行高按图片高度换算 |
| 原图/商品链接可点 | `cell.hyperlink = url`，只接受 http(s)，并加蓝色下划线样式 |
| 金额 2 位小数 | `number_format = "0.00"`，值仍来自视图，不重算 |
| 同日覆盖 | 同目录临时文件 + `os.replace()` 原子覆盖；被 Excel 占用时给出可操作错误 |
| 只读视图 | `sqlite3` URI `mode=ro` + `PRAGMA query_only=ON`，只 SELECT `v_lead_summary` / `v_lead_links` |
| 自测读回 | `load_workbook()` 断言 Sheet/表头/图片锚点尺寸/超链接/空复核列 |
