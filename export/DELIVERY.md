# Excel 导出模块交付说明（DELIVERY）

- 交付日期：2026-11-05
- 依据：`/workspace/requirements/Excel导出方案_v0.1.md`（文内 **v0.1.1**）、
  `后端方案_v0.1.md` §6、`UPDATES.md` 15:31、`需求文档_v0.3.md` §8、
  `验收清单_v0.3.md` C9/C10
- 代码位置：`/workspace/ip-monitor/export`
- 结论：**已完成，pytest 35/35 通过**（Python 3.13.5 + openpyxl 3.1.5 + Pillow 13.0.0）；
  已按 `storage.db_path`、只查两个视图且仅 `WHERE snapshot_date=?`、固定三列表头、
  视图版本列、明确导出异常逐条修改，并用后端 `pipeline/views.py` 的真实视图定义
  建库完成集成自测。

## 0. `UPDATES.md` 15:31 补充要求逐条核对

| # | 补充要求 | 实现位置 | 验证 | 结果 |
|---|---|---|---|---|
| 1 | 数据库配置键 `storage.db_path`，默认 `data/leads.db`，只读打开 | `config.py::DEFAULT_DB_PATH` / `resolve_db_path`；`db.py::connect_readonly` | `test_storage_db_path_config_and_default`、只读连接代码审查 | ✅ |
| 2 | 只查 `v_lead_summary` / `v_lead_links`，`WHERE snapshot_date=?` | `db.py::read_view`，已删除额外的 `status` 条件 | `test_only_views_are_queried_and_no_writes`、`test_snapshot_date_filter_excludes_other_days` | ✅ |
| 3 | 汇总表头「线索编号」「复核结论」「复核人」固定 | `columns.py::FIXED_SUMMARY_HEADERS`；`workbook.py::_write_summary` 强制覆盖 | `test_fixed_summary_headers_never_change`、`test_c9_three_sheets_and_complete_headers` | ✅ |
| 4 | 口径说明使用视图里的 `caliber_version` / `config_version` | `api.py::_resolve_versions` 视图优先、配置兜底；`caliber.py` 输出 | `test_caliber_versions_prefer_view_over_config`、`test_caliber_uses_configured_threshold_and_view_versions` | ✅ |
| 5 | 导出失败抛明确异常，供主流程记 `run_logs` 并报警 | `errors.py` 四级异常；`api.py` 公共入口统一兜底为 `ExportError` | `test_build_and_save_failures_raise_export_error`；新增 `ExportWriteError` 导出 | ✅ |
| 6 | 后端已有真实视图时，用它建的库做集成自测 | `tests/test_backend_integration.py` 直接加载后端 `CREATE_VIEWS_SQL` | `test_export_against_database_built_from_backend_views` | ✅ |
| 7 | pytest 全过 | 全部夹具 + 集成测试 | `35 passed`，日志 `self-test.log` | ✅ |
| 8 | 视图列名以后端方案 §6 v0.1.1 为准 | `columns.py::VIEW_COLUMNS` 英文列名（含辅助列）；`SUMMARY_EXCEL_FIELDS` 排除辅助列 | `test_view_columns_follow_backend_plan_v011_english_names`、后端真实视图集成测试 | ✅ |

## 1. 交付物

| 内容 | 位置 |
|---|---|
| Python 包 / 公开接口 | `lead_excel/`，`lead_excel/__init__.py` |
| 视图列名映射（唯一对齐点） | `lead_excel/columns.py` → `VIEW_COLUMNS` |
| 主流程接口 | `lead_excel/api.py` → `export_leads_excel(db_path=None, snapshot_date=None, config=None) -> str` |
| 只读 SQLite 访问 | `lead_excel/db.py` |
| 缩略图 | `lead_excel/images.py` |
| 三张 Sheet / 样式 / 嵌图 | `lead_excel/workbook.py` |
| 口径说明文案 | `lead_excel/caliber.py` |
| 配置解析 | `lead_excel/config.py` |
| CLI | `lead_excel/cli.py`、`python -m lead_excel` |
| 夹具 + pytest | `tests/conftest.py`、`tests/test_export.py` |
| 后端真实视图集成测试 | `tests/test_backend_integration.py` |
| GitHub 选型调研 | `research/github-survey.md` |
| 自测日志 | `self-test.log` |
| 使用说明 | `README.md` |

## 2. 运行方法

```bat
cd /d <项目>\ip-monitor\export
python -m pip install -r requirements.txt
python -m pytest -q
```

主流程：

```python
from lead_excel import export_leads_excel

xlsx_path = export_leads_excel(
    db_path=None,                 # 省略/None 时读 storage.db_path（默认 data/leads.db）
    snapshot_date="2026-11-05",   # 或 "20261105"
    config=rules_config,          # 完整 rules.yaml dict，或仅 export 段
)
# 返回绝对路径；同日重跑覆盖同名文件
```

后端 runner 预留了 `exporter(session, settings, run_date)` 钩子，接入示例：

```python
from lead_excel import export_leads_excel

def exporter(session, settings, run_date):
    config = dict(settings.raw)
    config["storage"] = {**dict(config.get("storage") or {}), "db_path": str(settings.database_path)}
    config["config_version"] = settings.config_version  # yaml sha256 前 12 位
    return export_leads_excel(None, run_date, config)
```

CLI（`--db` 可省略，读配置 `storage.db_path`）：

```bat
python -m lead_excel --db ..\data\leads.db --date 2026-11-05 --config ..\rules.yaml
python -m lead_excel --date 2026-11-05 --config ..\rules.yaml
```

失败时抛 `ExportConfigError` / `ExportDatabaseError` / `ExportDataError` /
`ExportWriteError`（均继承 `ExportError`）。主流程应把 `str(exc)` 写入
`run_logs.error_summary`，不要静默吞掉。

## 3. 方案 §6 自测逐条结果

测试命令：

开发容器：

```text
/workspace/ip-monitor/crawler/.venv/bin/python -m pytest -o addopts= -v
```

Windows（与后端共用同一 venv）：

```bat
cd /d <项目>\ip-monitor\export
..\crawler\.venv\Scripts\python.exe -m pytest -o addopts= -v
```

结果：**35 passed**；完整日志见 `self-test.log`。

| 方案 §6 / 验收项 | 结果 | 证据（测试） |
|---|---|---|
| C9 三张 Sheet | ✅ | `test_c9_three_sheets_and_complete_headers`：`线索汇总 / 链接明细 / 口径说明`，顺序正确 |
| C9 缩略图可见 | ✅ | `test_c9_thumbnail_embedded_and_about_80px`：2 张图，锚点渲染尺寸 80×60、80×120；`xl/media/` 内有 2 个文件 |
| C9 原图/商品链接可点 | ✅ | `test_c9_original_and_product_links_clickable`：所有 http(s) 单元格 `hyperlink.target` 正确 |
| C9 复核结论/复核人为空列 | ✅ | `test_c9_review_columns_are_empty_even_if_view_has_values`：夹具视图故意写“已确认/张三”，导出仍为空 |
| C10 汇总命中原因摘要非空 | ✅ | `test_c10_hit_reasons_summary_and_links_non_empty`（夹具全列非空） |
| C10 链接命中原因非空 | ✅ | 同上；另有 `test_empty_hit_reasons_are_flagged_and_reported` 验证空值会黄底+口径提示，不静默 |
| 字段齐全（§8.1 16 列） | ✅ | `test_all_required_fields_present_with_data` + 精确表头断言 |
| 字段齐全（§8.2 18 列） | ✅ | 同上（含缩略图、原图链接、商品链接、计算式） |
| 排序：店铺类型→置信度→金额 | ✅ | `test_sorting_shop_type_then_confidence_then_amount`：企业→个体户→未知→个人；个体户置信度低于未知仍排前；个人 99 分仍排最后 |
| 线索编号 `平台-YYYYMMDD-####` | ✅ | `test_lead_id_format_platform_yyyymmdd_sequence`：`^(taobao|tmall|jd|1688)-\d{8}-\d{4}$` |
| 同日重跑覆盖 | ✅ | `test_filename_directory_and_same_day_overwrite`：先写 OLD 再导出，仍为有效 xlsx；目录内仅 1 个同日文件；无临时文件残留 |
| 目录自动创建 | ✅ | 同上 + `test_default_export_dir_uses_userprofile`（含中文/空格路径） |
| 无线索仍生成空表 + 口径说明 | ✅ | `test_empty_snapshot_still_generates_three_sheets`：两个主表仅表头，口径说明照常 |
| 单测/夹具 openpyxl 读回核对列与嵌图数量 | ✅ | 全部测试均用 `load_workbook()` 读回；`test_c9_thumbnail_embedded_and_about_80px` 断言图片数量与锚点尺寸 |
| 只读视图，不读底层表 | ✅ | `test_only_views_are_queried_and_no_writes`：抓取 SQL，仅 SELECT `v_lead_summary` / `v_lead_links`，无底层表、无写语句 |
| 只按 `snapshot_date` 过滤 | ✅ | 同上：SQL 仅 `WHERE "snapshot_date" = ?`，不含 `status` 条件；`test_snapshot_date_filter_excludes_other_days` |
| `storage.db_path` 默认/优先级 | ✅ | `test_storage_db_path_config_and_default`：配置相对路径、默认 `data/leads.db`、显式参数优先 |
| 固定三列表头 | ✅ | `test_fixed_summary_headers_never_change`：误改 `SUMMARY_HEADERS` 后仍输出固定表头 |
| 口径版本取视图 | ✅ | `test_caliber_versions_prefer_view_over_config`：视图版本覆盖配置版本 |
| 失败异常可统一捕获 | ✅ | `test_build_and_save_failures_raise_export_error`：`ExportDataError` / `ExportWriteError` 均为 `ExportError` |
| 后端真实视图集成 | ✅ | `test_export_against_database_built_from_backend_views`：加载 `backend/pipeline/views.py::CREATE_VIEWS_SQL` 建库导出 |
| 文件名 `线索_YYYYMMDD.xlsx` | ✅ | `test_filename_directory_and_same_day_overwrite`、默认目录测试 |
| 默认 `%USERPROFILE%\Documents\侵权线索\` | ✅ | `test_default_export_dir_uses_userprofile`（monkeypatch USERPROFILE） |
| 口径说明含公式/门槛/版本/免责声明 | ✅ | `test_caliber_sheet_contains_formula_threshold_versions_and_disclaimer`、`test_caliber_uses_configured_threshold_and_view_versions` |
| 金额只来自视图、不重算 | ✅ | 金额/分数原样写入，`0.00` 格式；`test_amounts_use_two_decimal_number_format_without_recalculation` |
| 错误可操作、不静默 | ✅ | 缺视图/同名表/缺列/非法日期/非 http 链接/孤儿链接/公式注入均有测试 |

测试环境：

```text
Python 3.13.5
pytest 9.2.0
openpyxl 3.1.5
Pillow 13.0.0
PyYAML 6.0.3
SQLite 3.46.1
```

## 4. 与后端 v0.1.1 视图列名对齐

依据《后端方案_v0.1》§6（`UPDATES.md` 15:31 指明以新版为准）及后端当前
`pipeline/views.py`（2026-11-05 更新版），视图列名**全部为英文**：

- `v_lead_summary`（19 列）：`lead_id`、`snapshot_date`、`platform`、`shop_id`、
  `shop_name`、`shop_type`、`shop_label_raw`、`ip_codes`、`link_count`、`shop_amount`、
  `payment_ability`、`confidence`、`hit_reasons_summary`、`missing_flags`、
  `review_result`、`reviewer`，辅助列 `shop_type_rank`、`caliber_version`、`config_version`。
- `v_lead_links`（18 列）：`lead_id`、`snapshot_date`、`ip_code`、`product_id`、
  `image_path`、`main_image_url`、`title`、`url`、`price`、`sales_raw`、`comments_raw`、
  `estimated_sales`、`amount`、`amount_formula`、`text_score`、`visual_score`、
  `final_score`、`hit_reasons`、`missing_flags`。

`lead_excel/columns.py::VIEW_COLUMNS` 按上述英文名逐一映射；`SUMMARY_EXCEL_FIELDS` /
`LINK_EXCEL_FIELDS` 只取 16 / 18 个 Excel 业务列。`shop_type_rank` 用于二次排序，
`caliber_version` / `config_version` 只进「口径说明」，三者都不写入业务列。
若后端再次改名，只改 `VIEW_COLUMNS` 对应值，表头/列宽/格式化逻辑不变。

集成自测直接加载后端 `pipeline/views.py::CREATE_VIEWS_SQL` 建临时库（实际执行后端
真实视图定义，不是另抄 SQL），再调用导出：

```text
test_export_against_database_built_from_backend_views  PASS
SHEETS ['线索汇总', '链接明细', '口径说明']
LEADS  1（tmall-20261105-0001）
LINKS  1（P1）
固定表头、只按 snapshot_date 过滤、视图版本优先、openpyxl 读回均通过
```

图片路径已适配后端 `store.py` 的约定：`image_path` 是“后端根目录相对路径”
（如 `data/images/ab/xxxx.jpg`）；未配置 `export.image_base_dir` 时会依次尝试
`db_path` 所在目录、`db_path` 上一级（后端根目录）、当前工作目录。

> 说明：后端视图的 `hit_reasons` 已是“；”拼接文本；`missing_flags` 仍为 JSON 数组文本。
> 导出按视图原样写入（仅保证 C10 非空），不在导出侧改写规则文本或重算金额。

### v_lead_summary（16 个 Excel 业务列；视图 19 列）

| Excel 表头 | 内部字段 | 视图列名 | 类型/说明 |
|---|---|---|---|
| 线索编号 | `lead_id` | `lead_id` | TEXT，`平台-YYYYMMDD-####`；**固定表头** |
| 快照日期 | `snapshot_date` | `snapshot_date` | TEXT `YYYY-MM-DD` |
| 平台 | `platform` | `platform` | taobao/tmall/jd/1688 |
| 店铺ID | `shop_id` | `shop_id` | TEXT |
| 店铺名 | `shop_name` | `shop_name` | TEXT |
| 店铺类型 | `shop_type` | `shop_type` | 企业/个体户/未知/个人 |
| 平台原始标签 | `shop_label_raw` | `shop_label_raw` | TEXT |
| 涉及IP | `ip_codes` | `ip_codes` | TEXT，顿号拼接 |
| 链接数 | `link_count` | `link_count` | INTEGER |
| 店铺合计金额 | `shop_amount` | `shop_amount` | REAL |
| 赔付能力标签 | `payment_ability` | `payment_ability` | TEXT |
| 置信度 | `confidence` | `confidence` | REAL |
| 命中原因摘要 | `hit_reasons_summary` | `hit_reasons_summary` | TEXT，非空（C10） |
| 缺失标记 | `missing_flags` | `missing_flags` | TEXT，可空 |
| 复核结论 | `review_result` | `review_result` | 导出强制空；**固定表头** |
| 复核人 | `reviewer` | `reviewer` | 导出强制空；**固定表头** |

> 辅助列（不写入 Excel）：`shop_type_rank`（企业1/个体户2/未知3/个人4）、
> `caliber_version`、`config_version`；后两者由 `api.py::_resolve_versions` 从汇总
> 视图首行非空值取出，写入「口径说明」。

### v_lead_links（18 个 Excel 业务列；视图 18 列）

| Excel 表头 | 内部字段 | 视图列名 | 类型/说明 |
|---|---|---|---|
| 线索编号 | `lead_id` | `lead_id` | TEXT |
| IP | `ip_code` | `ip_code` | TEXT |
| 商品ID | `product_id` | `product_id` | TEXT |
| 缩略图 | `image_path` | `image_path` | TEXT，本地路径，可空 |
| 原图链接 | `main_image_url` | `main_image_url` | TEXT，http(s) |
| 标题 | `title` | `title` | TEXT |
| 商品链接 | `url` | `url` | TEXT，http(s) |
| 单价 | `price` | `price` | REAL |
| 销量原文 | `sales_raw` | `sales_raw` | TEXT |
| 评论数原文 | `comments_raw` | `comments_raw` | TEXT |
| 估算销量 | `estimated_sales` | `estimated_sales` | INTEGER |
| 金额 | `amount` | `amount` | REAL |
| 计算式 | `amount_formula` | `amount_formula` | TEXT |
| 文字分 | `text_score` | `text_score` | REAL |
| 视觉分 | `visual_score` | `visual_score` | REAL，可空 |
| 综合分 | `final_score` | `final_score` | REAL |
| 命中原因 | `hit_reasons` | `hit_reasons` | TEXT，非空（C10） |
| 缺失标记 | `missing_flags` | `missing_flags` | TEXT，可空 |

日期过滤统一使用 `snapshot_date`；SQL 只有 `WHERE "snapshot_date" = ?`，不追加 `status`。

### 已知边界 / 风险

1. **列名变化**：按 v0.1.1 英文名严格映射；若后端再次改名，会抛
   `ExportDatabaseError: 视图 ... 缺少导出所需列：...`，不会静默少列。
2. **视图过滤语义**：导出侧只加 `WHERE snapshot_date=?`。v0.1.1 已明确两个视图本身只含
   「待复核」线索，因此不再判断/追加 `status`；已确认/驳回不会出现在视图中，压制仍由
   后端聚合阶段保证。
3. **版本列**：使用 `v_lead_summary` 首行非空的 `caliber_version` / `config_version`；
   视图无数据或值为空时才回落 `rules.yaml`；两处都没有写“未提供”。
4. **`review_result` / `reviewer`**：无论视图返回什么，导出都强制留空；回写由后续
   `review.bat` 负责。
5. **只读**：`mode=ro` + `PRAGMA query_only=ON`；若 `storage.db_path` 指向不存在/非文件
   路径，直接抛 `ExportConfigError`，不会创建空库。

## 5. 实现要点 / 设计说明

- **唯一列名映射**：`VIEW_COLUMNS` 键是逻辑字段，值是视图英文列名；SQL 用
  `"真实列名" AS "逻辑字段"`，后端改名后下游逻辑不受影响。
- **只读 / 路径**：`storage.db_path` 默认 `data/leads.db`；SQLite URI `mode=ro` +
  `PRAGMA query_only=ON`；先校验 `sqlite_master.type='view'`；只 SELECT 映射列。
- **固定表头**：`FIXED_SUMMARY_HEADERS` 写死「线索编号」「复核结论」「复核人」，
  写表时强制覆盖，防止误改名破坏 `review.bat` 回写。
- **异常**：公共 API `export_leads_excel()` 统一兜底，非预期异常也转成 `ExportError`；
  主流程 `except ExportError as exc` 后写 `run_logs.error_summary` 并走报警。
- **不重算**：金额、分数、计算式、命中原因全部原样写入；`0.00` 仅是显示格式。
- **缩略图**：Pillow `thumbnail((80, 120))` 保持比例、不放大；EXIF 方向修正；统一 PNG；
  行高按图片高度换算（1px≈0.75pt），避免竖图重叠；支持后端根目录相对 `image_path`。
- **超链接**：仅 http(s) 设置 `Cell.hyperlink`；非 http(s) 只写文本并强制字符串。
- **公式注入**：`=` 开头的文本强制 `data_type='s'`；清理 xlsx 非法控制字符。
- **原子覆盖**：同目录临时文件 + `os.replace`；Excel 占用时提示关闭文件。
- **排序**：Python 侧二次排序，不依赖视图是否 ORDER BY；链接按汇总顺序、同线索金额降序。
- **空数据**：三张 Sheet 照常生成；主表只有表头；口径说明含“本次没有待复核线索”。

## 6. 本轮未做 / 后续建议

- 未做 Web 复核页、SaaS 自动写入、Excel 回写数据库（按方案 §7）。
- 未做图片 URL 下载：只用本地 `image_path`；采集/后端应保证主图已落盘。
- 未做超大报表分页/流式写：openpyxl 普通模式，适合单日线索量；若未来上万行再评估
  `write_only` 或 XlsxWriter。
- 建议联调时确认：后端 `settings.database_path` 与 export 的 `storage.db_path` 是同一文件；
  导出调用放在 `session.commit()` 之后（runner 已如此）；`settings.config_version`
  通过 config 传入（视图有版本列时以视图为准，见 §2）。
- 主流程接入建议：

```python
from lead_excel import ExportError, export_leads_excel

try:
    path = export_leads_excel(None, run_date, config)
except ExportError as exc:
    # 写 run_logs.error_summary，并让第 7 节报警逻辑发送“Excel 导出失败”邮件
    ...
```
