# Excel 导出模块

## 当前格式

导出的 xlsx **仅一个“链接明细”sheet**，表头顺序固定为：

平台｜IP｜店铺名｜标题｜缩略图｜商品链接｜单价｜销量｜评论｜总金额

- 一商品一行，单价、总金额以数值写入，显示两位小数；销量、评论保留采集原文（如“1万+”），缺失时留空。
- 商品金额原样使用后端视图的 `amount`，不在导出时用销量重新估算。
- 按店铺主体、IP连续排列。同店铺同IP有多条商品时，整行使用相同底色，组末增加“同IP小计（N条）”。
- 表尾“总计（N条）”只累加商品金额，不重复累加小计。求和使用 Decimal；缺失金额不当作零，小计/总计单元格批注说明未纳入的条数。
- 店铺身份优先使用后端 `shop_key` 与店铺ID，遵循淘宝/天猫同平台组规则；同名不同店、跨平台组不混合。空店名和空IP不强行分组。
- 多IP商品保留后端提供的IP组合和商品金额，不拆行重复累计；相同组合不同排列可归为同组。
- 缩略图读取本地 `image_path`，嵌入第五列，宽约80px、高不超过120px，保持比例。文件缺失时留空，商品链接继续保留。
- 商品链接仅 http(s) 可点击。标题、店铺名等以等号开头时强制按文本写入。
- 冻结首行、十列筛选；总计行在筛选范围之外。筛选不会动态重算金额，总计始终代表本次全部商品。
- 无数据时只输出十列表头。

工作台手动导出与采集任务自动导出均调用本模块。历史三sheet格式已被本格式替代；新表不含“线索编号 / 复核结论 / 复核人”，不能直接作为旧 `review.bat` 的复核回写模板。工作台页面复核和既有复核模板导入继续可用。

## 调用与配置

```python
from lead_excel import export_leads_excel

path = export_leads_excel(
    db_path="data/leads.db",
    snapshot_date="2026-11-05",
    config={"export": {"dir": "out", "run_id": "需要时填任务编号"}},
)
```

也可从 `export/` 目录运行：

```bat
python -m lead_excel --db ..\backend\data\leads.db --date 2026-11-05 --config ..\rules.yaml
```

数据库路径优先级：显式参数 → `storage.db_path` → 兼容旧 `paths.database` → `data/leads.db`。支持 `storage.base_dir`。日期接受 `YYYY-MM-DD` 和 `YYYYMMDD`。

```yaml
storage:
  db_path: "data/leads.db"
export:
  dir: "%USERPROFILE%/Documents/侵权线索"
  filename: "线索_{YYYYMMDD}.xlsx"
  # run_id: "某个任务编号"          # 提供时只导出该任务
  # image_base_dir: "D:/ip-monitor"
  # thumbnail_width: 80
  # thumbnail_max_height: 120
```

同日重跑原子覆盖同名文件；工作台采集任务使用包含任务编号的独立文件名。文件被Excel占用时提示关闭重试，失败不破坏原文件。

## 数据与升级

- 只读 SQLite `v_lead_summary` / `v_lead_links`，按快照日期和可选任务编号过滤；不写底层数据、不重新判断侵权。
- 后端升级 `0010_export_shop_fields` 为链接视图补充 `platform`、`shop_id`，使无关联线索的独立商品也能输出平台。工作台/后端启动会自动执行迁移。
- 旧视图没有新增辅助字段时仍可导出：正式线索的平台取自汇总，店铺按已知ID或线索编号隔离；旧视图无来源平台的独立行不会猜测平台。
- `lead_excel/columns.py` 的 `REPORT_HEADERS` 定义实际十列表头；旧 `SUMMARY_*` / `LINK_*` 常量作为视图校验契约保留，不再代表输出列。
- 构建、写入失败统一抛出 `ExportError` 子类，供调用方记录日志。

## 依赖与验证

沿用 Python 3.13、openpyxl、Pillow、PyYAML，无新增依赖。

```bat
python -m pytest -q
```

测试覆盖十列表头、单sheet、分组连续与底色、小计/总计、缺失值、任务隔离、缩略图、链接、公式文本、只读查询、异常和原子覆盖；真实后端视图集成测试使用 `backend/pipeline/views.py` 建库执行。
