# 后端交付说明（DELIVERY）

- 交付日期：2026-11-05
- 代码位置：`/workspace/ip-monitor/backend`（Windows 上为 `<项目目录>\ip-monitor\backend`）
- 依据：`/workspace/requirements/后端方案_v0.1.md`，需求文档 v0.3 第 6、7 节
- 运行环境：Windows 10 x64 / Python 3.13，与采集模块共用 `..\crawler\.venv`
- 数据库：`data/leads.db`（SQLite，WAL），Alembic 管迁移

## 1. 交付内容

| 内容 | 位置 |
|---|---|
| 一键运行 | `run.bat` → `python -m pipeline run` |
| 复核回写 | `review.bat` → `python -m pipeline review import <Excel>` |
| 表结构 / 迁移 | `pipeline/models.py`、`alembic/versions/0001_initial.py`、`0002_english_views.py` |
| 只读视图 | `pipeline/views.py`（`v_lead_summary`、`v_lead_links`，第 6 节最终列名） |
| Excel 主流程接入 / 失败报警 | `pipeline/excel_export.py`、`pipeline/runner.py` |
| 数据库路径配置 | `config/rules.yaml` 的 `storage.db_path`（默认 `data/leads.db`） |
| 去重压制 / 聚合 | `pipeline/aggregate.py` |
| 判定 / 金额 / 视觉 | `pipeline/scoring.py` |
| 日志 / 报警 | `pipeline/logging_utils.py`、`pipeline/notify.py` |
| SMTP 授权码 | `pipeline/smtp_setup.py`（keyring，仅 `setup-smtp` 写入） |
| 单实例锁 / 备份 | `pipeline/lock.py`、`pipeline/backup.py` |
| 测试 | `tests/test_acceptance.py`、`tests/test_updates_final.py`、`tests/test_b7_jd_detail.py`、`tests/test_r2_fixes.py`、`tests/test_smtp_test_command.py`（44 个用例） |
| 调研 | `research/github-survey.md`（5 个主项目 + 2 个补充，星数/日期/许可证实测） |
| 配置 | `config/rules.yaml` |

## 2. 运行方法

### 2.1 首次安装

```bat
cd /d <项目目录>\ip-monitor\backend
..\crawler\.venv\Scripts\python.exe -m pip install -r requirements.txt
..\crawler\.venv\Scripts\python.exe -m pipeline setup-smtp
```

`setup-smtp` 只把授权码写入 Windows 凭据管理器（服务名 `ip-lead-pipeline-smtp`，
用户名 = 发件邮箱），不写任何文件。发件/收件/SMTP 主机端口在 `config/rules.yaml`
的 `alert` 节。

### 2.2 一键运行

```bat
run.bat
run.bat --platform taobao,jd
run.bat --dry-run
run.bat --input-dir ..\crawler\out\2026-11-05 --platform 1688
```

- `run.bat` 结束后窗口停留并显示各平台条数、线索数、Excel 状态、报警状态、备份路径。
- 单实例锁：已有实例运行时第二次直接提示并退出（退出码 2）。
- 运行前自动备份，保留 14 份；日志 `logs/run_YYYYMMDD.log` 保留 30 天。
- 聚合完成后调用 export 模块生成 `线索_YYYYMMDD.xlsx`；失败写 `run_logs(stage=export)`
  并发报警邮件，不中断已经生成的线索数据。
- 单平台登录失败/异常：只停该平台，其他平台继续；`run_logs` 记录；报警邮件汇总。

### 2.3 复核回写

```bat
review.bat "C:\Users\Xuan\Documents\侵权线索\线索_20261105.xlsx"
```

或：

```bat
..\crawler\.venv\Scripts\python.exe -m pipeline review import "线索_20261105.xlsx"
..\crawler\.venv\Scripts\python.exe -m pipeline review mark-saas taobao-20261105-0001
```

### 2.4 手动迁移 / 补入库

```bat
..\crawler\.venv\Scripts\python.exe -m alembic -c alembic.ini upgrade head
..\crawler\.venv\Scripts\python.exe -m pipeline ingest --platform 1688 ^
  --input-dir ..\crawler\out\2026-11-05 --date 2026-11-05
..\crawler\.venv\Scripts\python.exe -m pipeline score --date 2026-11-05
```

## 3. 验收第 8 节：9 条逐条结果

测试环境：Linux x64 开发容器、Python 3.13.5、pytest 9.2.0；
生产目标环境为 Xuan 的 Windows 10 + 同一 Python 3.13 venv。
运行命令（Windows 把 `python` 换成 `..\crawler\.venv\Scripts\python.exe`）：

```bat
python -m pytest -q
```

**总结果：23 passed（含 2026-11-05 B7 京东临界带返工新增 2 个用例）。** 9 条验收对应测试与实测结果如下（证据日志 `logs/acceptance-9.log`）：

| # | 验收要求 | 测试用例 | 结果 | 关键断言 |
|---|---|---|---|---|
| 1 | 空库 `alembic upgrade head` 建出全部表和两个视图 | `test_01_alembic_upgrade_creates_tables_and_views` | **PASS**（1 passed） | `alembic_version` + 10 张业务表；`v_lead_summary` 19 列 / `v_lead_links` 19 列，列名、顺序、类型精确匹配第 6 节；`0002` 对存量库重建视图 |
| 2 | 同一商品同日跑两次：products 1 行、当日快照 1 行；次日 2 行 | `test_02_same_product_same_day_and_next_day` | **PASS**（1 passed） | 同日第二次运行后 product=1、snapshot=1；次日 snapshot=2；第二次运行前自动生成备份 |
| 3 | 已录入SaaS 90 天、驳回 30 天内被压制（C3） | `test_03_processed_lead_suppressed_next_day`、`test_03b_rejected_suppression_window` | **PASS**（2 passed） | 标记 SaaS 后次日 0 新线索，快照 `hit_reasons` 含 `已处理压制:已录入SaaS`；驳回第 29 天 0 条、第 31 天恢复 1 条 |
| 4 | 改 yaml 后新记录 config_version 变化，旧记录不变（C5） | `test_04_config_version_change_keeps_history` | **PASS**（1 passed） | 两份 YAML hash 不同；旧快照仍为版本 A、金额 600000；新快照版本 B、金额 300000；`config_versions` 两条 |
| 5 | 登录失效只停该平台、其他平台继续、run_logs 有记录、收到报警；低条数同样报警（C7） | `test_05_login_failure_and_low_items_alert` | **PASS**（1 passed） | taobao `login_failed`、jd `ok`；`run_logs` 3 行（taobao/jd/overall）；邮件正文同时含登录失效与 `jd: 采集条数 1 < 下限 10` |
| 6 | 双击两次 `run.bat`：第二次提示已在运行并退出 | `test_06_single_instance_lock`、`test_cli_second_instance_exits_2` | **PASS**（2 passed） | 第一次持锁成功，第二次抛 `AlreadyRunning`；独立 CLI 进程在锁被占用时退出码 2、输出“已在运行”；`run.bat` 含 `pause` |
| 7 | 运行前自动备份，超过 14 份自动清理 | `test_07_backup_and_keep_14`（+ 用例 2 的运行前备份断言） | **PASS**（1 passed） | 20 个旧备份 + 1 个新备份 → 保留 14 份；新备份可读且含 `backup-marker` |
| 8 | 数据库和日志目录搜不到授权码 | `test_08_no_auth_code_on_disk` | **PASS**（1 passed） | 授权码只存在 mock keyring；DB 二进制、`logs/`、`backups/`、`data/`、配置文件均搜不到哨兵字符串 |
| 9 | 第 9 节 B1–B7 通过视图结果复核一致 | `test_09_b1_b7_aggregation_via_views` | **PASS**（1 passed） | B1 S1=550000、2 链接；B2 恰好 500000 无；B3 不跨日无；B4 同名不同 ID 两条并标 `同名不同店`；B5 无 ID 有店名正常聚合、店名为空只进明细并标 `店铺名缺失`；B6 下架不计；B7 40 万触发、20 万不触发 |

补充用例（同样通过）：

| 用例 | 结果 | 关键断言 |
|---|---|---|
| `test_amount_cases_a1_a6` | PASS | A1 100000、A2 15000、A3 792000+销量缺失、A4 200000+按下限、A5 2000+评论缺失/待核验、A6 0+待人工补齐 |
| `test_combined_score_floor_and_cap` | PASS | 视觉 80/文字 20 → ≥75；视觉 10/文字 90 → ≤35；无视觉 final=text |
| `test_hard_exclusion_and_whitelist` | PASS | 正版信号命中排除；白名单按 platform+shop_id+IP 命中；均不生成线索 |
| `test_whitelist_is_scoped_by_ip` | PASS | 同店白名单只排除对应 IP；同店另一 IP 正常参与聚合并生成线索 |
| `test_runner_scores_and_aggregates_direct_table_writes` | PASS | 采集模块直接写原始快照（未判定）后，`run` 自动补判定并生成 600000 线索 |
| `test_review_excel_import_and_invalid_value` | PASS | Excel「线索汇总」已确认写回、写 history；非法结论跳过并在窗口提示 |

实机/集成补证：

```text
python -m pipeline run --dry-run --input-dir ../crawler/out/2026-11-05 --platform 1688 --no-alert
→ 1688: 登录正常，采集 51 条，状态 ok；生成线索 0 条；真实库未写入、未发邮件
```

## 4. 只读视图最终列名清单（后端方案第 6 节 2026-11-05 更新版）

两个视图都只包含状态为「待复核」的线索；日期为 TEXT `YYYY-MM-DD`，金额/分数为
REAL，计数为 INTEGER。已确认/驳回/已录入SaaS 的线索不会进入 Excel 数据源。

### 4.1 `v_lead_summary`（19 列，一店一行）

| 序号 | 列名 | 类型 | 说明 |
|---:|---|---|---|
| 1 | `lead_id` | TEXT | `leads.lead_no` 别名，`平台-YYYYMMDD-####` |
| 2 | `snapshot_date` | TEXT | `YYYY-MM-DD` |
| 3 | `platform` | TEXT | taobao/tmall/jd/1688 |
| 4 | `shop_id` | TEXT | 不拿店铺名代替 |
| 5 | `shop_name` | TEXT | 仅展示 |
| 6 | `shop_type` | TEXT | 企业/个体户/个人/未知 |
| 7 | `shop_label_raw` | TEXT | 天猫、旗舰店等 |
| 8 | `ip_codes` | TEXT | 多条用 `、` 分隔 |
| 9 | `link_count` | INTEGER | 进入聚合的链接数 |
| 10 | `shop_amount` | REAL | 当日同店链接金额之和，2 位小数 |
| 11 | `payment_ability` | TEXT | 平台可得信号，`、` 分隔 |
| 12 | `confidence` | REAL | 0–100 |
| 13 | `hit_reasons_summary` | TEXT | 非空 |
| 14 | `missing_flags` | TEXT | 可空 |
| 15 | `review_result` | TEXT | 空 |
| 16 | `reviewer` | TEXT | 空 |
| 17 | `shop_type_rank` | INTEGER | 企业 1、个体户 2、未知 3、个人 4 |
| 18 | `caliber_version` | TEXT | 口径版本 |
| 19 | `config_version` | TEXT | 配置版本 |

排序：`shop_type_rank, confidence DESC, shop_amount DESC`。

### 4.2 `v_lead_links`（19 列，一链接一行）

| 序号 | 列名 | 类型 | 说明 |
|---:|---|---|---|
| 1 | `lead_id` | TEXT | 关联 leads.lead_no |
| 2 | `snapshot_date` | TEXT | `YYYY-MM-DD` |
| 3 | `ip_code` | TEXT | 快照 ip_code |
| 4 | `product_id` | TEXT | products.product_id |
| 5 | `image_path` | TEXT | 本地路径（可空），供 Excel 嵌入 |
| 6 | `main_image_url` | TEXT | 原图链接 |
| 7 | `title` | TEXT | products.title |
| 8 | `url` | TEXT | 商品链接 |
| 9 | `price` | REAL | 快照 price |
| 10 | `sales_raw` | TEXT | 销量原文 |
| 11 | `comments_raw` | TEXT | 评论数原文 |
| 12 | `estimated_sales` | INTEGER | 按下限/倍数计算后的整数 |
| 13 | `amount` | REAL | 单链接金额，2 位小数 |
| 14 | `amount_formula` | TEXT | `lead_links.formula` 别名 |
| 15 | `text_score` | REAL | 0–100 |
| 16 | `visual_score` | REAL | 0–100 或空 |
| 17 | `final_score` | REAL | 0–100 |
| 18 | `hit_reasons` | TEXT | 非空，JSON 列表转成“；”拼接文本 |
| 19 | `missing_flags` | TEXT | 快照缺失标记 JSON 文本，可空 |

排序：`snapshot_date DESC, lead_id ASC, amount DESC`。
导出侧仍用 `WHERE snapshot_date=?` 过滤；视图本身已过滤「待复核」。

## 5. 关键设计说明

1. **config_version**：对 YAML 原始字节做 SHA-256 取前 12 位，不依赖手工版本号；
   `config_versions` 表保留首次使用时间与 YAML 全文，快照/线索各记一份。
2. **同日快照覆盖**：`product_snapshots(product_id, snapshot_date)` 唯一；同一天重跑
   更新同一条，次日插入新条。
3. **压制在聚合前**：按 `platform+shop_id+ip_code` 查历史已处理线索，命中则只写
   `hit_reasons`，不进入聚合；90/30 天从 state 的 `reviewed_at`/`saas_entered_at` 起算。
4. **同店同日唯一线索**：`leads(platform, shop_id, snapshot_date)` 唯一；
   `lead_links(lead_id, snapshot_id)` 唯一；同日重跑更新待复核线索，已处理线索不覆盖。
5. **金额严格大于门槛**：`total_amount > aggregate.shop_threshold`；B2 的 500000 不生成。
6. **邮件旁路**：keyring 读取失败、SMTP 发送失败都只写日志，不改变 run 结果；
   邮件正文不含授权码。
7. **视觉分**：pHash + 参考图最小汉明距离，`100 - 3×distance` 截断；
   同 `image_phash+ip_code` 复用 `vision_cache`。未配置参考图时视觉分为空，final=text。
8. **Excel 接入**：`pipeline/excel_export.py` 直接调用 export 模块公开接口
   `export_leads_excel(db_path, snapshot_date, config)`；数据库路径传
   `settings.database_path`（由 `storage.db_path` 解析），并在 config 里同步
   `storage.db_path`、`config_version`、`caliber_version`。
9. **导出失败闭环**：主流程捕获导出模块异常 → 写 `run_logs(stage='export',
   status='error')` → 整体 `run_logs` 状态 partial → 第 7 节报警邮件带
   `Excel 导出失败: ...`；邮件/日志都不影响已生成的线索数据。

## 6. GitHub 调研结论（详见 `research/github-survey.md`）

实测 5 个主项目：

| 项目 | 星数 | 许可证 | 借鉴 |
|---|---:|---|---|
| changedetection.io | 35,520 | Apache-2.0 | 监控/通知分层，通知失败不拖垮主流程 |
| ECommerceCrawlers | 5,765 | MIT | 按平台采集与入库分离 |
| alembic | 4,466 | MIT | 迁移 + SQLite batch 模式（直接依赖） |
| Price-Tracking-Web-Scraper | 1,293 | 无 LICENSE | 只借双击入口/终端摘要思路，不复制代码 |
| Crinibus/scraper | 252 | MIT | Product 与按日 DataPoint 分离 |
| （补充）PriceDive | 94 | MIT | 中文平台 + SQLite + run.bat 可行性 |

未整体 fork；MIT/Apache 也只借结构，未复制源码；无许可证项目只借产品思路。

## 7. 已知边界 / 移交事项

1. **Excel 文件由 export 模块生成**：本后端主流程调用其 `export_leads_excel`，
   只负责视图数据、调用时机、失败留痕与报警，不自行写 xlsx。
2. **真实 SMTP 未在开发容器发送**：测试用 mock SMTP + mock keyring 验证逻辑；
   需 Xuan 在本机执行 `setup-smtp` 后做一次真实收件验收。
3. **Windows 专属分支**：`keyring` 的 Windows Credential Locker、`run.bat` 双击、
   批处理窗口停留需在 Xuan 本机实机验收；Linux 自动测试覆盖了同样的锁/CLI 逻辑。
4. **视觉参考图**：`config/rules.yaml` 的 `reference_images` 目前为空；补图后才会产生视觉分。
5. **平台字段**：采集模块的 `shop_type` 由 `shop_type_raw_hint`/标签映射；1688 常见
   `shop_id=null`，按 C5/B5 规则不参与聚合，单列待人工补。
6. **数据库迁移 PostgreSQL**：模型与迁移未用 SQLite 专有语法；运行时可用
   `IP_LEAD_DB_URL` 覆盖连接串。备份逻辑仅对 SQLite 生效。

## 8. 2026-11-05 15:31 补充项逐条落实

| # | UPDATES / 后端方案第 6 节要求 | 落实位置 | 验证 |
|---:|---|---|---|
| 1 | `v_lead_summary` / `v_lead_links` 列名和类型以第 6 节新版为准；后端和 Excel 导出都按新版 | `pipeline/views.py` 重写为 19/19 列英文名；`0002_english_views.py` 对存量库重建视图；`pipeline/excel_export.py` 调用 export 时使用新视图 | `test_01_alembic_upgrade_creates_tables_and_views` 精确断言列名、顺序、TEXT/INTEGER/REAL 类型；`test_09_b1_b7_aggregation_via_views` 改用 `lead_id`/`shop_amount`/`amount_formula`；`test_runner_calls_export_module_and_writes_excel` 用真实 export 模块从新视图生成 xlsx |
| 2 | 视图只含「待复核」；`snapshot_date` 为 TEXT `YYYY-MM-DD`；`shop_type_rank` 企业1/个体户2/未知3/个人4；按 rank、置信度、金额排序 | `pipeline/views.py` 的 `WHERE l.status='待复核'`、`CAST(... AS TEXT)`、`CASE`、`ORDER BY` | `test_views_only_contain_pending_leads` 验证已确认后两个视图立即为空；`test_09...` 验证 rank 与排序；`test_01...` 验证日期列 TEXT |
| 3 | `hit_reasons`（链接视图）非空，JSON 列表转“；”拼接文本 | `pipeline/views.py` 用 `json_each + group_concat(..., '；')`，空列表回落“命中规则” | `test_09...` 断言每条链接 `hit_reasons` 非空且含“；”；Excel 导出用例正常读取 |
| 4 | 数据库配置键 `storage.db_path`，默认 `data/leads.db`（导出模块也读这个键） | `config/rules.yaml` 新增 `storage.db_path`；`Settings.database_path` 优先读该键，兼容旧 `paths.database`；`pipeline/excel_export.py` 把解析后的绝对路径同时作为 `db_path` 参数和 `storage.db_path` 传给导出模块 | `test_storage_db_path_key_is_used_and_legacy_paths_database_is_compatible` 验证默认值、优先级和旧配置兼容；`test_runner_calls_export_module_and_writes_excel` 验证主流程使用同一路径 |
| 5 | 主流程在「判定+写 leads 完成」后、报警检查前调用 `/workspace/ip-monitor/export` 的 `export_leads_excel` | `pipeline/runner.py` 聚合提交后调用 `pipeline.excel_export.export_leads_for_run`；`export.enabled=true` 默认开启，`exporter=` 钩子仍可注入 | 成功用例断言 `summary.excel` 存在、文件名为 `线索_20261105.xlsx`、`summary.failed=False` |
| 6 | 导出失败写 `run_logs` 并发报警邮件，不静默吞掉 | `pipeline/runner.py` 捕获异常：写 `run_logs(stage='export', status='error', error_summary='Excel 导出失败: ...')`，errors 进入整体汇总，随后 `alert_for_run(extra_lines=[...])` 走第 7 节邮件；`RunSummary.excel_error` 在窗口摘要显示 | `test_runner_export_failure_logs_run_logs_and_alerts` 用抛 `RuntimeError("xlsx 被占用")` 的 exporter，断言 export 行、overall partial、1 封邮件正文含错误摘要且不含授权码 |
| 7 | 补单测、pytest 全过 | `tests/test_updates_final.py` 新增 4 个用例；原验收用例同步到新视图列名 | `python -m pytest -q` → 本阶段 21 passed；B7 返工后总数 **23 passed** |
| 8 | 保留 Excel 汇总表头「线索编号」「复核结论」「复核人」供 review.bat 回写 | 视图只改字段名，不改 Excel 中文表头；`lead_excel/columns.py` 的 `SUMMARY_HEADERS` 仍固定这三列；`pipeline/review.py` 仍按这三列读取 | `test_review_excel_import_and_invalid_value`；export 模块自身 35 个用例不受影响 |

## 9. 结论

后端方案第 3–7 节已实现，第 8 节 9 条验收全部 PASS；补充项（第 6 节最终视图、
`storage.db_path`、Excel 主流程接入与失败报警）全部落实，**23 个 pytest 用例通过**。
`v_lead_summary` / `v_lead_links` 列名、类型、排序已锁定为新版，可交代码验收。

BACKEND_DONE2

## 10. 2026-11-05 后端初验返工：B7 京东临界带

初验意见：`pipeline/cli.py` 构造 `PipelineRunner` 时没有注入 `jd_detail_fetcher`，
且 `runner.py` 只遍历已经过门槛的线索（合计 > 500000）判断临界带，
因此默认 `run.bat` 永远不会对 300000–499999.99 的京东店铺补抓详情。

本次返工（未修改同级 `crawler/`、`export/` 目录）：

| 修改点 | 位置 | 说明 |
|---|---|---|
| 默认流程注入补抓器 | `pipeline/cli.py` | `cmd_run` 构造 runner 时注入 `make_jd_detail_fetcher(settings)`；保留 monkeypatch 测试缝 |
| 京东详情补抓 | `pipeline/jd_detail.py`（新增） | 默认调用同级采集模块 `python -m crawler detail --platform jd --ids-file ... --config ../crawler/config/rules.yaml`；用临时 ID 文件，读取 `out/<date>/jd_detail.jsonl` 的销量/评论/价格/下架字段 |
| 临界带识别 | `pipeline/aggregate.py` | 新增 `jd_near_band_targets()`，复用聚合完全相同的合格/下架/店铺ID/压制过滤，返回店铺合计落在 `0.6×门槛 ≤ 合计 < 1.0×门槛` 的京东店铺及链接 |
| 写回并重算 | `pipeline/runner.py` | 同一次运行：识别临界带店铺 → 逐店补抓 → patch 写回 `product_snapshots`（销量/评论/价格/下架）→ 清除旧“销量缺失”等派生标记 → `score_snapshot` 重算金额 → **重新 `aggregate_snapshots` 再判 500000 门槛**；单店失败只记日志，不影响其他店铺/平台 |
| 依赖说明 | `README.md` | 新增“同级模块依赖”：backend 默认依赖 `../crawler/`（列表采集 + 京东详情补抓）与 `../export/`（Excel），三者必须同级部署，backend 只读引用 |
| 回归测试 | `tests/test_b7_jd_detail.py`（新增） | 默认入口 `pipeline.cli.cmd_run` 构造的 runner：合计 400000 的京东店铺触发 1 次补抓，假 fetcher 把销量改为 3000 后重算为 600000 并生成线索；合计 200000 不触发补抓、不生成线索。全部使用假 fetcher + 本地 JSONL，不访问真实网站 |

验证命令与结果：

```text
/workspace/ip-monitor/crawler/.venv/bin/python -m pytest -q
23 passed in 2.16s
```

B7_DONE

## 11. 2026-11-05 后端复验遗留两处修复：B8

本次只改后端 `pipeline/runner.py`、`alembic/env.py` 及对应测试，未修改同级
`crawler/`、`export/` 目录。

| # | 问题 | 修复 | 测试 |
|---:|---|---|---|
| 1 | 京东详情补抓失败原来只 `logger.error`，`run_logs` 无记录、报警邮件收不到 | `runner.py` 新增 `_log_jd_detail_failure()`：每个失败店铺在聚合成功后写一行 `run_logs(stage='jd_detail', status='error')`，错误摘要含店铺/合计/异常；同时把错误加入本次 `errors`，使整体状态为 `partial`，并作为 `extra_lines` 进入报警邮件 | `tests/test_b7_jd_detail.py::test_jd_detail_failure_logs_run_logs_and_alerts`：400000 临界带店铺的假 fetcher 抛 `RuntimeError("详情页超时")`，断言 jd_detail 行、overall partial、1 封邮件正文含错误且不含授权码 |
| 2 | `alembic/env.py` 的 `fileConfig` 默认 `disable_existing_loggers=True`，会禁用 `runner.py` 在 `ensure_schema` 之前建立的 `pipeline` logger，导致 `logs/run_YYYYMMDD.log` 迁移后写不进去 | `alembic/env.py` 改为 `fileConfig(..., disable_existing_loggers=False)` | `tests/test_updates_final.py::test_migration_does_not_disable_pipeline_file_logger`：先 `setup_run_logger`，再 `ensure_schema`，随后写日志哨兵，断言当天 `logs/run_YYYYMMDD.log` 仍包含该内容 |

验证命令与结果：

```text
/workspace/ip-monitor/crawler/.venv/bin/python -m pytest -q
25 passed in 2.00s
```

B8_DONE

## 12. 2026-11-05 R2 终审返工逐条落实（仅 backend）

本次返工范围仅 `/workspace/ip-monitor/backend`，未修改同级 `crawler/`、`export/`。
对应《R2终审_backend_export_v0.1.md》第 5 节返工清单：

| # | R2 意见 | 修复位置 | 验证 |
|---:|---|---|---|
| 1 | **[P1] B8：`CrawlerJdDetailFetcher` 真实子进程失败被吞，不写 `run_logs`、不报警** | `pipeline/jd_detail.py`：解析 stdout 末尾 `RunRecord`；`returncode != 0`、`status` 不在 `ok/disabled`、`login_ok is False` 或找不到记录时抛含 shop/status/errors/stderr 的 `RuntimeError`；补抓前记录文件偏移，只读本次新增 JSONL，避免旧 `jd_detail.jsonl` 脏数据 | `tests/test_r2_fixes.py::test_crawler_jd_detail_fetcher_raises_on_nonzero_exit`、`test_crawler_jd_detail_fetcher_raises_on_login_failed`、`test_crawler_jd_detail_fetcher_ignores_stale_rows`、`test_real_jd_fetcher_failure_logs_run_logs_and_alerts`；真实 fetcher 路径触发 `run_logs(stage='jd_detail')` 与报警邮件 |
| 2 | **[P1] C4：淘宝/天猫详情按条件补抓缺失** | 新增 `pipeline/taobao_detail.py`：读取 `fetch.taobao.detail_min_text_score`（默认 35），对未硬排除且 `text_score >= 35` 或 `need_vision` 且尚无评论数的商品批量调用 `python -m crawler detail --platform taobao`；天猫写 `tmall:<product_id>`；回填 `comments_raw/comments` 后 `score_snapshot` 重算金额；失败写 `run_logs(stage='taobao_detail')` 并进入报警 | `tests/test_r2_fixes.py::test_c4_taobao_detail_policy`（天猫触发、`tmall:` 前缀、min 公式重算为 600000）、`test_c4_below_threshold_and_no_need_vision_skips`、`test_c4_need_vision_triggers`、`test_crawler_taobao_detail_fetcher_passes_tmall_prefix`、`test_c4_taobao_detail_failure_logs_and_alerts`；日志打印触发/跳过条数 |
| 3 | **[P2] `whitelist_file: whitelist.csv` 无加载实现** | 新增 `pipeline/whitelist.py`：按 `rules.yaml` 所在目录优先、后端根目录兜底解析 CSV；支持 `platform,shop_id,ip_code,reason,active`，同步 `whitelist` 表；文件不存在/为空不报错；`runner.run()` 每次启动时同步 | `tests/test_r2_fixes.py::test_whitelist_csv_sync_changes_take_effect`（加行即排除，清空表头后恢复参与聚合）、`test_whitelist_missing_or_empty_file_does_not_raise` |
| 4 | **[P2] 天猫 `platform=tmall` 被通道名 `taobao` 覆盖** | `pipeline/store.py`：通道为 taobao/tmall 时采用 `data["platform"]` 写 `Shop/Product`；`pipeline/runner.py`：taobao 正常时 `eligible_platforms` 同时含 `taobao`/`tmall`，空 JSONL 条数统计合并 tmall，并正确归属线索数 | `tests/test_r2_fixes.py::test_tmall_platform_is_preserved_and_aggregated`（product/shop/lead 均为 tmall，编号 `tmall-...`）、`test_runner_counts_existing_tmall_snapshots_for_taobao_channel`、`test_whitelist_csv_sync_changes_take_effect`（tmall 白名单命中） |
| 5 | **[P2] 报警靠 `extra_lines` 旁路；`min_items_per_platform=10`** | `pipeline/notify.py`：去掉 `platform IS NOT NULL`，从本次 `run_id` 的 `run_logs(stage != 'overall')` 统一读取 crawl 登录/低条数及 export/jd_detail/taobao_detail 错误；`pipeline/runner.py` 删除 `alert_extra_lines` 旁路；`config/rules.yaml` 改为 `min_items_per_platform: 50` | `tests/test_r2_fixes.py::test_collect_alert_lines_reads_export_and_detail_stages`、`test_alert_min_items_config_is_50`；原 `test_runner_export_failure_logs_run_logs_and_alerts`、B8 用例仍通过 |
| 6 | **[P2] 近门槛未过线店铺被静默丢弃** | `pipeline/aggregate.py::near_threshold_shops` 输出 `[0.6×threshold, threshold]` 店铺并写入 `RunSummary.near_threshold`/运行日志/窗口摘要；`pipeline/views.py` 新增只读 `v_near_threshold_shops`，`alembic/versions/0003_near_threshold_view.py` 建视图；`v_lead_summary`/`v_lead_links` 19/19 列契约不变 | `tests/test_r2_fixes.py::test_near_threshold_shops_view_and_run_summary`（400000 店铺进入视图和运行报告，`v_lead_summary` 仍为 0） |
| 7 | **[P3] 复核人尾列空值 / 采集输出目录** | `pipeline/review.py` 改为按列下标安全读取（行被裁掉尾列也能处理）；`pipeline/crawler_adapter.py` 注入 `CRAWLER_OUTPUT_DIR` | `tests/test_r2_fixes.py::test_review_import_handles_trimmed_reviewer_cell`；采集环境变量与 `jd_detail.py` 保持一致 |

验证命令与结果：

```text
cd /workspace/ip-monitor/backend
/workspace/ip-monitor/crawler/.venv/bin/python -m pytest -q
42 passed in 3.00s

cd /workspace/ip-monitor/export
/workspace/ip-monitor/crawler/.venv/bin/python -m pytest -o addopts= -q
35 passed
```

R2FIX_DONE

## 13. 2026-11-05 新增 SMTP 测试命令（test-smtp）

本次仅修改 `/workspace/ip-monitor/backend`，未修改 `config/rules.yaml`（含
`crawler_config`）、同级 `crawler/` 与 `export/`。

| 项目 | 落实 |
|---|---|
| 命令入口 | `pipeline/cli.py` 新增 `cmd_test_smtp` 与 `test-smtp` 子命令：`python -m pipeline test-smtp` |
| 邮件内容 | 标题固定 `[侵权线索] SMTP 测试`；收件人取配置 `alert.to`，主机/端口/SSL/发件人取 `alert.*` |
| 复用发送代码 | `pipeline/notify.py` 抽出共享 `_send_email`，原 `send_email`（报警）与新 `send_test_email` 走同一套 keyring 读取、SMTP 连接、登录、`send_message` 逻辑 |
| 成功/失败输出 | 成功打印“已发送”并返回 0；失败打印“发送失败：原因”并返回 1 |
| 脱敏 | `redact_secret` 对返回原因和日志统一抹掉授权码/密码为 `******`；认证失败即使服务端响应回显密码也不会进入窗口或日志 |
| 报警开关 | 显式 `test-smtp` 不受 `alert.enabled=false` 影响，便于关闭报警时单独验证 SMTP 配置 |
| README | 第 2 节新增“测试 SMTP 配置”命令说明；第 8 节测试数同步为 44 |
| 单测 | 新增 `tests/test_smtp_test_command.py`：mock smtplib 成功发送（标题/收件人/登录/输出“已发送”）；认证失败时退出码 1、输出含失败原因但绝不含授权码 |
| 数据库/采集 | `test-smtp` 只发邮件，不读写线索数据库，不触发采集/Excel 导出 |

验证命令与结果：

```text
cd /workspace/ip-monitor/backend
/workspace/ip-monitor/crawler/.venv/bin/python -m pytest -q
44 passed in 2.73s
```

SMTP_DONE
