# GitHub 同类项目调研（后端，2026-11-05）

数据来源：GitHub Search API（`price tracker`、`ecommerce price monitoring`、`sqlalchemy alembic`、`ecommerce scraper sqlite` 等按 stars 排序）+ `repos/{owner}/{repo}` 与 `commits?per_page=1` 实测；星数、最后提交、许可证均为本次查询结果，不是 README 自述。原始 JSON 留在 `research/data/`。

## 1. 结论（先看）

**不整体 fork 任何项目**，只借鉴结构与产品化细节，自己实现：

| 借鉴对象 | 借鉴内容 | 落到本后端 |
|---|---|---|
| Alembic（MIT，直接依赖） | 版本化迁移；SQLite 用 `render_as_batch` 避开 ALTER 限制 | `alembic/versions/0001_initial.py`、`alembic/env.py` |
| changedetection.io（Apache-2.0，只借思路） | 监控类产品的“目标状态 + 运行记录 + 通知失败不拖垮主流程”分层 | `run_logs`、`notify.py`（邮件失败只记日志） |
| ECommerceCrawlers（MIT，只借思路） | 采集与入库分离、按平台独立 pipeline | 采集子进程逐平台运行；`store.py` 统一入库 |
| Crinibus/scraper（MIT，只借思路） | `Product` 与按日 `DataPoint` 分离，历史价格不覆盖 | `products` + `product_snapshots`（product+date 唯一） |
| Price-Tracking-Web-Scraper / PriceDive | Windows 双击入口、SQLite 单文件、失败可读的终端摘要 | `run.bat` / `review.bat`、`RunSummary.lines()` |

许可证处理：
- MIT / Apache-2.0 项目也只借鉴设计，未复制源码；若未来要复制 Apache-2.0 代码，需保留 NOTICE。
- `techwithtim/Price-Tracking-Web-Scraper` 仓库**没有 LICENSE 文件**（API 返回 `license: null`），视为保留所有权利，只借“双击入口 + 终端摘要”的产品思路，不复制任何代码。
- 星数高但非商用许可的项目（如 MediaCrawler）不进入本后端依赖或代码路径。

## 2. 五个主项目

| # | 项目 | 星数 | 最后提交 | 许可证 | 方向 | 可借鉴点 | 是否用代码 |
|---|---|---:|---|---|---|---|---|
| 1 | [dgtlmoon/changedetection.io](https://github.com/dgtlmoon/changedetection.io) | 35,520 | 2026-11-05 | Apache-2.0 | 网页变更/价格/补货监控与报警 | 监控目标状态、运行记录、通知渠道解耦；通知失败不能中断主流程；备份/恢复的产品化 | 否，只借分层思路 |
| 2 | [DropsDevopsOrg/ECommerceCrawlers](https://github.com/DropsDevopsOrg/ECommerceCrawlers) | 5,765 | 2024-05-22 | MIT | 淘宝/京东/1688 等多平台采集合集 | 按平台拆模块；采集结果与保存逻辑分离（`TaobaoCrawler` 的 `dbproduct`/`save_*`）；字段缺失不伪造 | 否，只借“采集/入库分离” |
| 3 | [sqlalchemy/alembic](https://github.com/sqlalchemy/alembic) | 4,466 | 2026-10-27 | MIT | SQLAlchemy 迁移工具 | 版本化迁移；`render_as_batch=True` 处理 SQLite 的 ALTER 限制；迁移与 ORM 元数据同源 | **是**，作为运行依赖 |
| 4 | [techwithtim/Price-Tracking-Web-Scraper](https://github.com/techwithtim/Price-Tracking-Web-Scraper) | 1,293 | 2024-10-17 | 无（API `null`） | Playwright + Flask 价格追踪，含 `scheduler/run.bat` | 双击 `run.bat` 的交互、运行摘要、采集与调度分层 | 否；无许可证，只借 UX 思路 |
| 5 | [Crinibus/scraper](https://github.com/Crinibus/scraper) | 252 | 2025-01-29 | MIT | 多站点价格抓取/可视化，SQLModel + SQLite | `Product` 与 `DataPoint` 两张表分离；按日期保留历史；CLI 子命令组织 | 否，只借表结构思路 |

补充参考（不占五个名额）：
- [DAILtech/PriceDive](https://github.com/DAILtech/PriceDive)（94★，MIT，2025-10-08）：淘宝/京东/拼多多价格追踪、SQLite 单文件、Windows `run.bat`，验证了“本机手动运行 + SQLite + 终端摘要”的可行性；但其建表是手写 `sqlite3`，没有迁移，故不作为结构模板。
- [wangdw495/ecommerce-price-analysis](https://github.com/wangdw495/ecommerce-price-analysis)（51★，MIT）：多平台价格监控与导出，方向接近但规模小，未采用。

## 3. 为什么最终这样选

1. **迁移必须用 Alembic，而不是 `create_all`**：需求要求“空库 `alembic upgrade head` 建全部表和视图”，且以后可能迁 PostgreSQL。Crinibus 用 SQLModel `create_all`，PriceDive 手写 `CREATE TABLE`，都不满足；Alembic 是唯一直接满足验收项 1 的选择。
2. **商品与快照分离**：Crinibus 的 `Product`/`DataPoint` 与需求 C8（同一商品同日一条、次日新增）同构；我们把它扩展为 `products` + `product_snapshots`，并加 `product+snapshot_date` 唯一键。
3. **采集与入库解耦**：ECommerceCrawlers 的按平台目录/保存脚本说明“采集器不应知道数据库细节”。本后端让采集 CLI 逐平台跑、输出 JSONL，再由 `store.py` 统一写两张共享表；采集模块也可以直接 import `upsert_crawl_item`，不破坏既有输出。
4. **报警是旁路**：changedetection.io 把通知做成独立渠道；本后端 `notify.py` 失败只写日志，`alert_for_run` 只读本次 `run_logs`，主流程继续。
5. **不引入 Web 框架**：本轮是 Windows 本机手动运行，不需要 Flask/FastAPI；引入会增加 venv 与端口负担，违背“一键运行”。

## 4. 明确未借鉴/未使用

- 未使用 changedetection.io 的代码或数据模型（其存储并非本项目的 SQLAlchemy 表结构）。
- 未使用 ECommerceCrawlers 的签名/mitmproxy 方案（属采集模块职责）。
- 未使用任何非商用许可项目的代码。
- 未引入任务计划程序、Web 服务、消息队列；本轮由 Xuan 双击 `run.bat` 触发。
