# 电商侵权线索后端（ip-monitor/backend）

面向 Xuan 的 Windows 10 本机运行：SQLite + SQLAlchemy + Alembic，PyYAML 读规则，
`imagehash` 做视觉指纹，`keyring` 存 SMTP 授权码，`smtplib` 发报警。
与采集模块共用同一套 Python 3.13 venv：`..\crawler\.venv`。

本后端只负责“入库 / 判定 / 去重压制 / 线索聚合 / 复核回写 / 日志报警 / 只读视图”；
Excel 文件由 export 模块读取 `v_lead_summary`、`v_lead_links` 生成。

## 0. 同级模块依赖（backend 不复制、不修改）

backend 默认运行依赖仓库里与它同级的两个目录：

| 同级目录 | backend 用途 | 调用方式 |
|---|---|---|
| `../crawler/` | 三平台列表采集；京东临界带（店铺合计 0.6–1.0 门槛）详情补抓 | `python -m crawler run` / `python -m crawler detail` |
| `../export/` | 读取 `v_lead_summary`、`v_lead_links` 生成 Excel | `pipeline/excel_export.py` 调用 `export_leads_excel` |

因此部署时必须保持 `ip-monitor/backend`、`ip-monitor/crawler`、`ip-monitor/export`
三个目录同级；只把 backend 单独拷走会导致采集、京东详情补抓或 Excel 导出不可用。
backend 对这两个目录只读引用，不修改其代码或配置。

## 1. 目录

```
backend/
├─ run.bat                 # 双击一键运行（单实例锁、备份、采集、判定、报警）
├─ review.bat              # 双击选择 Excel，写回复核结论
├─ config/rules.yaml       # 规则 + 后端 paths/run/alert 配置
├─ pipeline/               # 业务代码
│  ├─ config.py            # YAML、config_version=sha256 前 12 位
│  ├─ models.py            # ORM：方案第 3 节全部表
│  ├─ views.py             # v_lead_summary / v_lead_links（第 6 节最终列名）
│  ├─ excel_export.py      # 调用 export 模块 export_leads_excel，失败交给 runner 报警
│  ├─ jd_detail.py         # 京东临界带详情补抓（调用同级 crawler detail）
│  ├─ db.py                # WAL、外键、Alembic upgrade
│  ├─ store.py             # 采集结果 → products/shops/product_snapshots
│  ├─ scoring.py           # 排除、文字分、视觉分、综合分、金额
│  ├─ aggregate.py         # 压制、同店同日聚合、线索生成
│  ├─ review.py            # Excel 复核导入 / mark-saas
│  ├─ lock.py              # data/run.lock（PID 探活）
│  ├─ backup.py            # 运行前备份，保留 14 份
│  ├─ logging_utils.py     # logs/run_YYYYMMDD.log，保留 30 天
│  ├─ notify.py            # smtplib + keyring，失败不影响主流程
│  ├─ smtp_setup.py        # setup-smtp，交互写入凭据管理器
│  └─ runner.py            # 一键运行编排
├─ alembic/                # 迁移（0001 建表 + 两个视图）
├─ tests/                  # pytest，23 个用例
├─ research/github-survey.md
├─ README.md
└─ DELIVERY.md
```

## 2. 首次安装（Xuan 本机）

前提：Windows 10 x64、Python 3.13；采集模块已建好 `..\crawler\.venv`。

```bat
cd /d D:\path\to\ip-monitor\backend
..\crawler\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

依赖：`SQLAlchemy>=2.0`、`alembic`、`PyYAML`、`keyring`、`imagehash`、`Pillow`、`openpyxl`、`pytest`。
`requirements.txt` 不含任何授权码。

> 仓库里的 `.deps/` 只是 Linux 开发容器的测试依赖缓存（已 gitignore），Windows 运行
> 不依赖它；Xuan 本机只需把 `requirements.txt` 装进 `..\crawler\.venv`。

### 首次配置 SMTP 授权码

```bat
..\crawler\.venv\Scripts\python.exe -m pipeline setup-smtp
```

- 交互输入发件邮箱（默认取 `config/rules.yaml` 的 `alert.from`）和授权码；授权码输入不回显。
- 只写入 Windows 凭据管理器：服务名 `ip-lead-pipeline-smtp`，用户名 = 发件邮箱。
- 代码、配置、SQLite、日志、备份里都不出现授权码；`test_08` 会扫描落盘文件验证。
- 重新设置随时再跑一次 `setup-smtp`。

### 测试 SMTP 配置

配置好授权码后，可发一封测试邮件确认主机、端口、SSL、发件人、收件人和授权码都可用：

```bat
..\crawler\.venv\Scripts\python.exe -m pipeline test-smtp
```

- 按 `config/rules.yaml` 的 `alert` 节发送，复用报警邮件的发送代码；标题固定为
  `[侵权线索] SMTP 测试`。
- 成功在窗口打印“已发送”（退出码 0）；失败打印原因（退出码 1）。
- 失败原因里的授权码/密码统一脱敏为 `******`，窗口和日志都不会出现明文。
- 该命令只发测试邮件，不读写线索数据库，也不触发采集或 Excel 导出。

## 3. 一键运行

双击 `run.bat`，或在 cmd 中：

```bat
run.bat
run.bat --platform taobao,jd
run.bat --dry-run
run.bat --input-dir ..\crawler\out\2026-11-05 --platform 1688
```

参数：

| 参数 | 说明 |
|---|---|
| `--platform taobao,jd,1688` | 只跑指定平台；默认读配置里 enabled 的平台 |
| `--date YYYY-MM-DD` | 快照日期，默认今天 |
| `--dry-run` | 用临时 SQLite 演练：不写真实库、不备份、不发邮件；仍打印摘要 |
| `--input-dir DIR` | 不调用采集，直接读目录里的 `<platform>.jsonl` / `<platform>.run.json`（补跑/回归） |
| `--no-alert` | 本次不发报警邮件 |
| `--config PATH` | 换一份 rules.yaml |

`run.bat` 结束后窗口停留，显示：各平台登录/条数/状态、生成线索数、Excel 状态、是否报警、备份路径。

运行顺序：

```
加载配置并登记 config_version
→ data/run.lock 单实例锁（PID 探活，异常退出可自动失效）
→ 备份 data/leads.db 到 backups/leads_YYYYMMDD_HHMMSS.db（保留 14 份）
→ alembic upgrade head
→ 逐平台调用采集（每个平台独立子进程；单平台失败只停该平台）
→ products / product_snapshots 入库
→ 排除 → 文字分 → 视觉分 → 综合分 → 金额
→ 已处理压制 → 同 platform+shop_id 当日聚合
→ 京东店铺合计落在 0.6–1.0 门槛带：调用同级 crawler 补抓链接详情
→ 销量/评论写回快照并重算 → 重新聚合、再判门槛 → 线索（待复核）
→ 调用 export 模块生成 Excel（失败写 run_logs 并报警）
→ 写 run_logs → 报警检查（登录失效 / 条数低于下限 / Excel 导出失败）
```

### 采集模块如何写这两张表

两条路径，最终都走 `pipeline/store.py`：

1. **保持现有 JSONL**（当前采集模块输出）：`pipeline/store.upsert_many()` 读取
   `../crawler/out/<date>/<platform>.jsonl` 后写 `products`、`product_snapshots`。
2. **采集模块直接 import**：

```python
from pipeline.store import upsert_crawl_item

upsert_crawl_item(
    session,
    platform="taobao",
    data={
        "product_id": "123", "url": "...", "title": "...",
        "shop_id": "S1", "price": 29.9, "sales": 100,
        "crawled_at": "2026-11-05T10:00:00+08:00",
        "ip_code": "huluxiongdi", "search_keyword": "葫芦娃",
        "listed": True, "flags": [],
    },
    settings=settings, run_id=run_id, snapshot_date=run_date,
)
```

唯一键：`products(platform, product_id)`；`product_snapshots(product_id, snapshot_date)`。
同日重跑更新同一行，不新增；次日新增一行（C8）。

手动导入 / 重跑判定：

```bat
..\crawler\.venv\Scripts\python.exe -m pipeline ingest --platform 1688 ^
  --input-dir ..\crawler\out\2026-11-05 --date 2026-11-05
..\crawler\.venv\Scripts\python.exe -m pipeline score --date 2026-11-05
```

`score` 对已经存在于 `product_snapshots` 的原始行重跑排除/文字分/视觉分/金额，
适用于采集模块直接写表、或补了视觉参考图后重算。

## 4. 复核回写

Excel「线索汇总」必须有 `线索编号`、`复核结论`、`复核人` 三列；复核结论只认
`已确认` / `驳回`，其他值跳过并在窗口提示。

```bat
review.bat "C:\Users\Xuan\Documents\侵权线索\线索_20261105.xlsx"
```

等价命令：

```bat
..\crawler\.venv\Scripts\python.exe -m pipeline review import "线索_20261105.xlsx"
..\crawler\.venv\Scripts\python.exe -m pipeline review mark-saas taobao-20261105-0001
```

- 状态流转：`待复核 → 已确认 / 驳回 → 已录入SaaS`。
- 每次变化追加 `lead_status_history`，只增不改。
- 回写后，次日聚合按 `dedup.suppress_days=90`（已确认/已录入SaaS）或
  `suppress_days_rejected=30`（驳回）压制同 `platform+shop_id+ip_code` 的新链接，
  并写入快照 `hit_reasons` 的 `已处理压制:状态:日期`。

## 5. 只读视图（给 Excel 模块）

两个视图都只包含状态为「待复核」的线索；日期为 TEXT `YYYY-MM-DD`，金额/分数为
REAL，计数为 INTEGER。列名以《后端方案_v0.1.md》第 6 节更新版为准。

### `v_lead_summary`（一店一行，19 列）

```
lead_id, snapshot_date, platform, shop_id, shop_name, shop_type, shop_label_raw,
ip_codes, link_count, shop_amount, payment_ability, confidence,
hit_reasons_summary, missing_flags, review_result, reviewer,
shop_type_rank, caliber_version, config_version
```

排序：`shop_type_rank`（企业1、个体户2、未知3、个人4）→ 置信度降序 → 金额降序。
`review_result` / `reviewer` 在视图中为空，Excel 导出时也强制留空供人工填写。

### `v_lead_links`（一链接一行，19 列）

```
lead_id, snapshot_date, ip_code, product_id, image_path, main_image_url,
title, url, price, sales_raw, comments_raw, estimated_sales, amount,
amount_formula, text_score, visual_score, final_score, hit_reasons, missing_flags
```

`image_path` 是本地相对路径（后端根目录，如 `data/images/ab/abcd….jpg`）；
`hit_reasons` 由 JSON 列表转成“；”拼接文本且非空。

Excel 默认筛选：`snapshot_date = 本次运行日期`（视图已过滤待复核）。

## 6. 配置要点（`config/rules.yaml`）

- `config_version` 不读文件里的字符串，而是对 YAML 原始字节做 `sha256` 取前 12 位；
  改任何阈值都会自动生成新版本，旧记录不变（C5）。
- `caliber_version` 写 `v1.0`，随快照/线索落库。
- 数据库：`storage.db_path`，默认 `data/leads.db`；导出模块读同一个键/同一个文件。
- 其他路径：`paths.backups`、`paths.logs`、`paths.images`、
  `paths.crawler_dir`、`paths.crawler_output_dir`；全部用 `pathlib`，相对路径按
  backend 根目录解析。
- `export.enabled=true`：主流程在聚合后调用 export 模块生成 Excel；导出失败写
  `run_logs` 并发报警邮件。
- `run.backup_keep=14`、`run.log_keep_days=30`。
- `alert.min_items_per_platform`：低于此条数报警（默认 10，按实际采集量调整）。
- `alert.from/to/smtp_host/smtp_port/smtp_ssl`：邮件参数；授权码只在 keyring。
- `amount.comment_multiplier`、`aggregate.shop_threshold` 等口径字段与需求文档一致。
  口径字段在 `backend/config/rules.yaml` 与 `crawler/config/rules.yaml` 各有一份，
  改口径时请同步两处，避免采集与判定漂移。
- 临时换库：环境变量 `IP_LEAD_DB_URL`（如 PostgreSQL）或 `IP_LEAD_DB_PATH`（SQLite）。

## 7. 日志 / 备份 / 锁

| 项 | 位置/行为 |
|---|---|
| 文件日志 | `logs/run_YYYYMMDD.log`，每天一个，保留 30 天；每步记录开始/结束/条数/耗时 |
| 运行记录 | `run_logs`：每平台一行 + 本次整体汇总一行 |
| 备份 | 运行前 `sqlite3.backup` 到 `backups/leads_YYYYMMDD_HHMMSS.db`；按 mtime 保留最近 14 份 |
| 单实例锁 | `data/run.lock` 存 PID；进程不存在视为失效；第二次运行提示“已在运行”并以 2 退出 |
| 图片 | `data/images/<phash前2位>/<phash>.jpg`，按指纹只存一份 |
| 报警 | 只读本次 `run_logs`：登录失败、条数 < 下限、平台状态异常；邮件失败只写日志 |

## 8. 测试

```bat
..\crawler\.venv\Scripts\python.exe -m pytest -q
```

开发环境结果：`44 passed`；9 条验收逐条结果见 `DELIVERY.md` 和 `logs/acceptance-9.log`。
覆盖金额 A1–A6、B1–B7、C1/C2/C3/C5/C6/C8、复核 Excel、双实例锁、备份保留、授权码不落盘、
新版视图列名/类型、`storage.db_path`、Excel 主流程调用与导出失败报警、`test-smtp` 成功/认证
失败脱敏，以及默认 CLI 入口下京东 400000 触发详情补抓并重算、200000 不触发。

## 9. 已知边界

- Excel 文件由 export 模块生成；本后端主流程负责调用 `export_leads_excel`、传
  `storage.db_path` 与版本号，并在失败时写 `run_logs` + 报警。`export.enabled=false` 时
  跳过导出，运行摘要显示“未启用（由 export 模块生成）”。
- 视觉分需要 `ips[].reference_images` 有可读参考图；未配置时 `visual_score=NULL`，综合分=文字分。
- `products.image_path` 是后端根目录相对路径，Excel 嵌入缩略图时需拼 backend 根目录。
- Windows 凭据管理器、`run.bat` 双击、真实 SMTP 发送需在 Xuan 本机做一次实机验收；
  自动测试用 mock SMTP + mock keyring 验证逻辑和不落盘。
