# 内网网页工作台 v1

## 启动

双击项目根目录 `启动工作台.bat`，或：

```bat
backend\.venv\Scripts\python.exe -m workbench serve --open
```

首次启动打开 `http://127.0.0.1:8765/setup` 设置唯一主管密码；内网其他电脑不能初始化。
默认只监听 `127.0.0.1`。需要开放内网时，在 `workbench/config/workbench.yaml` 里把
`server.host` 改成本机内网 IP（禁止 `0.0.0.0`），并用防火墙只放行公司网段。

## 数据

在“运行任务”点击“新建任务”，填写 IP、平台，按需填写常规采集截止时间，点击“检查并运行”，
核对摘要后点击“确认并立即运行”；无需设置开始时间。
配置字段仍为 `ends_at`，留空表示不限时，逐页采集到明确末页；填写时必须晚于当前时间。
空值会清除模板继承的截止时间。页数、商品数、关键词数、每日数量和补采数量均不截断任务，
后台等待采集子进程也没有默认整批时限。未知空页、重复列表或分页异常保留未完成检查点。
到达截止时间后停止发起新的常规采集请求；在途请求完成解析、保存，产生的补采继续处理。
排队中、执行中和按既有规则等待重试的补采全部收尾后，才完成入库、判定、报表导出，
提交整体任务终态，再按现有邮件配置发送完成通知。补采等待期间仍显示“运行中”，
不会填写实际完成时间或提前发完成邮件；实际完成时间可以晚于常规采集截止时间。
有限重试、单次请求超时、请求间隔、登录和风控规则继续生效。最终失败项保留成功结果，并进入
异常摘要及任务通知，任务显示“部分失败”或“失败”，不会标为全部成功。
任务详情分别显示开始、常规采集截止、实际完成时间。同一时间仍只运行一个任务。
已有的历史预约任务保持兼容，可在“历史预约任务”中查看。

本次调整与专项复验命令见 [不限量采集实施计划](../doc/不限量采集实施计划_20261111.md)。

- `workbench/data/workbench.db`：账号、邀请、IP 档案、白名单、任务、邮件/审计日志。
- `backend/data/leads.db`：线索与复核（继续复用 backend/export/review.bat）。
- `workbench/data/versions/`、`version_backups/`：版本包与自动备份。
- `workbench/data/runs/<任务号>/`：当次生成的后端/crawler 配置和白名单，不修改 `crawler/config`。

## 设置 SMTP

SMTP 主机/发件人默认读 `backend/config/rules.yaml` 的 `alert` 节；授权码只放 Windows
凭据管理器：

```bat
cd backend
.venv\Scripts\python.exe -m pipeline setup-smtp
```

没有 SMTP 配置时，任务完成通知只写 `workbench/data/logs/workbench.log` 和 `mail_logs`，不报错。

## 测试

```bat
cd backend
.venv\Scripts\python.exe -m pytest ..\workbench\tests -q
```

从项目根目录运行前端交互测试和正式 Edge 浏览验收：

```bat
node --test utils\workbench-interactions.test.cjs
python -X utf8 utils\workbench_glass_acceptance.py
```

`node utils\workbench-browser.test.cjs` 保留为同一套浏览验收的兼容入口。
浏览验收需要本机 Python Playwright 与 Microsoft Edge，自动使用系统分配的临时端口，
创建隔离数据库和版本目录并关闭测试服务。采集执行器为测试替身、邮件关闭，
不读取或写入正式业务数据库。机器报告写入 `doc/工作台正式玻璃版验收结果.json`，
截图位于 `prototype/formal-glass-*.png`。

## 正式玻璃版前端

正式页面继续由当前 Python 服务在 **8765** 端口提供，使用原生 HTML/CSS/JavaScript，
无需单独启动前端服务或安装新的前端依赖。源码保留在现有 `workbench/web/`，
以兼容静态资源路径和启动器。启动后打开 `http://127.0.0.1:8765/`；
如果浏览器仍显示旧样式，请在 Edge 按 `Ctrl+F5` 强制刷新。

- 分组侧栏、玻璃工具栏、线索摘要列表与双栏证据复核；窄屏使用导航菜单及卡片列表。
- 新建任务独立弹窗、运行前摘要确认；关闭弹窗或切换页面会保留当前会话草稿，刷新网页会清除。
- 账号与邀请分视图，一次性邀请码生成后保持显示，关闭前需复制保存。
- 设置按邮件、安全、任务与导出分区保存；分区切换保留输入，保存时合并已有配置。
- 顶部支持“降低透明度”，自动遵循减少动效设置。

详细修改及验收范围见 `doc/工作台正式玻璃版重构验收记录.md`。

## 版本包

管理员上传的 zip 按完整项目树打包（至少含 `workbench/` 与 `backend/`，根目录或
`workbench/` 下放 `version.json`/`VERSION`）。`启动工作台.bat` 会通过
`python -m workbench.launcher` 优先运行 `workbench/data/current_version.json`
指向的版本；数据目录始终指回原项目 `workbench/data`。回滚只切换该指针。
