# Excel 导出模块方案 v0.1.1

> 编写：前端与报表工程师 ｜ 2026-11-05 ｜ 依据：需求文档_v0.3 第 8 节、验收清单_v0.3 §8 / C9 C10、后端方案_v0.1 第 6 节  
> 变更：与后端确认视图列名；复核回写由 review.bat 读本 Excel；db 配置键 storage.db_path  
> 实现：dsh专家（优先成熟库，不手写 xlsx 二进制）｜ 初验：前端与报表工程师 ｜ 终审：代码验收  
> 本轮不做 Web 复核页。

## 1. 目标
每次手动运行结束后，根据当日线索生成一份可复核、可录入 SaaS 的 Excel：
- 文件名：`线索_YYYYMMDD.xlsx`（日期取本次运行的快照日期）
- 目录：配置 `export.dir`，默认 `%USERPROFILE%\Documents\侵权线索\`（目录不存在则创建）
- 同日重跑：覆盖同名文件
- 无合格线索：仍生成三张表（汇总/明细为空表头，口径说明照常写）

## 2. 技术选型（成熟方案）
| 项 | 选择 | 理由 |
|---|---|---|
| 写 xlsx | **openpyxl** | 原生支持嵌入图片、超链接、样式；生态成熟 |
| 读数 | **sqlite3** 标准库 | 只读视图，不引入 ORM |
| 配置 | 与主程序共用 `rules*.yaml` 的 `export` 段 | 改路径/文件名不改代码 |
| 缩略图 | 本地 `image_path` → Pillow 缩到宽约 80px → 嵌入单元格 | 不依赖外网；原图 URL 另作超链接 |

不要用 csv / xlsxwriter 拼二进制；不要在导出模块里重算金额或判定。

## 3. 数据来源（只读视图，已与后端方案_v0.1 §6 对齐）
Excel 模块**只**查询：
- `v_lead_summary` → Sheet「线索汇总」
- `v_lead_links` → Sheet「链接明细」

数据库：配置键 `storage.db_path`，默认 `data/leads.db`（相对程序根目录），**只读连接**打开。

过滤：`WHERE snapshot_date=?`（本次运行快照日）。视图本身只含「待复核」线索；被压制店铺不会出现。

### 3.1 视图列（中文表头 ↔ 英文字段，以此为准）
**v_lead_summary（一店一行）**  
导出到 Excel 的业务列（**「线索编号」「复核结论」「复核人」表头必须固定不变**，供 `review.bat` 回写）：
| Excel 列 | 字段名 | 说明 |
|---|---|---|
| 线索编号 | lead_id | `leads.lead_no` 别名，格式 `平台-YYYYMMDD-####` |
| 快照日期 | snapshot_date | TEXT YYYY-MM-DD |
| 平台 | platform | |
| 店铺ID | shop_id | |
| 店铺名 | shop_name | |
| 店铺类型 | shop_type | 企业/个体户/个人/未知 |
| 平台原始标签 | shop_label_raw | |
| 涉及IP | ip_codes | 顿号拼接 |
| 链接数 | link_count | INTEGER |
| 店铺合计金额 | shop_amount | REAL，2 位小数 |
| 赔付能力标签 | payment_ability | |
| 置信度 | confidence | REAL |
| 命中原因摘要 | hit_reasons_summary | 非空（C10） |
| 缺失标记 | missing_flags | 可空 |
| 复核结论 | review_result | 导出时空；人工填「已确认」或「驳回」后由 review.bat 回写 |
| 复核人 | reviewer | 导出时空 |

视图另有辅助列（**不写入 Excel 业务列**，导出侧可用）：`shop_type_rank`（企业1、个体户2、未知3、个人4）、`caliber_version`、`config_version`。视图已按 `shop_type_rank, confidence DESC, shop_amount DESC` 排序。

**v_lead_links（一链接一行）**
| Excel 列 | 字段名 | 说明 |
|---|---|---|
| 线索编号 | lead_id | |
| IP | ip_code | |
| 商品ID | product_id | |
| （缩略图，无表头文字或表头「缩略图」） | image_path | 本地路径；缺失则空 |
| 原图链接 | main_image_url | 超链接 |
| 标题 | title | |
| 商品链接 | url | 超链接 |
| 单价 | price | REAL |
| 销量原文 | sales_raw | |
| 评论数原文 | comments_raw | |
| 估算销量 | estimated_sales | INTEGER |
| 金额 | amount | REAL |
| 计算式 | amount_formula | |
| 文字分 | text_score | REAL |
| 视觉分 | visual_score | REAL，可空 |
| 综合分 | final_score | REAL |
| 命中原因 | hit_reasons | 非空；“；”拼接文本（C10） |
| 缺失标记 | missing_flags | |

口径说明：优先取汇总视图首行（或本次查询结果）的 `caliber_version` / `config_version`，再写固定公式/门槛/免责声明文案。

## 4. 生成逻辑
1. 解析 `export.dir`（展开 `%USERPROFILE%` 等环境变量）并 `mkdir -p`。
2. 打开 SQLite（路径由后端/主配置提供，只读连接）。
3. `SELECT … FROM v_lead_summary WHERE snapshot_date=?`（可不 SELECT 辅助列进 Excel；排序可信任视图，也可再按 shop_type_rank, confidence DESC, shop_amount DESC 排一次）。
4. `SELECT … FROM v_lead_links WHERE snapshot_date=?`，顺序与汇总线索编号一致，同线索内金额降序。
5. 用 openpyxl 建三张 Sheet，写中文表头；金额列数字格式 `0.00`。
6. 链接明细：对每行若 `image_path` 存在且可读，缩放到宽≈80px 嵌入对应行；原图链接、商品链接设为可点击超链接。
7. 「口径说明」写入：金额公式与降级规则摘要、门槛 50 万（当日快照、不跨日）、`caliber_version`、`config_version`、生成时间、免责声明「金额为估算，不构成法律意见」。
8. 保存覆盖目标路径；返回文件绝对路径给主流程写 `run_logs`。

## 5. 与主流程的接口
```text
export_leads_excel(db_path: str, snapshot_date: str, config: dict) -> str  # 返回 xlsx 绝对路径
```
- `db_path` 来自配置 `storage.db_path`（默认 `data/leads.db`）。
- 由主流程在「判定+写 leads 完成」之后、报警检查之前调用（与 `run.bat` / `pipeline run` 一致）。
- 导出失败：写 `run_logs` 错误摘要，不静默吞掉；**导出失败也报警**（后端已同意写入报警规则）。
- 复核闭环：人工填好「线索汇总」的复核结论/复核人后，由后端 `review.bat`（`python -m pipeline review import <Excel路径>`）按线索编号回写；导出模块不负责回写。

## 6. 验收（初验自测清单）
对照验收清单 v0.3：
- [ ] C9：三张 Sheet；缩略图可见；原图/商品链接可点；复核结论/复核人为空列
- [ ] C10：汇总命中原因摘要、明细命中原因非空（夹具数据）
- [ ] 字段齐全（文档 8.1 / 8.2）
- [ ] 排序符合 店铺类型 → 置信度 → 金额
- [ ] 线索编号格式 `平台-YYYYMMDD-####`
- [ ] 同日重跑覆盖；目录自动创建
- [ ] 无线索时仍能生成空表+口径说明
- [ ] 单元/夹具测试：用假视图数据生成文件，openpyxl 读回核对列与嵌入图数量

交付物（给代码验收）：代码位置、运行方法、上述自测结果截图或日志。

## 7. 本轮不做
- Web 复核界面
- 自动写入 SaaS
- 导出模块内直接 UPDATE 库（回写一律走后端 `review.bat`）

## 8. 依赖对齐
- **后端与数据工程师**：视图列名已确认（后端方案_v0.1 §6）；`storage.db_path`；导出失败报警；`review.bat` 依赖固定三列表头。
- **dsh专家**：按本文 v0.1.1 实现；优先 openpyxl + Pillow；与现有工程目录对齐；夹具可按 §3.1 造假视图数据。
